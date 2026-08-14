#!/usr/bin/env bash
# Restore a frozen DB+uploads backup and rehearse d9->c8 in an isolated network.
set -Eeuo pipefail
umask 077

readonly FROM_REV=d9f1a3c7e5b2
readonly TO_REV=c8e2a4f6b1d3
RESTORE_TMPFS_SIZE=${V122_RESTORE_TMPFS_SIZE:-6g}

fatal() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

safe_file() {
  [ -f "$1" ] && [ ! -L "$1" ] && [ -s "$1" ] \
    || fatal "$2 must be a non-empty regular file"
}

[ "$#" -eq 9 ] \
  || fatal "usage: v122_collection_reminders_rehearse.sh DB_DUMP UPLOADS_ARCHIVE TARGET_SHA PARENT_PROD_SHA DB_IMAGE_ID APP_IMAGE_ID FRONTEND_IMAGE_ID CANDIDATE_COMPOSE OUTPUT_DIR"
[[ "$RESTORE_TMPFS_SIZE" =~ ^[1-9][0-9]*(m|g)$ ]] \
  || fatal "V122_RESTORE_TMPFS_SIZE must be a positive m/g size"
DB_DUMP=$(realpath -e -- "$1")
UPLOADS_ARCHIVE=$(realpath -e -- "$2")
TARGET_SHA=$3
PARENT_SHA=$4
DB_IMAGE_ID=$5
APP_IMAGE_ID=$6
FRONTEND_IMAGE_ID=$7
CANDIDATE_COMPOSE=$(realpath -e -- "$8")
OUTPUT_DIR=$(realpath -m -- "$9")
readonly DB_DUMP UPLOADS_ARCHIVE TARGET_SHA PARENT_SHA DB_IMAGE_ID APP_IMAGE_ID FRONTEND_IMAGE_ID CANDIDATE_COMPOSE OUTPUT_DIR

[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || fatal "invalid target SHA"
[[ "$PARENT_SHA" =~ ^[0-9a-f]{40}$ ]] || fatal "invalid parent production SHA"
[ "$TARGET_SHA" != "$PARENT_SHA" ] || fatal "target and parent SHA must differ"
for image in "$DB_IMAGE_ID" "$APP_IMAGE_ID" "$FRONTEND_IMAGE_ID"; do
  [[ "$image" =~ ^sha256:[0-9a-f]{64}$ ]] || fatal "invalid Docker image ID"
done
safe_file "$DB_DUMP" "DB dump"
safe_file "$UPLOADS_ARCHIVE" "uploads archive"
safe_file "$CANDIDATE_COMPOSE" "candidate compose"
[ ! -e "$OUTPUT_DIR" ] && [ ! -L "$OUTPUT_DIR" ] || fatal "output already exists"

BACKUP_DIR=$(dirname -- "$DB_DUMP")
GLOBALS_FILE="$BACKUP_DIR/postgres_globals.sql"
CHECKSUM_FILE="$BACKUP_DIR/sha256sums"
BACKUP_MANIFEST="$BACKUP_DIR/backup-manifest.json"

# This is deliberately the first content inspection.  No Docker command may
# run before hostile archive names/types have been rejected.
python3 - "$UPLOADS_ARCHIVE" <<'PY'
import pathlib
import sys
import tarfile

archive_path = pathlib.Path(sys.argv[1])
try:
    with tarfile.open(archive_path, "r:*") as archive:
        seen = set()
        for member in archive.getmembers():
            path = pathlib.PurePosixPath(member.name)
            unsafe_name = (
                path.is_absolute()
                or not member.name
                or ".." in path.parts
                or "\x00" in member.name
            )
            unsafe_type = not (member.isdir() or member.isfile())
            if unsafe_name or unsafe_type or member.name in seen:
                raise SystemExit(f"unsafe uploads archive member: type={member.type!r}")
            seen.add(member.name)
except (tarfile.TarError, OSError) as exc:
    raise SystemExit(f"unsafe uploads archive member: unreadable archive ({exc})")
PY

safe_file "$GLOBALS_FILE" "sibling PostgreSQL globals dump"
safe_file "$CHECKSUM_FILE" "sibling backup checksums"
safe_file "$BACKUP_MANIFEST" "sibling full-backup manifest"
python3 - "$BACKUP_DIR" "$CHECKSUM_FILE" \
  "$(basename -- "$DB_DUMP")" "$(basename -- "$UPLOADS_ARCHIVE")" \
  "$(basename -- "$GLOBALS_FILE")" <<'PY'
import hashlib
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1]).resolve()
checksum = pathlib.Path(sys.argv[2])
required = set(sys.argv[3:])
rows = {}
for line in checksum.read_text(encoding="ascii").splitlines():
    match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
    if not match or match.group(2) in rows:
        raise SystemExit("invalid or duplicate backup checksum row")
    rows[match.group(2)] = match.group(1)
if not required <= rows.keys():
    raise SystemExit("backup checksums do not cover DB/globals/uploads")
for name, expected in rows.items():
    path = root / name
    if path.parent != root or not path.is_file() or path.is_symlink():
        raise SystemExit("backup checksum references unsafe/missing file")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise SystemExit("backup checksum mismatch")
PY

PACKAGE_DIR=$(dirname -- "$CANDIDATE_COMPOSE")
[ "$(basename -- "$CANDIDATE_COMPOSE")" = "candidate-compose.yml" ] \
  || fatal "candidate compose must come from the flat release package"
PACKAGE_MANIFEST="$PACKAGE_DIR/manifest.json"
PACKAGE_TOOL="$PACKAGE_DIR/v122_collection_reminders_manifest.py"
CONTRACT_FILE="$PACKAGE_DIR/contract.yaml"
safe_file "$PACKAGE_MANIFEST" "package manifest"
safe_file "$PACKAGE_TOOL" "package manifest verifier"
safe_file "$CONTRACT_FILE" "package contract"
EXPECTED_MANIFEST_SHA256=${V122_EXPECTED_MANIFEST_SHA256:-}
[[ "$EXPECTED_MANIFEST_SHA256" =~ ^[0-9a-f]{64}$ ]] \
  || fatal "V122_EXPECTED_MANIFEST_SHA256 trust anchor is required"
[ "$(sha256sum "$PACKAGE_MANIFEST" | awk '{print $1}')" = "$EXPECTED_MANIFEST_SHA256" ] \
  || fatal "package manifest differs from external trust anchor"
python3 "$PACKAGE_TOOL" verify "$PACKAGE_DIR" >/dev/null \
  || fatal "flat package verification failed"

REHEARSAL_STAGE=${V122_REHEARSAL_STAGE:-}
case "$REHEARSAL_STAGE" in preliminary|final) ;; *) fatal "V122_REHEARSAL_STAGE must be preliminary or final";; esac
python3 - "$PACKAGE_MANIFEST" "$BACKUP_MANIFEST" "$CANDIDATE_COMPOSE" \
  "$CONTRACT_FILE" "$REHEARSAL_STAGE" "$TARGET_SHA" "$PARENT_SHA" \
  "$DB_IMAGE_ID" "$APP_IMAGE_ID" "$FRONTEND_IMAGE_ID" "$DB_DUMP" \
  "$GLOBALS_FILE" "$UPLOADS_ARCHIVE" <<'PY'
import hashlib
import json
import pathlib
import sys

# Keep argument unpacking explicit so no path is accidentally string-coerced.
manifest_path = pathlib.Path(sys.argv[1])
backup_path = pathlib.Path(sys.argv[2])
compose_path = pathlib.Path(sys.argv[3])
contract_path = pathlib.Path(sys.argv[4])
stage, target, parent, db_image, app_image, frontend_image = sys.argv[5:11]
db_dump, globals_file, uploads = map(pathlib.Path, sys.argv[11:14])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
backup = json.loads(backup_path.read_text(encoding="utf-8"))
if manifest.get("target_sha") != target or manifest.get("parent_production_sha") != parent:
    raise SystemExit("package SHA lineage does not match rehearsal arguments")
if manifest.get("database") != {"from": "d9f1a3c7e5b2", "to": "c8e2a4f6b1d3", "image_id": db_image}:
    raise SystemExit("package DB/image binding mismatch")
if manifest.get("images") != {"app_image_id": app_image, "frontend_image_id": frontend_image}:
    raise SystemExit("package application image binding mismatch")
if manifest.get("runtime_flags", {}).get("maintenance_collection_plan_apply_enabled") is not False:
    raise SystemExit("candidate package does not start apply false")
state = manifest.get("contract", {}).get("state")
allowed = manifest.get("contract", {}).get("production_apply_allowed")
if stage == "preliminary" and (state, allowed) != ("approved_for_implementation", False):
    raise SystemExit("preliminary rehearsal requires false implementation contract")
if stage == "final" and (state, allowed) != ("approved_for_production_candidate", True):
    raise SystemExit("final rehearsal requires promoted true candidate contract")
if hashlib.sha256(contract_path.read_bytes()).hexdigest() != manifest.get("contract", {}).get("sha256"):
    raise SystemExit("contract hash binding mismatch")
if hashlib.sha256(compose_path.read_bytes()).hexdigest() != manifest.get("artifacts", {}).get("compose", {}).get("sha256"):
    raise SystemExit("compose hash binding mismatch")
if backup.get("format") != "v122-collection-reminders-full-backup-v2" or backup.get("phase") != "frozen":
    raise SystemExit("backup manifest is not a frozen full backup")
expected = {
    "target_sha": target,
    "parent_production_sha": parent,
    "db_image_id": db_image,
    "app_image_id": app_image,
    "frontend_image_id": frontend_image,
    "db_dump_sha256": hashlib.sha256(db_dump.read_bytes()).hexdigest(),
    "globals_sha256": hashlib.sha256(globals_file.read_bytes()).hexdigest(),
    "uploads_archive_sha256": hashlib.sha256(uploads.read_bytes()).hexdigest(),
    "candidate_compose_sha256": hashlib.sha256(compose_path.read_bytes()).hexdigest(),
}
for key, wanted in expected.items():
    if backup.get(key) != wanted:
        raise SystemExit(f"full-backup manifest binding mismatch: {key}")
if not isinstance(backup.get("wal_lsn"), str) or not backup["wal_lsn"]:
    raise SystemExit("full-backup manifest lacks WAL LSN")
for key in ("uploads_file_count", "uploads_total_bytes"):
    if not isinstance(backup.get(key), int) or backup[key] < 0:
        raise SystemExit(f"full-backup manifest lacks {key}")
PY

# Only after all archive/package/backup bindings are verified may Docker run.
for image in "$DB_IMAGE_ID" "$APP_IMAGE_ID" "$FRONTEND_IMAGE_ID"; do
  [ "$(docker image inspect --format '{{.Id}}' "$image")" = "$image" ] \
    || fatal "Docker image is not loaded exactly: $image"
done

SAMPLE_FILE=$(realpath -e -- "${V122_REHEARSAL_SAMPLE_XLS:-/nonexistent}") \
  || fatal "V122_REHEARSAL_SAMPLE_XLS mode-600 real sample is required"
APPLY_SPEC=$(realpath -e -- "${V122_REHEARSAL_APPLY_SPEC:-/nonexistent}") \
  || fatal "V122_REHEARSAL_APPLY_SPEC mode-600 named-account spec is required"
for secret_file in "$SAMPLE_FILE" "$APPLY_SPEC"; do
  safe_file "$secret_file" "rehearsal secret input"
  [ "$(stat -c '%a' "$secret_file")" = "600" ] \
    || fatal "rehearsal sample/spec must be mode 600"
done

WORK=$(mktemp -d -t v122-rehearsal.XXXXXXXX)
RUN_TOKEN=$(basename -- "$WORK" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9')
DB_NAME="v122-rehearsal-db-$RUN_TOKEN"
APP_NAME="v122-rehearsal-app-$RUN_TOKEN"
NETWORK_NAME="v122-rehearsal-$RUN_TOKEN"
UPLOADS_DIR="$WORK/uploads"
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  docker rm -f "$APP_NAME" "$DB_NAME" >/dev/null 2>&1 || true
  docker network rm "$NETWORK_NAME" >/dev/null 2>&1 || true
  rm -rf -- "$WORK"
  exit "$status"
}
trap cleanup EXIT HUP INT TERM
mkdir -m 700 -- "$WORK/output" "$UPLOADS_DIR"

python3 - "$UPLOADS_ARCHIVE" "$UPLOADS_DIR" <<'PY'
import pathlib
import sys
import tarfile

archive_path, output = map(pathlib.Path, sys.argv[1:])
with tarfile.open(archive_path, "r:*") as archive:
    for member in archive.getmembers():
        target = output.joinpath(*pathlib.PurePosixPath(member.name).parts).resolve()
        if output.resolve() not in (target, *target.parents):
            raise SystemExit("unsafe uploads archive member during extraction")
        archive.extract(member, path=output, set_attrs=True, numeric_owner=True)
PY
python3 - "$UPLOADS_DIR" "$BACKUP_MANIFEST" "$WORK/output/uploads-restore.json" <<'PY'
import hashlib
import json
import pathlib
import stat
import sys

root, manifest_path, output = map(pathlib.Path, sys.argv[1:])
manifest = json.loads(manifest_path.read_text())
files = sorted(path for path in root.rglob("*") if path.is_file() and not path.is_symlink())
if any(path.is_symlink() for path in root.rglob("*")):
    raise SystemExit("uploads extraction produced a symlink")
count = len(files)
size = sum(path.stat().st_size for path in files)
if count != manifest["uploads_file_count"] or size != manifest["uploads_total_bytes"]:
    raise SystemExit("uploads extracted metadata count/bytes mismatch")
digest = hashlib.sha256()
for path in files:
    relative = path.relative_to(root).as_posix().encode()
    row = b"\0".join([
        relative,
        str(stat.S_IMODE(path.stat().st_mode)).encode(),
        str(path.stat().st_uid).encode(),
        str(path.stat().st_gid).encode(),
        str(path.stat().st_size).encode(),
        str(path.stat().st_mtime_ns).encode(),
        hashlib.sha256(path.read_bytes()).hexdigest().encode(),
    ])
    digest.update(row + b"\n")
output.write_text(json.dumps({"file_count": count, "total_bytes": size, "metadata_tree_sha256": digest.hexdigest()}, sort_keys=True) + "\n")
PY

docker network create --internal "$NETWORK_NAME" >/dev/null
docker run -d --pull=never --name "$DB_NAME" --network "$NETWORK_NAME" --network-alias db \
  --tmpfs "/var/lib/postgresql/data:rw,noexec,nosuid,size=$RESTORE_TMPFS_SIZE" \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_USER=restore_admin \
  -e POSTGRES_DB=postgres "$DB_IMAGE_ID" >/dev/null
for attempt in $(seq 1 60); do
  docker exec "$DB_NAME" pg_isready -U restore_admin -d postgres >/dev/null 2>&1 && break
  [ "$attempt" -lt 60 ] || fatal "isolated rehearsal DB did not become ready"
  sleep 1
done
docker exec -i "$DB_NAME" psql -X -v ON_ERROR_STOP=1 -U restore_admin -d postgres <"$GLOBALS_FILE"
docker exec "$DB_NAME" createdb -U restore_admin -O spareparts spareparts
docker exec -i "$DB_NAME" pg_restore -U spareparts -d spareparts \
  --exit-on-error --no-owner --no-acl <"$DB_DUMP"
BEFORE_HEAD=$(docker exec "$DB_NAME" psql -X -U restore_admin -d spareparts -At \
  -c 'SELECT version_num FROM alembic_version;')
[ "$BEFORE_HEAD" = "$FROM_REV" ] || fatal "restored backup is not at d9"
SPAREPARTS_ROLE=$(docker exec "$DB_NAME" psql -X -U spareparts -d spareparts -At \
  -c "SELECT current_user || ':' || current_database();")
[ "$SPAREPARTS_ROLE" = "spareparts:spareparts" ] \
  || fatal "restored spareparts application role cannot connect"

docker run --rm --pull=never --network "$NETWORK_NAME" \
  -e DATABASE_URL=postgresql+psycopg://spareparts@db:5432/spareparts \
  -e ENVIRONMENT=dev \
  -e MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=false \
  -e MAINTENANCE_COLLECTION_CANARY_PROJECT_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["runtime_flags"]["maintenance_collection_canary_project_id"])' "$PACKAGE_MANIFEST")" \
  --entrypoint alembic "$APP_IMAGE_ID" upgrade "$TO_REV"
AFTER_HEAD=$(docker exec "$DB_NAME" psql -X -U restore_admin -d spareparts -At \
  -c 'SELECT version_num FROM alembic_version;')
[ "$AFTER_HEAD" = "$TO_REV" ] || fatal "isolated migration did not reach c8"

docker exec "$DB_NAME" psql -X -v ON_ERROR_STOP=1 -U restore_admin -d spareparts -At <<'SQL' >"$WORK/invariants.txt"
SELECT 'batch_table', to_regclass('maintenance_collection_plan_import_batch') IS NOT NULL;
SELECT 'binding_table', to_regclass('maintenance_collection_plan_source_binding') IS NOT NULL;
SELECT 'operation_table', to_regclass('maintenance_collection_milestone_operation') IS NOT NULL;
SELECT 'append_only_trigger', EXISTS (
  SELECT 1 FROM pg_trigger WHERE tgname='trg_maintenance_collection_milestone_operation_append_only' AND NOT tgisinternal
);
SELECT 'role_defaults_false', NOT EXISTS (
  SELECT 1 FROM sys_role_template WHERE
    COALESCE((permissions->>'action_maintenance_collection_follow_up')::boolean,false)
    OR COALESCE((permissions->>'action_maintenance_collection_plan_import')::boolean,false)
);
SELECT 'user_defaults_false', NOT EXISTS (
  SELECT 1 FROM sys_user WHERE
    COALESCE((template_perms->>'action_maintenance_collection_follow_up')::boolean,false)
    OR COALESCE((template_perms->>'action_maintenance_collection_plan_import')::boolean,false)
    OR COALESCE((permissions->>'action_maintenance_collection_follow_up')::boolean,false)
    OR COALESCE((permissions->>'action_maintenance_collection_plan_import')::boolean,false)
);
SQL
[ "$(grep -c '|t$' "$WORK/invariants.txt")" -eq 6 ] \
  || fatal "schema/permission/append-only invariants failed"

# Resolve every DB file reference against the restored root without persisting
# business filenames.  Orphans are counted and hashed only; nothing is removed.
docker exec "$DB_NAME" psql -X -U restore_admin -d spareparts -At -F $'\t' \
  >"$WORK/db-references.tsv" <<'SQL'
SELECT 'raw', storage_path FROM sys_raw_file WHERE storage_path IS NOT NULL
UNION ALL
SELECT 'collection', storage_key FROM maintenance_collection_plan_import_batch;
SQL
python3 - "$UPLOADS_DIR" "$WORK/output/db-uploads-consistency.json" \
  "$WORK/db-references.tsv" <<'PY'
import hashlib
import json
import pathlib
import sys

root, output, references = map(pathlib.Path, sys.argv[1:])
root = root.resolve()
referenced = set()
missing = 0
for raw in references.read_text(encoding="utf-8").splitlines():
    kind, value = raw.split("\t", 1)
    if kind == "raw" and value.startswith("/app/data/raw/"):
        relative = value.removeprefix("/app/data/raw/")
    elif kind == "collection":
        relative = f"maintenance-collection-plans/{value}"
    else:
        relative = value
    path = pathlib.PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit("DB upload reference escapes restored root")
    candidate = root.joinpath(*path.parts).resolve()
    if root not in (candidate, *candidate.parents):
        raise SystemExit("DB upload reference escapes restored root")
    referenced.add(candidate)
    if not candidate.is_file() or candidate.is_symlink():
        missing += 1
if missing:
    raise SystemExit("DB upload reference is missing from restored archive")
physical = {path.resolve() for path in root.rglob("*") if path.is_file() and not path.is_symlink()}
orphans = sorted(path.relative_to(root).as_posix() for path in physical - referenced)
digest = hashlib.sha256("\n".join(orphans).encode()).hexdigest()
output.write_text(json.dumps({"reference_count": len(referenced), "missing_count": 0, "orphan_count": len(orphans), "orphan_set_sha256": digest, "orphan_action": "reported_not_deleted"}, sort_keys=True) + "\n")
PY

# Real parser acceptance runs with no network and no DB.  Its result is derived
# from the parser process, while domain-table counts prove zero domain writes.
DOMAIN_BEFORE=$(docker exec "$DB_NAME" psql -X -U restore_admin -d spareparts -At -c \
  "SELECT (SELECT count(*) FROM maintenance_collection_milestone)::text || ':' || (SELECT count(*) FROM maintenance_collection_plan_source_binding)::text || ':' || (SELECT count(*) FROM maintenance_collection_milestone_operation)::text;")
docker run --rm --pull=never --network none --read-only \
  -v "$SAMPLE_FILE:/sample.xls:ro" --entrypoint python "$APP_IMAGE_ID" - /sample.xls \
  >"$WORK/parser-result.json" <<'PY'
import json
import pathlib
import sys
from app.services.maintenance_collection_plan_xls import parse_project_manager_collection_xls

payload = parse_project_manager_collection_xls(pathlib.Path(sys.argv[1]).read_bytes(), filename="sample.xls")
print(json.dumps({"project_count": len(payload.rows), "milestone_count": sum(len(row.nodes) for row in payload.rows)}, sort_keys=True))
PY
python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); assert p["project_count"]>0 and p["milestone_count"]>0' "$WORK/parser-result.json" \
  || fatal "real-sample parser produced no plan facts"
DOMAIN_AFTER=$(docker exec "$DB_NAME" psql -X -U restore_admin -d spareparts -At -c \
  "SELECT (SELECT count(*) FROM maintenance_collection_milestone)::text || ':' || (SELECT count(*) FROM maintenance_collection_plan_source_binding)::text || ':' || (SELECT count(*) FROM maintenance_collection_milestone_operation)::text;")
[ "$DOMAIN_BEFORE" = "$DOMAIN_AFTER" ] || fatal "real-sample parser changed domain tables"

# A mode-600 named-account spec drives actual login/preview/apply HTTP calls in
# the isolated network.  Apply is first proved closed, then enabled only for the
# same canary project.  The response and DB state, not a literal JSON true,
# determine synthetic_apply_verified.
CANARY_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["runtime_flags"]["maintenance_collection_canary_project_id"])' "$PACKAGE_MANIFEST")
start_app() {
  local apply=$1
  docker rm -f "$APP_NAME" >/dev/null 2>&1 || true
  docker run -d --pull=never --name "$APP_NAME" --network "$NETWORK_NAME" --network-alias app \
    -v "$UPLOADS_DIR:/app/data/raw:rw" \
    -e DATABASE_URL=postgresql+psycopg://spareparts@db:5432/spareparts \
    -e ENVIRONMENT=dev -e SECRET_KEY=v122-rehearsal-secret-only \
    -e ADMIN_PASSWORD=v122-rehearsal-admin-disabled \
    -e MAINTENANCE_BETA_ENABLED=true \
    -e MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED="$apply" \
    -e MAINTENANCE_COLLECTION_CANARY_PROJECT_ID="$CANARY_ID" \
    "$APP_IMAGE_ID" >/dev/null
  for attempt in $(seq 1 60); do
    docker exec "$APP_NAME" python -c \
      'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health/db", timeout=2)' \
      >/dev/null 2>&1 && return
    [ "$attempt" -lt 60 ] || fatal "isolated rehearsal app did not become ready"
    sleep 1
  done
}
SPEC_USERNAME=$(python3 - "$APPLY_SPEC" <<'PY'
import json
import pathlib
import sys

spec = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
username = spec.get("username")
if not isinstance(username, str) or not username or "\n" in username:
    raise SystemExit("rehearsal apply spec lacks a safe named username")
print(username)
PY
)
GRANT_PERMS_JSON='{"page_maintenance":true,"page_maintenance_beta":true,"data_purchase_cost":true,"data_profit":true,"action_maintenance_collection_follow_up":true,"action_maintenance_collection_plan_import":true}'
UPDATED_NAMED_ACCOUNT=$(docker exec "$DB_NAME" psql -X -U spareparts -d spareparts -At \
  -v username="$SPEC_USERNAME" -v perms="$GRANT_PERMS_JSON" <<'SQL'
UPDATE sys_user
SET template_perms = COALESCE(template_perms, '{}'::jsonb) || :'perms'::jsonb,
    perm_overrides = COALESCE(perm_overrides, '{}'::jsonb) || :'perms'::jsonb,
    permissions = COALESCE(permissions, '{}'::jsonb) || :'perms'::jsonb,
    token_version = token_version + 1
WHERE username = :'username' AND is_active IS TRUE
RETURNING 1;
SQL
)
[ "$UPDATED_NAMED_ACCOUNT" = "1" ] \
  || fatal "named rehearsal account not found or inactive in restored DB"
start_app false
HTTP_PREVIEW_DOMAIN_BEFORE=$(docker exec "$DB_NAME" psql -X -U spareparts -d spareparts -At -c \
  "SELECT (SELECT count(*) FROM maintenance_collection_milestone)::text || ':' || (SELECT count(*) FROM maintenance_collection_plan_source_binding)::text || ':' || (SELECT count(*) FROM maintenance_collection_milestone_operation)::text;")
docker run --rm --pull=never --network "$NETWORK_NAME" \
  -v "$SAMPLE_FILE:/sample.xls:ro" -v "$APPLY_SPEC:/spec.json:ro" \
  -v "$WORK:/evidence:rw" --entrypoint python "$APP_IMAGE_ID" - preview \
  >"$WORK/http-preview-summary.json" <<'PY'
import json
import pathlib
import sys
import urllib.error
import urllib.request
import uuid

mode = sys.argv[1]
spec = json.loads(pathlib.Path("/spec.json").read_text())
base = "http://app:8000/api"
def request(path, body, *, token=None, content_type="application/json", extra=None):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(extra or {})
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)
status, login = request("/auth/login", {"username": spec["username"], "password": spec["password"]})
if status != 200 or not login.get("token"):
    raise SystemExit("named rehearsal login failed")
token = login["token"]
boundary = "v122-" + uuid.uuid4().hex
content = pathlib.Path("/sample.xls").read_bytes()
multipart = (
    f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"sample.xls\"\r\n"
    "Content-Type: application/vnd.ms-excel\r\n\r\n"
).encode() + content + f"\r\n--{boundary}--\r\n".encode()
status, preview = request(
    "/maintenance/collection-plan-imports/preview",
    multipart,
    token=token,
    content_type=f"multipart/form-data; boundary={boundary}",
    extra={"Idempotency-Key": spec["idempotency_key"]},
)
if status != 200 or preview.get("status") != "valid":
    raise SystemExit("actual preview failed")
by_order = {row["external_order_no"]: row for row in preview["rows"]}
bindings = []
for supplied in spec["bindings"]:
    row = by_order[supplied["external_order_no"]]
    bindings.append({**supplied, "row_key": row["row_key"]})
apply_body = {
    "expected_batch_version": preview["batch_version"],
    "expected_data_version": preview["data_version"],
    "bindings": bindings,
}
status, denied = request(
    f"/maintenance/collection-plan-imports/{preview['batch_id']}/apply",
    apply_body,
    token=token,
)
if status != 403 or denied.get("detail", {}).get("code") != "permission_denied":
    raise SystemExit("apply=false did not fail closed")
pathlib.Path("/evidence/apply-request.json").write_text(json.dumps({"batch_id": preview["batch_id"], "token": token, "body": apply_body}))
print(json.dumps({"preview_http_status": 200, "apply_false_http_status": 403}, sort_keys=True))
PY
HTTP_PREVIEW_DOMAIN_AFTER=$(docker exec "$DB_NAME" psql -X -U spareparts -d spareparts -At -c \
  "SELECT (SELECT count(*) FROM maintenance_collection_milestone)::text || ':' || (SELECT count(*) FROM maintenance_collection_plan_source_binding)::text || ':' || (SELECT count(*) FROM maintenance_collection_milestone_operation)::text;")
[ "$HTTP_PREVIEW_DOMAIN_BEFORE" = "$HTTP_PREVIEW_DOMAIN_AFTER" ] \
  || fatal "actual HTTP preview changed domain tables"
start_app true
docker run --rm --pull=never --network "$NETWORK_NAME" \
  -v "$WORK:/evidence:rw" --entrypoint python "$APP_IMAGE_ID" - \
  >"$WORK/http-apply-summary.json" <<'PY'
import json
import pathlib
import urllib.error
import urllib.request

state = json.loads(pathlib.Path("/evidence/apply-request.json").read_text())
request = urllib.request.Request(
    f"http://app:8000/api/maintenance/collection-plan-imports/{state['batch_id']}/apply",
    data=json.dumps(state["body"]).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {state['token']}"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
        status = response.status
except urllib.error.HTTPError as exc:
    raise SystemExit(f"synthetic apply failed with HTTP {exc.code}")
counts = payload.get("counts", {})
changed = sum(int(counts.get(key, 0)) for key in ("created", "updated"))
if status != 200 or payload.get("status") != "applied" or changed < 1:
    raise SystemExit("synthetic apply produced no verified domain change")
print(json.dumps({"apply_http_status": status, "changed_domain_rows": changed, "batch_status": payload["status"]}, sort_keys=True))
PY
APPLIED_ROWS=$(docker exec "$DB_NAME" psql -X -U restore_admin -d spareparts -At -c \
  "SELECT count(*) FROM maintenance_collection_plan_import_batch WHERE status='applied' AND applied_at IS NOT NULL;")
AUDIT_ROWS=$(docker exec "$DB_NAME" psql -X -U restore_admin -d spareparts -At -c \
  "SELECT count(*) FROM sys_access_log WHERE action='collection_plan_import_apply';")
[ "$APPLIED_ROWS" -ge 1 ] && [ "$AUDIT_ROWS" -ge 1 ] \
  || fatal "synthetic apply lacks persisted batch/audit evidence"

python3 - "$WORK/output/rehearsal-evidence.json" "$REHEARSAL_STAGE" "$TARGET_SHA" \
  "$PARENT_SHA" "$BEFORE_HEAD" "$AFTER_HEAD" "$DB_DUMP" "$GLOBALS_FILE" \
  "$UPLOADS_ARCHIVE" "$CANDIDATE_COMPOSE" "$CONTRACT_FILE" "$PACKAGE_MANIFEST" \
  "$APP_IMAGE_ID" "$FRONTEND_IMAGE_ID" "$DB_IMAGE_ID" "$DOMAIN_BEFORE" "$DOMAIN_AFTER" \
  "$HTTP_PREVIEW_DOMAIN_BEFORE" "$HTTP_PREVIEW_DOMAIN_AFTER" "$APPLIED_ROWS" "$AUDIT_ROWS" <<'PY'
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys

out = pathlib.Path(sys.argv[1])
stage, target, parent, before, after = sys.argv[2:7]
db_dump, globals_file, uploads, compose, contract, manifest = map(pathlib.Path, sys.argv[7:13])
app_image, frontend_image, db_image, parser_before, parser_after, http_preview_before, http_preview_after, applied, audit = sys.argv[13:22]
payload = {
    "format": "v122-collection-reminders-rehearsal-v2",
    "stage": stage,
    "success": before == "d9f1a3c7e5b2" and after == "c8e2a4f6b1d3" and parser_before == parser_after and http_preview_before == http_preview_after and int(applied) > 0 and int(audit) > 0,
    "target_sha": target,
    "parent_production_sha": parent,
    "from_revision": before,
    "to_revision": after,
    "database_image_id": db_image,
    "app_image_id": app_image,
    "frontend_image_id": frontend_image,
    "db_dump_sha256": hashlib.sha256(db_dump.read_bytes()).hexdigest(),
    "globals_sha256": hashlib.sha256(globals_file.read_bytes()).hexdigest(),
    "uploads_archive_sha256": hashlib.sha256(uploads.read_bytes()).hexdigest(),
    "candidate_compose_sha256": hashlib.sha256(compose.read_bytes()).hexdigest(),
    "contract_sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
    "package_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    "db_restore": True,
    "globals_restore": True,
    "uploads_restore_verified": True,
    "db_uploads_references_complete": True,
    "parser_zero_domain_write": parser_before == parser_after,
    "preview_zero_domain_write": http_preview_before == http_preview_after,
    "synthetic_apply_verified": int(applied) > 0 and int(audit) > 0,
    "completed_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
}
if not payload["success"]:
    raise SystemExit("rehearsal success predicates were not all derived true")
with out.open("x", encoding="utf-8") as stream:
    json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.chmod(out, 0o600)
PY
cp -- "$WORK/invariants.txt" "$WORK/output/invariants.txt"
(cd "$WORK/output" && sha256sum db-uploads-consistency.json invariants.txt rehearsal-evidence.json uploads-restore.json >sha256sums)
chmod 600 "$WORK/output"/*
mv -T -- "$WORK/output" "$OUTPUT_DIR"
