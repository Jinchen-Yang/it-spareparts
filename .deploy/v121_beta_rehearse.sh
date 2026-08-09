#!/usr/bin/env bash
# Rehearse f1->d9 against an isolated production-copy dump; never touches production DB.
set -Eeuo pipefail
umask 077

readonly FROM_REV=f1c8e4a7b2d9
readonly TO_REV=d9f1a3c7e5b2
RESTORE_TMPFS_SIZE=${V121_RESTORE_TMPFS_SIZE:-4g}
[[ "$RESTORE_TMPFS_SIZE" =~ ^[1-9][0-9]*(m|g)$ ]] || {
  printf 'FATAL: V121_RESTORE_TMPFS_SIZE must be a positive m/g size\n' >&2
  exit 1
}
readonly RESTORE_TMPFS_SIZE

fatal() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

[ "$#" -eq 11 ] || fatal \
  "usage: v121_beta_rehearse.sh DUMP TARGET_SHA PARENT_PROD_SHA DB_IMAGE_ID NEW_APP_IMAGE_ID OLD_APP_IMAGE_ID OLD_FRONTEND_IMAGE_ID CANDIDATE_COMPOSE STABLE_CREDENTIAL_JSON forward-only|test-old-images OUTPUT_DIR"
DUMP=$(realpath -e -- "$1")
TARGET_SHA=$2
PARENT_SHA=$3
DB_IMAGE_ID=$4
NEW_APP_IMAGE_ID=$5
OLD_APP_IMAGE_ID=$6
OLD_FRONTEND_IMAGE_ID=$7
CANDIDATE_COMPOSE=$(realpath -e -- "$8")
CREDENTIAL=$(realpath -e -- "$9")
MODE=${10}
OUTPUT_DIR=$(realpath -m -- "${11}")
readonly DUMP TARGET_SHA PARENT_SHA DB_IMAGE_ID NEW_APP_IMAGE_ID
readonly OLD_APP_IMAGE_ID OLD_FRONTEND_IMAGE_ID CANDIDATE_COMPOSE CREDENTIAL MODE OUTPUT_DIR
[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || fatal "invalid target SHA"
[[ "$PARENT_SHA" =~ ^[0-9a-f]{40}$ ]] || fatal "invalid parent production SHA"
for image in "$DB_IMAGE_ID" "$NEW_APP_IMAGE_ID" "$OLD_APP_IMAGE_ID" "$OLD_FRONTEND_IMAGE_ID"; do
  [[ "$image" =~ ^sha256:[0-9a-f]{64}$ ]] || fatal "invalid Docker image ID"
  [ "$(docker image inspect -f '{{.Id}}' "$image")" = "$image" ] \
    || fatal "Docker image is not loaded: $image"
done
[ "$MODE" = forward-only ] || [ "$MODE" = test-old-images ] || fatal "invalid rehearsal mode"
[ -f "$DUMP" ] && [ ! -L "$DUMP" ] && [ -s "$DUMP" ] || fatal "unsafe/empty dump"
[ -f "$CANDIDATE_COMPOSE" ] && [ ! -L "$CANDIDATE_COMPOSE" ] \
  || fatal "candidate Compose is unsafe"
[ -f "$CREDENTIAL" ] && [ ! -L "$CREDENTIAL" ] \
  && [ "$(stat -c '%a' "$CREDENTIAL")" = 600 ] || fatal "credential must be a real mode-600 file"
[ ! -e "$OUTPUT_DIR" ] && [ ! -L "$OUTPUT_DIR" ] || fatal "output already exists"

WORK=$(mktemp -d)
DB_NAME="v121-rehearsal-db-${TARGET_SHA:0:12}"
APP_NAME="v121-rehearsal-app-${TARGET_SHA:0:12}"
NETWORK_NAME="v121-rehearsal-${TARGET_SHA:0:12}"
PROJECT_NAME="v121rehearsal${TARGET_SHA:0:12}"
OLD_APP_CID=
OLD_FRONTEND_CID=
readonly WORK DB_NAME APP_NAME NETWORK_NAME PROJECT_NAME
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  [ -z "$OLD_APP_CID" ] || docker rm -f "$OLD_APP_CID" >/dev/null 2>&1 || true
  [ -z "$OLD_FRONTEND_CID" ] || docker rm -f "$OLD_FRONTEND_CID" >/dev/null 2>&1 || true
  docker rm -f "$APP_NAME" "$DB_NAME" >/dev/null 2>&1 || true
  docker network rm "$NETWORK_NAME" >/dev/null 2>&1 || true
  rm -rf -- "$WORK"
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

mkdir -m 700 -- "$WORK/output"
docker network create --internal "$NETWORK_NAME" >/dev/null
docker run -d --name "$DB_NAME" --network "$NETWORK_NAME" --network-alias db \
  --tmpfs "/var/lib/postgresql/data:rw,noexec,nosuid,size=$RESTORE_TMPFS_SIZE" \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_USER=spareparts \
  -e POSTGRES_DB=spareparts "$DB_IMAGE_ID" >/dev/null
for attempt in $(seq 1 60); do
  docker exec "$DB_NAME" pg_isready -U spareparts >/dev/null 2>&1 && break
  [ "$attempt" -lt 60 ] || fatal "isolated rehearsal DB did not become ready"
  sleep 1
done
docker cp "$DUMP" "$DB_NAME:/tmp/production-copy.dump"
docker exec "$DB_NAME" pg_restore -U spareparts -d spareparts \
  --exit-on-error --no-owner --no-acl /tmp/production-copy.dump
BEFORE_HEAD=$(docker exec "$DB_NAME" psql -X -U spareparts -d spareparts -At \
  -c 'SELECT version_num FROM alembic_version;')
[ "$BEFORE_HEAD" = "$FROM_REV" ] || fatal "production-copy dump is not at f1"

PRESSURE="$WORK/output/migration-pressure.jsonl"
(
  while :; do
    row=$(docker exec "$DB_NAME" psql -X -U spareparts -d spareparts -At -F '|' <<'SQL'
WITH a AS (
  SELECT COALESCE(array_length(pg_blocking_pids(pid), 1), 0) blocked_by,
         EXTRACT(EPOCH FROM (clock_timestamp() - xact_start))::bigint xact_seconds
  FROM pg_stat_activity
  WHERE datname=current_database() AND pid<>pg_backend_pid() AND xact_start IS NOT NULL
), l AS (
  SELECT count(*) FILTER (WHERE NOT granted) waiting, count(*) total
  FROM pg_locks WHERE database=(SELECT oid FROM pg_database WHERE datname=current_database())
)
SELECT COALESCE((SELECT count(*) FROM a WHERE blocked_by>0),0),
       COALESCE((SELECT count(*) FROM a WHERE xact_seconds>=60),0),
       COALESCE((SELECT waiting FROM l),0), COALESCE((SELECT total FROM l),0);
SQL
    ) || exit 1
    printf '{"captured_at":"%s","blocked_sessions":%s,"long_transactions":%s,"waiting_locks":%s,"total_locks":%s}\n' \
      "$(date --iso-8601=seconds)" "${row%%|*}" \
      "$(cut -d'|' -f2 <<<"$row")" "$(cut -d'|' -f3 <<<"$row")" \
      "${row##*|}" >>"$PRESSURE"
    sleep 1
  done
) &
SAMPLER_PID=$!
START_NS=$(date +%s%N)
set +e
docker run --rm --network "$NETWORK_NAME" \
  -e DATABASE_URL=postgresql+psycopg://spareparts@db:5432/spareparts \
  -e ENVIRONMENT=dev \
  -e "PGOPTIONS=-c statement_timeout=120000 -c lock_timeout=5000" \
  "$NEW_APP_IMAGE_ID" alembic upgrade "$TO_REV"
MIGRATION_STATUS=$?
set -e
END_NS=$(date +%s%N)
kill "$SAMPLER_PID" >/dev/null 2>&1 || true
wait "$SAMPLER_PID" >/dev/null 2>&1 || true
[ "$MIGRATION_STATUS" -eq 0 ] || fatal "isolated production-copy migration failed"
[ -s "$PRESSURE" ] || fatal "isolated migration pressure sampler produced no evidence"
python3 - "$PRESSURE" <<'PY'
import json,sys
with open(sys.argv[1],encoding="utf-8") as stream:
    rows=[json.loads(line) for line in stream if line.strip()]
if not rows:
    raise SystemExit("empty migration pressure evidence")
required={"captured_at","blocked_sessions","long_transactions","waiting_locks","total_locks"}
if any(set(row) != required for row in rows):
    raise SystemExit("malformed migration pressure evidence")
PY
AFTER_HEAD=$(docker exec "$DB_NAME" psql -X -U spareparts -d spareparts -At \
  -c 'SELECT version_num FROM alembic_version;')
[ "$AFTER_HEAD" = "$TO_REV" ] || fatal "isolated migration did not reach d9"
chmod 600 "$PRESSURE"

python3 - "$WORK/output/production-copy-migration-rehearsal.json" \
  "$TARGET_SHA" "$PARENT_SHA" "$BEFORE_HEAD" "$AFTER_HEAD" \
  "$START_NS" "$END_NS" "$(sha256sum "$DUMP" | awk '{print $1}')" \
  "$(sha256sum "$PRESSURE" | awk '{print $1}')" "$DB_IMAGE_ID" \
  "$(sha256sum "$CANDIDATE_COMPOSE" | awk '{print $1}')" "$RESTORE_TMPFS_SIZE" <<'PY'
import datetime as dt,json,os,sys
payload={"format":"v121-production-copy-migration-rehearsal-v1",
 "target_sha":sys.argv[2],"parent_production_sha":sys.argv[3],
 "from_revision":sys.argv[4],"to_revision":sys.argv[5],
 "duration_milliseconds":(int(sys.argv[7])-int(sys.argv[6]))//1_000_000,
 "production_copy_dump_sha256":sys.argv[8],"pressure_samples_sha256":sys.argv[9],
 "database_image_id":sys.argv[10],"candidate_compose_sha256":sys.argv[11],
 "restore_tmpfs_size":sys.argv[12],
 "statement_timeout_ms":120000,"lock_timeout_ms":5000,
 "isolated":True,"completed_at":dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
 "conclusion":"success"}
with open(sys.argv[1],"w",encoding="utf-8") as stream:
    json.dump(payload,stream,sort_keys=True,separators=(",",":")); stream.write("\n")
os.chmod(sys.argv[1],0o600)
PY

if [ "$MODE" = test-old-images ]; then
  OLD_APP_TAG="it-spareparts-v121-rehearsal/app:${TARGET_SHA:0:12}"
  OLD_FRONTEND_TAG="it-spareparts-v121-rehearsal/frontend:${TARGET_SHA:0:12}"
  docker image tag "$OLD_APP_IMAGE_ID" "$OLD_APP_TAG"
  docker image tag "$OLD_FRONTEND_IMAGE_ID" "$OLD_FRONTEND_TAG"
  OVERRIDE="$WORK/old-images.override.yml"
  python3 - "$OVERRIDE" "$OLD_APP_TAG" "$OLD_FRONTEND_TAG" "$NETWORK_NAME" <<'PY'
import pathlib,sys
pathlib.Path(sys.argv[1]).write_text(f'''services:
  app:
    image: {sys.argv[2]}
    networks: !override [rehearsal]
    volumes: !override []
    environment:
      DATABASE_URL: postgresql+psycopg://spareparts@db:5432/spareparts
      ENVIRONMENT: dev
      ADMIN_PASSWORD: rehearsal-only-not-production
      SECRET_KEY: rehearsal-only-not-production-secret
  frontend:
    image: {sys.argv[3]}
    networks: !override [rehearsal]
    ports: !override []
networks:
  rehearsal:
    external: true
    name: {sys.argv[4]}
''',encoding='utf-8')
PY
  rehearsal_compose() {
    env -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME -u COMPOSE_PROFILES \
      docker compose --project-name "$PROJECT_NAME" \
        -f "$CANDIDATE_COMPOSE" -f "$OVERRIDE" "$@"
  }
  rehearsal_compose config --quiet
  rehearsal_compose up --no-deps --no-build -d app frontend
  OLD_APP_CID=$(rehearsal_compose ps -q app)
  OLD_FRONTEND_CID=$(rehearsal_compose ps -q frontend)
  [ "$(docker inspect -f '{{.Image}}' "$OLD_APP_CID")" = "$OLD_APP_IMAGE_ID" ] \
    || fatal "old app image identity drifted in Compose rehearsal"
  [ "$(docker inspect -f '{{.Image}}' "$OLD_FRONTEND_CID")" = "$OLD_FRONTEND_IMAGE_ID" ] \
    || fatal "old frontend image identity drifted in Compose rehearsal"
  for attempt in $(seq 1 60); do
    if docker exec "$OLD_FRONTEND_CID" wget -qO- http://127.0.0.1/ >/dev/null 2>&1 \
      && docker exec "$OLD_FRONTEND_CID" wget -qO- http://127.0.0.1/api/health >/dev/null 2>&1 \
      && docker exec -i "$OLD_APP_CID" python -c '
import json,sys,urllib.request
credential=json.load(sys.stdin)
def request(path,payload=None,token=None):
    headers={"Accept":"application/json"}; data=None
    if payload is not None:
        headers["Content-Type"]="application/json"; data=json.dumps(payload).encode()
    if token: headers["Authorization"]="Bearer "+token
    with urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8000"+path,headers=headers,data=data),timeout=5) as r:
        return r.status,json.load(r)
assert request("/health")[0] == 200
status,login=request("/api/auth/login",credential)
assert status == 200 and login.get("token")
status,_=request("/api/maintenance/projects?lifecycle=ongoing",token=login["token"])
assert status == 200
' <"$CREDENTIAL" >/dev/null 2>&1
    then
      break
    fi
    [ "$attempt" -lt 60 ] || fatal "old app image failed stable smoke on d9"
    sleep 1
  done
  python3 - "$WORK/output/old-images-on-d9-rehearsal.json" "$TARGET_SHA" \
    "$PARENT_SHA" "$OLD_APP_IMAGE_ID" "$OLD_FRONTEND_IMAGE_ID" \
    "$(sha256sum "$CANDIDATE_COMPOSE" | awk '{print $1}')" <<'PY'
import datetime as dt,json,os,sys
payload={"format":"old-images-on-d9-rehearsal-v1","target_sha":sys.argv[2],
 "parent_production_sha":sys.argv[3],"db_head":"d9f1a3c7e5b2",
 "old_app_image_id":sys.argv[4],"old_frontend_image_id":sys.argv[5],
 "candidate_compose_sha256":sys.argv[6],
 "isolated":True,"stable_smoke":"passed","conclusion":"success",
 "completed_at":dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()}
with open(sys.argv[1],"w",encoding="utf-8") as stream:
    json.dump(payload,stream,sort_keys=True,separators=(",",":")); stream.write("\n")
os.chmod(sys.argv[1],0o600)
PY
fi

mv -T -- "$WORK/output" "$OUTPUT_DIR"
printf 'MIGRATION_REHEARSAL_SHA256=%s\n' \
  "$(sha256sum "$OUTPUT_DIR/production-copy-migration-rehearsal.json" | awk '{print $1}')"
if [ -f "$OUTPUT_DIR/old-images-on-d9-rehearsal.json" ]; then
  printf 'OLD_IMAGE_D9_REHEARSAL_SHA256=%s\n' \
    "$(sha256sum "$OUTPUT_DIR/old-images-on-d9-rehearsal.json" | awk '{print $1}')"
fi
