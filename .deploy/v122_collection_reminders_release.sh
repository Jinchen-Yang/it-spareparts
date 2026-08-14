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
python3 "$MANIFEST_TOOL" verify "$PACKAGE_DIR" >/dev/null \
  || fatal "flat package verification failed"
PACKAGE_PRODUCTION_READY=$(python3 -c 'import json,sys; print("true" if json.load(open(sys.argv[1]))["production_ready"] else "false")' "$MANIFEST")
readonly PACKAGE_PRODUCTION_READY
STATE_FILE="$EVIDENCE_DIR/release-state.json"
LOCK_FILE=${V122_GLOBAL_LOCK_FILE:-/run/lock/it-spareparts-v122-collection-reminders.lock}
readonly STATE_FILE LOCK_FILE
[[ "$LOCK_FILE" == /* && "$LOCK_FILE" != / && "$LOCK_FILE" != *'/../'* ]] \
  || fatal "V122_GLOBAL_LOCK_FILE must be a narrow absolute path"
mkdir -p -m 700 -- "$(dirname -- "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
flock -n 9 || fatal "another v1.22 release command holds the global lock"

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

require_production_ready() {
  [ "$PACKAGE_PRODUCTION_READY" = true ] \
    || fatal "production-ready package is required for $COMMAND"
}

state_get() {
  python3 - "$STATE_FILE" "$1" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
for key in sys.argv[2].split("."):
    value = value[key]
print(value)
PY
}

require_phase_in() {
  local current allowed
  current=$(state_get phase) || fatal "release phase state is missing"
  require_phase "$current"
  for allowed in "$@"; do
    [ "$current" = "$allowed" ] && return 0
  done
  fatal "release phase $current does not permit $COMMAND"
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

restore_previous_exact_images() {
  retag_and_start_exact_images \
    "$(manifest_get previous_images.app_image_id)" \
    "$(manifest_get previous_images.frontend_image_id)"
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
if case_name in {"cross_project_negative", "permission_negative"} and case["expected_status"] != 403:
    raise SystemExit("negative canary cases must explicitly expect HTTP 403")
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
	  python3 - "$workspace/$case_name.response" "$case_name" <<'PY'
import json
import pathlib
import sys

response = pathlib.Path(sys.argv[1])
case_name = sys.argv[2]
expected_codes = {
    "cross_project_negative": "canary_scope_denied",
    "permission_negative": "permission_denied",
}
expected = expected_codes.get(case_name)
if expected is None:
    raise SystemExit(0)
try:
    payload = json.loads(response.read_text(encoding="utf-8") or "{}")
except json.JSONDecodeError as exc:
    raise SystemExit(f"{case_name} response is not JSON: {exc}")
detail = payload.get("detail")
code = detail.get("code") if isinstance(detail, dict) else None
if code != expected:
    raise SystemExit(f"{case_name} must return {expected}")
PY
	  python3 - "$workspace/$case_name.outcome.json" "$case_name" "$status" \
    "$workspace/$case_name.response" <<'PY'
import hashlib
import json
import pathlib
import sys
response = pathlib.Path(sys.argv[4])
payload = {
    "case": sys.argv[2],
    "http_status": int(sys.argv[3]),
    "response_sha256": hashlib.sha256(response.read_bytes()).hexdigest(),
}
pathlib.Path(sys.argv[1]).write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
PY
}

collection_domain_fingerprint() {
  local dbc
  dbc=$(db_cid)
  [ -n "$dbc" ] || fatal "db container is not running for canary verification"
  docker exec "$dbc" psql -X -U spareparts -d spareparts -At -c \
    "SELECT (SELECT count(*) FROM maintenance_collection_milestone)::text || ':' || (SELECT count(*) FROM maintenance_collection_plan_source_binding)::text || ':' || (SELECT count(*) FROM maintenance_collection_milestone_operation)::text || ':' || (SELECT count(*) FROM maintenance_collection_plan_import_batch)::text;"
}

validate_canary_spec() {
  python3 - "$1" "$2" <<'PY'
import json
import pathlib
import sys
value=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if value.get("canary_project_id", sys.argv[2]) != sys.argv[2]:
    raise SystemExit("canary spec project mismatch")
accounts=value.get("named_accounts")
if not isinstance(accounts,dict) or len(accounts) < 2:
    raise SystemExit("canary spec needs named positive and denied accounts")
required=("action_grant","action_verify_granted","action_restore","action_verify_restored","follow_up_positive","cross_project_negative","permission_negative","import_preview_positive","apply_last")
for name in required:
    case=value.get(name)
    if not isinstance(case,dict): raise SystemExit("canary spec lacks case: "+name)
    if case.get("method") not in {"GET","POST"}: raise SystemExit("invalid canary method: "+name)
    if not isinstance(case.get("path"),str) or not case["path"].startswith("/"): raise SystemExit("invalid canary path: "+name)
    if not isinstance(case.get("expected_status"),int): raise SystemExit("invalid expected status: "+name)
for account_key, account in accounts.items():
    if not isinstance(account, dict):
        raise SystemExit("canary named account must be an object: "+str(account_key))
    role = account.get("expected_role")
    if not isinstance(role, str) or not role or "\n" in role:
        raise SystemExit("canary named account must declare expected_role: "+str(account_key))
def account_for(case_name):
    account_key = value[case_name].get("account")
    account = accounts.get(account_key)
    if not isinstance(account, dict):
        raise SystemExit("canary case references unknown named account: "+case_name)
    return account

follow_account = account_for("follow_up_positive")
if follow_account.get("expected_role") == "admin":
    raise SystemExit("follow-up positive account must not be admin")
if "action_maintenance_collection_follow_up" not in (follow_account.get("required_permissions") or []):
    raise SystemExit("follow-up positive account must explicitly hold follow-up action")

# The real import API intentionally requires a named admin in addition to the
# explicit high-risk action.  Admin still does not bypass the action check.
for name in ("import_preview_positive", "apply_last"):
    account = account_for(name)
    if account.get("expected_role") != "admin":
        raise SystemExit("collection-plan import positive account must be admin: "+name)
    if "action_maintenance_collection_plan_import" not in (account.get("required_permissions") or []):
        raise SystemExit("collection-plan import positive account lacks explicit import action: "+name)

denied_account = account_for("permission_negative")
if denied_account.get("expected_role") != "admin":
    raise SystemExit("permission-negative account must be admin to prove no admin bypass")
if "action_maintenance_collection_plan_import" not in (denied_account.get("forbidden_permissions") or []):
    raise SystemExit("permission-negative admin must explicitly lack import action")
for name in ("action_grant","action_verify_granted","action_restore","action_verify_restored"):
    if not isinstance(value[name].get("token"),str) or not value[name]["token"]:
        raise SystemExit("action control cases require sealed control token")
if value["cross_project_negative"]["expected_status"] != 403 or value["permission_negative"]["expected_status"] != 403:
    raise SystemExit("negative canary cases must expect 403")
if not value["apply_last"]["path"].endswith("/apply") or value["apply_last"]["expected_status"] // 100 != 2:
    raise SystemExit("apply_last contract mismatch")
PY
}

login_named_canary_accounts() {
  local spec=$1
  local workspace=$2
  local accounts name status
  accounts=$(python3 - "$spec" "$workspace" <<'PY'
import json
import os
import pathlib
import sys

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
for name, account in accounts.items():
    if not isinstance(name, str) or not name.replace("_", "").replace("-", "").isalnum():
        raise SystemExit("canary named account key is unsafe")
    if not isinstance(account, dict) or not account.get("username") or not account.get("password"):
        raise SystemExit(f"canary named account lacks credentials: {name}")
    expected_role = account.get("expected_role")
    if not isinstance(expected_role, str) or not expected_role or "\n" in expected_role:
        raise SystemExit(f"canary named account lacks expected_role: {name}")
    required = account.get("required_permissions") or []
    forbidden = account.get("forbidden_permissions") or []
    if not isinstance(required, list) or not isinstance(forbidden, list):
        raise SystemExit("canary permission expectations must be arrays")
    request = work / f"login-{name}.request.json"
    expectation = work / f"login-{name}.expectation.json"
    request.write_text(json.dumps({"username": account["username"], "password": account["password"]}, separators=(",", ":")), encoding="utf-8")
    expectation.write_text(json.dumps({"required": required, "forbidden": forbidden, "expected_role": expected_role, "base_url": base.rstrip("/")}, separators=(",", ":")), encoding="utf-8")
    os.chmod(request, 0o600); os.chmod(expectation, 0o600)
    print(name)
PY
)
  [ -n "$accounts" ] || fatal "canary spec has no named accounts"
  while IFS= read -r name; do
    status=$(curl --silent --show-error \
      --output "$workspace/login-$name.response.json" --write-out '%{http_code}' \
      --request POST --header 'Content-Type: application/json' \
      --data-binary "@$workspace/login-$name.request.json" \
      "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["base_url"]+"/auth/login")' "$workspace/login-$name.expectation.json")") \
      || fatal "named canary account login transport failed"
    [ "$status" = 200 ] || fatal "named canary account login status mismatch"
    python3 - "$name" "$workspace" <<'PY'
import hashlib
import json
import os
import pathlib
import sys
name=sys.argv[1]; work=pathlib.Path(sys.argv[2])
payload=json.loads((work/f"login-{name}.response.json").read_text())
expect=json.loads((work/f"login-{name}.expectation.json").read_text())
token=payload.get("token"); permissions=payload.get("permissions") or {}
if not isinstance(token,str) or not token: raise SystemExit("named canary login lacks token")
if payload.get("role") != expect["expected_role"]: raise SystemExit("named canary role readback mismatch")
missing=[key for key in expect["required"] if permissions.get(key) is not True]
granted=[key for key in expect["forbidden"] if permissions.get(key) is True]
if missing or granted: raise SystemExit("named canary permission readback mismatch")
token_path=work/"tokens"/f"{name}.token"; token_path.write_text(token); os.chmod(token_path,0o600)
summary={"account_key_sha256":hashlib.sha256(name.encode()).hexdigest(),"role":payload.get("role"),"expected_role":expect["expected_role"],"required_permissions":sorted(expect["required"]),"forbidden_permissions":sorted(expect["forbidden"])}
path=work/f"login-{name}.summary.json"; path.write_text(json.dumps(summary,sort_keys=True,separators=(",",":"))+"\n"); os.chmod(path,0o600)
PY
  done <<<"$accounts"
  python3 - "$workspace" <<'PY'
import json,pathlib,sys
work=pathlib.Path(sys.argv[1]); values=[json.loads(path.read_text()) for path in sorted(work.glob("login-*.summary.json"))]
(work/"named-account-readback.json").write_text(json.dumps(values,sort_keys=True,separators=(",",":"))+"\n")
PY
}

verify_active_release_lineage() {
  local expected_parent expected_db expected_app expected_frontend expected_compose
  safe_file "$ROOT_RELEASE_STATE" "active root release state"
  safe_file "$COMPOSE_FILE" "active compose"
  expected_parent=$(manifest_get parent_production_sha)
  expected_db=$(manifest_get database.image_id)
  expected_app=$(manifest_get previous_images.app_image_id)
  expected_frontend=$(manifest_get previous_images.frontend_image_id)
  expected_compose=$(sha256sum "$COMPOSE_FILE" | awk '{print $1}')
  python3 - "$ROOT_RELEASE_STATE" "$expected_parent" "$expected_db" "$expected_app" "$expected_frontend" "$expected_compose" <<'PY'
import json
import pathlib
import sys
state = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = {
    "production_sha": sys.argv[2],
    "database_image_id": sys.argv[3],
    "app_image_id": sys.argv[4],
    "frontend_image_id": sys.argv[5],
    "compose_sha256": sys.argv[6],
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
payload = dict(old)
payload.update({
    "format": "v122-collection-reminders-release-state-v2",
    "manifest_sha256": manifest_sha,
    "package_dir": str(pathlib.Path(package).resolve()),
    "target_sha": target,
    "parent_production_sha": parent,
    "phase": phase,
    "generation": int(old.get("generation", 0)) + 1,
})
for index in range(0, len(extra), 2):
    raw = extra[index + 1]
    payload[extra[index]] = True if raw == "true" else False if raw == "false" else raw
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
  migrate) [ "$#" -eq 0 ] || usage; require_production_ready; require_phase restore_checked ;;
  deploy) [ "$#" -eq 0 ] || usage; require_production_ready; require_phase migrated ;;
  canary)
    [ "$#" -eq 2 ] || usage
    require_production_ready
    CANARY_PROJECT_ID=$1
    CANARY_SPEC=$(realpath -e -- "$2")
    [ "$CANARY_PROJECT_ID" = "$(manifest_get runtime_flags.maintenance_collection_canary_project_id)" ] \
      || fatal "canary project id does not match manifest"
    [ -f "$CANARY_SPEC" ] && [ ! -L "$CANARY_SPEC" ] \
      && [ "$(stat -c '%a' "$CANARY_SPEC")" = 600 ] \
      || fatal "canary spec must be a mode-600 regular file"
    validate_canary_spec "$CANARY_SPEC" "$CANARY_PROJECT_ID"
    require_phase deployed
    ;;
  observe) require_production_ready; [ "$#" -eq 1 ] || usage; case "$1" in 0|5|15|30) ;; *) fatal "observe point must be 0, 5, 15, or 30";; esac ;;
  rollback-images)
    [ "$#" -le 1 ] || usage
    require_phase_in preflight frozen backup restore_checked migrated deployed canary observe_0 observe_5 observe_15 observed
    STATE_ACTIONS_GRANTED=$(python3 -c 'import json,sys; print("true" if json.load(open(sys.argv[1])).get("actions_granted") else "false")' "$STATE_FILE")
    if [ "$STATE_ACTIONS_GRANTED" = true ]; then
      [ "$#" -eq 1 ] || fatal "mode-600 canary spec is required to restore action permissions"
      ROLLBACK_SPEC=$(realpath -e -- "$1")
      [ -f "$ROLLBACK_SPEC" ] && [ ! -L "$ROLLBACK_SPEC" ] \
        && [ "$(stat -c '%a' "$ROLLBACK_SPEC")" = 600 ] \
        || fatal "rollback canary spec must be a mode-600 regular file"
    else
      [ "$#" -eq 0 ] || usage
    fi
    ;;
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
	    FREEZE_STOPPED_APP=false
	    freeze_abort() {
	      local message=$1
	      trap - HUP INT TERM
	      close_collection_writes >/dev/null 2>&1 || true
	      if [ "$FREEZE_STOPPED_APP" = true ]; then
	        restore_previous_exact_images >/dev/null 2>&1 || true
	      fi
	      fatal "$message"
	    }
	    trap 'freeze_abort "freeze-writes interrupted"' HUP INT TERM
	    close_collection_writes
	    compose stop app >/dev/null || freeze_abort "app container did not stop"
	    FREEZE_STOPPED_APP=true
	    [ -z "$(app_cid)" ] || freeze_abort "app container did not stop"
	    DBC=$(db_cid) || freeze_abort "db container is not running"
	    [ -n "$DBC" ] || freeze_abort "db container is not running"
	    PROCESSING=$(docker exec "$DBC" psql -X -U spareparts -d spareparts -At -c \
	      "SELECT count(*) FROM maintenance_collection_plan_import_batch WHERE status = 'processing';") \
	      || freeze_abort "could not check collection import processing"
	    [ "$PROCESSING" = 0 ] || freeze_abort "collection import batches are still processing"
	    advance_phase frozen processing_batches 0 writes_closed true \
	      || freeze_abort "could not persist frozen phase"
	    trap - HUP INT TERM
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
    cp -- "$ROOT_RELEASE_STATE" "$BACKUP_DIR/root-release-state.json"
    require_exact_image "$(manifest_get previous_images.app_image_id)"
    require_exact_image "$(manifest_get previous_images.frontend_image_id)"
    require_exact_image "$(manifest_get database.image_id)"
    docker image inspect \
      "$(manifest_get previous_images.app_image_id)" \
      "$(manifest_get previous_images.frontend_image_id)" \
      "$(manifest_get database.image_id)" >"$BACKUP_DIR/exact-images.json"
    docker ps --no-trunc --format '{{json .}}' >"$BACKUP_DIR/image_manifest.jsonl"
    python3 - "$BACKUP_DIR/uploads.tar" "$BACKUP_DIR/uploads-metadata.json" "$UPLOADS_COUNT" "$UPLOADS_BYTES" <<'PY'
import hashlib
import json
import pathlib
import tarfile
import sys

archive_path = pathlib.Path(sys.argv[1])
rows = []
with tarfile.open(archive_path, "r:*") as archive:
    for member in archive.getmembers():
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
            raise SystemExit("unsafe uploads archive metadata")
        if member.isfile():
            stream = archive.extractfile(member)
            if stream is None:
                raise SystemExit("cannot hash uploads archive member")
            rows.append({
                "path": path.as_posix(), "mode": member.mode, "uid": member.uid,
                "gid": member.gid, "mtime": member.mtime, "size": member.size,
                "sha256": hashlib.sha256(stream.read()).hexdigest(),
            })
rows.sort(key=lambda row: row["path"])
canonical = b"".join(
    (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
    for row in rows
)
payload = {
    "format": "v122-uploads-metadata-v1",
    "file_count": len(rows),
    "total_bytes": sum(row["size"] for row in rows),
    "metadata_tree_sha256": hashlib.sha256(canonical).hexdigest(),
}
if payload["file_count"] != int(sys.argv[3]) or payload["total_bytes"] != int(sys.argv[4]):
    raise SystemExit("live uploads count/bytes changed during backup")
pathlib.Path(sys.argv[2]).write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
PY
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
      "$(cat "$BACKUP_DIR/wal_lsn.txt")" "$UPLOADS_COUNT" "$UPLOADS_BYTES" \
      "$BACKUP_DIR/docker-compose.yml" "$BACKUP_DIR/env.snapshot" \
      "$BACKUP_DIR/root-release-state.json" "$BACKUP_DIR/uploads-metadata.json" \
      "$(manifest_get previous_images.app_image_id)" \
      "$(manifest_get previous_images.frontend_image_id)" <<'PY'
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
    active_compose,
    active_env,
    root_state,
    uploads_metadata,
    previous_app_image,
    previous_frontend_image,
) = sys.argv[1:20]

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
    "previous_app_image_id": previous_app_image,
    "previous_frontend_image_id": previous_frontend_image,
    "db_dump_sha256": digest(db_dump),
    "globals_sha256": digest(globals_file),
    "uploads_archive_sha256": digest(uploads),
    "candidate_compose_sha256": digest(compose),
    "wal_lsn": wal_lsn.strip(),
    "uploads_file_count": int(uploads_count),
    "uploads_total_bytes": int(uploads_bytes),
    "uploads_metadata_sha256": digest(uploads_metadata),
    "active_compose_sha256": digest(active_compose),
    "active_env_sha256": digest(active_env),
    "root_release_state_sha256": digest(root_state),
}
with open(output, "x", encoding="utf-8") as stream:
    json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
    stream.write("\n")
PY
    (cd "$BACKUP_DIR" && sha256sum * >sha256sums && sha256sum -c sha256sums)
	    restore_previous_exact_images
	    advance_phase backup \
	      backup_dir "$BACKUP_DIR" \
	      backup_manifest_sha256 "$(sha256sum "$BACKUP_DIR/backup-manifest.json" | awk '{print $1}')" \
	      backup_checksums_sha256 "$(sha256sum "$BACKUP_DIR/sha256sums" | awk '{print $1}')" \
	      backup_uploads_file_count "$UPLOADS_COUNT" \
	      backup_uploads_total_bytes "$UPLOADS_BYTES" \
	      service_restored true
	    ;;
  restore-check)
    [ "$#" -eq 2 ] || usage
    DB_DUMP=$(realpath -e -- "$1")
    UPLOADS_ARCHIVE=$(realpath -e -- "$2")
    BACKUP_DIR=$(realpath -e -- "$(state_get backup_dir)")
    [ "$DB_DUMP" = "$BACKUP_DIR/postgres_custom.dump" ] \
      && [ "$UPLOADS_ARCHIVE" = "$BACKUP_DIR/uploads.tar" ] \
      || fatal "restore-check must consume state-bound backup assets"
    [ "$(sha256sum "$BACKUP_DIR/backup-manifest.json" | awk '{print $1}')" = "$(state_get backup_manifest_sha256)" ] \
      || fatal "state-bound backup manifest hash mismatch"
    [ "$(sha256sum "$BACKUP_DIR/sha256sums" | awk '{print $1}')" = "$(state_get backup_checksums_sha256)" ] \
      || fatal "state-bound backup checksum hash mismatch"
    (cd "$BACKUP_DIR" && sha256sum -c sha256sums >/dev/null) \
      || fatal "state-bound backup checksums are stale"
    if [ "$PACKAGE_PRODUCTION_READY" = true ]; then REHEARSAL_STAGE=final; else REHEARSAL_STAGE=preliminary; fi
    V122_REHEARSAL_STAGE="$REHEARSAL_STAGE" \
    V122_EXPECTED_MANIFEST_SHA256="$EXPECTED_MANIFEST_SHA256" \
    "$SCRIPT_DIR/v122_collection_reminders_rehearse.sh" "$DB_DUMP" "$UPLOADS_ARCHIVE" \
      "$(manifest_get target_sha)" "$(manifest_get parent_production_sha)" \
      "$(manifest_get database.image_id)" "$(manifest_get images.app_image_id)" \
      "$(manifest_get images.frontend_image_id)" "$PACKAGE_DIR/candidate-compose.yml" \
      "$EVIDENCE_DIR/restore-check"
    advance_phase restore_checked
    ;;
	  migrate)
	    [ "$#" -eq 0 ] || usage
	    close_collection_writes
	    compose stop app >/dev/null
	    [ -z "$(app_cid)" ] || fatal "app container did not stop before migrate"
	    DBC=$(db_cid)
	    [ -n "$DBC" ] || fatal "db container is not running"
	    CURRENT=$(docker exec "$DBC" psql -X -U spareparts -d spareparts -At -c 'SELECT version_num FROM alembic_version;')
	    [ "$CURRENT" = "$FROM_REV" ] || fatal "production DB is not at d9 before migrate"
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
    APPLY_OPEN=false
    ACTIONS_GRANTED=false
    emergency_close_canary() {
      local original_status=${1:-$?}
      trap - ERR HUP INT TERM
      update_env_key MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED false || true
      if [ "$ACTIONS_GRANTED" = true ]; then
        run_sealed_canary_case "$CANARY_SPEC" action_restore "$CANARY_WORK" || true
        run_sealed_canary_case "$CANARY_SPEC" action_verify_restored "$CANARY_WORK" || true
      fi
      compose up --no-deps --no-build --force-recreate -d app >/dev/null 2>&1 || true
      return "$original_status"
    }
    canary_exit_guard() {
      local original_status=$?
      trap - EXIT ERR HUP INT TERM
      if [ "$original_status" -ne 0 ]; then
        emergency_close_canary "$original_status" || true
      fi
      cleanup_canary_work
      exit "$original_status"
    }
    trap canary_exit_guard EXIT
    trap 'exit 130' HUP INT TERM
    # Explicit action grants are part of the sealed release input.  A failed
    # grant or verification triggers the emergency restore path above.
    run_sealed_canary_case "$CANARY_SPEC" action_grant "$CANARY_WORK"
    ACTIONS_GRANTED=true
    run_sealed_canary_case "$CANARY_SPEC" action_verify_granted "$CANARY_WORK"
    login_named_canary_accounts "$CANARY_SPEC" "$CANARY_WORK"
    # The request sequence is deliberately fixed.  Both negative cases and
    # preview must succeed while apply is false.  The canary apply flag is
    # opened only for the single manifest-named project, and only for the last
    # request; if that request fails, the script closes the flag again.
    run_sealed_canary_case "$CANARY_SPEC" follow_up_positive "$CANARY_WORK"
    DOMAIN_BEFORE=$(collection_domain_fingerprint)
    run_sealed_canary_case "$CANARY_SPEC" cross_project_negative "$CANARY_WORK"
    [ "$(collection_domain_fingerprint)" = "$DOMAIN_BEFORE" ] \
      || fatal "cross-project negative canary changed collection domain state"
    DOMAIN_BEFORE=$(collection_domain_fingerprint)
    run_sealed_canary_case "$CANARY_SPEC" permission_negative "$CANARY_WORK"
    [ "$(collection_domain_fingerprint)" = "$DOMAIN_BEFORE" ] \
      || fatal "permission-negative canary changed collection domain state"
    run_sealed_canary_case "$CANARY_SPEC" import_preview_positive "$CANARY_WORK"
    update_env_key MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED true
    compose up --no-deps --no-build --force-recreate -d app
    RUNNING_FLAGS=$(compose exec -T app sh -ceu 'printf "%s\n%s\n" "$MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED" "$MAINTENANCE_COLLECTION_CANARY_PROJECT_ID"')
    [ "$RUNNING_FLAGS" = "true
$CANARY_PROJECT_ID" ] || fatal "running apply canary configuration readback mismatch"
    if ! (run_sealed_canary_case "$CANARY_SPEC" apply_last "$CANARY_WORK"); then
      fatal "apply canary failed; collection apply flag was closed"
    fi
    python3 - "$CANARY_WORK" "$EVIDENCE_DIR/canary-evidence.json" <<'PY'
import hashlib
import json
import pathlib
import sys
work = pathlib.Path(sys.argv[1])
outcomes = {}
for path in sorted(work.glob("*.outcome.json")):
    value = json.loads(path.read_text(encoding="utf-8"))
    outcomes[value["case"]] = {
        "http_status": value["http_status"],
        "response_sha256": value["response_sha256"],
    }
account = work / "named-account-readback.json"
payload = {
    "format": "v122-canary-evidence-v1",
    "cases": outcomes,
    "named_account_readback_sha256": hashlib.sha256(account.read_bytes()).hexdigest(),
    "contains_secrets": False,
}
pathlib.Path(sys.argv[2]).write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
PY
	    advance_phase canary \
	      canary_project_id "$CANARY_PROJECT_ID" \
	      apply_enabled true actions_granted true \
	      canary_evidence_sha256 "$(sha256sum "$EVIDENCE_DIR/canary-evidence.json" | awk '{print $1}')"
	    trap - EXIT ERR HUP INT TERM
	    rm -rf -- "$CANARY_WORK"
	    ;;
  observe)
    case "$1" in
      0) require_phase canary; NEXT_PHASE=observe_0 ;;
      5) require_phase observe_0; NEXT_PHASE=observe_5 ;;
      15) require_phase observe_5; NEXT_PHASE=observe_15 ;;
      30) require_phase observe_15; NEXT_PHASE=observed ;;
    esac
    APP_CID=$(app_cid); DBC=$(db_cid); FRONTEND_CID=$(frontend_cid)
    [ -n "$APP_CID" ] && [ -n "$DBC" ] && [ -n "$FRONTEND_CID" ] \
      || fatal "observation requires running app/frontend/db containers"
    HEALTH_STATUS=$(docker exec "$APP_CID" python -c \
      'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5).status)')
    READINESS_STATUS=$(docker exec "$APP_CID" python -c \
      'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/health/db", timeout=5).status)')
    [ "$HEALTH_STATUS" = 200 ] && [ "$READINESS_STATUS" = 200 ] \
      || fatal "health/readiness failed during observation"
    WINDOW=$1; [ "$WINDOW" = 0 ] && WINDOW=1
    docker logs --since "${WINDOW}m" "$APP_CID" >"$EVIDENCE_DIR/observe-$1-app.log" 2>&1
    HTTP_5XX=$(python3 - "$EVIDENCE_DIR/observe-$1-app.log" <<'PY'
import pathlib
import re
import sys
print(sum(1 for line in pathlib.Path(sys.argv[1]).read_text(errors="replace").splitlines() if re.search(r"(?:^|\s)5\d\d(?:\s|$)", line)))
PY
)
    BLOCKING_LOCKS=$(docker exec "$DBC" psql -X -U spareparts -d spareparts -At -c \
      "SELECT count(*) FROM pg_locks blocked JOIN pg_stat_activity activity ON activity.pid=blocked.pid WHERE NOT blocked.granted;")
    SLOW_QUERIES=$(docker exec "$DBC" psql -X -U spareparts -d spareparts -At -c \
      "SELECT count(*) FROM pg_stat_activity WHERE state = 'active' AND pid <> pg_backend_pid() AND now() - query_start > interval '30 seconds';")
    AUDIT_COUNT=$(docker exec "$DBC" psql -X -U spareparts -d spareparts -At -c \
      "SELECT count(*) FROM maintenance_collection_milestone_operation WHERE created_at >= now() - interval '${WINDOW} minutes';")
    APP_RESTARTS=$(docker inspect --format '{{.RestartCount}}' "$APP_CID")
    FRONTEND_RESTARTS=$(docker inspect --format '{{.RestartCount}}' "$FRONTEND_CID")
    DB_RESTARTS=$(docker inspect --format '{{.RestartCount}}' "$DBC")
    RESTART_COUNT=$((APP_RESTARTS + FRONTEND_RESTARTS + DB_RESTARTS))
    UPLOADS_COUNT=$(docker run --rm -v "$UPLOADS_VOLUME:/uploads:ro" alpine:3.20 \
      sh -c 'find /uploads -type f | wc -l' | tr -d '[:space:]')
    UPLOADS_BYTES=$(docker run --rm -v "$UPLOADS_VOLUME:/uploads:ro" alpine:3.20 \
      sh -c 'find /uploads -type f -exec wc -c {} + | awk "{if (\$2 != \"total\") s += \$1} END {print s+0}"' | tr -d '[:space:]')
    compose ps >"$EVIDENCE_DIR/observe-$1-compose-ps.txt"
    docker stats --no-stream >"$EVIDENCE_DIR/observe-$1-docker-stats.txt"
	    python3 - "$EVIDENCE_DIR/observe-$1.json" "$1" "$HEALTH_STATUS" \
	      "$READINESS_STATUS" "$HTTP_5XX" "$BLOCKING_LOCKS" "$SLOW_QUERIES" \
	      "$RESTART_COUNT" "$UPLOADS_COUNT" "$UPLOADS_BYTES" "$AUDIT_COUNT" <<'PY'
import json
import pathlib
import sys
keys = ("minute", "health_status", "readiness_status", "http_5xx_count", "blocking_lock_count", "slow_query_count", "restart_count", "uploads_file_count", "uploads_total_bytes", "audit_count")
payload = {key: int(value) for key, value in zip(keys, sys.argv[2:])}
payload["format"] = "v122-observation-v1"
pathlib.Path(sys.argv[1]).write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
PY
	    if ! python3 - "$STATE_FILE" "$EVIDENCE_DIR/observe-$1.json" <<'PY'
import json
import pathlib
import sys

state = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
metrics = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
problems = []
for key in ("http_5xx_count", "blocking_lock_count", "slow_query_count", "restart_count"):
    if metrics.get(key) != 0:
        problems.append(key)
baseline_count = state.get("observe_uploads_file_count", state.get("backup_uploads_file_count"))
baseline_bytes = state.get("observe_uploads_total_bytes", state.get("backup_uploads_total_bytes"))
if baseline_count is not None:
    baseline_count = int(baseline_count)
if baseline_bytes is not None:
    baseline_bytes = int(baseline_bytes)
if baseline_count is not None and metrics.get("uploads_file_count") != baseline_count:
    problems.append("uploads_file_count")
if baseline_bytes is not None and metrics.get("uploads_total_bytes") != baseline_bytes:
    problems.append("uploads_total_bytes")
if problems:
    raise SystemExit(",".join(problems))
PY
	    then
	      fatal "observation failed release thresholds"
	    fi
	    advance_phase "$NEXT_PHASE" \
	      observe_minutes "$1" \
	      observation_sha256 "$(sha256sum "$EVIDENCE_DIR/observe-$1.json" | awk '{print $1}')" \
	      observe_uploads_file_count "$UPLOADS_COUNT" \
	      observe_uploads_total_bytes "$UPLOADS_BYTES"
	    ;;
  rollback-images)
    close_collection_writes
    if [ "${STATE_ACTIONS_GRANTED:-false}" = true ]; then
      ROLLBACK_WORK=$(mktemp -d -t v122-rollback.XXXXXXXX)
      if ! run_sealed_canary_case "$ROLLBACK_SPEC" action_restore "$ROLLBACK_WORK" \
        || ! run_sealed_canary_case "$ROLLBACK_SPEC" action_verify_restored "$ROLLBACK_WORK"; then
        rm -rf -- "$ROLLBACK_WORK"
        fatal "action permission restore failed; images were not changed"
      fi
      python3 - "$ROLLBACK_WORK" "$EVIDENCE_DIR/action-restore-evidence.json" <<'PY'
import json
import pathlib
import sys
work = pathlib.Path(sys.argv[1])
cases = [json.loads(path.read_text()) for path in sorted(work.glob("*.outcome.json"))]
pathlib.Path(sys.argv[2]).write_text(json.dumps({"format":"v122-action-restore-evidence-v1","outcomes":cases,"contains_secrets":False}, sort_keys=True, separators=(",", ":")) + "\n")
PY
      rm -rf -- "$ROLLBACK_WORK"
    fi
    printf 'rollback-images requested; additive schema is retained; no automatic downgrade; restore DB/uploads only after incident approval\n'
    retag_and_start_exact_images \
      "$(manifest_get previous_images.app_image_id)" \
      "$(manifest_get previous_images.frontend_image_id)"
    advance_phase rolled_back \
      actions_granted false apply_enabled false \
      rollback_note 'images-actions-and-flags-only; no downgrade/delete/automatic restore'
    ;;
  *)
    usage
    ;;
esac
