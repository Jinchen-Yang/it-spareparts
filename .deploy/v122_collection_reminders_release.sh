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

snapshot_sealed_canary_spec() {
  local source=$1
  local workspace=$2
  local snapshot="$workspace/sealed-canary-spec.json"
  [ -d "$workspace" ] && [ ! -L "$workspace" ] \
    && [ "$(stat -c '%a' "$workspace")" = 700 ] || return 1
  python3 - "$source" "$snapshot" <<'PY' || return 1
import os
import stat
import sys

source, snapshot = sys.argv[1:3]
source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    source_stat = os.fstat(source_fd)
    if not stat.S_ISREG(source_stat.st_mode):
        raise SystemExit("canary spec snapshot source is not regular")
    with os.fdopen(source_fd, "rb", closefd=False) as stream:
        payload = stream.read()
finally:
    os.close(source_fd)

destination_fd = os.open(
    snapshot,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
)
try:
    with os.fdopen(destination_fd, "wb", closefd=False) as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.fchmod(destination_fd, 0o600)
finally:
    os.close(destination_fd)
directory_fd = os.open(os.path.dirname(snapshot), os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
  [ -f "$snapshot" ] && [ ! -L "$snapshot" ] \
    && [ "$(stat -c '%a' "$snapshot")" = 600 ] || return 1
  printf '%s\n' "$snapshot"
}

snapshot_historical_gap_approval() {
  local source=$1
  local workspace=$2
  local snapshot="$workspace/historical-upload-gap-approval.json"
  [ -d "$workspace" ] && [ ! -L "$workspace" ] \
    && [ "$(stat -c '%a' "$workspace")" = 700 ] || return 1
  python3 - "$source" "$snapshot" <<'PY' || return 1
import os
import stat
import sys

source, snapshot = sys.argv[1:3]
source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    source_stat = os.fstat(source_fd)
    if not stat.S_ISREG(source_stat.st_mode):
        raise SystemExit("historical gap approval snapshot source is not regular")
    with os.fdopen(source_fd, "rb", closefd=False) as stream:
        payload = stream.read()
finally:
    os.close(source_fd)

destination_fd = os.open(
    snapshot,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
)
try:
    with os.fdopen(destination_fd, "wb", closefd=False) as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.fchmod(destination_fd, 0o600)
finally:
    os.close(destination_fd)
directory_fd = os.open(os.path.dirname(snapshot), os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
  [ -f "$snapshot" ] && [ ! -L "$snapshot" ] \
    && [ "$(stat -c '%a' "$snapshot")" = 600 ] || return 1
  printf '%s\n' "$snapshot"
}

usage() {
  cat >&2 <<'EOF'
usage: v122_collection_reminders_release.sh PACKAGE_DIR EVIDENCE_DIR preflight|freeze-writes|backup|restore-check|migrate|deploy|canary|observe|rollback-images [ARGS]

commands:
  preflight
  freeze-writes
  backup
  restore-check DB_DUMP UPLOADS_ARCHIVE [MODE600_HISTORICAL_UPLOAD_GAP_APPROVAL]
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
  local metadata status expected method header payload url
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
if "{canary_milestone_id}" in case["path"]:
    state_path = work / "milestone-state.json"
    if not state_path.is_file() or state_path.is_symlink():
        raise SystemExit("dynamic canary milestone is not resolved")
    milestone_id = json.loads(state_path.read_text(encoding="utf-8")).get("milestone_id")
    if not isinstance(milestone_id, str) or not milestone_id:
        raise SystemExit("dynamic canary milestone is invalid")
    case["path"] = case["path"].replace("{canary_milestone_id}", milestone_id)
if case_name == "apply_last":
    if not case["path"].endswith("/apply") or case["expected_status"] // 100 != 2:
        raise SystemExit("apply_last must be the sole successful /apply request")
elif case_name == "cross_project_negative":
    if not case["path"].endswith("/apply") or case["expected_status"] != 403:
        raise SystemExit("cross_project_negative must be a forbidden /apply request")
elif case["path"].endswith("/apply"):
    raise SystemExit("only apply_last and cross_project_negative may target /apply")
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
) || return 1
	  expected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["expected_status"])' "$metadata") || return 1
	  method=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["method"])' "$metadata") || return 1
	  header=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["header"])' "$metadata") || return 1
	  payload=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["payload"])' "$metadata") || return 1
	  url=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["url"])' "$metadata") || return 1
	  status=$(curl --silent --show-error --output "$workspace/$case_name.response" \
    --write-out '%{http_code}' --request "$method" \
    --header "@$header" \
    --header 'Content-Type: application/json' \
    --data-binary "@$payload" \
	    "$url") || {
	    printf 'canary HTTP transport failed for %s\n' "$case_name" >&2
	    return 1
	  }
	  [ "$status" = "$expected" ] || {
	    printf 'canary HTTP status mismatch for %s\n' "$case_name" >&2
	    return 1
	  }
	  python3 - "$workspace/$case_name.response" "$case_name" <<'PY' || return 1
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
    "$workspace/$case_name.response" <<'PY' || return 1
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

run_sealed_action_cases() {
  local spec=$1
  local list_name=$2
  local workspace=$3
  local metadata_files metadata expected status failed=false
  metadata_files=$(python3 - "$spec" "$list_name" "$workspace" <<'PY'
import json
import os
import pathlib
import re
import sys

spec = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
list_name = sys.argv[2]
work = pathlib.Path(sys.argv[3])
base = spec.get("base_url")
cases = spec.get(list_name)
username_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
if not isinstance(base, str) or not base.startswith(("http://", "https://")) or "\n" in base:
    raise SystemExit("canary base_url is invalid")
if list_name not in {"action_grant", "action_restore"} or not isinstance(cases, list) or not cases:
    raise SystemExit("action control cases must be a non-empty list")
for index, case in enumerate(cases):
    if not isinstance(case, dict):
        raise SystemExit("action control case must be an object")
    path = case.get("path")
    match = re.fullmatch(r"/api/accounts/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})", path or "")
    body = case.get("body")
    overrides = body.get("overrides") if isinstance(body, dict) else None
    token = case.get("token")
    if (
        case.get("method") != "PUT"
        or match is None
        or username_pattern.fullmatch(match.group(1)) is None
        or case.get("expected_status") != 200
        or not isinstance(token, str)
        or not token
        or not isinstance(body, dict)
        or set(body) != {"overrides"}
        or not isinstance(overrides, dict)
    ):
        raise SystemExit("invalid sealed account action case")
    name = f"{list_name}-{index:03d}"
    header = work / f"{name}.header"
    payload = work / f"{name}.json"
    response = work / f"{name}.response"
    outcome = work / f"{name}.outcome.json"
    metadata = work / f"{name}.meta.json"
    header.write_text(f"Authorization: Bearer {token}\n", encoding="utf-8")
    payload.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
    for sealed in (header, payload):
        os.chmod(sealed, 0o600)
    metadata.write_text(
        json.dumps(
            {
                "case": f"{list_name}[{index}]",
                "url": base.rstrip("/") + path,
                "header": str(header),
                "payload": str(payload),
                "response": str(response),
                "outcome": str(outcome),
                "expected_status": 200,
                "target": match.group(1),
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.chmod(metadata, 0o600)
    print(metadata)
PY
) || return 1
  [ -n "$metadata_files" ] || return 1
  while IFS= read -r metadata; do
    expected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["expected_status"])' "$metadata")
    if ! status=$(curl --silent --show-error \
      --output "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["response"])' "$metadata")" \
      --write-out '%{http_code}' --request PUT \
      --header "@$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["header"])' "$metadata")" \
      --header 'Content-Type: application/json' \
      --data-binary "@$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["payload"])' "$metadata")" \
      "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["url"])' "$metadata")"); then
      printf 'action account update transport failed: %s\n' "$list_name" >&2
      failed=true
      continue
    fi
    if [ "$status" != "$expected" ]; then
      printf 'action account update status mismatch: %s\n' "$list_name" >&2
      failed=true
      continue
    fi
    if ! python3 - "$metadata" "$status" <<'PY'
import hashlib
import json
import pathlib
import sys

metadata = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
request = json.loads(pathlib.Path(metadata["payload"]).read_text(encoding="utf-8"))
response_path = pathlib.Path(metadata["response"])
try:
    response = json.loads(response_path.read_text(encoding="utf-8"))
except json.JSONDecodeError as exc:
    raise SystemExit(f"action account response is not JSON: {exc}")
if not isinstance(response, dict):
    raise SystemExit("action account response must be an object")
if response.get("username") != metadata["target"]:
    raise SystemExit("action account response username mismatch")
if response.get("overrides") != request["overrides"]:
    raise SystemExit("action account response overrides mismatch")
outcome = {
    "case": metadata["case"],
    "http_status": int(sys.argv[2]),
    "response_sha256": hashlib.sha256(response_path.read_bytes()).hexdigest(),
    "target_username_sha256": hashlib.sha256(metadata["target"].encode()).hexdigest(),
}
pathlib.Path(metadata["outcome"]).write_text(
    json.dumps(outcome, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
    then
      failed=true
    fi
  done <<<"$metadata_files"
  [ "$failed" = false ]
}

verify_action_account_state() {
  local spec=$1
  local verify_case=$2
  local expected_list=$3
  local workspace=$4
  run_sealed_canary_case "$spec" "$verify_case" "$workspace" || return 1
  python3 - "$spec" "$expected_list" "$workspace/$verify_case.response" <<'PY' || return 1
import json
import pathlib
import re
import sys

spec = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
cases = spec.get(sys.argv[2])
try:
    rows = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
except json.JSONDecodeError as exc:
    raise SystemExit(f"account overrides state mismatch: response is not JSON: {exc}")
if not isinstance(cases, list) or not cases or not isinstance(rows, list):
    raise SystemExit("account overrides state mismatch: invalid expected or actual account list")
expected = {}
for case in cases:
    match = re.fullmatch(
        r"/api/accounts/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})",
        case.get("path", "") if isinstance(case, dict) else "",
    )
    body = case.get("body") if isinstance(case, dict) else None
    if match is None or not isinstance(body, dict) or not isinstance(body.get("overrides"), dict):
        raise SystemExit("account overrides state mismatch: invalid expected account case")
    expected[match.group(1)] = body["overrides"]
actual = {}
for row in rows:
    if not isinstance(row, dict):
        continue
    username = row.get("username")
    if username not in expected:
        continue
    if username in actual or not isinstance(row.get("overrides"), dict):
        raise SystemExit("account overrides state mismatch: duplicate or invalid target account")
    actual[username] = row["overrides"]
if set(actual) != set(expected):
    raise SystemExit("account overrides state mismatch: target account set differs")
if any(actual[username] != overrides for username, overrides in expected.items()):
    raise SystemExit("account overrides state mismatch: exact overrides differ")
PY
}

run_canary_setup_contract() {
  local spec=$1
  local workspace=$2
  run_sealed_canary_case "$spec" setup_contract "$workspace"
  python3 - "$spec" "$workspace/setup_contract.response" "$workspace/contract-state.json" "$CANARY_PROJECT_ID" <<'PY'
import json
import os
import pathlib
import sys

spec = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
response = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8") or "{}")
output = pathlib.Path(sys.argv[3])
canary_project_id = sys.argv[4]
contract_id = response.get("project_contract_id")
version = response.get("version")
if response.get("project_id") != canary_project_id:
    raise SystemExit("canary setup contract returned a non-canary project")
if not isinstance(contract_id, str) or not contract_id:
    raise SystemExit("canary setup contract did not return project_contract_id")
if not isinstance(version, int) or version < 1:
    raise SystemExit("canary setup contract did not return a valid version")
if "included_in_total" in response and response["included_in_total"] is not False:
    raise SystemExit("canary setup contract must not be included in business totals")
project_version = spec["import_preview_positive"].get("project_version")
if not isinstance(project_version, int) or project_version < 1:
    raise SystemExit("canary import preview must declare current canary project_version")
payload = {
    "project_contract_id": contract_id,
    "project_contract_version": version,
    "project_version": project_version,
}
output.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
os.chmod(output, 0o600)
PY
}

run_canary_import_preview() {
  local spec=$1
  local workspace=$2
  local metadata status expected
  metadata=$(python3 - "$spec" "$workspace" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

spec = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
work = pathlib.Path(sys.argv[2])
case = spec["import_preview_positive"]
account = case["account"]
token_path = work / "tokens" / f"{account}.token"
token = token_path.read_text(encoding="utf-8").strip()
workbook = pathlib.Path(case["workbook_path"])
header = work / "import_preview_positive.header"
meta = work / "import_preview_positive.meta.json"
header.write_text(f"Authorization: Bearer {token}\n", encoding="utf-8")
os.chmod(header, 0o600)
payload = {
    "url": spec["base_url"].rstrip("/") + case["path"],
    "header": str(header),
    "workbook": str(workbook),
    "idempotency_key": case["idempotency_key"],
    "expected_status": case["expected_status"],
}
meta.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
os.chmod(meta, 0o600)
print(meta)
PY
)
  expected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["expected_status"])' "$metadata")
  status=$(curl --silent --show-error \
    --output "$workspace/import_preview_positive.response" \
    --write-out '%{http_code}' --request POST \
    --header "@$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["header"])' "$metadata")" \
    --header "Idempotency-Key: $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["idempotency_key"])' "$metadata")" \
    --form "file=@$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["workbook"])' "$metadata");type=application/vnd.ms-excel" \
    "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["url"])' "$metadata")") \
    || fatal "canary HTTP transport failed for import_preview_positive"
  [ "$status" = "$expected" ] \
    || fatal "canary HTTP status mismatch for import_preview_positive"
  python3 - "$spec" "$workspace/import_preview_positive.response" \
    "$workspace/apply-state.json" "$CANARY_PROJECT_ID" "$workspace/contract-state.json" <<'PY'
import json
import os
import pathlib
import re
import sys

spec = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
preview = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
output = pathlib.Path(sys.argv[3])
canary_project_id = sys.argv[4]
contract_state = json.loads(pathlib.Path(sys.argv[5]).read_text(encoding="utf-8"))
if preview.get("status") != "valid":
    raise SystemExit("canary import preview is not valid")
batch_id = preview.get("batch_id")
if not isinstance(batch_id, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", batch_id) is None:
    raise SystemExit("canary import preview returned an invalid batch id")
batch_version = preview.get("batch_version")
data_version = preview.get("data_version")
rows = preview.get("rows")
if not isinstance(batch_version, int) or batch_version < 1 or not isinstance(data_version, str) or not data_version:
    raise SystemExit("canary import preview lacks versions")
if not isinstance(rows, list) or not rows:
    raise SystemExit("canary import preview has no rows")
row_keys = {}
for row in rows:
    if not isinstance(row, dict):
        raise SystemExit("canary import preview row is invalid")
    external = row.get("external_order_no")
    row_key = row.get("row_key")
    if not isinstance(external, str) or not external or not isinstance(row_key, str) or not row_key:
        raise SystemExit("canary import preview row lacks stable keys")
    if external in row_keys:
        raise SystemExit("canary import preview has duplicate external order")
    row_keys[external] = row_key
bindings = spec["import_preview_positive"]["bindings"]
provided = {binding["external_order_no"] for binding in bindings}
if provided != set(row_keys):
    raise SystemExit("canary bindings do not exactly cover preview rows")
resolved = []
for binding in bindings:
    if binding["project_id"] != canary_project_id:
        raise SystemExit("canary binding targets a non-canary project")
    if binding["project_contract_id"] == "{setup_contract.project_contract_id}":
        binding = {**binding, "project_contract_id": contract_state["project_contract_id"]}
    if binding["project_contract_version"] == "{setup_contract.version}":
        binding = {**binding, "project_contract_version": contract_state["project_contract_version"]}
    resolved.append({**binding, "row_key": row_keys[binding["external_order_no"]]})
payload = {
    "batch_id": batch_id,
    "body": {
        "expected_batch_version": batch_version,
        "expected_data_version": data_version,
        "bindings": resolved,
    },
}
output.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
os.chmod(output, 0o600)
PY
  python3 - "$workspace/import_preview_positive.outcome.json" "$status" \
    "$workspace/import_preview_positive.response" <<'PY'
import hashlib
import json
import pathlib
import sys
response = pathlib.Path(sys.argv[3])
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    "case": "import_preview_positive",
    "http_status": int(sys.argv[2]),
    "response_sha256": hashlib.sha256(response.read_bytes()).hexdigest(),
}, sort_keys=True, separators=(",", ":")) + "\n")
PY
}

run_canary_cross_project_negative() {
  local spec=$1
  local workspace=$2
  local metadata expected status
  metadata=$(python3 - "$spec" "$workspace" <<'PY'
import json
import os
import pathlib
import sys

spec = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
work = pathlib.Path(sys.argv[2])
case = spec["cross_project_negative"]
state = json.loads((work / "apply-state.json").read_text(encoding="utf-8"))
account = case["account"]
token = (work / "tokens" / f"{account}.token").read_text(encoding="utf-8").strip()
body = state["body"]
cross_project_id = case["body"]["project_id"]
if cross_project_id == spec.get("canary_project_id"):
    raise SystemExit("cross-project negative must target a non-canary project")
bindings = []
for binding in body["bindings"]:
    bindings.append({
        **binding,
        "project_id": cross_project_id,
        "project_version": case["body"]["project_version"],
        "project_contract_id": case["body"]["project_contract_id"],
        "project_contract_version": case["body"]["project_contract_version"],
    })
body = {**body, "bindings": bindings}
header = work / "cross_project_negative.header"
payload = work / "cross_project_negative.json"
meta = work / "cross_project_negative.meta.json"
header.write_text(f"Authorization: Bearer {token}\n", encoding="utf-8")
payload.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
for path in (header, payload):
    os.chmod(path, 0o600)
url_path = case["path"].replace("{batch_id}", state["batch_id"])
meta.write_text(json.dumps({
    "url": spec["base_url"].rstrip("/") + url_path,
    "header": str(header),
    "payload": str(payload),
    "expected_status": case["expected_status"],
}), encoding="utf-8")
os.chmod(meta, 0o600)
print(meta)
PY
)
  expected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["expected_status"])' "$metadata")
  status=$(curl --silent --show-error --output "$workspace/cross_project_negative.response" \
    --write-out '%{http_code}' --request POST \
    --header "@$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["header"])' "$metadata")" \
    --header 'Content-Type: application/json' \
    --data-binary "@$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["payload"])' "$metadata")" \
    "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["url"])' "$metadata")") \
    || fatal "canary HTTP transport failed for cross_project_negative"
  [ "$status" = "$expected" ] || fatal "canary HTTP status mismatch for cross_project_negative"
  python3 - "$workspace/cross_project_negative.response" <<'PY'
import json
import pathlib
import sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8") or "{}")
detail = payload.get("detail")
code = detail.get("code") if isinstance(detail, dict) else None
if code != "canary_scope_denied":
    raise SystemExit("cross_project_negative must return canary_scope_denied")
PY
  python3 - "$workspace/cross_project_negative.outcome.json" "$status" \
    "$workspace/cross_project_negative.response" <<'PY'
import hashlib
import json
import pathlib
import sys
response = pathlib.Path(sys.argv[3])
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    "case": "cross_project_negative",
    "http_status": int(sys.argv[2]),
    "response_sha256": hashlib.sha256(response.read_bytes()).hexdigest(),
}, sort_keys=True, separators=(",", ":")) + "\n")
PY
}

run_canary_apply_last() {
  local spec=$1
  local workspace=$2
  local metadata status expected
  metadata=$(python3 - "$spec" "$workspace" <<'PY'
import json
import os
import pathlib
import sys

spec = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
work = pathlib.Path(sys.argv[2])
case = spec["apply_last"]
state = json.loads((work / "apply-state.json").read_text(encoding="utf-8"))
token = (work / "tokens" / f"{case['account']}.token").read_text(encoding="utf-8").strip()
header = work / "apply_last.header"
payload = work / "apply_last.json"
meta = work / "apply_last.meta.json"
header.write_text(f"Authorization: Bearer {token}\n", encoding="utf-8")
payload.write_text(json.dumps(state["body"], separators=(",", ":")), encoding="utf-8")
for path in (header, payload):
    os.chmod(path, 0o600)
url_path = case["path"].replace("{batch_id}", state["batch_id"])
value = {
    "url": spec["base_url"].rstrip("/") + url_path,
    "header": str(header),
    "payload": str(payload),
    "expected_status": case["expected_status"],
}
meta.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
os.chmod(meta, 0o600)
print(meta)
PY
)
  expected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["expected_status"])' "$metadata")
  status=$(curl --silent --show-error --output "$workspace/apply_last.response" \
    --write-out '%{http_code}' --request POST \
    --header "@$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["header"])' "$metadata")" \
    --header 'Content-Type: application/json' \
    --data-binary "@$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["payload"])' "$metadata")" \
    "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["url"])' "$metadata")") \
    || fatal "canary HTTP transport failed for apply_last"
  [ "$status" = "$expected" ] || fatal "canary HTTP status mismatch for apply_last"
  python3 - "$workspace/apply_last.outcome.json" "$status" \
    "$workspace/apply_last.response" <<'PY'
import hashlib
import json
import pathlib
import sys
response = pathlib.Path(sys.argv[3])
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    "case": "apply_last",
    "http_status": int(sys.argv[2]),
    "response_sha256": hashlib.sha256(response.read_bytes()).hexdigest(),
}, sort_keys=True, separators=(",", ":")) + "\n")
PY
}

resolve_canary_milestone() {
  local workspace=$1
  local dbc batch_id milestone_id
  dbc=$(db_cid)
  [ -n "$dbc" ] || fatal "db container is not running for canary milestone resolution"
  batch_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["batch_id"])' "$workspace/apply-state.json")
  milestone_id=$(docker exec "$dbc" psql -X -U spareparts -d spareparts -At -v ON_ERROR_STOP=1 -c \
    "SELECT milestone_id FROM maintenance_collection_milestone WHERE project_id='${CANARY_PROJECT_ID}' AND collection_plan_import_batch_id='${batch_id}' ORDER BY sequence, milestone_id LIMIT 1;")
  [ -n "$milestone_id" ] || fatal "canary apply did not create a resolvable milestone"
  python3 - "$workspace/milestone-state.json" "$milestone_id" <<'PY'
import json
import os
import pathlib
import re
import sys
path = pathlib.Path(sys.argv[1])
milestone_id = sys.argv[2]
if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,35}", milestone_id) is None:
    raise SystemExit("resolved canary milestone id is invalid")
path.write_text(json.dumps({"milestone_id": milestone_id}, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
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
import re
import sys
value=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if value.get("canary_project_id", sys.argv[2]) != sys.argv[2]:
    raise SystemExit("canary spec project mismatch")
accounts=value.get("named_accounts")
if not isinstance(accounts,dict) or len(accounts) < 2:
    raise SystemExit("canary spec needs named positive and denied accounts")
required=("action_verify_granted","action_verify_restored","setup_contract","follow_up_positive","cross_project_negative","permission_negative","import_preview_positive","apply_last")
for name in required:
    case=value.get(name)
    if not isinstance(case,dict): raise SystemExit("canary spec lacks case: "+name)
    if case.get("method") not in {"GET","POST"}: raise SystemExit("invalid canary method: "+name)
    if not isinstance(case.get("path"),str) or not case["path"].startswith("/"): raise SystemExit("invalid canary path: "+name)
    if not isinstance(case.get("expected_status"),int): raise SystemExit("invalid expected status: "+name)
username_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
seen_usernames = set()
for account_key, account in accounts.items():
    if not isinstance(account, dict):
        raise SystemExit("canary named account must be an object: "+str(account_key))
    username = account.get("username")
    if not isinstance(username, str) or username_pattern.fullmatch(username) is None:
        raise SystemExit("canary named account username is unsafe: "+str(account_key))
    if username in seen_usernames:
        raise SystemExit("canary named account username is duplicate")
    seen_usernames.add(username)
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
preview = value["import_preview_positive"]
preview_account_key = preview.get("account")
for name in ("import_preview_positive", "apply_last"):
    account = account_for(name)
    if account.get("expected_role") != "admin":
        raise SystemExit("collection-plan import positive account must be admin: "+name)
    if "action_maintenance_collection_plan_import" not in (account.get("required_permissions") or []):
        raise SystemExit("collection-plan import positive account lacks explicit import action: "+name)
setup_contract = value["setup_contract"]
setup_account = account_for("setup_contract")
if setup_account.get("expected_role") != "admin" or setup_contract.get("account") != preview_account_key:
    raise SystemExit("canary setup_contract must use the import admin account")
if setup_contract.get("method") != "POST" or setup_contract.get("path") != "/api/maintenance/projects/stable/" + sys.argv[2] + "/contracts" or setup_contract.get("expected_status") != 201:
    raise SystemExit("canary setup_contract must create a contract under the manifest canary project")
setup_body = setup_contract.get("body")
if not isinstance(setup_body, dict):
    raise SystemExit("canary setup_contract body is invalid")
if setup_body.get("included_in_total") is not False:
    raise SystemExit("canary setup_contract must not be included in business totals")
for key, expected in {
    "contract_id": "canary-contract-source",
    "contract_no": "CANARY-CONTRACT-001",
    "source": "release_canary",
}.items():
    if setup_body.get(key) != expected:
        raise SystemExit("canary setup_contract " + key + " is invalid")

denied_account = account_for("permission_negative")
if denied_account.get("expected_role") != "admin":
    raise SystemExit("permission-negative account must be admin to prove no admin bypass")
if "action_maintenance_collection_plan_import" not in (denied_account.get("forbidden_permissions") or []):
    raise SystemExit("permission-negative admin must explicitly lack import action")
if "action_maintenance_collection_follow_up" not in (denied_account.get("forbidden_permissions") or []):
    raise SystemExit("permission-negative admin must explicitly lack follow-up action")

for name in ("action_verify_granted", "action_verify_restored"):
    case = value[name]
    if case.get("method") != "GET" or case.get("path") != "/api/accounts" or case.get("expected_status") != 200:
        raise SystemExit(name + " must GET the real account list endpoint")
    if not isinstance(case.get("token"), str) or not case["token"]:
        raise SystemExit("action verification cases require sealed control token")

def validate_action_list(name):
    cases = value.get(name)
    if not isinstance(cases, list) or not cases:
        raise SystemExit(name + " must be a non-empty PUT case array")
    targets = []
    overrides_by_target = {}
    for case in cases:
        if not isinstance(case, dict):
            raise SystemExit(name + " entries must be objects")
        path = case.get("path")
        match = re.fullmatch(r"/api/accounts/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})", path or "")
        if case.get("method") != "PUT" or match is None or case.get("expected_status") != 200:
            raise SystemExit(name + " must use real single-account PUT endpoints")
        if not isinstance(case.get("token"), str) or not case["token"]:
            raise SystemExit(name + " entries require sealed control token")
        body = case.get("body")
        if not isinstance(body, dict) or set(body) != {"overrides"} or not isinstance(body["overrides"], dict):
            raise SystemExit(name + " bodies must contain only a complete overrides object")
        if not all(isinstance(key, str) and key and "\n" not in key and isinstance(enabled, bool)
                   for key, enabled in body["overrides"].items()):
            raise SystemExit(name + " overrides must be boolean permission entries")
        target = match.group(1)
        targets.append(target)
        overrides_by_target[target] = body["overrides"]
    if len(targets) != len(set(targets)):
        raise SystemExit(name + " target usernames must be unique")
    return targets, overrides_by_target

grant_targets, grant_overrides = validate_action_list("action_grant")
restore_targets, restore_overrides = validate_action_list("action_restore")
expected_targets = {
    account_for("import_preview_positive")["username"],
    account_for("follow_up_positive")["username"],
    account_for("permission_negative")["username"],
}
if len(expected_targets) != 3 or set(grant_targets) != expected_targets:
    raise SystemExit("action grant targets must exactly match the three named canary accounts")
if restore_targets != list(reversed(grant_targets)):
    raise SystemExit("action restore targets must reverse the grant order")
mutable_during_canary = {
    "page_maintenance",
    "page_maintenance_beta",
    "action_maintenance_collection_plan_import",
    "action_maintenance_collection_follow_up",
}
missing = object()
for target in grant_targets:
    original = restore_overrides[target]
    granted = grant_overrides[target]
    if not set(original) <= set(granted):
        raise SystemExit("action grant must not delete original override keys")
    for key in set(original) | set(granted):
        if key not in mutable_during_canary and original.get(key, missing) != granted.get(key, missing):
            raise SystemExit("action grant may not add or change unrelated overrides")

follow_path = re.compile(
    r"^/api/maintenance/collection-milestones/([A-Za-z0-9][A-Za-z0-9_-]{0,35}|\{canary_milestone_id\})/follow-ups$"
)
def validate_follow_up_case(name, *, successful):
    case = value[name]
    match = follow_path.fullmatch(case.get("path", ""))
    if case.get("method") != "POST" or match is None:
        raise SystemExit(name + " must call the real collection milestone follow-up endpoint")
    if successful and case.get("expected_status", 0) // 100 != 2:
        raise SystemExit(name + " must expect a successful response")
    body = case.get("body")
    allowed = {"expected_version", "idempotency_key", "action", "planned_month", "note", "reason"}
    if not isinstance(body, dict) or not {"expected_version", "idempotency_key", "action"} <= set(body) or not set(body) <= allowed:
        raise SystemExit(name + " must use the real follow-up request shape")
    if not isinstance(body["expected_version"], int) or body["expected_version"] < 1:
        raise SystemExit(name + " expected_version is invalid")
    key = body["idempotency_key"]
    if not isinstance(key, str) or not 8 <= len(key) <= 128 or "\n" in key:
        raise SystemExit(name + " idempotency_key is invalid")
    action = body["action"]
    if action == "handle":
        if body.get("planned_month") is not None or body.get("reason") is not None:
            raise SystemExit(name + " handle payload is invalid")
    elif action == "reschedule":
        if not isinstance(body.get("planned_month"), str) or not isinstance(body.get("reason"), str) or not body["reason"].strip() or body.get("note") is not None:
            raise SystemExit(name + " reschedule payload is invalid")
    elif action == "reopen":
        if not isinstance(body.get("reason"), str) or not body["reason"].strip() or body.get("planned_month") is not None or body.get("note") is not None:
            raise SystemExit(name + " reopen payload is invalid")
    else:
        raise SystemExit(name + " follow-up action is invalid")
    return match.group(1)

positive_milestone = validate_follow_up_case("follow_up_positive", successful=True)
permission_milestone = validate_follow_up_case("permission_negative", successful=False)
if permission_milestone != positive_milestone:
    raise SystemExit("permission negative must target the canary milestone")
cross_case = value["cross_project_negative"]
if cross_case.get("method") != "POST" or cross_case.get("path") != "/api/maintenance/collection-plan-imports/{batch_id}/apply" or cross_case.get("expected_status") != 403:
    raise SystemExit("cross-project negative must use the dynamic collection-plan apply endpoint")
if cross_case.get("account") != preview.get("account"):
    raise SystemExit("cross-project negative must use the import/apply account")
cross_body = cross_case.get("body")
if not isinstance(cross_body, dict) or cross_body.get("project_id") == sys.argv[2]:
    raise SystemExit("cross-project negative must declare a non-canary project binding")
for key in ("project_id", "project_contract_id"):
    if not isinstance(cross_body.get(key), str) or not cross_body[key]:
        raise SystemExit("cross-project negative binding is invalid")
for key in ("project_version", "project_contract_version"):
    if not isinstance(cross_body.get(key), int) or cross_body[key] < 1:
        raise SystemExit("cross-project negative binding version is invalid")
if preview.get("method") != "POST" or preview.get("path") != "/api/maintenance/collection-plan-imports/preview" or preview.get("expected_status") != 200:
    raise SystemExit("import preview canary must call the real multipart endpoint")
workbook = pathlib.Path(preview.get("workbook_path", ""))
if not workbook.is_absolute() or not workbook.is_file() or workbook.is_symlink() or (workbook.stat().st_mode & 0o777) != 0o600:
    raise SystemExit("canary workbook must be an absolute mode-600 regular file")
import hashlib
digest = hashlib.sha256(workbook.read_bytes()).hexdigest()
if preview.get("workbook_sha256") != digest:
    raise SystemExit("canary workbook SHA mismatch")
idempotency_key = preview.get("idempotency_key")
if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128 or "\n" in idempotency_key:
    raise SystemExit("canary preview idempotency key is invalid")
bindings = preview.get("bindings")
if not isinstance(preview.get("project_version"), int) or preview["project_version"] < 1:
    raise SystemExit("canary preview must declare current project_version for dynamic setup contract")
required_binding_keys = {"external_order_no","project_id","project_version","project_contract_id","project_contract_version","existing_binding_version","reason"}
if not isinstance(bindings, list) or not bindings:
    raise SystemExit("canary preview needs explicit bindings")
seen_orders = set()
for binding in bindings:
    if not isinstance(binding, dict) or set(binding) != required_binding_keys:
        raise SystemExit("canary binding keys mismatch")
    external = binding.get("external_order_no")
    if not isinstance(external, str) or not external or external in seen_orders:
        raise SystemExit("canary binding external order is invalid or duplicate")
    seen_orders.add(external)
    if binding.get("project_id") != sys.argv[2]:
        raise SystemExit("canary binding must target the manifest canary project")
    if not isinstance(binding.get("project_version"), int) or binding["project_version"] < 1:
        raise SystemExit("canary binding project version is invalid")
    if not isinstance(binding.get("project_contract_id"), str) or not binding["project_contract_id"]:
        raise SystemExit("canary binding contract is invalid")
    if binding["project_contract_id"] == "{setup_contract.project_contract_id}":
        if binding.get("project_contract_version") != "{setup_contract.version}":
            raise SystemExit("dynamic setup contract binding must use the dynamic version token")
    elif not isinstance(binding.get("project_contract_version"), int) or binding["project_contract_version"] < 1:
        raise SystemExit("canary binding contract version is invalid")
apply_case = value["apply_last"]
if apply_case.get("method") != "POST" or apply_case.get("path") != "/api/maintenance/collection-plan-imports/{batch_id}/apply" or apply_case.get("expected_status", 0) // 100 != 2:
    raise SystemExit("apply_last must use the dynamic collection-plan apply endpoint")
if apply_case.get("account") != preview.get("account"):
    raise SystemExit("preview and apply must use the same named batch owner")
if value["cross_project_negative"]["expected_status"] != 403 or value["permission_negative"]["expected_status"] != 403:
    raise SystemExit("negative canary cases must expect 403")
PY
}

validate_rollback_action_spec() {
  python3 - "$1" <<'PY'
import json
import pathlib
import re
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(value, dict):
    raise SystemExit("rollback action spec must be a JSON object")
base_url = value.get("base_url")
if (
    not isinstance(base_url, str)
    or not base_url.startswith(("http://", "https://"))
    or "\n" in base_url
    or not base_url.rstrip("/").startswith(("http://", "https://"))
):
    raise SystemExit("rollback action spec base_url is invalid")

for name in ("action_verify_granted", "action_verify_restored"):
    case = value.get(name)
    if not isinstance(case, dict) or set(case) != {
        "method", "path", "token", "expected_status",
    }:
        raise SystemExit(name + " must contain only the sealed account-list GET case")
    if (
        case.get("method") != "GET"
        or case.get("path") != "/api/accounts"
        or case.get("expected_status") != 200
        or not isinstance(case.get("token"), str)
        or not case["token"]
    ):
        raise SystemExit(name + " must GET the real account list with a control token")

username_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

def validate_action_list(name):
    cases = value.get(name)
    if not isinstance(cases, list) or len(cases) != 3:
        raise SystemExit(name + " must contain exactly three PUT cases")
    targets = []
    overrides_by_target = {}
    for case in cases:
        if not isinstance(case, dict) or set(case) != {
            "method", "path", "token", "expected_status", "body",
        }:
            raise SystemExit(name + " entries must contain only the sealed PUT case fields")
        path = case.get("path")
        match = re.fullmatch(
            r"/api/accounts/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})",
            path or "",
        )
        body = case.get("body")
        overrides = body.get("overrides") if isinstance(body, dict) else None
        if (
            case.get("method") != "PUT"
            or match is None
            or username_pattern.fullmatch(match.group(1)) is None
            or case.get("expected_status") != 200
            or not isinstance(case.get("token"), str)
            or not case["token"]
            or not isinstance(body, dict)
            or set(body) != {"overrides"}
            or not isinstance(overrides, dict)
            or not all(
                isinstance(key, str)
                and key
                and "\n" not in key
                and isinstance(enabled, bool)
                for key, enabled in overrides.items()
            )
        ):
            raise SystemExit(name + " contains an invalid account PUT case")
        target = match.group(1)
        targets.append(target)
        overrides_by_target[target] = overrides
    if len(set(targets)) != 3:
        raise SystemExit(name + " target usernames must be unique")
    return targets, overrides_by_target

grant_targets, grant_overrides = validate_action_list("action_grant")
restore_targets, restore_overrides = validate_action_list("action_restore")
if restore_targets != list(reversed(grant_targets)):
    raise SystemExit("action restore targets must reverse the grant order")
mutable_during_canary = {
    "page_maintenance",
    "page_maintenance_beta",
    "action_maintenance_collection_plan_import",
    "action_maintenance_collection_follow_up",
}
missing = object()
for target in grant_targets:
    original = restore_overrides[target]
    granted = grant_overrides[target]
    if not set(original) <= set(granted):
        raise SystemExit("action grant must not delete original override keys")
    for key in set(original) | set(granted):
        if key not in mutable_during_canary and original.get(key, missing) != granted.get(key, missing):
            raise SystemExit("action grant may not add or change unrelated overrides")
PY
}

action_plan_sha256() {
  python3 - "$1" "$2" <<'PY'
import hashlib
import json
import pathlib
import re
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
project_id = sys.argv[2]

def project_verify(case):
    return {
        "method": case["method"],
        "path": case["path"],
        "expected_status": case["expected_status"],
    }

def project_update(case):
    match = re.fullmatch(
        r"/api/accounts/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})",
        case["path"],
    )
    if match is None:
        raise SystemExit("action plan account path is invalid")
    return {
        "method": case["method"],
        "path": case["path"],
        "target_username": match.group(1),
        "expected_status": case["expected_status"],
        "overrides": case["body"]["overrides"],
    }

plan = {
    "format": "v122-rollback-action-plan-v1",
    "base_url": value["base_url"].rstrip("/"),
    "canary_project_id": project_id,
    "action_grant": [project_update(case) for case in value["action_grant"]],
    "action_restore": [project_update(case) for case in value["action_restore"]],
    "action_verify_granted": project_verify(value["action_verify_granted"]),
    "action_verify_restored": project_verify(value["action_verify_restored"]),
}
canonical = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
print(hashlib.sha256(canonical).hexdigest())
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
    key = extra[index]
    if raw == "true":
        payload[key] = True
    elif raw == "false":
        payload[key] = False
    elif key in {"approved_missing_count", "unexpected_missing_count"}:
        payload[key] = int(raw)
    else:
        payload[key] = raw
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

# Command shape and phase gates run before Docker/env writes.  Rollback action
# spec reads are intentionally deferred until the running app is proven closed.
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
  restore-check)
    { [ "$#" -eq 2 ] || [ "$#" -eq 3 ]; } || usage
    require_phase backup
    ;;
  migrate) [ "$#" -eq 0 ] || usage; require_production_ready; require_phase restore_checked ;;
  deploy) [ "$#" -eq 0 ] || usage; require_production_ready; require_phase migrated ;;
  canary)
    [ "$#" -eq 2 ] || usage
    require_production_ready
    CANARY_PROJECT_ID=$1
    CANARY_SPEC_INPUT=$(realpath -e -- "$2")
    [ "$CANARY_PROJECT_ID" = "$(manifest_get runtime_flags.maintenance_collection_canary_project_id)" ] \
      || fatal "canary project id does not match manifest"
    [ -f "$CANARY_SPEC_INPUT" ] && [ ! -L "$CANARY_SPEC_INPUT" ] \
      && [ "$(stat -c '%a' "$CANARY_SPEC_INPUT")" = 600 ] \
      || fatal "canary spec must be a mode-600 regular file"
    validate_canary_spec "$CANARY_SPEC_INPUT" "$CANARY_PROJECT_ID"
    CANARY_SPEC_PREHASH=$(sha256sum "$CANARY_SPEC_INPUT" | awk '{print $1}') \
      || fatal "canary spec SHA-256 could not be read"
    [[ "$CANARY_SPEC_PREHASH" =~ ^[0-9a-f]{64}$ ]] \
      || fatal "canary spec SHA-256 is invalid"
    require_phase deployed
    CANARY_WORK=$(mktemp -d -t v122-canary.XXXXXXXX) \
      || fatal "canary private workspace could not be created"
    chmod 700 "$CANARY_WORK" || {
      rm -rf -- "$CANARY_WORK"
      fatal "canary private workspace could not be secured"
    }
    CANARY_SPEC=$(snapshot_sealed_canary_spec "$CANARY_SPEC_INPUT" "$CANARY_WORK") || {
      rm -rf -- "$CANARY_WORK"
      fatal "canary spec snapshot could not be created"
    }
    CANARY_SPEC_SHA256=$(sha256sum "$CANARY_SPEC" | awk '{print $1}') || {
      rm -rf -- "$CANARY_WORK"
      fatal "canary spec snapshot SHA-256 could not be read"
    }
    if [[ ! "$CANARY_SPEC_SHA256" =~ ^[0-9a-f]{64}$ ]] \
      || [ "$CANARY_SPEC_SHA256" != "$CANARY_SPEC_PREHASH" ]; then
      rm -rf -- "$CANARY_WORK"
      fatal "canary spec snapshot does not match the validated input"
    fi
    if ! validate_canary_spec "$CANARY_SPEC" "$CANARY_PROJECT_ID"; then
      rm -rf -- "$CANARY_WORK"
      fatal "canary spec snapshot validation failed"
    fi
    CANARY_ACTION_PLAN_SHA256=$(action_plan_sha256 \
      "$CANARY_SPEC" "$CANARY_PROJECT_ID") || {
      rm -rf -- "$CANARY_WORK"
      fatal "canary action plan SHA-256 could not be calculated"
    }
    if [[ ! "$CANARY_ACTION_PLAN_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
      rm -rf -- "$CANARY_WORK"
      fatal "canary action plan SHA-256 is invalid"
    fi
    ;;
  observe) require_production_ready; [ "$#" -eq 1 ] || usage; case "$1" in 0|5|15|30) ;; *) fatal "observe point must be 0, 5, 15, or 30";; esac ;;
  rollback-images)
    [ "$#" -le 1 ] || usage
    require_phase_in preflight frozen backup restore_checked migrated deployed canary observe_0 observe_5 observe_15 observed
    STATE_ACTIONS_GRANTED=$(python3 -c 'import json,sys; print("true" if json.load(open(sys.argv[1])).get("actions_granted") else "false")' "$STATE_FILE") \
      || fatal "rollback action state is unreadable"
    if [ "$STATE_ACTIONS_GRANTED" = true ]; then
      [ "$#" -eq 1 ] || fatal "mode-600 canary spec is required to restore action permissions"
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
	    IMPORT_TABLE_EXISTS=$(docker exec "$DBC" psql -X -U spareparts -d spareparts -At \
	      -v ON_ERROR_STOP=1 -c \
	      "SELECT to_regclass('public.maintenance_collection_plan_import_batch') IS NOT NULL;") \
	      || freeze_abort "could not check collection import processing"
	    case "$IMPORT_TABLE_EXISTS" in
	      f) PROCESSING=0 ;;
	      t)
	        PROCESSING=$(docker exec "$DBC" psql -X -U spareparts -d spareparts -At \
	          -v ON_ERROR_STOP=1 -c \
	          "SELECT count(*) FROM maintenance_collection_plan_import_batch WHERE status = 'processing';") \
	          || freeze_abort "could not check collection import processing"
	        [[ "$PROCESSING" =~ ^[0-9]+$ ]] \
	          || freeze_abort "could not check collection import processing"
	        ;;
	      *) freeze_abort "could not check collection import processing" ;;
	    esac
	    [ "$PROCESSING" -eq 0 ] || freeze_abort "collection import batches are still processing"
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
    HISTORICAL_GAP_APPROVAL=
    HISTORICAL_GAP_WORK=
    HISTORICAL_GAP_SUMMARY=
    if [ "$#" -eq 3 ]; then
      [ -f "$3" ] && [ ! -L "$3" ] \
        || fatal "historical upload gap approval must be a regular non-symlink file"
      HISTORICAL_GAP_INPUT=$(realpath -e -- "$3") \
        || fatal "historical upload gap approval is missing"
      [ "$(stat -c '%a' "$HISTORICAL_GAP_INPUT")" = 600 ] \
        || fatal "historical upload gap approval must be mode 600"
      python3 "$MANIFEST_TOOL" validate-historical-upload-gap \
        "$HISTORICAL_GAP_INPUT" \
        --parent-production-sha "$(manifest_get parent_production_sha)" >/dev/null \
        || fatal "historical upload gap approval validation failed"
      HISTORICAL_GAP_PREHASH=$(sha256sum "$HISTORICAL_GAP_INPUT" | awk '{print $1}') \
        || fatal "historical upload gap approval SHA-256 could not be read"
      [[ "$HISTORICAL_GAP_PREHASH" =~ ^[0-9a-f]{64}$ ]] \
        || fatal "historical upload gap approval SHA-256 is invalid"
      HISTORICAL_GAP_WORK=$(mktemp -d -t v122-historical-gap.XXXXXXXX) \
        || fatal "historical upload gap private workspace could not be created"
      chmod 700 "$HISTORICAL_GAP_WORK" || {
        rm -rf -- "$HISTORICAL_GAP_WORK"
        fatal "historical upload gap private workspace could not be secured"
      }
      cleanup_historical_gap_work() {
        rm -rf -- "$HISTORICAL_GAP_WORK"
      }
      trap cleanup_historical_gap_work EXIT
      HISTORICAL_GAP_APPROVAL=$(snapshot_historical_gap_approval \
        "$HISTORICAL_GAP_INPUT" "$HISTORICAL_GAP_WORK") \
        || fatal "historical upload gap approval snapshot could not be created"
      HISTORICAL_GAP_SNAPSHOT_SHA=$(sha256sum "$HISTORICAL_GAP_APPROVAL" | awk '{print $1}') \
        || fatal "historical upload gap approval snapshot SHA-256 could not be read"
      [ "$HISTORICAL_GAP_SNAPSHOT_SHA" = "$HISTORICAL_GAP_PREHASH" ] \
        || fatal "historical upload gap approval changed during snapshot"
      HISTORICAL_GAP_SUMMARY="$HISTORICAL_GAP_WORK/validated-summary.json"
      python3 "$MANIFEST_TOOL" validate-historical-upload-gap \
        "$HISTORICAL_GAP_APPROVAL" \
        --parent-production-sha "$(manifest_get parent_production_sha)" \
        >"$HISTORICAL_GAP_SUMMARY" \
        || fatal "historical upload gap approval snapshot validation failed"
      chmod 600 "$HISTORICAL_GAP_SUMMARY"
    fi
    PRE_REHEARSAL_REFERENCE_BINDING=$(python3 - "${HISTORICAL_GAP_SUMMARY:-}" <<'PY'
import hashlib
import json
import pathlib
import re
import sys

summary = (
    json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    if sys.argv[1]
    else None
)
empty_set_sha = hashlib.sha256(b"[]\n").hexdigest()
if summary is None:
    expected = {
        "db_uploads_reference_state": "complete",
        "db_uploads_references_complete": True,
        "approved_missing_count": 0,
        "unexpected_missing_count": 0,
        "historical_upload_gap_set_sha256": empty_set_sha,
        "historical_upload_gap_approval_sha256": None,
        "recovery_search_evidence_sha256": None,
    }
else:
    expected = {
        "db_uploads_reference_state": "complete_with_approved_historical_gaps",
        "db_uploads_references_complete": False,
        "approved_missing_count": summary["approved_missing_count"],
        "unexpected_missing_count": 0,
        "historical_upload_gap_set_sha256": summary["historical_upload_gap_set_sha256"],
        "historical_upload_gap_approval_sha256": summary["historical_upload_gap_approval_sha256"],
        "recovery_search_evidence_sha256": summary["recovery_search_evidence_sha256"],
    }
if re.fullmatch(r"[0-9a-f]{64}", str(expected["historical_upload_gap_set_sha256"])) is None:
    raise SystemExit("restore-check historical upload gap SHA is invalid")
for key in (
    "db_uploads_reference_state",
    "approved_missing_count",
    "unexpected_missing_count",
    "historical_upload_gap_set_sha256",
    "historical_upload_gap_approval_sha256",
    "recovery_search_evidence_sha256",
):
    value = expected[key]
    print("null" if value is None else value)
print("true" if expected["db_uploads_references_complete"] else "false")
PY
    ) || fatal "restore-check historical upload gap reference binding is invalid"
    if [ "$PACKAGE_PRODUCTION_READY" = true ]; then
      PACKAGED_FINAL_REHEARSAL="$PACKAGE_DIR/final-rehearsal.json"
      safe_file "$PACKAGED_FINAL_REHEARSAL" "packaged final rehearsal evidence"
      PACKAGED_REFERENCE_BINDING=$(python3 - "$PACKAGED_FINAL_REHEARSAL" <<'PY'
import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
for key in (
    "db_uploads_reference_state",
    "approved_missing_count",
    "unexpected_missing_count",
    "historical_upload_gap_set_sha256",
    "historical_upload_gap_approval_sha256",
    "recovery_search_evidence_sha256",
):
    item = value[key]
    print("null" if item is None else item)
print("true" if value["db_uploads_references_complete"] else "false")
PY
      ) || fatal "packaged final rehearsal reference binding is unreadable"
      [ "$PRE_REHEARSAL_REFERENCE_BINDING" = "$PACKAGED_REFERENCE_BINDING" ] \
        || fatal "runtime restore-check reference binding differs from packaged final rehearsal"
    fi
    if [ "$PACKAGE_PRODUCTION_READY" = true ]; then REHEARSAL_STAGE=final; else REHEARSAL_STAGE=preliminary; fi
    REHEARSAL_ARGUMENTS=(
      "$DB_DUMP" "$UPLOADS_ARCHIVE" \
      "$(manifest_get target_sha)" "$(manifest_get parent_production_sha)" \
      "$(manifest_get database.image_id)" "$(manifest_get images.app_image_id)" \
      "$(manifest_get images.frontend_image_id)" "$PACKAGE_DIR/candidate-compose.yml" \
      "$EVIDENCE_DIR/restore-check"
    )
    if [ -n "$HISTORICAL_GAP_APPROVAL" ]; then
      REHEARSAL_ARGUMENTS+=("$HISTORICAL_GAP_APPROVAL")
    fi
    V122_REHEARSAL_STAGE="$REHEARSAL_STAGE" \
    V122_EXPECTED_MANIFEST_SHA256="$EXPECTED_MANIFEST_SHA256" \
    "$SCRIPT_DIR/v122_collection_reminders_rehearse.sh" "${REHEARSAL_ARGUMENTS[@]}"
    REHEARSAL_EVIDENCE="$EVIDENCE_DIR/restore-check/rehearsal-evidence.json"
    safe_file "$REHEARSAL_EVIDENCE" "restore-check rehearsal evidence"
    REFERENCE_BINDING=$(python3 - "$REHEARSAL_EVIDENCE" \
      "${HISTORICAL_GAP_SUMMARY:-}" <<'PY'
import hashlib
import json
import pathlib
import re
import sys

evidence = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
summary = (
    json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
    if sys.argv[2]
    else None
)
empty_set_sha = hashlib.sha256(b"[]\n").hexdigest()
if summary is None:
    expected = {
        "db_uploads_reference_state": "complete",
        "db_uploads_references_complete": True,
        "approved_missing_count": 0,
        "unexpected_missing_count": 0,
        "historical_upload_gap_set_sha256": empty_set_sha,
        "historical_upload_gap_approval_sha256": None,
        "recovery_search_evidence_sha256": None,
    }
else:
    expected = {
        "db_uploads_reference_state": "complete_with_approved_historical_gaps",
        "db_uploads_references_complete": False,
        "approved_missing_count": summary["approved_missing_count"],
        "unexpected_missing_count": 0,
        "historical_upload_gap_set_sha256": summary["historical_upload_gap_set_sha256"],
        "historical_upload_gap_approval_sha256": summary["historical_upload_gap_approval_sha256"],
        "recovery_search_evidence_sha256": summary["recovery_search_evidence_sha256"],
    }
for key, value in expected.items():
    if evidence.get(key) != value:
        raise SystemExit("restore-check rehearsal historical upload gap binding mismatch: " + key)
for key in ("historical_upload_gap_set_sha256",):
    if re.fullmatch(r"[0-9a-f]{64}", str(expected[key])) is None:
        raise SystemExit("restore-check rehearsal historical upload gap SHA is invalid")
for key in (
    "db_uploads_reference_state",
    "approved_missing_count",
    "unexpected_missing_count",
    "historical_upload_gap_set_sha256",
    "historical_upload_gap_approval_sha256",
    "recovery_search_evidence_sha256",
):
    value = expected[key]
    print("null" if value is None else value)
print("true" if expected["db_uploads_references_complete"] else "false")
PY
    ) || fatal "restore-check rehearsal historical upload gap evidence is invalid"
    mapfile -t REFERENCE_VALUES <<<"$REFERENCE_BINDING"
    [ "${#REFERENCE_VALUES[@]}" -eq 7 ] \
      || fatal "restore-check rehearsal historical upload gap binding is incomplete"
    [ "$REFERENCE_BINDING" = "$PRE_REHEARSAL_REFERENCE_BINDING" ] \
      || fatal "restore-check rehearsal reference binding differs from preflight binding"
    REHEARSAL_EVIDENCE_SHA256=$(sha256sum "$REHEARSAL_EVIDENCE" | awk '{print $1}')
    if [ -n "$HISTORICAL_GAP_WORK" ]; then
      cleanup_historical_gap_work
      HISTORICAL_GAP_WORK=
      trap - EXIT
    fi
    if [ "${REFERENCE_VALUES[0]}" = complete ]; then
      advance_phase restore_checked \
        db_uploads_reference_state "${REFERENCE_VALUES[0]}" \
        db_uploads_references_complete "${REFERENCE_VALUES[6]}" \
        approved_missing_count "${REFERENCE_VALUES[1]}" \
        unexpected_missing_count "${REFERENCE_VALUES[2]}" \
        historical_upload_gap_set_sha256 "${REFERENCE_VALUES[3]}" \
        restore_check_rehearsal_evidence_sha256 "$REHEARSAL_EVIDENCE_SHA256"
    else
      advance_phase restore_checked \
        db_uploads_reference_state "${REFERENCE_VALUES[0]}" \
        db_uploads_references_complete "${REFERENCE_VALUES[6]}" \
        approved_missing_count "${REFERENCE_VALUES[1]}" \
        unexpected_missing_count "${REFERENCE_VALUES[2]}" \
        historical_upload_gap_set_sha256 "${REFERENCE_VALUES[3]}" \
        historical_upload_gap_approval_sha256 "${REFERENCE_VALUES[4]}" \
        recovery_search_evidence_sha256 "${REFERENCE_VALUES[5]}" \
        restore_check_rehearsal_evidence_sha256 "$REHEARSAL_EVIDENCE_SHA256"
    fi
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
    cleanup_canary_work() { rm -rf -- "$CANARY_WORK"; }
    trap cleanup_canary_work EXIT
    update_env_key MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED false
    update_env_key MAINTENANCE_COLLECTION_CANARY_PROJECT_ID "$CANARY_PROJECT_ID"
    compose up --no-deps --no-build --force-recreate -d app
    # Read back from the actual container rather than trusting the staged .env.
    RUNNING_FLAGS=$(compose exec -T app sh -ceu 'printf "%s\n%s\n" "$MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED" "$MAINTENANCE_COLLECTION_CANARY_PROJECT_ID"')
    [ "$RUNNING_FLAGS" = "false
$CANARY_PROJECT_ID" ] || fatal "running canary configuration readback mismatch"
    APPLY_OPEN=false
    ACTIONS_GRANTED=false
    emergency_close_canary() {
      local original_status=${1:-$?}
      local cleanup_failed=false
      local running_flags=''
      trap - ERR HUP INT TERM
      update_env_key MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED false \
        || cleanup_failed=true
      compose up --no-deps --no-build --force-recreate -d app >/dev/null 2>&1 \
        || cleanup_failed=true
      running_flags=$(compose exec -T app sh -ceu \
        'printf "%s\n%s\n" "$MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED" "$MAINTENANCE_COLLECTION_CANARY_PROJECT_ID"') \
        || cleanup_failed=true
      if [ "$running_flags" != "false
$CANARY_PROJECT_ID" ]; then
        printf 'running canary cleanup configuration readback mismatch\n' >&2
        cleanup_failed=true
      fi
      if [ "$ACTIONS_GRANTED" = true ]; then
        run_sealed_action_cases "$CANARY_SPEC" action_restore "$CANARY_WORK" \
          || cleanup_failed=true
        verify_action_account_state "$CANARY_SPEC" action_verify_restored \
          action_restore "$CANARY_WORK" || cleanup_failed=true
      fi
      if [ "$original_status" -ne 0 ]; then
        return "$original_status"
      fi
      [ "$cleanup_failed" = false ]
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
    verify_action_account_state "$CANARY_SPEC" action_verify_restored \
      action_restore "$CANARY_WORK"
    ACTIONS_GRANTED=true
    run_sealed_action_cases "$CANARY_SPEC" action_grant "$CANARY_WORK"
    verify_action_account_state "$CANARY_SPEC" action_verify_granted \
      action_grant "$CANARY_WORK"
    login_named_canary_accounts "$CANARY_SPEC" "$CANARY_WORK"
    # The request sequence is deliberately fixed for first deployment: create a
    # real canary contract, preview the real workbook with writes closed, prove
    # canary-scope denial with apply open, apply the canary project only, then
    # follow up the milestone that was just created.
    run_canary_setup_contract "$CANARY_SPEC" "$CANARY_WORK"
    run_canary_import_preview "$CANARY_SPEC" "$CANARY_WORK"
    update_env_key MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED true
    compose up --no-deps --no-build --force-recreate -d app
    RUNNING_FLAGS=$(compose exec -T app sh -ceu 'printf "%s\n%s\n" "$MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED" "$MAINTENANCE_COLLECTION_CANARY_PROJECT_ID"')
    [ "$RUNNING_FLAGS" = "true
$CANARY_PROJECT_ID" ] || fatal "running apply canary configuration readback mismatch"
    DOMAIN_BEFORE=$(collection_domain_fingerprint)
    run_canary_cross_project_negative "$CANARY_SPEC" "$CANARY_WORK"
    [ "$(collection_domain_fingerprint)" = "$DOMAIN_BEFORE" ] \
      || fatal "cross-project negative canary changed collection domain state"
    if ! (run_canary_apply_last "$CANARY_SPEC" "$CANARY_WORK"); then
      fatal "apply canary failed; collection apply flag was closed"
    fi
    resolve_canary_milestone "$CANARY_WORK"
    run_sealed_canary_case "$CANARY_SPEC" follow_up_positive "$CANARY_WORK"
    DOMAIN_BEFORE=$(collection_domain_fingerprint)
    run_sealed_canary_case "$CANARY_SPEC" permission_negative "$CANARY_WORK"
    [ "$(collection_domain_fingerprint)" = "$DOMAIN_BEFORE" ] \
      || fatal "permission-negative canary changed collection domain state"
    FINAL_CANARY_SPEC_SHA256=$(sha256sum "$CANARY_SPEC" | awk '{print $1}') \
      || fatal "sealed canary spec snapshot SHA-256 could not be read"
    [ "$FINAL_CANARY_SPEC_SHA256" = "$CANARY_SPEC_SHA256" ] \
      || fatal "sealed canary spec snapshot changed during execution"
    python3 - "$CANARY_WORK" "$EVIDENCE_DIR/canary-evidence.json" \
      "$CANARY_SPEC_SHA256" "$CANARY_ACTION_PLAN_SHA256" <<'PY'
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
    "canary_spec_sha256": sys.argv[3],
    "action_plan_sha256": sys.argv[4],
    "named_account_readback_sha256": hashlib.sha256(account.read_bytes()).hexdigest(),
    "contains_secrets": False,
}
pathlib.Path(sys.argv[2]).write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
PY
	    advance_phase canary \
	      canary_project_id "$CANARY_PROJECT_ID" \
	      apply_enabled true actions_granted true \
	      canary_spec_sha256 "$CANARY_SPEC_SHA256" \
	      action_plan_sha256 "$CANARY_ACTION_PLAN_SHA256" \
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
    if [ "${STATE_ACTIONS_GRANTED:-false}" = true ]; then
      cleanup_rollback_work() {
        [ -z "${ROLLBACK_WORK:-}" ] || rm -rf -- "$ROLLBACK_WORK"
      }
      trap cleanup_rollback_work EXIT
    fi
    close_collection_writes
    compose up --no-deps --no-build --force-recreate -d app \
      || fatal "rollback could not restart the app with collection writes closed"
    ROLLBACK_CANARY_PROJECT_ID=$(manifest_get \
      runtime_flags.maintenance_collection_canary_project_id) \
      || fatal "rollback canary project id could not be read"
    ROLLBACK_RUNNING_FLAGS=$(compose exec -T app sh -ceu \
      'printf "%s\n%s\n" "$MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED" "$MAINTENANCE_COLLECTION_CANARY_PROJECT_ID"') \
      || fatal "rollback running configuration readback failed"
    [ "$ROLLBACK_RUNNING_FLAGS" = "false
$ROLLBACK_CANARY_PROJECT_ID" ] \
      || fatal "rollback running configuration readback mismatch"
    if [ "${STATE_ACTIONS_GRANTED:-false}" = true ]; then
      ROLLBACK_SPEC_INPUT=$(realpath -e -- "$1") \
        || fatal "rollback action spec could not be resolved"
      [ -f "$ROLLBACK_SPEC_INPUT" ] && [ ! -L "$ROLLBACK_SPEC_INPUT" ] \
        && [ "$(stat -c '%a' "$ROLLBACK_SPEC_INPUT")" = 600 ] \
        || fatal "rollback action spec must be a mode-600 regular file"
      if ! validate_rollback_action_spec "$ROLLBACK_SPEC_INPUT"; then
        fatal "rollback action spec validation failed"
      fi
      ROLLBACK_SPEC_PREHASH=$(sha256sum "$ROLLBACK_SPEC_INPUT" | awk '{print $1}') \
        || fatal "rollback action spec SHA-256 could not be read"
      [[ "$ROLLBACK_SPEC_PREHASH" =~ ^[0-9a-f]{64}$ ]] \
        || fatal "rollback action spec SHA-256 is invalid"
      STATE_ACTION_PLAN_SHA256=$(state_get action_plan_sha256) \
        || fatal "rollback state lacks the sealed action plan SHA-256"
      [[ "$STATE_ACTION_PLAN_SHA256" =~ ^[0-9a-f]{64}$ ]] \
        || fatal "rollback state has an invalid action plan SHA-256"
      ROLLBACK_WORK=$(mktemp -d -t v122-rollback.XXXXXXXX) \
        || fatal "rollback private workspace could not be created"
      chmod 700 "$ROLLBACK_WORK" \
        || fatal "rollback private workspace could not be secured"
      ROLLBACK_SPEC=$(snapshot_sealed_canary_spec \
        "$ROLLBACK_SPEC_INPUT" "$ROLLBACK_WORK") \
        || fatal "rollback action spec snapshot could not be created"
      ROLLBACK_SPEC_SHA256=$(sha256sum "$ROLLBACK_SPEC" | awk '{print $1}') \
        || fatal "rollback action spec snapshot SHA-256 could not be read"
      if [[ ! "$ROLLBACK_SPEC_SHA256" =~ ^[0-9a-f]{64}$ ]] \
        || [ "$ROLLBACK_SPEC_SHA256" != "$ROLLBACK_SPEC_PREHASH" ]; then
        fatal "rollback action spec snapshot does not match the validated input"
      fi
      if ! validate_rollback_action_spec "$ROLLBACK_SPEC"; then
        fatal "rollback action spec snapshot validation failed"
      fi
      ROLLBACK_ACTION_PLAN_SHA256=$(action_plan_sha256 \
        "$ROLLBACK_SPEC" "$ROLLBACK_CANARY_PROJECT_ID") \
        || fatal "rollback action plan SHA-256 could not be calculated"
      [[ "$ROLLBACK_ACTION_PLAN_SHA256" =~ ^[0-9a-f]{64}$ ]] \
        || fatal "rollback action plan SHA-256 is invalid"
      [ "$ROLLBACK_ACTION_PLAN_SHA256" = "$STATE_ACTION_PLAN_SHA256" ] \
        || fatal "rollback action plan does not match the successful canary"
      CURRENT_ROLLBACK_SPEC_SHA256=$(sha256sum "$ROLLBACK_SPEC" | awk '{print $1}') \
        || fatal "rollback action spec snapshot SHA-256 could not be read"
      [ "$CURRENT_ROLLBACK_SPEC_SHA256" = "$ROLLBACK_SPEC_SHA256" ] \
        || fatal "rollback action spec snapshot changed before execution"
      if ! verify_action_account_state "$ROLLBACK_SPEC" action_verify_granted \
        action_grant "$ROLLBACK_WORK"; then
        fatal "live action permissions differ from the successful canary; images were not changed"
      fi
      RESTORE_OK=true
      run_sealed_action_cases "$ROLLBACK_SPEC" action_restore "$ROLLBACK_WORK" \
        || RESTORE_OK=false
      verify_action_account_state "$ROLLBACK_SPEC" action_verify_restored \
        action_restore "$ROLLBACK_WORK" || RESTORE_OK=false
      if [ "$RESTORE_OK" != true ]; then
        fatal "action permission restore failed; images were not changed"
      fi
      if ! python3 - "$ROLLBACK_WORK" "$EVIDENCE_DIR/action-restore-evidence.json" <<'PY'
import json
import pathlib
import sys
work = pathlib.Path(sys.argv[1])
cases = [json.loads(path.read_text()) for path in sorted(work.glob("*.outcome.json"))]
pathlib.Path(sys.argv[2]).write_text(json.dumps({"format":"v122-action-restore-evidence-v1","outcomes":cases,"contains_secrets":False}, sort_keys=True, separators=(",", ":")) + "\n")
PY
      then
        fatal "action permission restore evidence write failed; images were not changed"
      fi
      ACTION_RESTORE_EVIDENCE_SHA256=$(sha256sum \
        "$EVIDENCE_DIR/action-restore-evidence.json" | awk '{print $1}') \
        || fatal "action permission restore evidence SHA-256 could not be read"
      [[ "$ACTION_RESTORE_EVIDENCE_SHA256" =~ ^[0-9a-f]{64}$ ]] \
        || fatal "action permission restore evidence SHA-256 is invalid"
      CURRENT_ROLLBACK_SPEC_SHA256=$(sha256sum "$ROLLBACK_SPEC" | awk '{print $1}') \
        || fatal "rollback action spec snapshot SHA-256 could not be read"
      [ "$CURRENT_ROLLBACK_SPEC_SHA256" = "$ROLLBACK_SPEC_SHA256" ] \
        || fatal "rollback action spec snapshot changed during execution"
      cleanup_rollback_work
      ROLLBACK_WORK=''
      trap - EXIT
    fi
    printf 'rollback-images requested; additive schema is retained; no automatic downgrade; restore DB/uploads only after incident approval\n'
    retag_and_start_exact_images \
      "$(manifest_get previous_images.app_image_id)" \
      "$(manifest_get previous_images.frontend_image_id)"
    if [ "${STATE_ACTIONS_GRANTED:-false}" = true ]; then
      CURRENT_ACTION_RESTORE_EVIDENCE_SHA256=$(sha256sum \
        "$EVIDENCE_DIR/action-restore-evidence.json" | awk '{print $1}') \
        || fatal "action permission restore evidence SHA-256 could not be read"
      [ "$CURRENT_ACTION_RESTORE_EVIDENCE_SHA256" = "$ACTION_RESTORE_EVIDENCE_SHA256" ] \
        || fatal "action permission restore evidence changed before state binding"
      advance_phase rolled_back \
        actions_granted false apply_enabled false \
        action_restore_evidence_sha256 "$ACTION_RESTORE_EVIDENCE_SHA256" \
        rollback_note 'images-actions-and-flags-only; no downgrade/delete/automatic restore'
    else
      advance_phase rolled_back \
        actions_granted false apply_enabled false \
        rollback_note 'images-actions-and-flags-only; no downgrade/delete/automatic restore'
    fi
    ;;
  *)
    usage
    ;;
esac
