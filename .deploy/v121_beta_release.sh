#!/usr/bin/env bash
# v1.21 Maintenance/Replenishment Beta production control. Fail closed.
set -Eeuo pipefail
umask 077

readonly EXPECTED_FROM=f1c8e4a7b2d9
readonly EXPECTED_TO=d9f1a3c7e5b2
readonly DEFAULT_APP_DIR=/home/ubuntu/apps/it-spareparts
readonly LOCK_PATH=/run/lock/it-spareparts-v121-beta.lock
readonly HOST_NAME=hbzgc.icu
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly SCRIPT_DIR
readonly MANIFEST_TOOL="$SCRIPT_DIR/v121_beta_manifest.py"

fatal() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat >&2 <<'EOF'
usage: v121_beta_release.sh PACKAGE_DIR EVIDENCE_DIR COMMAND [ARGS]

commands:
  preflight IMAGE_BUNDLE
  backup-restore
  migrate
  deploy STABLE_CREDENTIAL_JSON DENIED_CREDENTIAL_JSON
  open-empty-beta DENIED_CREDENTIAL_JSON
  pilot-smoke DENIED_CREDENTIAL_JSON ALLOWED_CREDENTIAL_JSON
  observe 0|5|15|30 STABLE_CREDENTIAL_JSON DENIED_CREDENTIAL_JSON ALLOWED_CREDENTIAL_JSON
  contain
  rollback-app

V121_APP_DIR may override /home/ubuntu/apps/it-spareparts with an absolute path.
Credential files are root-readable mode 600 JSON: {"username":"...","password":"..."}.
EOF
  exit 64
}

[ "$#" -ge 3 ] || usage
[ "$EUID" -eq 0 ] || fatal "release control must run as root"
PACKAGE_DIR=$(realpath -e -- "$1")
EVIDENCE_DIR=$(realpath -m -- "$2")
COMMAND=$3
shift 3
APP_DIR=${V121_APP_DIR:-$DEFAULT_APP_DIR}
[[ "$APP_DIR" == /* && "$APP_DIR" != / && "$APP_DIR" != *'/../'* ]] \
  || fatal "V121_APP_DIR must be a narrow absolute path"
readonly PACKAGE_DIR EVIDENCE_DIR COMMAND APP_DIR
readonly MANIFEST="$PACKAGE_DIR/manifest.json"
readonly STATE="$EVIDENCE_DIR/release-state.json"
readonly ACTIVE_COMPOSE="$APP_DIR/docker-compose.yml"
readonly ENV_FILE="$APP_DIR/.env"

[ -f "$MANIFEST_TOOL" ] && [ ! -L "$MANIFEST_TOOL" ] \
  || fatal "manifest verifier is missing or unsafe"
python3 "$MANIFEST_TOOL" verify "$PACKAGE_DIR" >/dev/null \
  || fatal "release package verification failed"

exec 9>"$LOCK_PATH"
flock -n 9 || fatal "another v1.21 Beta release operation is running"

manifest_get() {
  local dotted=$1
  python3 - "$MANIFEST" "$dotted" <<'PY'
import json
import sys

value = json.load(open(sys.argv[1], encoding="utf-8"))
for key in sys.argv[2].split("."):
    if isinstance(value, list):
        value = value[int(key)]
    else:
        value = value[key]
if isinstance(value, bool):
    print("true" if value else "false")
elif value is None:
    print("null")
elif isinstance(value, (dict, list)):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
else:
    print(value)
PY
}

sha256_file() {
  sha256sum -- "$1" | awk '{print $1}'
}

MANIFEST_SHA=$(sha256_file "$MANIFEST")
TARGET_SHA=$(manifest_get target_sha)
PARENT_SHA=$(manifest_get parent_production_sha)
CURRENT_COMPOSE_SHA=$(manifest_get compose.current_sha256)
CANDIDATE_COMPOSE_SHA=$(manifest_get compose.candidate_sha256)
CANDIDATE_COMPOSE="$PACKAGE_DIR/$(manifest_get compose.candidate_path)"
DB_IMAGE_ID=$(manifest_get database.image_id)
OLD_APP_IMAGE_ID=$(manifest_get images.old_app_id)
OLD_FRONTEND_IMAGE_ID=$(manifest_get images.old_frontend_id)
NEW_APP_IMAGE_ID=$(manifest_get images.new_app_id)
NEW_FRONTEND_IMAGE_ID=$(manifest_get images.new_frontend_id)
APP_IMAGE_REF=$(manifest_get images.app_ref)
FRONTEND_IMAGE_REF=$(manifest_get images.frontend_ref)
ROLLBACK_MODE=$(manifest_get rollback.mode)
readonly MANIFEST_SHA TARGET_SHA PARENT_SHA CURRENT_COMPOSE_SHA
readonly CANDIDATE_COMPOSE_SHA CANDIDATE_COMPOSE DB_IMAGE_ID
readonly OLD_APP_IMAGE_ID OLD_FRONTEND_IMAGE_ID NEW_APP_IMAGE_ID
readonly NEW_FRONTEND_IMAGE_ID APP_IMAGE_REF FRONTEND_IMAGE_REF ROLLBACK_MODE

compose() {
  env -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME -u COMPOSE_PROFILES \
    docker compose \
      --project-name it-spareparts \
      --env-file "$ENV_FILE" \
      -f "$ACTIVE_COMPOSE" "$@"
}

db_cid() {
  compose ps -q db
}

service_cid() {
  compose ps -q "$1"
}

container_image_id() {
  docker inspect -f '{{.Image}}' "$1"
}

container_restarts() {
  docker inspect -f '{{.RestartCount}}' "$1"
}

db_head() {
  compose exec -T db psql -X -U spareparts -d spareparts -At \
    -c 'SELECT version_num FROM alembic_version;'
}

require_real_file_600() {
  local path=$1
  [ -f "$path" ] && [ ! -L "$path" ] || fatal "unsafe credential file: $path"
  [ "$(stat -c '%a' "$path")" = 600 ] || fatal "credential file must be mode 600: $path"
}

write_json_atomic() {
  local destination=$1
  local payload=$2
  python3 - "$destination" "$payload" <<'PY'
import json
import os
import pathlib
import sys
import tempfile

destination = pathlib.Path(sys.argv[1])
payload = json.loads(sys.argv[2])
fd, temporary = tempfile.mkstemp(prefix=".v121-", dir=destination.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
    directory_fd = os.open(destination.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

state_get() {
  local key=$1
  [ -f "$STATE" ] && [ ! -L "$STATE" ] || fatal "release state is missing or unsafe"
  python3 - "$STATE" "$key" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8"))[sys.argv[2]]
print(value)
PY
}

state_update() {
  local phase=$1
  local extra
  extra=${2:-'{}'}
  python3 - "$STATE" "$MANIFEST_SHA" "$phase" "$extra" <<'PY'
import datetime as dt
import json
import os
import pathlib
import sys
import tempfile

path = pathlib.Path(sys.argv[1])
state = json.load(path.open(encoding="utf-8")) if path.exists() else {}
if state and state.get("manifest_sha256") != sys.argv[2]:
    raise SystemExit("state belongs to another manifest")
state.update(json.loads(sys.argv[4]))
state.update({
    "format": "v121-beta-release-state-1",
    "manifest_sha256": sys.argv[2],
    "phase": sys.argv[3],
    "updated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
})
fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

require_phase() {
  local wanted=$1
  [ "$(state_get manifest_sha256)" = "$MANIFEST_SHA" ] \
    || fatal "release state manifest mismatch"
  [ "$(state_get phase)" = "$wanted" ] \
    || fatal "release phase must be $wanted"
}

safe_env_snapshot() {
  python3 - "$ENV_FILE" <<'PY'
import hashlib
import json
import pathlib
import re
import shlex
import sys

path = pathlib.Path(sys.argv[1])
if not path.is_file() or path.is_symlink():
    raise SystemExit("unsafe .env")
wanted = {
    "MAINTENANCE_BETA_ENABLED",
    "REPLENISHMENT_BETA_ENABLED",
    "MAINTENANCE_CUTOVER_ENABLED",
    "MAINTENANCE_MANIFEST_ACTIVE_KEY_ID",
    "MAINTENANCE_MANIFEST_ACTIVE_HMAC_KEY",
}
values = {}
for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if "=" not in line:
        raise SystemExit(f"invalid .env line {number}")
    key, value = line.split("=", 1)
    key = key.strip()
    if key not in wanted:
        continue
    if key in values:
        raise SystemExit(f"duplicate protected .env key: {key}")
    value = value.strip()
    if value.startswith(("'", '"')):
        parsed = shlex.split(value, posix=True)
        if len(parsed) != 1:
            raise SystemExit(f"invalid quoted .env value: {key}")
        value = parsed[0]
    values[key] = value
missing = wanted - values.keys()
if missing:
    raise SystemExit("missing protected .env keys: " + ",".join(sorted(missing)))
for key in (
    "MAINTENANCE_BETA_ENABLED",
    "REPLENISHMENT_BETA_ENABLED",
    "MAINTENANCE_CUTOVER_ENABLED",
):
    if values[key].lower() not in {"true", "false"}:
        raise SystemExit(f"invalid boolean .env value: {key}")
if not values["MAINTENANCE_MANIFEST_ACTIVE_KEY_ID"]:
    raise SystemExit("empty maintenance manifest HMAC key id")
secret = values.pop("MAINTENANCE_MANIFEST_ACTIVE_HMAC_KEY")
if not secret:
    raise SystemExit("empty maintenance manifest HMAC key")
values["MAINTENANCE_MANIFEST_HMAC_KEY_FINGERPRINT_SHA256"] = hashlib.sha256(
    secret.encode("utf-8")
).hexdigest()
print(json.dumps(values, sort_keys=True, separators=(",", ":")))
PY
}

assert_flags() {
  local maintenance=$1
  local replenishment=$2
  local cutover=$3
  local snapshot
  snapshot=$(safe_env_snapshot) || fatal "cannot read protected .env settings"
  python3 - "$snapshot" "$maintenance" "$replenishment" "$cutover" <<'PY'
import json
import sys
values = json.loads(sys.argv[1])
expected = {
    "MAINTENANCE_BETA_ENABLED": sys.argv[2],
    "REPLENISHMENT_BETA_ENABLED": sys.argv[3],
    "MAINTENANCE_CUTOVER_ENABLED": sys.argv[4],
}
for key, value in expected.items():
    if values[key].lower() != value:
        raise SystemExit(f"{key} must be {value}")
PY
}

verify_hmac_identity() {
  local snapshot
  snapshot=$(safe_env_snapshot) || fatal "cannot read HMAC identity"
  python3 - "$snapshot" \
    "$(manifest_get maintenance_manifest_hmac.key_id)" \
    "$(manifest_get maintenance_manifest_hmac.key_fingerprint_sha256)" <<'PY'
import json
import sys
values = json.loads(sys.argv[1])
if values["MAINTENANCE_MANIFEST_ACTIVE_KEY_ID"] != sys.argv[2]:
    raise SystemExit("maintenance manifest HMAC key id mismatch")
if values["MAINTENANCE_MANIFEST_HMAC_KEY_FINGERPRINT_SHA256"] != sys.argv[3]:
    raise SystemExit("maintenance manifest HMAC key fingerprint mismatch")
PY
}

set_flags() {
  local maintenance=$1
  local replenishment=$2
  [ "$maintenance" = true ] || [ "$maintenance" = false ] || fatal "invalid maintenance flag"
  [ "$replenishment" = true ] || [ "$replenishment" = false ] || fatal "invalid replenishment flag"
  python3 - "$ENV_FILE" "$maintenance" "$replenishment" <<'PY'
import os
import pathlib
import re
import sys
import tempfile

path = pathlib.Path(sys.argv[1])
stat = path.stat(follow_symlinks=False)
if not path.is_file() or path.is_symlink():
    raise SystemExit("unsafe .env")
replacements = {
    "MAINTENANCE_BETA_ENABLED": sys.argv[2],
    "REPLENISHMENT_BETA_ENABLED": sys.argv[3],
    "MAINTENANCE_CUTOVER_ENABLED": "false",
}
lines = path.read_text(encoding="utf-8").splitlines()
seen = set()
output = []
for raw in lines:
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", raw)
    if match and match.group(1) in replacements:
        key = match.group(1)
        if key in seen:
            raise SystemExit(f"duplicate protected .env key: {key}")
        output.append(f"{key}={replacements[key]}")
        seen.add(key)
    else:
        output.append(raw)
for key in replacements:
    if key not in seen:
        output.append(f"{key}={replacements[key]}")
fd, temporary = tempfile.mkstemp(prefix=".env-v121-", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("\n".join(output) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chown(temporary, stat.st_uid, stat.st_gid)
    os.chmod(temporary, stat.st_mode & 0o777)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

assert_db_unchanged() {
  local cid
  cid=$(db_cid)
  [ -n "$cid" ] || fatal "database container is absent"
  [ "$cid" = "$(state_get db_container_id)" ] || fatal "database container changed"
  [ "$(container_image_id "$cid")" = "$DB_IMAGE_ID" ] || fatal "database image changed"
  [ "$(container_restarts "$cid")" = "$(state_get db_restart_count)" ] \
    || fatal "database restart count changed"
  [ "$(docker inspect -f '{{.State.Running}}' "$cid")" = true ] \
    || fatal "database container is not running"
}

database_pressure_json() {
  compose exec -T db psql -X -U spareparts -d spareparts -At -F '|' <<'SQL' |
WITH activity AS (
  SELECT pid,
         COALESCE(array_length(pg_blocking_pids(pid), 1), 0) AS blocked_by,
         EXTRACT(EPOCH FROM (clock_timestamp() - xact_start))::bigint AS xact_seconds
  FROM pg_stat_activity
  WHERE datname = current_database()
    AND pid <> pg_backend_pid()
    AND xact_start IS NOT NULL
), lock_summary AS (
  SELECT count(*) FILTER (WHERE NOT granted) AS waiting_locks,
         count(*) AS total_locks
  FROM pg_locks
  WHERE database = (SELECT oid FROM pg_database WHERE datname = current_database())
)
SELECT COALESCE((SELECT count(*) FROM activity WHERE blocked_by > 0), 0),
       COALESCE((SELECT count(*) FROM activity WHERE xact_seconds >= 60), 0),
       COALESCE((SELECT waiting_locks FROM lock_summary), 0),
       COALESCE((SELECT total_locks FROM lock_summary), 0);
SQL
  python3 -c '
import json,sys
row=sys.stdin.read().strip().split("|")
if len(row) != 4: raise SystemExit("malformed DB pressure probe")
print(json.dumps(dict(zip(("blocked_sessions","long_transactions","waiting_locks","total_locks"),map(int,row))),sort_keys=True,separators=(",",":")))
'
}

assert_no_db_pressure() {
  local pressure
  pressure=$(database_pressure_json) || fatal "database pressure probe failed"
  python3 - "$pressure" <<'PY'
import json
import sys
row=json.loads(sys.argv[1])
if row["blocked_sessions"] or row["long_transactions"] or row["waiting_locks"]:
    raise SystemExit("blocked session, long transaction or waiting lock is present")
PY
}

live_allowlist_json() {
  local raw
  raw=$(compose exec -T db psql -X -U spareparts -d spareparts -At -F $'\t' <<'SQL'
WITH effective AS (
  SELECT username,
         role,
         COALESCE((perm_overrides->>'page_maintenance')::boolean,
                  (template_perms->>'page_maintenance')::boolean, false) AS page_maintenance,
         COALESCE((perm_overrides->>'page_maintenance_beta')::boolean,
                  (template_perms->>'page_maintenance_beta')::boolean, false) AS page_maintenance_beta,
         COALESCE((perm_overrides->>'action_maintenance_roundtrip_apply')::boolean,
                  (template_perms->>'action_maintenance_roundtrip_apply')::boolean, false) AS action_maintenance_roundtrip_apply,
         COALESCE((perm_overrides->>'action_maintenance_manager_workbook_apply')::boolean,
                  (template_perms->>'action_maintenance_manager_workbook_apply')::boolean, false) AS action_maintenance_manager_workbook_apply,
         COALESCE((perm_overrides->>'action_maintenance_project_manage')::boolean,
                  (template_perms->>'action_maintenance_project_manage')::boolean, false) AS action_maintenance_project_manage,
         COALESCE((perm_overrides->>'action_maintenance_demand_delete')::boolean,
                  (template_perms->>'action_maintenance_demand_delete')::boolean, false) AS action_maintenance_demand_delete,
         COALESCE((perm_overrides->>'action_maintenance_site_issue_manage')::boolean,
                  (template_perms->>'action_maintenance_site_issue_manage')::boolean, false) AS action_maintenance_site_issue_manage,
         COALESCE((perm_overrides->>'action_maintenance_bad_return_manage')::boolean,
                  (template_perms->>'action_maintenance_bad_return_manage')::boolean, false) AS action_maintenance_bad_return_manage,
         COALESCE((perm_overrides->>'action_maintenance_acceptance_submit')::boolean,
                  (template_perms->>'action_maintenance_acceptance_submit')::boolean, false) AS action_maintenance_acceptance_submit,
         COALESCE((perm_overrides->>'action_maintenance_acceptance_review')::boolean,
                  (template_perms->>'action_maintenance_acceptance_review')::boolean, false) AS action_maintenance_acceptance_review,
         COALESCE((perm_overrides->>'action_maintenance_warehouse_manage')::boolean,
                  (template_perms->>'action_maintenance_warehouse_manage')::boolean, false) AS action_maintenance_warehouse_manage,
         COALESCE((perm_overrides->>'action_maintenance_migration_review')::boolean,
                  (template_perms->>'action_maintenance_migration_review')::boolean, false) AS action_maintenance_migration_review,
         COALESCE((perm_overrides->>'page_replenishment_beta')::boolean,
                  (template_perms->>'page_replenishment_beta')::boolean, false) AS page_replenishment_beta,
         COALESCE((perm_overrides->>'data_pool_price_governance')::boolean,
                  (template_perms->>'data_pool_price_governance')::boolean, false) AS data_pool_price_governance,
         COALESCE((perm_overrides->>'action_replenishment_create')::boolean,
                  (template_perms->>'action_replenishment_create')::boolean, false) AS action_replenishment_create,
         COALESCE((perm_overrides->>'action_replenishment_review')::boolean,
                  (template_perms->>'action_replenishment_review')::boolean, false) AS action_replenishment_review
  FROM sys_user
  WHERE is_active IS TRUE AND template_perms IS NOT NULL
)
SELECT username, role,
       page_maintenance, page_maintenance_beta,
       action_maintenance_roundtrip_apply,
       action_maintenance_manager_workbook_apply,
       action_maintenance_project_manage,
       action_maintenance_demand_delete,
       action_maintenance_site_issue_manage,
       action_maintenance_bad_return_manage,
       action_maintenance_acceptance_submit,
       action_maintenance_acceptance_review,
       action_maintenance_warehouse_manage,
       action_maintenance_migration_review,
       page_replenishment_beta, data_pool_price_governance,
       action_replenishment_create, action_replenishment_review
FROM effective
WHERE page_maintenance_beta IS TRUE OR page_replenishment_beta IS TRUE
ORDER BY username;
SQL
  ) || fatal "cannot calculate live Beta allowlist"
  python3 - "$raw" <<'PY'
import hashlib
import json
import sys

maintenance_keys = (
    "page_maintenance", "page_maintenance_beta",
    "action_maintenance_roundtrip_apply",
    "action_maintenance_manager_workbook_apply",
    "action_maintenance_project_manage", "action_maintenance_demand_delete",
    "action_maintenance_site_issue_manage", "action_maintenance_bad_return_manage",
    "action_maintenance_acceptance_submit", "action_maintenance_acceptance_review",
    "action_maintenance_warehouse_manage", "action_maintenance_migration_review",
)
replenishment_keys = (
    "page_replenishment_beta", "data_pool_price_governance",
    "action_replenishment_create", "action_replenishment_review",
)
rows=[]
for line in sys.argv[1].splitlines():
    fields=line.split("\t")
    if len(fields) != 2 + len(maintenance_keys) + len(replenishment_keys):
        raise SystemExit("malformed live allowlist row")
    values=[value == "t" for value in fields[2:]]
    if any(value not in {"t","f"} for value in fields[2:]):
        raise SystemExit("malformed live allowlist boolean")
    maintenance=dict(zip(maintenance_keys, values[:len(maintenance_keys)]))
    replenishment=dict(zip(replenishment_keys, values[len(maintenance_keys):]))
    rows.append({"username":fields[0],"role":fields[1],
                 "maintenance":dict(sorted(maintenance.items())),
                 "replenishment":dict(sorted(replenishment.items()))})
rows.sort(key=lambda row: row["username"])
def digest(value):
    if not rows:
        return hashlib.sha256(b"").hexdigest()
    raw=(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n").encode()
    return hashlib.sha256(raw).hexdigest()
maintenance=[{"username":row["username"],"role":row["role"],**row["maintenance"]} for row in rows]
replenishment=[{"username":row["username"],"role":row["role"],**row["replenishment"]} for row in rows]
result={
  "account_count":len(rows),
  "permission_graph_sha256":digest(rows),
  "maintenance_effective_permissions_sha256":digest(maintenance),
  "replenishment_effective_permissions_sha256":digest(replenishment),
}
print(json.dumps(result,sort_keys=True,separators=(",",":")))
PY
}

assert_empty_allowlist() {
  local live
  live=$(live_allowlist_json)
  python3 - "$live" "$(manifest_get intended_beta_allowlist.empty_stage_sha256)" <<'PY'
import json
import sys
row=json.loads(sys.argv[1])
if row["account_count"] != 0:
    raise SystemExit("Beta allowlist is not empty")
for key, value in row.items():
    if key.endswith("sha256") and value != sys.argv[2]:
        raise SystemExit("empty allowlist digest mismatch")
PY
}

assert_intended_allowlist() {
  local live
  live=$(live_allowlist_json)
  python3 - "$live" "$MANIFEST" <<'PY'
import json
import sys
live=json.loads(sys.argv[1])
expected=json.load(open(sys.argv[2],encoding="utf-8"))["intended_beta_allowlist"]
for key in (
    "account_count", "permission_graph_sha256",
    "maintenance_effective_permissions_sha256",
    "replenishment_effective_permissions_sha256",
):
    if live[key] != expected[key]:
        raise SystemExit(f"live intended Beta permission graph mismatch: {key}")
PY
}

smoke() {
  local mode=$1
  local credential=$2
  require_real_file_600 "$credential"
  python3 - "$mode" "$credential" "https://$HOST_NAME" <<'PY'
import json
import ssl
import sys
import urllib.error
import urllib.request

mode, credential_path, origin = sys.argv[1:]
credential=json.load(open(credential_path,encoding="utf-8"))
if set(credential) != {"username","password"} or not all(isinstance(v,str) and v for v in credential.values()):
    raise SystemExit("credential JSON must contain only non-empty username/password")
context=ssl.create_default_context()
def request(path, token=None, payload=None):
    headers={"Accept":"application/json"}
    data=None
    if token:
        headers["Authorization"]="Bearer "+token
    if payload is not None:
        headers["Content-Type"]="application/json"
        data=json.dumps(payload).encode()
    req=urllib.request.Request(origin+path,headers=headers,data=data)
    try:
        with urllib.request.urlopen(req,timeout=15,context=context) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            body=json.load(exc)
        except Exception:
            body=None
        return exc.code, body
status, login=request("/api/auth/login",payload=credential)
if status != 200 or not isinstance(login,dict) or not login.get("token"):
    raise SystemExit("login smoke failed")
token=login["token"]
status, features=request("/api/auth/beta-features",token=token)
if status != 200 or not isinstance(features,dict):
    raise SystemExit("Beta feature snapshot smoke failed")
status, _=request("/api/maintenance/projects?lifecycle=ongoing",token=token)
if status != 200:
    raise SystemExit(f"stable Maintenance smoke failed: {status}")
if mode == "stable":
    if features.get("maintenance") or features.get("replenishment"):
        raise SystemExit("stable-only smoke unexpectedly exposes Beta")
elif mode == "deny":
    if features.get("maintenance") or features.get("replenishment"):
        raise SystemExit("denied account unexpectedly exposes Beta")
    status, _=request("/api/maintenance/projects/stable?page_size=1",token=token)
    if status not in {403,404}:
        raise SystemExit(f"Maintenance Beta deny smoke failed: {status}")
    status, _=request("/api/replenishment-beta/catalog?page_size=1",token=token)
    if status not in {403,404}:
        raise SystemExit(f"replenishment Beta deny smoke failed: {status}")
elif mode == "allow":
    if not features.get("maintenance") and not features.get("replenishment"):
        raise SystemExit("allowed account has no Beta feature")
    if features.get("maintenance"):
        status, _=request("/api/maintenance/projects/stable?page_size=1",token=token)
        if status != 200:
            raise SystemExit(f"Maintenance Beta allow smoke failed: {status}")
    if features.get("replenishment"):
        status, _=request("/api/replenishment-beta/catalog?page_size=1",token=token)
        if status != 200:
            raise SystemExit(f"replenishment Beta allow smoke failed: {status}")
else:
    raise SystemExit("unknown smoke mode")
PY
}

internal_health() {
  local attempt
  for attempt in $(seq 1 30); do
    if curl --noproxy '*' --fail --silent --show-error --max-time 5 \
        http://127.0.0.1:8080/ >/dev/null 2>&1 \
      && curl --noproxy '*' --proto '=https' --tlsv1.2 --fail --silent \
        --show-error --max-time 8 "https://$HOST_NAME/health" >/dev/null 2>&1 \
      && curl --noproxy '*' --proto '=https' --tlsv1.2 --fail --silent \
        --show-error --max-time 8 "https://$HOST_NAME/health/db" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

recreate_app() {
  compose up --no-deps --no-build --force-recreate -d app
  assert_db_unchanged
  internal_health || fatal "application did not recover after configuration change"
}

install_candidate_compose() {
  local active_hash
  active_hash=$(sha256_file "$ACTIVE_COMPOSE")
  if [ "$active_hash" = "$CANDIDATE_COMPOSE_SHA" ]; then
    return 0
  fi
  [ "$active_hash" = "$CURRENT_COMPOSE_SHA" ] || fatal "active compose drifted"
  [ "$(sha256_file "$CANDIDATE_COMPOSE")" = "$CANDIDATE_COMPOSE_SHA" ] \
    || fatal "candidate compose drifted"
  install -m 600 -- "$ACTIVE_COMPOSE" "$EVIDENCE_DIR/pre-v121-compose.yml"
  install -m 644 -- "$CANDIDATE_COMPOSE" "$APP_DIR/.docker-compose.v121.tmp"
  mv -T -- "$APP_DIR/.docker-compose.v121.tmp" "$ACTIVE_COMPOSE"
  [ "$(sha256_file "$ACTIVE_COMPOSE")" = "$CANDIDATE_COMPOSE_SHA" ] \
    || fatal "candidate compose install failed"
  assert_db_unchanged
}

tag_candidate_images() {
  [ "$(docker image inspect -f '{{.Id}}' "$NEW_APP_IMAGE_ID")" = "$NEW_APP_IMAGE_ID" ]
  [ "$(docker image inspect -f '{{.Id}}' "$NEW_FRONTEND_IMAGE_ID")" = "$NEW_FRONTEND_IMAGE_ID" ]
  docker image tag "$OLD_APP_IMAGE_ID" "it-spareparts-release/app:pre-v121-$MANIFEST_SHA"
  docker image tag "$OLD_FRONTEND_IMAGE_ID" "it-spareparts-release/frontend:pre-v121-$MANIFEST_SHA"
  docker image tag "$NEW_APP_IMAGE_ID" "$APP_IMAGE_REF"
  docker image tag "$NEW_FRONTEND_IMAGE_ID" "$FRONTEND_IMAGE_REF"
  [ "$(docker image inspect -f '{{.Id}}' "$APP_IMAGE_REF")" = "$NEW_APP_IMAGE_ID" ]
  [ "$(docker image inspect -f '{{.Id}}' "$FRONTEND_IMAGE_REF")" = "$NEW_FRONTEND_IMAGE_ID" ]
}

preflight() {
  [ "$#" -eq 1 ] || usage
  local image_bundle=$1
  [ -f "$image_bundle" ] && [ ! -L "$image_bundle" ] || fatal "image bundle is missing or unsafe"
  [ "$(sha256_file "$image_bundle")" = "$(manifest_get build.image_bundle_sha256)" ] \
    || fatal "image bundle digest does not match manifest"
  [ ! -e "$EVIDENCE_DIR" ] && [ ! -L "$EVIDENCE_DIR" ] \
    || fatal "evidence directory must not already exist"
  mkdir -p -- "$EVIDENCE_DIR"
  chmod 700 -- "$EVIDENCE_DIR"
  [ -f "$ACTIVE_COMPOSE" ] && [ ! -L "$ACTIVE_COMPOSE" ] || fatal "active compose unsafe"
  [ "$(sha256_file "$ACTIVE_COMPOSE")" = "$CURRENT_COMPOSE_SHA" ] \
    || fatal "current production compose hash mismatch"
  [ -f "$ENV_FILE" ] && [ ! -L "$ENV_FILE" ] || fatal "production .env unsafe"
  [ "$(stat -c '%a' "$ENV_FILE")" = 600 ] || fatal "production .env must be mode 600"
  assert_flags false false false
  verify_hmac_identity
  local db app frontend
  db=$(db_cid)
  app=$(service_cid app)
  frontend=$(service_cid frontend)
  [ -n "$db" ] && [ -n "$app" ] && [ -n "$frontend" ] || fatal "production containers missing"
  [ "$(container_image_id "$db")" = "$DB_IMAGE_ID" ] || fatal "database image mismatch"
  [ "$(container_image_id "$app")" = "$OLD_APP_IMAGE_ID" ] || fatal "old app image mismatch"
  [ "$(container_image_id "$frontend")" = "$OLD_FRONTEND_IMAGE_ID" ] \
    || fatal "old frontend image mismatch"
  [ "$(db_head)" = "$EXPECTED_FROM" ] || fatal "production DB is not at $EXPECTED_FROM"
  [ "$(docker image inspect -f '{{.Id}}' "$NEW_APP_IMAGE_ID")" = "$NEW_APP_IMAGE_ID" ] \
    || fatal "new app image is not loaded"
  [ "$(docker image inspect -f '{{.Id}}' "$NEW_FRONTEND_IMAGE_ID")" = "$NEW_FRONTEND_IMAGE_ID" ] \
    || fatal "new frontend image is not loaded"
  assert_no_db_pressure
  assert_empty_allowlist
  local processing
  processing=$(compose exec -T db psql -X -U spareparts -d spareparts -At \
    -c "SELECT count(*) FROM import_job WHERE status = 'processing';")
  [ "$processing" = 0 ] || fatal "an import job is still processing"
  state_update preflight "$(python3 - "$db" "$app" "$frontend" \
    "$(container_restarts "$db")" <<'PY'
import json,sys
print(json.dumps({"db_container_id":sys.argv[1],"old_app_container_id":sys.argv[2],
                  "old_frontend_container_id":sys.argv[3],"db_restart_count":int(sys.argv[4])},
                 separators=(",",":")))
PY
)"
  write_json_atomic "$EVIDENCE_DIR/preflight.json" "$(python3 - \
    "$MANIFEST_SHA" "$TARGET_SHA" "$PARENT_SHA" "$(safe_env_snapshot)" \
    "$(database_pressure_json)" <<'PY'
import datetime as dt,json,sys
print(json.dumps({"format":"v121-preflight-1","manifest_sha256":sys.argv[1],
 "target_sha":sys.argv[2],"parent_production_sha":sys.argv[3],
 "checked_at":dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
 "safe_env":json.loads(sys.argv[4]),"database_pressure":json.loads(sys.argv[5]),
 "result":"passed"},separators=(",",":"),sort_keys=True))
PY
)"
  printf 'preflight passed; all Beta/cutover flags are false\n'
}

backup_restore() {
  [ "$#" -eq 0 ] || usage
  require_phase preflight
  assert_flags false false false
  assert_db_unchanged
  assert_no_db_pressure
  local backup globals toc restore_name started ended restored_head
  backup="$EVIDENCE_DIR/pre-migration.dump"
  globals="$EVIDENCE_DIR/pre-migration-globals.sql"
  toc="$EVIDENCE_DIR/pre-migration.toc"
  [ ! -e "$backup" ] && [ ! -e "$globals" ] && [ ! -e "$toc" ] \
    || fatal "backup evidence already exists"
  started=$(date +%s)
  compose exec -T db pg_dump -U spareparts -d spareparts --format=custom \
    --blobs --no-owner --no-acl >"$backup"
  compose exec -T db pg_dumpall -U spareparts --globals-only >"$globals"
  docker run --rm --network none -v "$EVIDENCE_DIR:/evidence:ro" "$DB_IMAGE_ID" \
    pg_restore --list /evidence/pre-migration.dump >"$toc"
  [ -s "$backup" ] && [ -s "$globals" ] && [ -s "$toc" ] || fatal "backup evidence is incomplete"
  chmod 600 "$backup" "$globals" "$toc"
  restore_name="it-spareparts-v121-restore-${MANIFEST_SHA:0:12}"
  docker container inspect "$restore_name" >/dev/null 2>&1 \
    && fatal "isolated restore container already exists"
  cleanup_restore() {
    docker rm -f "$restore_name" >/dev/null 2>&1 || true
  }
  trap cleanup_restore EXIT HUP INT TERM
  docker run -d --name "$restore_name" --network none \
    --tmpfs /var/lib/postgresql/data:rw,noexec,nosuid,size=4g \
    -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_USER=spareparts \
    -e POSTGRES_DB=spareparts "$DB_IMAGE_ID" >/dev/null
  local attempt
  for attempt in $(seq 1 60); do
    docker exec "$restore_name" pg_isready -U spareparts >/dev/null 2>&1 && break
    [ "$attempt" -lt 60 ] || fatal "isolated restore database did not become ready"
    sleep 1
  done
  docker cp "$backup" "$restore_name:/tmp/pre-migration.dump"
  docker exec "$restore_name" pg_restore -U spareparts -d spareparts \
    --exit-on-error --no-owner --no-acl /tmp/pre-migration.dump
  restored_head=$(docker exec "$restore_name" psql -X -U spareparts -d spareparts -At \
    -c 'SELECT version_num FROM alembic_version;')
  [ "$restored_head" = "$EXPECTED_FROM" ] || fatal "isolated restore DB head mismatch"
  ended=$(date +%s)
  cleanup_restore
  trap - EXIT HUP INT TERM
  write_json_atomic "$EVIDENCE_DIR/backup-restore.json" "$(python3 - \
    "$MANIFEST_SHA" "$(sha256_file "$backup")" "$(sha256_file "$globals")" \
    "$(sha256_file "$toc")" "$restored_head" "$((ended-started))" "$DB_IMAGE_ID" <<'PY'
import datetime as dt,json,sys
print(json.dumps({"format":"v121-backup-restore-1","manifest_sha256":sys.argv[1],
 "database_dump_sha256":sys.argv[2],"globals_sha256":sys.argv[3],
 "toc_sha256":sys.argv[4],"isolated_restore_head":sys.argv[5],
 "duration_seconds":int(sys.argv[6]),"restore_image_id":sys.argv[7],
 "network":"none","completed_at":dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
 "result":"passed"},sort_keys=True,separators=(",",":")))
PY
)"
  state_update backup_restored "$(python3 - "$EVIDENCE_DIR/backup-restore.json" <<'PY'
import hashlib,json,sys
p=sys.argv[1]
print(json.dumps({"backup_restore_evidence_sha256":hashlib.sha256(open(p,"rb").read()).hexdigest()},separators=(",",":")))
PY
)"
  printf 'full backup and isolated restore passed\n'
}

migrate() {
  [ "$#" -eq 0 ] || usage
  require_phase backup_restored
  assert_flags false false false
  assert_db_unchanged
  assert_no_db_pressure
  [ "$(db_head)" = "$EXPECTED_FROM" ] || fatal "DB head changed before migration"
  install_candidate_compose
  tag_candidate_images
  # Avoid running an unrehearsed old application image against committed d9.
  compose stop app frontend
  [ -z "$(compose ps -q app)" ] && [ -z "$(compose ps -q frontend)" ] \
    || fatal "old business containers did not stop"
  assert_db_unchanged
  local samples="$EVIDENCE_DIR/migration-db-pressure.jsonl"
  : >"$samples"
  chmod 600 "$samples"
  (
    while :; do
      printf '{"captured_at":"%s","pressure":%s}\n' \
        "$(date --iso-8601=seconds)" "$(database_pressure_json)" >>"$samples" || exit 1
      sleep 1
    done
  ) &
  local sampler_pid=$!
  local started_ns ended_ns status
  started_ns=$(date +%s%N)
  set +e
  compose run --rm --no-deps \
    -e "PGOPTIONS=-c statement_timeout=120000 -c lock_timeout=5000" \
    app alembic upgrade "$EXPECTED_TO"
  status=$?
  set -e
  ended_ns=$(date +%s%N)
  kill "$sampler_pid" >/dev/null 2>&1 || true
  wait "$sampler_pid" >/dev/null 2>&1 || true
  if [ "$status" -ne 0 ]; then
    state_update migration_failed '{"beta_contained":true,"database_restore_forbidden":true}'
    fatal "migration failed; Beta remains off, do not downgrade or restore DB; forward fix required"
  fi
  [ "$(db_head)" = "$EXPECTED_TO" ] || fatal "migration completed without expected d9 head"
  assert_db_unchanged
  write_json_atomic "$EVIDENCE_DIR/migration.json" "$(python3 - \
    "$MANIFEST_SHA" "$EXPECTED_FROM" "$EXPECTED_TO" "$started_ns" "$ended_ns" \
    "$(sha256_file "$samples")" "$(database_pressure_json)" <<'PY'
import datetime as dt,json,sys
print(json.dumps({"format":"v121-migration-1","manifest_sha256":sys.argv[1],
 "from_revision":sys.argv[2],"to_revision":sys.argv[3],
 "duration_milliseconds":(int(sys.argv[5])-int(sys.argv[4]))//1_000_000,
 "statement_timeout_ms":120000,"lock_timeout_ms":5000,
 "pressure_samples_sha256":sys.argv[6],"post_pressure":json.loads(sys.argv[7]),
 "completed_at":dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
 "result":"passed"},sort_keys=True,separators=(",",":")))
PY
)"
  state_update migrated "$(python3 - "$EVIDENCE_DIR/migration.json" <<'PY'
import hashlib,json,sys
print(json.dumps({"migration_evidence_sha256":hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest(),
                  "database_restore_forbidden":True},separators=(",",":")))
PY
)"
  printf 'database migrated to d9; only forward fix or rehearsed old-image app rollback is allowed\n'
}

deploy() {
  [ "$#" -eq 2 ] || usage
  require_phase migrated
  assert_flags false false false
  assert_db_unchanged
  [ "$(db_head)" = "$EXPECTED_TO" ] || fatal "DB is not at d9"
  tag_candidate_images
  compose up --no-deps --no-build --force-recreate -d app frontend
  assert_db_unchanged
  [ "$(container_image_id "$(service_cid app)")" = "$NEW_APP_IMAGE_ID" ] \
    || fatal "new app image did not start"
  [ "$(container_image_id "$(service_cid frontend)")" = "$NEW_FRONTEND_IMAGE_ID" ] \
    || fatal "new frontend image did not start"
  internal_health || fatal "new stable surface did not become healthy"
  smoke stable "$1"
  smoke deny "$2"
  local deployed_at
  deployed_at=$(date +%s)
  state_update deployed "$(python3 - "$deployed_at" <<'PY'
import json,sys
print(json.dumps({"deployed_at_epoch":int(sys.argv[1]),"beta_contained":True},separators=(",",":")))
PY
)"
  printf 'candidate app/frontend deployed with all Beta/cutover flags false\n'
}

open_empty_beta() {
  [ "$#" -eq 1 ] || usage
  require_phase deployed
  assert_empty_allowlist
  set_flags true true
  recreate_app
  assert_flags true true false
  assert_empty_allowlist
  smoke deny "$1"
  state_update empty_beta_open '{"beta_contained":false,"empty_allowlist_stage_passed":true}'
  printf 'global Beta gates are open, empty allowlist still denies every account\n'
}

pilot_smoke() {
  [ "$#" -eq 2 ] || usage
  require_phase empty_beta_open
  assert_flags true true false
  assert_intended_allowlist
  smoke deny "$1"
  smoke allow "$2"
  local opened
  opened=$(date +%s)
  state_update pilot_open "$(python3 - "$opened" <<'PY'
import json,sys
print(json.dumps({"pilot_opened_at_epoch":int(sys.argv[1]),"allowlist_verified":True},separators=(",",":")))
PY
)"
  printf 'named-account pilot smoke passed\n'
}

observe() {
  [ "$#" -eq 4 ] || usage
  local minute=$1
  case "$minute" in 0|5|15|30) ;; *) usage ;; esac
  require_phase pilot_open
  assert_flags true true false
  assert_db_unchanged
  assert_intended_allowlist
  local opened now required_elapsed
  opened=$(state_get pilot_opened_at_epoch)
  now=$(date +%s)
  required_elapsed=$((minute * 60))
  [ "$((now-opened))" -ge "$required_elapsed" ] \
    || fatal "observation point $minute minutes has not been reached"
  internal_health || fatal "health observation failed"
  smoke stable "$2"
  smoke deny "$3"
  smoke allow "$4"
  assert_no_db_pressure
  write_json_atomic "$EVIDENCE_DIR/observe-${minute}.json" "$(python3 - \
    "$MANIFEST_SHA" "$minute" "$(database_pressure_json)" \
    "$(container_restarts "$(db_cid)")" \
    "$(container_restarts "$(service_cid app)")" \
    "$(container_restarts "$(service_cid frontend)")" <<'PY'
import datetime as dt,json,sys
print(json.dumps({"format":"v121-observation-1","manifest_sha256":sys.argv[1],
 "minute":int(sys.argv[2]),"database_pressure":json.loads(sys.argv[3]),
 "restart_counts":{"db":int(sys.argv[4]),"app":int(sys.argv[5]),"frontend":int(sys.argv[6])},
 "stable_smoke":"passed","beta_deny_smoke":"passed","beta_allow_smoke":"passed",
 "observed_at":dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
 "result":"passed"},sort_keys=True,separators=(",",":")))
PY
)"
  printf 'observation minute %s passed\n' "$minute"
}

contain() {
  [ "$#" -eq 0 ] || usage
  [ -f "$STATE" ] || fatal "release state missing"
  set_flags false false
  recreate_app
  assert_flags false false false
  state_update contained '{"beta_contained":true}'
  printf 'both Beta gates and maintenance cutover are false\n'
}

rollback_app() {
  [ "$#" -eq 0 ] || usage
  [ -f "$STATE" ] || fatal "release state missing"
  [ "$(db_head)" = "$EXPECTED_TO" ] || fatal "rollback control only applies after committed d9"
  set_flags false false
  if [ "$ROLLBACK_MODE" != old_images_on_d9_allowed ]; then
    recreate_app
    state_update contained_forward_fix \
      '{"beta_contained":true,"old_image_rollback_refused":true,"database_restore_forbidden":true}'
    fatal "old images were not rehearsed on d9; Beta was contained, forward fix is mandatory"
  fi
  local evidence_path evidence_sha
  evidence_path="$PACKAGE_DIR/$(manifest_get rollback.rehearsal_evidence.path)"
  evidence_sha=$(manifest_get rollback.rehearsal_evidence.sha256)
  [ "$(sha256_file "$evidence_path")" = "$evidence_sha" ] \
    || fatal "old-image d9 rehearsal evidence drifted"
  docker image tag "$OLD_APP_IMAGE_ID" "$APP_IMAGE_REF"
  docker image tag "$OLD_FRONTEND_IMAGE_ID" "$FRONTEND_IMAGE_REF"
  compose up --no-deps --no-build --force-recreate -d app frontend
  assert_db_unchanged
  [ "$(container_image_id "$(service_cid app)")" = "$OLD_APP_IMAGE_ID" ] \
    || fatal "old app image did not start"
  [ "$(container_image_id "$(service_cid frontend)")" = "$OLD_FRONTEND_IMAGE_ID" ] \
    || fatal "old frontend image did not start"
  internal_health || fatal "rehearsed old-image application rollback is unhealthy"
  state_update old_images_on_d9 \
    '{"beta_contained":true,"database_restore_forbidden":true,"database_revision":"d9f1a3c7e5b2"}'
  printf 'rehearsed old application images restored on d9; database was not downgraded or restored\n'
}

case "$COMMAND" in
  preflight) preflight "$@" ;;
  backup-restore) backup_restore "$@" ;;
  migrate) migrate "$@" ;;
  deploy) deploy "$@" ;;
  open-empty-beta) open_empty_beta "$@" ;;
  pilot-smoke) pilot_smoke "$@" ;;
  observe) observe "$@" ;;
  contain) contain "$@" ;;
  rollback-app) rollback_app "$@" ;;
  *) usage ;;
esac
