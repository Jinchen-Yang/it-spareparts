#!/usr/bin/env bash
# v1.22 collection-reminders production control. Fail closed, forward-only DB.
set -Eeuo pipefail
umask 077

readonly FROM_REV=d9f1a3c7e5b2
readonly TO_REV=c8e2a4f6b1d3
readonly DEFAULT_APP_DIR=/home/ubuntu/apps/it-spareparts
readonly UPLOADS_VOLUME=it-spareparts_uploaded_files
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly SCRIPT_DIR
readonly MANIFEST_TOOL="$SCRIPT_DIR/v122_collection_reminders_manifest.py"

fatal() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

safe_file() {
  [ -f "$1" ] && [ ! -L "$1" ] && [ -s "$1" ] \
    || fatal "$2 must be a non-empty regular file"
}

usage() {
  cat >&2 <<'EOF'
usage: v122_collection_reminders_release.sh PACKAGE_DIR EVIDENCE_DIR preflight|freeze-writes|backup|restore-check|migrate|deploy|canary|observe|rollback-images [ARGS]

commands:
  preflight
  freeze-writes
  backup
  restore-check DB_DUMP UPLOADS_ARCHIVE
  migrate
  deploy
  canary CANARY_PROJECT_ID MODE600_CANARY_SPEC
  observe 0|5|15|30
  rollback-images

Rollback note: additive schema is retained; no automatic downgrade; no blind DB/uploads restore.
EOF
  exit 64
}

[ "$#" -ge 3 ] || usage
PACKAGE_DIR=$(realpath -e -- "$1")
EVIDENCE_DIR=$(realpath -m -- "$2")
COMMAND=$3
shift 3
APP_DIR=${V122_APP_DIR:-$DEFAULT_APP_DIR}
ENV_FILE="$APP_DIR/.env"
COMPOSE_FILE="$APP_DIR/docker-compose.yml"
MANIFEST="$PACKAGE_DIR/manifest.json"
readonly PACKAGE_DIR EVIDENCE_DIR COMMAND APP_DIR ENV_FILE COMPOSE_FILE MANIFEST

[[ "$APP_DIR" == /* && "$APP_DIR" != / && "$APP_DIR" != *'/../'* ]] || fatal "V122_APP_DIR must be a narrow absolute path"
mkdir -p -m 700 -- "$EVIDENCE_DIR"
[ -f "$MANIFEST" ] && [ ! -L "$MANIFEST" ] || fatal "manifest is missing"
[ "$(realpath -e -- "${BASH_SOURCE[0]}")" = "$PACKAGE_DIR/v122_collection_reminders_release.sh" ] \
  || fatal "release must execute the package-contained controller"
if [ "${V122_TEST_MODE:-0}" != 1 ]; then
  [ "$(id -u)" -eq 0 ] || fatal "release controller must run as root"
  [ "$(stat -c '%a %U:%G' "${BASH_SOURCE[0]}")" = "700 root:root" ] \
    || fatal "release controller must be mode 700 root:root"
fi
EXPECTED_MANIFEST_SHA256=${V122_EXPECTED_MANIFEST_SHA256:-}
[[ "$EXPECTED_MANIFEST_SHA256" =~ ^[0-9a-f]{64}$ ]] \
  || fatal "V122_EXPECTED_MANIFEST_SHA256 trust anchor is required"
[ "$(sha256sum "$MANIFEST" | awk '{print $1}')" = "$EXPECTED_MANIFEST_SHA256" ] \
  || fatal "manifest differs from external trust anchor"
python3 "$MANIFEST_TOOL" preflight "$PACKAGE_DIR" >/dev/null \
  || fatal "production-ready flat package verification failed"
STATE_FILE="$EVIDENCE_DIR/release-state.json"
LOCK_FILE="$EVIDENCE_DIR/release.lock"
readonly STATE_FILE LOCK_FILE
exec 9>"$LOCK_FILE"
flock -n 9 || fatal "another v1.22 release command holds the lock"

manifest_get() {
  python3 - "$MANIFEST" "$1" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
for key in sys.argv[2].split("."):
    value = value[int(key)] if isinstance(value, list) else value[key]
if isinstance(value, bool):
    print("true" if value else "false")
else:
    print(value)
PY
}

compose() {
  env -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME -u COMPOSE_PROFILES \
    docker compose --project-name it-spareparts --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

db_cid() {
  compose ps -q db
}

app_cid() {
  compose ps -q app
}

frontend_cid() {
  compose ps -q frontend
}

update_env_key() {
  local key=$1
  local value=$2
  python3 - "$ENV_FILE" "$key" "$value" <<'PY'
import os
import pathlib
import re
import sys
import tempfile
path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
if not path.is_file() or path.is_symlink():
    raise SystemExit("unsafe .env")
stat = path.stat(follow_symlinks=False)
lines = path.read_text(encoding="utf-8").splitlines()
seen = False
out = []
for raw in lines:
    if re.match(rf"^{re.escape(key)}=", raw):
        if seen:
            raise SystemExit(f"duplicate protected key: {key}")
        out.append(f"{key}={value}")
        seen = True
    else:
        out.append(raw)
if not seen:
    out.append(f"{key}={value}")
fd, tmp = tempfile.mkstemp(prefix=".env-v122-", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("\n".join(out) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chown(tmp, stat.st_uid, stat.st_gid)
    os.chmod(tmp, stat.st_mode & 0o777)
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
PY
}

close_collection_writes() {
  update_env_key MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED false
  update_env_key MAINTENANCE_COLLECTION_CANARY_PROJECT_ID "$(manifest_get runtime_flags.maintenance_collection_canary_project_id)"
  printf 'MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=false\n'
}

require_exact_image() {
  local image_id=$1
  [ "$(docker image inspect --format '{{.Id}}' "$image_id")" = "$image_id" ] \
    || fatal "exact Docker image is not available: $image_id"
}

retag_exact_app_image() {
  local app_image=$1
  require_exact_image "$app_image"
  docker tag "$app_image" it-spareparts-app:latest
}

retag_and_start_exact_images() {
  local app_image=$1
  local frontend_image=$2
  local app_container frontend_container
  require_exact_image "$app_image"
  require_exact_image "$frontend_image"
  retag_exact_app_image "$app_image"
  docker tag "$frontend_image" it-spareparts-frontend:latest
  compose up --no-deps --no-build --force-recreate -d app frontend
  app_container=$(app_cid)
  frontend_container=$(frontend_cid)
  [ -n "$app_container" ] && [ -n "$frontend_container" ] \
    || fatal "app/frontend did not start"
  [ "$(docker inspect --format '{{.Image}}' "$app_container")" = "$app_image" ] \
    || fatal "app is not running the exact requested image"
  [ "$(docker inspect --format '{{.Image}}' "$frontend_container")" = "$frontend_image" ] \
    || fatal "frontend is not running the exact requested image"
}

run_sealed_canary_case() {
  local spec=$1
  local case_name=$2
  local workspace=$3
  local metadata status expected
  metadata=$(python3 - "$spec" "$case_name" "$workspace" <<'PY'
import json
import os
import pathlib
import sys

spec = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
case_name = sys.argv[2]
work = pathlib.Path(sys.argv[3])
case = spec.get(case_name)
if not isinstance(case, dict):
    raise SystemExit(f"canary spec lacks case: {case_name}")
for key in ("method", "path", "token", "expected_status"):
    if key not in case and not (key == "token" and "account" in case):
        raise SystemExit(f"canary case lacks {key}: {case_name}")
if not isinstance(case["method"], str) or case["method"] not in {"GET", "POST"}:
    raise SystemExit("canary method must be GET or POST")
if not isinstance(case["path"], str) or not case["path"].startswith("/") or "\n" in case["path"]:
    raise SystemExit("canary path must be a safe absolute API path")
token = case.get("token")
if token is None:
    account = case.get("account")
    if not isinstance(account, str) or not account.replace("_", "").replace("-", "").isalnum():
        raise SystemExit("canary account reference is invalid")
    token_path = work / "tokens" / f"{account}.token"
    if not token_path.is_file() or token_path.is_symlink():
        raise SystemExit("canary account token is missing")
    token = token_path.read_text(encoding="utf-8").strip()
if not isinstance(token, str) or not token:
    raise SystemExit("canary token must be a non-empty sealed string")
if not isinstance(case["expected_status"], int):
    raise SystemExit("canary expected_status must be an integer")
if case_name == "apply_last":
    if not case["path"].endswith("/apply") or case["expected_status"] // 100 != 2:
        raise SystemExit("apply_last must be the sole successful /apply request")
elif case["path"].endswith("/apply"):
    raise SystemExit("only apply_last may target /apply")
base = spec.get("base_url")
if not isinstance(base, str) or not base.startswith(("http://", "https://")) or "\n" in base:
    raise SystemExit("canary base_url is invalid")
body = case.get("body", {})
if not isinstance(body, (dict, list)):
    raise SystemExit("canary body must be a JSON object or array")
header = work / f"{case_name}.header"
payload = work / f"{case_name}.json"
meta = work / f"{case_name}.meta.json"
header.write_text(f"Authorization: Bearer {token}\n", encoding="utf-8")
payload.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
for path in (header, payload):
    os.chmod(path, 0o600)
meta.write_text(json.dumps({"method": case["method"], "url": base.rstrip("/") + case["path"], "header": str(header), "payload": str(payload), "expected_status": case["expected_status"]}), encoding="utf-8")
os.chmod(meta, 0o600)
print(meta)
PY
)
  expected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["expected_status"])' "$metadata")
  status=$(curl --silent --show-error --output "$workspace/$case_name.response" \
    --write-out '%{http_code}' --request "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["method"])' "$metadata")" \
    --header "@$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["header"])' "$metadata")" \
    --header 'Content-Type: application/json' \
    --data-binary "@$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["payload"])' "$metadata")" \
    "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["url"])' "$metadata")") || fatal "canary HTTP transport failed for $case_name"
  [ "$status" = "$expected" ] || fatal "canary HTTP status mismatch for $case_name"
}

login_named_canary_accounts() {
  local spec=$1
  local workspace=$2
  python3 - "$spec" "$workspace" <<'PY'
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

spec = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
work = pathlib.Path(sys.argv[2])
accounts = spec.get("named_accounts")
base = spec.get("base_url")
if not isinstance(base, str) or not base.startswith(("http://", "https://")) or "\n" in base:
    raise SystemExit("canary base_url is invalid")
if not isinstance(accounts, dict) or not accounts:
    raise SystemExit("canary spec must include named_accounts for real login verification")
token_dir = work / "tokens"
token_dir.mkdir(mode=0o700, exist_ok=False)
summary: dict[str, dict] = {}

def post_json(path: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except Exception:
            return exc.code, {"detail": str(exc)}

for name, account in accounts.items():
    if not isinstance(name, str) or not name.replace("_", "").replace("-", "").isalnum():
        raise SystemExit("canary named account key is unsafe")
    if not isinstance(account, dict) or not account.get("username") or not account.get("password"):
        raise SystemExit(f"canary named account lacks credentials: {name}")
    status, payload = post_json(
        "/auth/login",
        {"username": account["username"], "password": account["password"]},
    )
    if status != 200 or not payload.get("token"):
        raise SystemExit(f"canary named account login failed: {name}")
    permissions = payload.get("permissions") or {}
    required = account.get("required_permissions") or []
    forbidden = account.get("forbidden_permissions") or []
    if not isinstance(required, list) or not isinstance(forbidden, list):
        raise SystemExit("canary permission expectations must be arrays")
    missing = [key for key in required if permissions.get(key) is not True]
    unexpectedly_granted = [key for key in forbidden if permissions.get(key) is True]
    if missing or unexpectedly_granted:
        raise SystemExit(f"canary permission readback mismatch: {name}")
    token_path = token_dir / f"{name}.token"
    token_path.write_text(payload["token"], encoding="utf-8")
    os.chmod(token_path, 0o600)
    summary[name] = {
        "role": payload.get("role"),
        "required_permissions": sorted(required),
        "forbidden_permissions": sorted(forbidden),
    }
(work / "named-account-readback.json").write_text(
    json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
os.chmod(work / "named-account-readback.json", 0o600)
PY
}

verify_active_release_lineage() {
  local expected_parent expected_db expected_compose
  safe_file "$ROOT_RELEASE_STATE" "active root release state"
  safe_file "$COMPOSE_FILE" "active compose"
  expected_parent=$(manifest_get parent_production_sha)
  expected_db=$(manifest_get database.image_id)
  expected_compose=$(sha256sum "$COMPOSE_FILE" | awk '{print $1}')
  python3 - "$ROOT_RELEASE_STATE" "$expected_parent" "$expected_db" "$expected_compose" <<'PY'
import json
import pathlib
import sys
state = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = {
    "production_sha": sys.argv[2],
    "database_image_id": sys.argv[3],
    "compose_sha256": sys.argv[4],
}
for key, wanted in expected.items():
    if state.get(key) != wanted:
        raise SystemExit(f"active release lineage mismatch: {key}")
PY
}

require_no_state() {
  [ ! -e "$STATE_FILE" ] && [ ! -L "$STATE_FILE" ] \
    || fatal "phase state already exists; preflight cannot repeat or regress"
}

require_phase() {
  local expected=$1
  python3 - "$STATE_FILE" "$expected" "$EXPECTED_MANIFEST_SHA256" \
    "$PACKAGE_DIR" "$(manifest_get target_sha)" "$(manifest_get parent_production_sha)" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
if not path.is_file() or path.is_symlink():
    raise SystemExit("phase state is missing or unsafe")
value = json.loads(path.read_text(encoding="utf-8"))
expected = {
    "format": "v122-collection-reminders-release-state-v2",
    "phase": sys.argv[2],
    "manifest_sha256": sys.argv[3],
    "package_dir": str(pathlib.Path(sys.argv[4]).resolve()),
    "target_sha": sys.argv[5],
    "parent_production_sha": sys.argv[6],
}
for key, wanted in expected.items():
    if value.get(key) != wanted:
        raise SystemExit(f"phase state mismatch: {key}")
if not isinstance(value.get("generation"), int) or value["generation"] < 1:
    raise SystemExit("phase state generation is invalid")
PY
}

advance_phase() {
  local next_phase=$1
  shift
  python3 - "$STATE_FILE" "$next_phase" "$EXPECTED_MANIFEST_SHA256" \
    "$PACKAGE_DIR" "$(manifest_get target_sha)" "$(manifest_get parent_production_sha)" "$@" <<'PY'
import json
import os
import pathlib
import sys
import tempfile
path = pathlib.Path(sys.argv[1])
phase, manifest_sha, package, target, parent = sys.argv[2:7]
old = {}
if path.exists():
    if not path.is_file() or path.is_symlink():
        raise SystemExit("unsafe release state")
    old = json.loads(path.read_text(encoding="utf-8"))
extra = sys.argv[7:]
if len(extra) % 2:
    raise SystemExit("invalid state metadata")
payload = {
    "format": "v122-collection-reminders-release-state-v2",
    "manifest_sha256": manifest_sha,
    "package_dir": str(pathlib.Path(package).resolve()),
    "target_sha": target,
    "parent_production_sha": parent,
    "phase": phase,
    "generation": int(old.get("generation", 0)) + 1,
}
for index in range(0, len(extra), 2):
    payload[extra[index]] = extra[index + 1]
fd, temporary = tempfile.mkstemp(prefix=".release-state-", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

BACKUP_ROOT=${V122_BACKUP_ROOT:-/var/backups/it-spareparts/v122}
BACKUP_GENERATION=${V122_BACKUP_GENERATION:-}
ROOT_RELEASE_STATE=${V122_ROOT_RELEASE_STATE:-$APP_DIR/release-state.json}
readonly BACKUP_ROOT BACKUP_GENERATION

# Ordering and zero-side-effect argument gates run before any Docker/env write.
case "$COMMAND" in
  preflight) [ "$#" -eq 0 ] || usage; require_no_state ;;
  freeze-writes) [ "$#" -eq 0 ] || usage; require_phase preflight ;;
  backup)
    [ "$#" -eq 0 ] || usage
    require_phase frozen
    [[ "$BACKUP_GENERATION" =~ ^[A-Za-z0-9_.-]{8,128}$ ]] \
      || fatal "V122_BACKUP_GENERATION is required"
    BACKUP_DIR="$BACKUP_ROOT/$BACKUP_GENERATION"
    [ ! -e "$BACKUP_DIR" ] && [ ! -L "$BACKUP_DIR" ] \
      || fatal "fresh backup generation already exists"
    ;;
  restore-check) require_phase backup ;;
  migrate) [ "$#" -eq 0 ] || usage; require_phase restore_checked ;;
  deploy) [ "$#" -eq 0 ] || usage; require_phase migrated ;;
  canary)
    [ "$#" -eq 2 ] || usage
    CANARY_PROJECT_ID=$1
    CANARY_SPEC=$(realpath -e -- "$2")
    [ "$CANARY_PROJECT_ID" = "$(manifest_get runtime_flags.maintenance_collection_canary_project_id)" ] \
      || fatal "canary project id does not match manifest"
    [ -f "$CANARY_SPEC" ] && [ ! -L "$CANARY_SPEC" ] \
      && [ "$(stat -c '%a' "$CANARY_SPEC")" = 600 ] \
      || fatal "canary spec must be a mode-600 regular file"
    require_phase deployed
    ;;
  observe) [ "$#" -eq 1 ] || usage; case "$1" in 0|5|15|30) ;; *) fatal "observe point must be 0, 5, 15, or 30";; esac ;;
  rollback-images) require_phase deployed ;;
  *) usage ;;
esac

case "$COMMAND" in
  preflight)
    [ -f "$COMPOSE_FILE" ] && [ -f "$ENV_FILE" ] || fatal "active compose/env missing"
    verify_active_release_lineage
    compose config -q
    advance_phase preflight
    ;;
  freeze-writes)
    [ "$#" -eq 0 ] || usage
    close_collection_writes
    compose stop app >/dev/null
    [ -z "$(app_cid)" ] || fatal "app container did not stop"
    advance_phase frozen
    ;;
  backup)
    mkdir -m 700 -- "$BACKUP_DIR"
    DBC=$(db_cid)
    [ -n "$DBC" ] || fatal "db container is not running"
    docker exec "$DBC" psql -X -U spareparts -d spareparts -At -c "SELECT pg_current_wal_lsn()" >"$BACKUP_DIR/wal_lsn.txt"
    docker exec "$DBC" pg_dump -U spareparts -d spareparts --format=custom --file=/tmp/v122-db.dump
    docker cp "$DBC:/tmp/v122-db.dump" "$BACKUP_DIR/postgres_custom.dump"
    docker exec "$DBC" pg_dumpall --globals-only -U spareparts >"$BACKUP_DIR/postgres_globals.sql"
    docker run --rm -v "$UPLOADS_VOLUME:/uploads:ro" -v "$BACKUP_DIR:/backup" alpine:3.20 \
      tar -C /uploads -cpf /backup/uploads.tar .
    UPLOADS_COUNT=$(docker run --rm -v "$UPLOADS_VOLUME:/uploads:ro" alpine:3.20 \
      sh -c 'find /uploads -type f | wc -l' | tr -d '[:space:]')
    UPLOADS_BYTES=$(docker run --rm -v "$UPLOADS_VOLUME:/uploads:ro" alpine:3.20 \
      sh -c 'find /uploads -type f -exec wc -c {} + | awk "{if (\$2 != \"total\") s += \$1} END {print s+0}"' | tr -d '[:space:]')
    cp -- "$COMPOSE_FILE" "$BACKUP_DIR/docker-compose.yml"
    cp -- "$ENV_FILE" "$BACKUP_DIR/env.snapshot"
    docker ps --no-trunc --format '{{json .}}' >"$BACKUP_DIR/image_manifest.jsonl"
    python3 - "$BACKUP_DIR/release_state.json" "$FROM_REV" "$TO_REV" "$(manifest_get target_sha)" <<'PY'
import json
import sys
payload = {"format": "v122-collection-reminders-backup-state-v1", "from": sys.argv[2], "to": sys.argv[3], "target_sha": sys.argv[4]}
json.dump(payload, open(sys.argv[1], "w", encoding="utf-8"), sort_keys=True, separators=(",", ":"))
PY
    python3 - "$BACKUP_DIR/backup-manifest.json" \
      "$(manifest_get target_sha)" "$(manifest_get parent_production_sha)" \
      "$(manifest_get database.image_id)" "$(manifest_get images.app_image_id)" \
      "$(manifest_get images.frontend_image_id)" \
      "$BACKUP_DIR/postgres_custom.dump" "$BACKUP_DIR/postgres_globals.sql" \
      "$BACKUP_DIR/uploads.tar" "$PACKAGE_DIR/candidate-compose.yml" \
      "$(cat "$BACKUP_DIR/wal_lsn.txt")" "$UPLOADS_COUNT" "$UPLOADS_BYTES" <<'PY'
import hashlib
import json
import pathlib
import sys

(
    output,
    target,
    parent,
    db_image,
    app_image,
    frontend_image,
    db_dump,
    globals_file,
    uploads,
    compose,
    wal_lsn,
    uploads_count,
    uploads_bytes,
) = sys.argv[1:14]

def digest(path: str) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()

payload = {
    "format": "v122-collection-reminders-full-backup-v2",
    "phase": "frozen",
    "target_sha": target,
    "parent_production_sha": parent,
    "db_image_id": db_image,
    "app_image_id": app_image,
    "frontend_image_id": frontend_image,
    "db_dump_sha256": digest(db_dump),
    "globals_sha256": digest(globals_file),
    "uploads_archive_sha256": digest(uploads),
    "candidate_compose_sha256": digest(compose),
    "wal_lsn": wal_lsn.strip(),
    "uploads_file_count": int(uploads_count),
    "uploads_total_bytes": int(uploads_bytes),
}
with open(output, "x", encoding="utf-8") as stream:
    json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
    stream.write("\n")
PY
    (cd "$BACKUP_DIR" && sha256sum * >sha256sums && sha256sum -c sha256sums)
    advance_phase backup backup_dir "$BACKUP_DIR"
    ;;
  restore-check)
    [ "$#" -eq 2 ] || usage
    DB_DUMP=$(realpath -e -- "$1")
    UPLOADS_ARCHIVE=$(realpath -e -- "$2")
    "$SCRIPT_DIR/v122_collection_reminders_rehearse.sh" "$DB_DUMP" "$UPLOADS_ARCHIVE" \
      "$(manifest_get target_sha)" "$(manifest_get parent_production_sha)" \
      "$(manifest_get database.image_id)" "$(manifest_get images.app_image_id)" \
      "$(manifest_get images.frontend_image_id)" "$PACKAGE_DIR/candidate-compose.yml" \
      "$EVIDENCE_DIR/restore-check"
    advance_phase restore_checked
    ;;
  migrate)
    [ "$#" -eq 0 ] || usage
    DBC=$(db_cid)
    [ -n "$DBC" ] || fatal "db container is not running"
    CURRENT=$(docker exec "$DBC" psql -X -U spareparts -d spareparts -At -c 'SELECT version_num FROM alembic_version;')
    [ "$CURRENT" = "$FROM_REV" ] || fatal "production DB is not at d9 before migrate"
    close_collection_writes
    retag_exact_app_image "$(manifest_get images.app_image_id)"
    compose run --rm --no-deps --no-build \
      -e MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=false \
      app alembic upgrade "$TO_REV"
    CURRENT=$(docker exec "$DBC" psql -X -U spareparts -d spareparts -At -c 'SELECT version_num FROM alembic_version;')
    [ "$CURRENT" = "$TO_REV" ] || fatal "production DB did not reach c8 after migrate"
    advance_phase migrated
    ;;
  deploy)
    [ "$#" -eq 0 ] || usage
    close_collection_writes
    retag_and_start_exact_images \
      "$(manifest_get images.app_image_id)" \
      "$(manifest_get images.frontend_image_id)"
    advance_phase deployed
    ;;
  canary)
    shift 2
    update_env_key MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED false
    update_env_key MAINTENANCE_COLLECTION_CANARY_PROJECT_ID "$CANARY_PROJECT_ID"
    compose up --no-deps --no-build --force-recreate -d app
    # Read back from the actual container rather than trusting the staged .env.
    RUNNING_FLAGS=$(compose exec -T app sh -ceu 'printf "%s\n%s\n" "$MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED" "$MAINTENANCE_COLLECTION_CANARY_PROJECT_ID"')
    [ "$RUNNING_FLAGS" = "false
$CANARY_PROJECT_ID" ] || fatal "running canary configuration readback mismatch"
    CANARY_WORK=$(mktemp -d -t v122-canary.XXXXXXXX)
    cleanup_canary_work() { rm -rf -- "$CANARY_WORK"; }
    trap cleanup_canary_work EXIT HUP INT TERM
    login_named_canary_accounts "$CANARY_SPEC" "$CANARY_WORK"
    # The request sequence is deliberately fixed.  Both negative cases and
    # preview must succeed while apply is false.  The canary apply flag is
    # opened only for the single manifest-named project, and only for the last
    # request; if that request fails, the script closes the flag again.
    run_sealed_canary_case "$CANARY_SPEC" follow_up_positive "$CANARY_WORK"
    run_sealed_canary_case "$CANARY_SPEC" cross_project_negative "$CANARY_WORK"
    run_sealed_canary_case "$CANARY_SPEC" permission_negative "$CANARY_WORK"
    run_sealed_canary_case "$CANARY_SPEC" import_preview_positive "$CANARY_WORK"
    update_env_key MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED true
    compose up --no-deps --no-build --force-recreate -d app
    RUNNING_FLAGS=$(compose exec -T app sh -ceu 'printf "%s\n%s\n" "$MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED" "$MAINTENANCE_COLLECTION_CANARY_PROJECT_ID"')
    [ "$RUNNING_FLAGS" = "true
$CANARY_PROJECT_ID" ] || fatal "running apply canary configuration readback mismatch"
    if ! (run_sealed_canary_case "$CANARY_SPEC" apply_last "$CANARY_WORK"); then
      update_env_key MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED false
      compose up --no-deps --no-build --force-recreate -d app || true
      fatal "apply canary failed; collection apply flag was closed"
    fi
    rm -rf -- "$CANARY_WORK"
    trap - EXIT HUP INT TERM
    advance_phase canary canary_project_id "$CANARY_PROJECT_ID" apply_enabled true
    ;;
  observe)
    case "$1" in
      0) require_phase canary; NEXT_PHASE=observe_0 ;;
      5) require_phase observe_0; NEXT_PHASE=observe_5 ;;
      15) require_phase observe_5; NEXT_PHASE=observe_15 ;;
      30) require_phase observe_15; NEXT_PHASE=observed ;;
    esac
    compose ps >"$EVIDENCE_DIR/observe-$1-compose-ps.txt"
    docker stats --no-stream >"$EVIDENCE_DIR/observe-$1-docker-stats.txt"
    advance_phase "$NEXT_PHASE" observe_minutes "$1"
    ;;
  rollback-images)
    [ "$#" -eq 0 ] || usage
    close_collection_writes
    printf 'rollback-images requested; additive schema is retained; no automatic downgrade; restore DB/uploads only after incident approval\n'
    retag_and_start_exact_images \
      "$(manifest_get previous_images.app_image_id)" \
      "$(manifest_get previous_images.frontend_image_id)"
    advance_phase rolled_back rollback_note 'images-and-flags-only; no downgrade/delete/automatic restore'
    ;;
  *)
    usage
    ;;
esac
