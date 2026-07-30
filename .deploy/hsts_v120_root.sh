#!/usr/bin/env bash
# Root-only, HSTS-scoped control. It never restores the wider HTTPS ingress.
set -Eeuo pipefail
umask 077

readonly HSTS_PRE=300
readonly HSTS_POST=31536000
TEST_MODE=${HSTS_V120_TEST_MODE:-0}
if [ "$TEST_MODE" = 1 ]; then
  [ "$EUID" -ne 0 ] || {
    printf 'FATAL: root may not enable HSTS test mode\n' >&2
    exit 1
  }
  APP_DIR=${HSTS_APP_DIR:?}
  ASSISTANT_DIR=${HSTS_ASSISTANT_DIR:?}
  CONTROL_DIR=${HSTS_CONTROL_DIR:?}
  LOCK_PATH=${HSTS_LOCK_PATH:?}
  CADDY_LOCK_PATH=${HSTS_CADDY_LOCK_PATH:?}
  COMMAND_DIR=${HSTS_COMMAND_DIR:?}
  IT_URL=${HSTS_IT_URL:?}
  ASSISTANT_HEALTH_FILE=${HSTS_ASSISTANT_HEALTH_FILE:?}
  ASSISTANT_HEALTH_URL=${HSTS_ASSISTANT_HEALTH_URL:?}
  AUTHORITY_MARKER=${HSTS_AUTHORITY_MARKER:?}
  RUNTIME_CONTROL_MANIFEST_HASH=${HSTS_CONTROL_MANIFEST_HASH:?}
  V120_STATE_LIBRARY=${HSTS_V120_STATE_LIBRARY:?}
else
  [ "$EUID" -eq 0 ] || {
    printf 'FATAL: hsts_v120_root.sh must run as root\n' >&2
    exit 1
  }
  APP_DIR=/home/ubuntu/apps/it-spareparts
  ASSISTANT_DIR=/opt/personal-ai-assistant
  CONTROL_DIR=/var/lib/it-spareparts-release-control
  LOCK_PATH=/run/lock/it-spareparts-v120
  CADDY_LOCK_PATH=/etc/it-spareparts/shared-caddy.lock
  COMMAND_DIR=
  IT_URL=https://hbzgc.icu/
  ASSISTANT_HEALTH_FILE=/etc/it-spareparts/assistant-health.url
  ASSISTANT_HEALTH_URL=https://118.25.94.90/health
  AUTHORITY_MARKER=/etc/it-spareparts/v120-authority.marker
  RUNTIME_CONTROL_MANIFEST_HASH=
  V120_STATE_LIBRARY=
fi
readonly TEST_MODE APP_DIR ASSISTANT_DIR CONTROL_DIR LOCK_PATH \
  CADDY_LOCK_PATH COMMAND_DIR \
  IT_URL ASSISTANT_HEALTH_FILE ASSISTANT_HEALTH_URL AUTHORITY_MARKER
readonly HSTS_DIR="$CONTROL_DIR/hsts"
readonly GENERATIONS_DIR="$HSTS_DIR/generations"
readonly SUCCESSOR_LINEAGE="$CONTROL_DIR/edge/successor-lineage.txt"
readonly RECREATE_PENDING="$CONTROL_DIR/edge/recreate-pending.txt"
readonly COMPOSE_FILE="$ASSISTANT_DIR/compose.production.yml"
readonly CADDYFILE="$ASSISTANT_DIR/Caddyfile"
readonly APP_COMPOSE="$APP_DIR/docker-compose.yml"
readonly APP_ENV="$APP_DIR/.env"
readonly ASSISTANT_ENV="$ASSISTANT_DIR/.env"
export PATH="${COMMAND_DIR:+$COMMAND_DIR:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
unset CDPATH ENV BASH_ENV GIT_DIR GIT_WORK_TREE GIT_OBJECT_DIRECTORY \
  GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_REPLACE_REF_BASE

fatal() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

expected_owner() {
  if [ "$TEST_MODE" = 1 ]; then
    printf '%s:%s\n' "$(id -un)" "$(id -gn)"
  else
    printf 'root:root\n'
  fi
}

ensure_directory() {
  local path=$1
  local mode=$2
  local owner
  owner=$(expected_owner)
  if [ -e "$path" ] || [ -L "$path" ]; then
    [ -d "$path" ] && [ ! -L "$path" ] \
      || fatal "unsafe directory: $path"
    [ "$(stat -c '%a %U:%G' "$path")" = "$mode $owner" ] \
      || fatal "directory owner/mode mismatch: $path"
    return 0
  fi
  mkdir -- "$path"
  if [ "$TEST_MODE" != 1 ]; then
    chown root:root "$path"
  fi
  chmod "$mode" "$path"
  [ "$(stat -c '%a %U:%G' "$path")" = "$mode $owner" ] \
    || fatal "new directory owner/mode mismatch: $path"
}

safe_regular() {
  local path=$1
  local owner
  owner=$(expected_owner)
  [ -f "$path" ] && [ ! -L "$path" ] \
    && [ "$(stat -c '%U:%G %h' "$path")" = "$owner 1" ] \
    && [ $((8#$(stat -c '%a' "$path") & 8#022)) -eq 0 ]
}

safe_assistant_env() {
  if [ "$TEST_MODE" = 1 ]; then
    safe_regular "$ASSISTANT_ENV" \
      && [ "$(stat -c '%a' "$ASSISTANT_ENV")" = 600 ]
    return
  fi
  [ -f "$ASSISTANT_ENV" ] && [ ! -L "$ASSISTANT_ENV" ] \
    && [ "$(stat -c '%a %U:%G %h' "$ASSISTANT_ENV")" \
      = "600 ubuntu:ubuntu 1" ]
}

safe_app_env() {
  if [ "$TEST_MODE" = 1 ]; then
    safe_regular "$APP_ENV" && [ "$(stat -c '%a' "$APP_ENV")" = 600 ]
    return
  fi
  [ -f "$APP_ENV" ] && [ ! -L "$APP_ENV" ] \
    && [ "$(stat -c '%a %U %h' "$APP_ENV")" = "600 ubuntu 1" ]
}

app_compose() {
  docker compose \
    --project-name it-spareparts \
    --env-file "$APP_ENV" \
    -f "$APP_COMPOSE" \
    "$@"
}

assistant_health_sha256() {
  printf '%s\n' "$ASSISTANT_HEALTH_URL" | sha256sum | cut -d' ' -f1
}

verify_assistant_health_locator() {
  safe_regular "$ASSISTANT_HEALTH_FILE" \
    && [ "$(stat -c '%a' "$ASSISTANT_HEALTH_FILE")" = 600 ] \
    && [ "$(sha256sum "$ASSISTANT_HEALTH_FILE" | cut -d' ' -f1)" \
      = "$(assistant_health_sha256)" ]
}

ensure_assistant_health_locator() {
  local directory
  local temporary=
  local status
  directory=$(dirname -- "$ASSISTANT_HEALTH_FILE")
  ensure_directory "$directory" 700
  if [ -e "$ASSISTANT_HEALTH_FILE" ] \
      || [ -L "$ASSISTANT_HEALTH_FILE" ]; then
    verify_assistant_health_locator \
      || return 73
    return 0
  fi
  temporary=$(mktemp -- "$directory/.assistant-health.XXXXXX")
  cleanup_health_locator() {
    status=$?
    trap - RETURN
    if [ -n "$temporary" ] && ! rm -f -- "$temporary"; then
      [ "$status" -ne 0 ] || status=97
    fi
    return "$status"
  }
  trap cleanup_health_locator RETURN
  printf '%s\n' "$ASSISTANT_HEALTH_URL" > "$temporary"
  chmod 600 "$temporary"
  if [ "$TEST_MODE" != 1 ]; then
    chown root:root "$temporary"
  fi
  sync -f "$temporary" || return $?
  ln -- "$temporary" "$ASSISTANT_HEALTH_FILE" || return $?
  rm -f -- "$temporary" || return $?
  temporary=
  sync -f "$ASSISTANT_HEALTH_FILE" || return $?
  sync -d "$directory" || return $?
  verify_assistant_health_locator || return $?
  trap - RETURN
}

verify_packaged_control() {
  local target=$1
  if [ "$TEST_MODE" = 1 ]; then
    [[ "$RUNTIME_CONTROL_MANIFEST_HASH" =~ ^[0-9a-f]{64}$ ]] \
      && [ -f "$V120_STATE_LIBRARY" ] \
      && [ ! -L "$V120_STATE_LIBRARY" ] \
      || fatal "test control authority is invalid"
    return 0
  fi
  local script_path
  local script_dir
  local manifest_hash
  local package_target
  local current_target
  local expected_current
  script_path=$(realpath -e -- "${BASH_SOURCE[0]}") \
    || fatal "cannot resolve HSTS root control"
  script_dir=$(dirname -- "$script_path")
  manifest_hash=$(basename -- "$script_dir")
  [[ "$manifest_hash" =~ ^[0-9a-f]{64}$ ]] \
    || fatal "HSTS root control is not hash-addressed"
  [ "$script_dir" = "$CONTROL_DIR/versions/$manifest_hash" ] \
    || fatal "HSTS root control path is outside current control versions"
  expected_current="versions/$manifest_hash"
  [ -L "$CONTROL_DIR/current" ] \
    && [ "$(stat -c '%F %U:%G %h' "$CONTROL_DIR/current")" \
      = "symbolic link root:root 1" ] \
    || fatal "current control pointer is unsafe"
  current_target=$(readlink -- "$CONTROL_DIR/current") \
    || fatal "cannot read current control pointer"
  [ "$current_target" = "$expected_current" ] \
    || fatal "current control pointer does not select this package"
  [ "$(stat -c '%a %U:%G %h' "$script_path")" = "700 root:root 1" ] \
    || fatal "HSTS root control owner/mode is unsafe"
  "$script_dir/install-v120-control.sh" verify "$manifest_hash" >/dev/null \
    || fatal "HSTS root control package verification failed"
  [ "$(readlink -- "$CONTROL_DIR/current")" = "$current_target" ] \
    && [ "$(realpath -e -- "$CONTROL_DIR/current/hsts-v120-root.sh")" \
      = "$script_path" ] \
    || fatal "current control pointer changed under release lock"
  [ "$(grep -c '^TARGET_COMMIT=' "$script_dir/manifest.txt")" -eq 1 ] \
    || fatal "control package target is ambiguous"
  package_target=$(
    sed -n 's/^TARGET_COMMIT=//p' "$script_dir/manifest.txt"
  )
  [ "$package_target" = "$target" ] \
    || fatal "control package target differs from HSTS target"
  RUNTIME_CONTROL_MANIFEST_HASH=$manifest_hash
  V120_STATE_LIBRARY="$script_dir/v120_state.sh"
}

acquire_lock() {
  local expected_mode=750
  local expected_group=ubuntu
  if [ "$TEST_MODE" = 1 ]; then
    expected_mode=700
    expected_group=$(id -gn)
  fi
  [ -d "$LOCK_PATH" ] && [ ! -L "$LOCK_PATH" ] \
    || fatal "release lock is unsafe"
  [ "$(stat -c '%a %U:%G' "$LOCK_PATH")" \
    = "$expected_mode $(id -un):$expected_group" ] \
    || {
      [ "$TEST_MODE" != 1 ] \
        && [ "$(stat -c '%a %U:%G' "$LOCK_PATH")" \
          = "$expected_mode root:$expected_group" ] \
        || fatal "release lock owner/mode mismatch"
    }
  exec 9<"$LOCK_PATH"
  flock -n 9 || {
    printf 'HSTS_BUSY: another release operation holds the lock\n' >&2
    exit 75
  }
}

acquire_shared_caddy_lock() {
  local owner=root:root
  local path_identity fd_identity
  if [ "$TEST_MODE" = 1 ]; then
    owner="$(id -un):$(id -gn)"
  fi
  [ -f "$CADDY_LOCK_PATH" ] && [ ! -L "$CADDY_LOCK_PATH" ] \
    && [ "$(stat -c '%F %a %U:%G %h' "$CADDY_LOCK_PATH")" \
      = "regular empty file 600 $owner 1" ] \
    || fatal "shared Caddy lock file is unsafe"
  path_identity=$(stat -Lc '%d:%i' "$CADDY_LOCK_PATH")
  exec 8<"$CADDY_LOCK_PATH"
  fd_identity=$(stat -Lc '%d:%i' "/proc/$$/fd/8")
  [ "$fd_identity" = "$path_identity" ] \
    || fatal "shared Caddy lock changed while opening"
  flock -n 8 || {
    printf 'CADDY_BUSY: another shared Caddy writer holds the lock\n' >&2
    exit 75
  }
  [ "$(stat -Lc '%d:%i' "$CADDY_LOCK_PATH")" = "$fd_identity" ] \
    || fatal "shared Caddy lock changed after acquisition"
}

verify_target_commit() {
  local target=$1 action=$2
  local state="$CONTROL_DIR/v120-state.state"
  local app_state
  local app_state_identity="600 ubuntu:ubuntu 1"
  local marker_identity="600 root:root 1"
  local root_identity="600 root:root 1"
  local -a marker_lines=()
  declare -gA AUTHORITY_STATE=()
  if [ "$TEST_MODE" = 1 ]; then
    app_state_identity="600 $(id -un):$(id -gn) 1"
    marker_identity=$app_state_identity
    root_identity=$app_state_identity
  fi
  [ -f "$AUTHORITY_MARKER" ] && [ ! -L "$AUTHORITY_MARKER" ] \
    && [ "$(stat -c '%a %U:%G %h' "$AUTHORITY_MARKER")" \
      = "$marker_identity" ] \
    || fatal "root release authority marker is unsafe"
  mapfile -t marker_lines < "$AUTHORITY_MARKER"
  [ "${#marker_lines[@]}" -eq 3 ] \
    && [ "${marker_lines[0]}" = "AUTHORITY_FORMAT=v120-authority-1" ] \
    && [[ "${marker_lines[1]}" \
      =~ ^INITIAL_CONTROL_MANIFEST_HASH=[0-9a-f]{64}$ ]] \
    && [[ "${marker_lines[2]}" \
      =~ ^INITIAL_TARGET_COMMIT=[0-9a-f]{40}$ ]] \
    || fatal "root release authority marker is invalid"
  [ -f "$state" ] && [ ! -L "$state" ] \
    && [ "$(stat -c '%a %U:%G %h' "$state")" = "$root_identity" ] \
    || fatal "root release authority is unsafe"
  v120_state_parse_to_array "$state" AUTHORITY_STATE \
    || fatal "root release authority schema is invalid"
  [ "${AUTHORITY_STATE[CONTROL_MANIFEST_HASH]}" \
    = "$RUNTIME_CONTROL_MANIFEST_HASH" ] \
    || fatal "root release authority targets another control package"
  AUTH_TARGET_COMMIT=${AUTHORITY_STATE[TARGET_COMMIT]}
  AUTH_RELEASE_PHASE=${AUTHORITY_STATE[RELEASE_PHASE]}
  AUTH_RELEASE_ID=${AUTHORITY_STATE[RELEASE_ID]}
  AUTH_STATE_GENERATION=${AUTHORITY_STATE[STATE_GENERATION]}
  AUTH_STATE_SHA256=$(sha256sum "$state" | cut -d' ' -f1)
  [ "$AUTH_TARGET_COMMIT" = "$target" ] \
    && [ "$AUTH_RELEASE_PHASE" = observed ] \
    || fatal "root release authority is not exact observed target"
  app_state="$APP_DIR/backups/$AUTH_RELEASE_ID.state"
  if ! [ -f "$app_state" ] || [ -L "$app_state" ] \
      || [ "$(stat -c '%a %U:%G %h' "$app_state")" \
      != "$app_state_identity" ] \
      || ! cmp -s "$state" "$app_state"; then
    fatal "root and app release authority mirrors differ"
  fi
  [ "$(sha256sum "$APP_COMPOSE" | cut -d' ' -f1)" \
    = "${AUTHORITY_STATE[APP_COMPOSE_HASH]}" ] \
    || fatal "app compose differs from root release authority"
  AUTH_APP_CID=$(app_compose ps -q app)
  AUTH_FRONTEND_CID=$(app_compose ps -q frontend)
  AUTH_DB_CID=$(app_compose ps -q db)
  [ "$AUTH_APP_CID" = "${AUTHORITY_STATE[NEW_APP_CID]}" ] \
    && [ "$AUTH_FRONTEND_CID" = "${AUTHORITY_STATE[NEW_FRONTEND_CID]}" ] \
    && [ "$AUTH_DB_CID" = "${AUTHORITY_STATE[BASE_DB_CID]}" ] \
    || fatal "live release containers differ from root authority"
  AUTH_APP_IMAGE=$(docker inspect -f '{{.Image}}' "$AUTH_APP_CID")
  AUTH_FRONTEND_IMAGE=$(
    docker inspect -f '{{.Image}}' "$AUTH_FRONTEND_CID"
  )
  AUTH_DB_IMAGE=$(docker inspect -f '{{.Image}}' "$AUTH_DB_CID")
  [ "$AUTH_APP_IMAGE" = "${AUTHORITY_STATE[NEW_APP_IMAGE_ID]}" ] \
    && [ "$AUTH_FRONTEND_IMAGE" \
      = "${AUTHORITY_STATE[NEW_FRONTEND_IMAGE_ID]}" ] \
    && [ "$AUTH_DB_IMAGE" = "${AUTHORITY_STATE[BASE_DB_IMAGE_ID]}" ] \
    || fatal "live release containers differ from root authority"
  AUTH_APP_RESTARTS=$(docker inspect -f '{{.RestartCount}}' "$AUTH_APP_CID")
  AUTH_FRONTEND_RESTARTS=$(
    docker inspect -f '{{.RestartCount}}' "$AUTH_FRONTEND_CID"
  )
  AUTH_DB_RESTARTS=$(docker inspect -f '{{.RestartCount}}' "$AUTH_DB_CID")
  [ "$AUTH_APP_RESTARTS" = 0 ] \
    && [ "$AUTH_FRONTEND_RESTARTS" = 0 ] \
    && [ "$AUTH_DB_RESTARTS" = "${AUTHORITY_STATE[BASE_DB_RESTARTS]}" ] \
    || fatal "live release containers differ from root authority"
  local live_caddy_cid
  live_caddy_cid=$(docker inspect -f '{{.Id}}' personal-ai-assistant-caddy)
  [[ "$live_caddy_cid" =~ ^[0-9a-f]{64}$ ]] \
    || fatal "shared Caddy identity is invalid"
  if [ -e "$RECREATE_PENDING" ] || [ -L "$RECREATE_PENDING" ]; then
    case "$action" in
      inspect)
        if ! reconcile_recreate_pending "$target" "$EDGE_GENERATION" \
            "$GENERATION" "$live_caddy_cid" inspect inspect; then
          printf 'divergent-or-unknown\n'
          exit 78
        fi
        ;;
      promote|rollback)
        reconcile_recreate_pending "$target" "$EDGE_GENERATION" \
          "$GENERATION" "$live_caddy_cid" recover "$action" \
          || fatal "Caddy recreate pending intent is not exactly recoverable"
        ;;
      *)
        fatal "pending Caddy recreation requires explicit promote or rollback"
        ;;
    esac
  fi
}

render_sha256() {
  local compose_file=$1
  docker compose \
    --env-file "$ASSISTANT_ENV" \
    -f "$compose_file" \
    config --format json |
    sha256sum | cut -d' ' -f1
}

replace_hsts() {
  local source=$1
  local destination=$2
  local old_value=$3
  local new_value=$4
  python3 - "$source" "$destination" "$old_value" "$new_value" <<'PY'
import os
import re
import sys

source, destination, old_value, new_value = sys.argv[1:]
with open(source, "rb") as handle:
    raw = handle.read()
pattern = re.compile(
    rb'(?m)^([ \t]*IT_DATA_HSTS_MAX_AGE:[ \t]*["\']?)'
    + old_value.encode("ascii")
    + rb'(["\']?[ \t]*(?:#.*)?\r?)$'
)
updated, count = pattern.subn(
    lambda match: match.group(1) + new_value.encode("ascii") + match.group(2),
    raw,
)
if count != 1:
    raise SystemExit("expected exactly one HSTS assignment")
with open(destination, "xb") as handle:
    handle.write(updated)
    handle.flush()
    os.fsync(handle.fileno())
PY
}

write_state() {
  local destination=$1
  local state=$2
  printf 'HSTS_STATE=%s\n' "$state" > "$destination"
  chmod 600 "$destination"
}

validate_hsts_manifest_schema() {
  python3 - "$1" <<'PY'
import re
import sys

allowed = {
    "HSTS_SNAPSHOT_FORMAT",
    "TARGET_COMMIT",
    "GENERATION",
    "CONTROL_MANIFEST_HASH",
    "RELEASE_ID",
    "RELEASE_STATE_GENERATION",
    "RELEASE_STATE_SHA256",
    "EDGE_GENERATION",
    "EDGE_MANIFEST_SHA256",
    "EDGE_STATE_SHA256",
    "EDGE_COMPOSE_POST_SHA256",
    "EDGE_CADDYFILE_POST_SHA256",
    "ASSISTANT_COMPOSE_PRE_SHA256",
    "ASSISTANT_COMPOSE_POST_SHA256",
    "ASSISTANT_RENDER_PRE_SHA256",
    "ASSISTANT_RENDER_POST_SHA256",
    "CADDYFILE_SHA256",
    "APP_COMPOSE_SHA256",
    "ASSISTANT_HEALTH_URL_SHA256",
    "AUTH_APP_CID",
    "AUTH_APP_IMAGE",
    "AUTH_APP_RESTARTS",
    "AUTH_FRONTEND_CID",
    "AUTH_FRONTEND_IMAGE",
    "AUTH_FRONTEND_RESTARTS",
    "AUTH_DB_CID",
    "AUTH_DB_IMAGE",
    "AUTH_DB_RESTARTS",
}
values = {}
for line in open(sys.argv[1], encoding="ascii").read().splitlines():
    if line.count("=") != 1:
        raise SystemExit(1)
    key, value = line.split("=", 1)
    if key not in allowed or key in values:
        raise SystemExit(1)
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
        raise SystemExit(1)
    values[key] = value
if set(values) != allowed:
    raise SystemExit(1)
if values["HSTS_SNAPSHOT_FORMAT"] != "hsts-v120-1":
    raise SystemExit(1)
PY
}

validate_generation() {
  local generation_dir=$1
  local target=$2
  local generation=$3
  local owner
  owner=$(expected_owner)
  [ -d "$generation_dir" ] && [ ! -L "$generation_dir" ] \
    && [ "$(stat -c '%a %U:%G' "$generation_dir")" = "700 $owner" ] \
    || return 1
  local name
  for name in manifest.txt snapshot.txt SHA256SUMS state.txt; do
    safe_regular "$generation_dir/$name" \
      && [ "$(stat -c '%a' "$generation_dir/$name")" = 600 ] \
      || return 1
  done
  (
    cd "$generation_dir"
    sha256sum -c SHA256SUMS >/dev/null
  ) || return 1
  validate_hsts_manifest_schema "$generation_dir/manifest.txt" || return 1
  [ "$(sed -n 's/^TARGET_COMMIT=//p' "$generation_dir/manifest.txt")" \
    = "$target" ] || return 1
  [ "$(sed -n 's/^GENERATION=//p' "$generation_dir/manifest.txt")" \
    = "$generation" ] || return 1
  [ "$(sed -n 's/^CONTROL_MANIFEST_HASH=//p' \
    "$generation_dir/manifest.txt")" \
    = "$RUNTIME_CONTROL_MANIFEST_HASH" ] || return 1
  [ "$(sed -n 's/^RELEASE_ID=//p' "$generation_dir/manifest.txt")" \
    = "$AUTH_RELEASE_ID" ] || return 1
  [ "$(sed -n 's/^RELEASE_STATE_GENERATION=//p' \
    "$generation_dir/manifest.txt")" = "$AUTH_STATE_GENERATION" ] || return 1
  [ "$(sed -n 's/^RELEASE_STATE_SHA256=//p' \
    "$generation_dir/manifest.txt")" = "$AUTH_STATE_SHA256" ] || return 1
  [ "$(sed -n 's/^EDGE_GENERATION=//p' \
    "$generation_dir/manifest.txt")" = "$EDGE_GENERATION" ] || return 1
  [ "$(sed -n 's/^EDGE_MANIFEST_SHA256=//p' \
    "$generation_dir/manifest.txt")" \
    = "$EDGE_GENERATION_MANIFEST_SHA256" ] || return 1
  [ "$(sed -n 's/^EDGE_STATE_SHA256=//p' \
    "$generation_dir/manifest.txt")" \
    = "$EDGE_GENERATION_STATE_SHA256" ] || return 1
  [ "$(sed -n 's/^EDGE_COMPOSE_POST_SHA256=//p' \
    "$generation_dir/manifest.txt")" \
    = "$EDGE_COMPOSE_POST_SHA256" ] || return 1
  [ "$(sed -n 's/^EDGE_CADDYFILE_POST_SHA256=//p' \
    "$generation_dir/manifest.txt")" \
    = "$EDGE_CADDYFILE_POST_SHA256" ] || return 1
  verify_assistant_health_locator || return 1
  [ "$(sed -n 's/^ASSISTANT_HEALTH_URL_SHA256=//p' \
    "$generation_dir/manifest.txt")" = "$(assistant_health_sha256)" ] \
    || return 1
  [ "$(sed -n 's/^AUTH_APP_CID=//p' \
    "$generation_dir/manifest.txt")" = "$AUTH_APP_CID" ] \
    && [ "$(sed -n 's/^AUTH_APP_IMAGE=//p' \
      "$generation_dir/manifest.txt")" = "$AUTH_APP_IMAGE" ] \
    && [ "$(sed -n 's/^AUTH_APP_RESTARTS=//p' \
      "$generation_dir/manifest.txt")" = "$AUTH_APP_RESTARTS" ] \
    && [ "$(sed -n 's/^AUTH_FRONTEND_CID=//p' \
      "$generation_dir/manifest.txt")" = "$AUTH_FRONTEND_CID" ] \
    && [ "$(sed -n 's/^AUTH_FRONTEND_IMAGE=//p' \
      "$generation_dir/manifest.txt")" = "$AUTH_FRONTEND_IMAGE" ] \
    && [ "$(sed -n 's/^AUTH_FRONTEND_RESTARTS=//p' \
      "$generation_dir/manifest.txt")" = "$AUTH_FRONTEND_RESTARTS" ] \
    && [ "$(sed -n 's/^AUTH_DB_CID=//p' \
      "$generation_dir/manifest.txt")" = "$AUTH_DB_CID" ] \
    && [ "$(sed -n 's/^AUTH_DB_IMAGE=//p' \
      "$generation_dir/manifest.txt")" = "$AUTH_DB_IMAGE" ] \
    && [ "$(sed -n 's/^AUTH_DB_RESTARTS=//p' \
      "$generation_dir/manifest.txt")" = "$AUTH_DB_RESTARTS" ] \
    || return 1
}

manifest_value() {
  local manifest=$1
  local key=$2
  local value
  local count
  if count=$(grep -c "^${key}=" "$manifest"); then
    :
  else
    local grep_status=$?
    [ "$grep_status" -eq 1 ] || return "$grep_status"
    count=0
  fi
  [ "$count" -eq 1 ] || return 64
  value=$(sed -n "s/^${key}=//p" "$manifest")
  [[ "$value" =~ ^[A-Za-z0-9._:-]+$ ]] || return 64
  printf '%s\n' "$value"
}

validate_successor_lineage_schema() {
  python3 - "$1" <<'PY'
import re
import sys

allowed = {
    "LINEAGE_FORMAT",
    "TARGET_COMMIT",
    "CONTROL_MANIFEST_HASH",
    "ROOT_BASE_CADDY_CID",
    "ROOT_BASE_CADDY_IMAGE",
    "ROOT_BASE_CADDY_RESTARTS",
    "GENERATION",
    "MUTATION_DOMAIN",
    "MUTATION_GENERATION",
    "ACTION",
    "GENERATION_BASE_CADDY_CID",
    "GENERATION_BASE_CADDY_IMAGE",
    "GENERATION_BASE_CADDY_RESTARTS",
    "ACTION_BASE_CADDY_CID",
    "ACTION_BASE_CADDY_IMAGE",
    "ACTION_BASE_CADDY_RESTARTS",
    "CURRENT_CADDY_CID",
    "CURRENT_CADDY_IMAGE",
    "CURRENT_CADDY_RESTARTS",
    "ASSISTANT_COMPOSE_SHA256",
    "CADDYFILE_SHA256",
}
lines = open(sys.argv[1], encoding="ascii").read().splitlines()
values = {}
for line in lines:
    if line.count("=") != 1:
        raise SystemExit(1)
    key, value = line.split("=", 1)
    if key not in allowed or key in values:
        raise SystemExit(1)
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
        raise SystemExit(1)
    values[key] = value
if set(values) != allowed:
    raise SystemExit(1)
if values["LINEAGE_FORMAT"] != "edge-successor-v1":
    raise SystemExit(1)
if values["MUTATION_DOMAIN"] not in {"edge", "hsts"}:
    raise SystemExit(1)
if values["ACTION"] not in {"promote", "rollback"}:
    raise SystemExit(1)
PY
}

verify_successor_lineage() {
  local target=$1 edge_generation=$2 live_cid=$3
  local current_image current_restarts
  safe_regular "$SUCCESSOR_LINEAGE" \
    && [ "$(stat -c '%a' "$SUCCESSOR_LINEAGE")" = 600 ] \
    && validate_successor_lineage_schema "$SUCCESSOR_LINEAGE" \
    || return 1
  current_image=$(docker inspect -f '{{.Image}}' "$live_cid")
  current_restarts=$(docker inspect -f '{{.RestartCount}}' "$live_cid")
  [ "$(manifest_value "$SUCCESSOR_LINEAGE" TARGET_COMMIT)" = "$target" ] \
    && [ "$(manifest_value "$SUCCESSOR_LINEAGE" CONTROL_MANIFEST_HASH)" \
      = "$RUNTIME_CONTROL_MANIFEST_HASH" ] \
    && [ "$(manifest_value "$SUCCESSOR_LINEAGE" ROOT_BASE_CADDY_CID)" \
      = "${AUTHORITY_STATE[BASE_EDGE_CID]}" ] \
    && [ "$(manifest_value "$SUCCESSOR_LINEAGE" ROOT_BASE_CADDY_RESTARTS)" \
      = "${AUTHORITY_STATE[BASE_EDGE_RESTARTS]}" ] \
    && [ "$(manifest_value "$SUCCESSOR_LINEAGE" GENERATION)" \
      = "$edge_generation" ] \
    && [ "$(manifest_value "$SUCCESSOR_LINEAGE" CURRENT_CADDY_CID)" \
      = "$live_cid" ] \
    && [ "$(manifest_value "$SUCCESSOR_LINEAGE" CURRENT_CADDY_IMAGE)" \
      = "$current_image" ] \
    && [ "$(manifest_value "$SUCCESSOR_LINEAGE" CURRENT_CADDY_RESTARTS)" \
      = "$current_restarts" ] \
    && [ "$current_restarts" = 0 ] \
    && [ "$(manifest_value "$SUCCESSOR_LINEAGE" ASSISTANT_COMPOSE_SHA256)" \
      = "$(sha256sum "$COMPOSE_FILE" | cut -d' ' -f1)" ] \
    && [ "$(manifest_value "$SUCCESSOR_LINEAGE" CADDYFILE_SHA256)" \
      = "$(sha256sum "$CADDYFILE" | cut -d' ' -f1)" ]
}

publish_successor_lineage() {
  local action=$1 action_base_cid=$2
  local action_base_image=$3 action_base_restarts=$4
  local current_cid current_image current_restarts temporary status
  current_cid=$(docker inspect -f '{{.Id}}' personal-ai-assistant-caddy)
  current_image=$(docker inspect -f '{{.Image}}' "$current_cid")
  current_restarts=$(docker inspect -f '{{.RestartCount}}' "$current_cid")
  [[ "$current_cid" =~ ^[0-9a-f]{64}$ ]] \
    && [ "$current_image" = "$EDGE_CADDY_IMAGE" ] \
    && [ "$current_restarts" = 0 ] \
    || fatal "HSTS recreated Caddy successor is not exact"
  temporary=$(mktemp -- "$CONTROL_DIR/edge/.successor-lineage.XXXXXX")
  cleanup_lineage() {
    status=$?
    trap - RETURN
    if [ -n "$temporary" ] && ! rm -f -- "$temporary"; then
      [ "$status" -ne 0 ] || status=97
    fi
    return "$status"
  }
  trap cleanup_lineage RETURN
  {
    printf 'LINEAGE_FORMAT=edge-successor-v1\n'
    printf 'TARGET_COMMIT=%s\n' "$TARGET_COMMIT"
    printf 'CONTROL_MANIFEST_HASH=%s\n' "$RUNTIME_CONTROL_MANIFEST_HASH"
    printf 'ROOT_BASE_CADDY_CID=%s\n' "${AUTHORITY_STATE[BASE_EDGE_CID]}"
    printf 'ROOT_BASE_CADDY_IMAGE=%s\n' "$ROOT_BASE_CADDY_IMAGE"
    printf 'ROOT_BASE_CADDY_RESTARTS=%s\n' \
      "${AUTHORITY_STATE[BASE_EDGE_RESTARTS]}"
    printf 'GENERATION=%s\n' "$EDGE_GENERATION"
    printf 'MUTATION_DOMAIN=hsts\nMUTATION_GENERATION=%s\n' "$GENERATION"
    printf 'ACTION=%s\n' "$action"
    printf 'GENERATION_BASE_CADDY_CID=%s\n' "$EDGE_BASE_CADDY_CID"
    printf 'GENERATION_BASE_CADDY_IMAGE=%s\n' "$EDGE_BASE_CADDY_IMAGE"
    printf 'GENERATION_BASE_CADDY_RESTARTS=%s\n' \
      "$EDGE_BASE_CADDY_RESTARTS"
    printf 'ACTION_BASE_CADDY_CID=%s\n' "$action_base_cid"
    printf 'ACTION_BASE_CADDY_IMAGE=%s\n' "$action_base_image"
    printf 'ACTION_BASE_CADDY_RESTARTS=%s\n' "$action_base_restarts"
    printf 'CURRENT_CADDY_CID=%s\n' "$current_cid"
    printf 'CURRENT_CADDY_IMAGE=%s\n' "$current_image"
    printf 'CURRENT_CADDY_RESTARTS=%s\n' "$current_restarts"
    printf 'ASSISTANT_COMPOSE_SHA256=%s\n' \
      "$(sha256sum "$COMPOSE_FILE" | cut -d' ' -f1)"
    printf 'CADDYFILE_SHA256=%s\n' \
      "$(sha256sum "$CADDYFILE" | cut -d' ' -f1)"
  } > "$temporary"
  chmod 600 "$temporary"
  if [ "$TEST_MODE" != 1 ]; then chown root:root "$temporary"; fi
  validate_successor_lineage_schema "$temporary" \
    || fatal "HSTS successor lineage staging is invalid"
  sync -f "$temporary" || return $?
  mv -fT -- "$temporary" "$SUCCESSOR_LINEAGE" || return $?
  temporary=
  sync -f "$SUCCESSOR_LINEAGE" || return $?
  sync -d "$CONTROL_DIR/edge" || return $?
  trap - RETURN
}

publish_current_successor_lineage() {
  local action=$1
  local current_cid current_image current_restarts
  current_cid=$(docker inspect -f '{{.Id}}' personal-ai-assistant-caddy)
  current_image=$(docker inspect -f '{{.Image}}' "$current_cid")
  current_restarts=$(docker inspect -f '{{.RestartCount}}' "$current_cid")
  publish_successor_lineage "$action" "$current_cid" \
    "$current_image" "$current_restarts"
}

validate_recreate_pending_schema() {
  python3 - "$1" <<'PY'
import re
import sys

allowed = {
    "RECREATE_PENDING_FORMAT",
    "TARGET_COMMIT",
    "CONTROL_MANIFEST_HASH",
    "EDGE_GENERATION",
    "MUTATION_DOMAIN",
    "MUTATION_GENERATION",
    "ACTION",
    "OLD_CADDY_CID",
    "OLD_CADDY_IMAGE",
    "OLD_CADDY_RESTARTS",
    "OLD_ASSISTANT_COMPOSE_SHA256",
    "OLD_CADDYFILE_SHA256",
    "TARGET_CADDY_IMAGE",
    "TARGET_ASSISTANT_COMPOSE_SHA256",
    "TARGET_CADDYFILE_SHA256",
    "TARGET_CADDY_NETWORKS",
}
values = {}
for line in open(sys.argv[1], encoding="ascii").read().splitlines():
    if line.count("=") != 1:
        raise SystemExit(1)
    key, value = line.split("=", 1)
    if key not in allowed or key in values:
        raise SystemExit(1)
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
        raise SystemExit(1)
    values[key] = value
if set(values) != allowed:
    raise SystemExit(1)
if values["RECREATE_PENDING_FORMAT"] != "caddy-recreate-v1":
    raise SystemExit(1)
if values["MUTATION_DOMAIN"] not in {"edge", "hsts"}:
    raise SystemExit(1)
if values["ACTION"] not in {"promote", "rollback"}:
    raise SystemExit(1)
if values["TARGET_CADDY_NETWORKS"] != "dual-network-v1":
    raise SystemExit(1)
PY
}

validate_recreate_pending() {
  local target=$1 edge_generation=$2 mutation_generation=$3
  safe_regular "$RECREATE_PENDING" \
    && [ "$(stat -c '%a' "$RECREATE_PENDING")" = 600 ] \
    && validate_recreate_pending_schema "$RECREATE_PENDING" \
    && safe_regular "$SUCCESSOR_LINEAGE" \
    && [ "$(stat -c '%a' "$SUCCESSOR_LINEAGE")" = 600 ] \
    && validate_successor_lineage_schema "$SUCCESSOR_LINEAGE" \
    || return 1
  [ "$(manifest_value "$RECREATE_PENDING" TARGET_COMMIT)" = "$target" ] \
    && [ "$(manifest_value "$RECREATE_PENDING" CONTROL_MANIFEST_HASH)" \
      = "$RUNTIME_CONTROL_MANIFEST_HASH" ] \
    && [ "$(manifest_value "$RECREATE_PENDING" EDGE_GENERATION)" \
      = "$edge_generation" ] \
    && [ "$(manifest_value "$RECREATE_PENDING" MUTATION_DOMAIN)" = hsts ] \
    && [ "$(manifest_value "$RECREATE_PENDING" MUTATION_GENERATION)" \
      = "$mutation_generation" ] \
    && [ "$(manifest_value "$SUCCESSOR_LINEAGE" TARGET_COMMIT)" = "$target" ] \
    && [ "$(manifest_value "$SUCCESSOR_LINEAGE" CONTROL_MANIFEST_HASH)" \
      = "$RUNTIME_CONTROL_MANIFEST_HASH" ] \
    && [ "$(manifest_value "$SUCCESSOR_LINEAGE" GENERATION)" \
      = "$edge_generation" ] \
    && [ "$(manifest_value "$RECREATE_PENDING" TARGET_CADDY_IMAGE)" \
      = "$(manifest_value "$SUCCESSOR_LINEAGE" CURRENT_CADDY_IMAGE)" ]
}

clear_recreate_pending() {
  [ -e "$RECREATE_PENDING" ] || [ -L "$RECREATE_PENDING" ] || return 0
  safe_regular "$RECREATE_PENDING" \
    && [ "$(stat -c '%a' "$RECREATE_PENDING")" = 600 ] \
    || return 73
  rm -f -- "$RECREATE_PENDING" || return $?
  sync -d "$CONTROL_DIR/edge" || return $?
}

write_recreate_pending() {
  local action=$1 old_cid=$2 old_image=$3 old_restarts=$4
  local old_compose_hash=$5 old_caddy_hash=$6
  local target_compose_hash=$7 target_caddy_hash=$8
  local temporary='' status
  if [ -e "$RECREATE_PENDING" ] || [ -L "$RECREATE_PENDING" ]; then
    validate_recreate_pending "$TARGET_COMMIT" "$EDGE_GENERATION" \
      "$GENERATION" \
      && [ "$(manifest_value "$RECREATE_PENDING" ACTION)" = "$action" ] \
      && [ "$(manifest_value "$RECREATE_PENDING" OLD_CADDY_CID)" \
        = "$old_cid" ] \
      && [ "$(manifest_value "$RECREATE_PENDING" \
        OLD_ASSISTANT_COMPOSE_SHA256)" = "$old_compose_hash" ] \
      && [ "$(manifest_value "$RECREATE_PENDING" OLD_CADDYFILE_SHA256)" \
        = "$old_caddy_hash" ] \
      && [ "$(manifest_value "$RECREATE_PENDING" \
        TARGET_ASSISTANT_COMPOSE_SHA256)" = "$target_compose_hash" ] \
      && [ "$(manifest_value "$RECREATE_PENDING" \
        TARGET_CADDYFILE_SHA256)" = "$target_caddy_hash" ] \
      || return 73
    return 0
  fi
  safe_regular "$SUCCESSOR_LINEAGE" \
    && [ "$(stat -c '%a' "$SUCCESSOR_LINEAGE")" = 600 ] \
    && validate_successor_lineage_schema "$SUCCESSOR_LINEAGE" \
    || return 73
  temporary=$(mktemp -- "$CONTROL_DIR/edge/.recreate-pending.XXXXXX")
  cleanup_recreate_pending() {
    status=$?
    trap - RETURN
    if [ -n "$temporary" ] && ! rm -f -- "$temporary"; then
      [ "$status" -ne 0 ] || status=97
    fi
    return "$status"
  }
  trap cleanup_recreate_pending RETURN
  {
    printf 'RECREATE_PENDING_FORMAT=caddy-recreate-v1\n'
    printf 'TARGET_COMMIT=%s\n' "$TARGET_COMMIT"
    printf 'CONTROL_MANIFEST_HASH=%s\n' "$RUNTIME_CONTROL_MANIFEST_HASH"
    printf 'EDGE_GENERATION=%s\n' "$EDGE_GENERATION"
    printf 'MUTATION_DOMAIN=hsts\nMUTATION_GENERATION=%s\n' "$GENERATION"
    printf 'ACTION=%s\n' "$action"
    printf 'OLD_CADDY_CID=%s\nOLD_CADDY_IMAGE=%s\n' "$old_cid" "$old_image"
    printf 'OLD_CADDY_RESTARTS=%s\n' "$old_restarts"
    printf 'OLD_ASSISTANT_COMPOSE_SHA256=%s\n' "$old_compose_hash"
    printf 'OLD_CADDYFILE_SHA256=%s\n' "$old_caddy_hash"
    printf 'TARGET_CADDY_IMAGE=%s\n' "$EDGE_CADDY_IMAGE"
    printf 'TARGET_ASSISTANT_COMPOSE_SHA256=%s\n' "$target_compose_hash"
    printf 'TARGET_CADDYFILE_SHA256=%s\n' "$target_caddy_hash"
    printf 'TARGET_CADDY_NETWORKS=dual-network-v1\n'
  } > "$temporary"
  chmod 600 "$temporary" || return $?
  if [ "$TEST_MODE" != 1 ]; then chown root:root "$temporary" || return $?; fi
  validate_recreate_pending_schema "$temporary" || return 73
  sync -f "$temporary" || return $?
  mv -fT -- "$temporary" "$RECREATE_PENDING" || return $?
  temporary=
  sync -f "$RECREATE_PENDING" || return $?
  sync -d "$CONTROL_DIR/edge" || return $?
  trap - RETURN
}

reconcile_recreate_pending() {
  local target=$1 edge_generation=$2 mutation_generation=$3 live_cid=$4
  local mode=${5:-recover} expected_action=${6:-}
  local old_cid old_image old_restarts lineage_cid
  local live_image live_restarts networks ingress_members action
  local actual_compose actual_caddy old_compose old_caddy
  local target_compose target_caddy lineage_compose lineage_caddy
  [ -e "$RECREATE_PENDING" ] || [ -L "$RECREATE_PENDING" ] || return 0
  validate_recreate_pending "$target" "$edge_generation" \
    "$mutation_generation" || return 73
  old_cid=$(manifest_value "$RECREATE_PENDING" OLD_CADDY_CID)
  old_image=$(manifest_value "$RECREATE_PENDING" OLD_CADDY_IMAGE)
  old_restarts=$(manifest_value "$RECREATE_PENDING" OLD_CADDY_RESTARTS)
  lineage_cid=$(manifest_value "$SUCCESSOR_LINEAGE" CURRENT_CADDY_CID)
  actual_compose=$(sha256sum "$COMPOSE_FILE" | cut -d' ' -f1)
  actual_caddy=$(sha256sum "$CADDYFILE" | cut -d' ' -f1)
  old_compose=$(
    manifest_value "$RECREATE_PENDING" OLD_ASSISTANT_COMPOSE_SHA256
  )
  old_caddy=$(manifest_value "$RECREATE_PENDING" OLD_CADDYFILE_SHA256)
  target_compose=$(
    manifest_value "$RECREATE_PENDING" TARGET_ASSISTANT_COMPOSE_SHA256
  )
  target_caddy=$(
    manifest_value "$RECREATE_PENDING" TARGET_CADDYFILE_SHA256
  )
  { [ "$actual_compose" = "$old_compose" ] \
      || [ "$actual_compose" = "$target_compose" ]; } \
    && { [ "$actual_caddy" = "$old_caddy" ] \
      || [ "$actual_caddy" = "$target_caddy" ]; } \
    || return 73
  action=$(manifest_value "$RECREATE_PENDING" ACTION)
  [[ "$mode" =~ ^(inspect|recover)$ ]] || return 64
  if [ "$mode" = recover ]; then
    [ "$action" = "$expected_action" ] || return 73
  fi
  if [ "$live_cid" = "$old_cid" ]; then
    lineage_compose=$(
      manifest_value "$SUCCESSOR_LINEAGE" ASSISTANT_COMPOSE_SHA256
    )
    lineage_caddy=$(manifest_value "$SUCCESSOR_LINEAGE" CADDYFILE_SHA256)
    [ "$lineage_cid" = "$old_cid" ] \
      && [ "$(manifest_value "$SUCCESSOR_LINEAGE" MUTATION_DOMAIN)" = hsts ] \
      && [ "$(manifest_value "$SUCCESSOR_LINEAGE" MUTATION_GENERATION)" \
        = "$mutation_generation" ] \
      && [ "$(manifest_value "$SUCCESSOR_LINEAGE" ACTION)" = "$action" ] \
      && [ "$(manifest_value "$SUCCESSOR_LINEAGE" CURRENT_CADDY_IMAGE)" \
        = "$old_image" ] \
      && [ "$(manifest_value "$SUCCESSOR_LINEAGE" \
        CURRENT_CADDY_RESTARTS)" = "$old_restarts" ] \
      && { [ "$lineage_compose" = "$old_compose" ] \
        || [ "$lineage_compose" = "$actual_compose" ]; } \
      && { [ "$lineage_caddy" = "$old_caddy" ] \
        || [ "$lineage_caddy" = "$actual_caddy" ]; } \
      || return 73
    [ "$mode" = recover ] || return 0
    if [ "$actual_compose" != "$old_compose" ] \
        || [ "$actual_caddy" != "$old_caddy" ]; then
      ROOT_BASE_CADDY_IMAGE=$(
        manifest_value "$SUCCESSOR_LINEAGE" ROOT_BASE_CADDY_IMAGE
      )
      EDGE_BASE_CADDY_CID=$(
        manifest_value "$SUCCESSOR_LINEAGE" GENERATION_BASE_CADDY_CID
      )
      EDGE_BASE_CADDY_IMAGE=$(
        manifest_value "$SUCCESSOR_LINEAGE" GENERATION_BASE_CADDY_IMAGE
      )
      EDGE_BASE_CADDY_RESTARTS=$(
        manifest_value "$SUCCESSOR_LINEAGE" GENERATION_BASE_CADDY_RESTARTS
      )
      EDGE_CADDY_IMAGE=$(
        manifest_value "$RECREATE_PENDING" TARGET_CADDY_IMAGE
      )
      publish_successor_lineage "$action" "$old_cid" \
        "$old_image" "$old_restarts" || return $?
    else
      clear_recreate_pending
    fi
    return $?
  fi
  [ "$actual_compose" = "$target_compose" ] \
    && [ "$actual_caddy" = "$target_caddy" ] || return 73
  live_image=$(docker inspect -f '{{.Image}}' "$live_cid") || return $?
  live_restarts=$(docker inspect -f '{{.RestartCount}}' "$live_cid") || return $?
  [ "$(docker inspect -f '{{.State.Running}}' \
    personal-ai-assistant-caddy)" = true ] \
    && [ "$live_image" \
      = "$(manifest_value "$RECREATE_PENDING" TARGET_CADDY_IMAGE)" ] \
    && [ "$live_restarts" = 0 ] \
    || return 73
  networks=$(docker inspect -f '{{json .NetworkSettings.Networks}}' \
    personal-ai-assistant-caddy) || return $?
  json_object_has_exact_keys "$networks" \
    personal-ai-assistant-network it-spareparts-ingress || return 73
  [ "$(docker network inspect -f '{{.Internal}}' it-spareparts-ingress)" \
    = true ] || return 73
  ingress_members=$(docker network inspect \
    -f '{{json .Containers}}' it-spareparts-ingress) || return $?
  json_object_has_exact_keys "$ingress_members" "$live_cid" \
    "$AUTH_FRONTEND_CID" || return 73
  ROOT_BASE_CADDY_IMAGE=$(
    manifest_value "$SUCCESSOR_LINEAGE" ROOT_BASE_CADDY_IMAGE
  )
  EDGE_BASE_CADDY_CID=$(
    manifest_value "$SUCCESSOR_LINEAGE" GENERATION_BASE_CADDY_CID
  )
  EDGE_BASE_CADDY_IMAGE=$(
    manifest_value "$SUCCESSOR_LINEAGE" GENERATION_BASE_CADDY_IMAGE
  )
  EDGE_BASE_CADDY_RESTARTS=$(
    manifest_value "$SUCCESSOR_LINEAGE" GENERATION_BASE_CADDY_RESTARTS
  )
  EDGE_CADDY_IMAGE=$(
    manifest_value "$RECREATE_PENDING" TARGET_CADDY_IMAGE
  )
  if [ "$lineage_cid" = "$old_cid" ]; then
    [ "$(manifest_value "$SUCCESSOR_LINEAGE" CURRENT_CADDY_IMAGE)" \
      = "$old_image" ] \
      && [ "$(manifest_value "$SUCCESSOR_LINEAGE" CURRENT_CADDY_RESTARTS)" \
        = "$old_restarts" ] \
      || return 73
    [ "$mode" = recover ] || return 0
    publish_successor_lineage "$action" "$old_cid" \
      "$old_image" "$old_restarts" || return $?
  elif [ "$lineage_cid" = "$live_cid" ]; then
    [ "$(manifest_value "$SUCCESSOR_LINEAGE" MUTATION_DOMAIN)" = hsts ] \
      && [ "$(manifest_value "$SUCCESSOR_LINEAGE" MUTATION_GENERATION)" \
        = "$mutation_generation" ] \
      && [ "$(manifest_value "$SUCCESSOR_LINEAGE" ACTION)" = "$action" ] \
      && [ "$(manifest_value "$SUCCESSOR_LINEAGE" ACTION_BASE_CADDY_CID)" \
        = "$old_cid" ] \
      && [ "$(manifest_value "$SUCCESSOR_LINEAGE" ACTION_BASE_CADDY_IMAGE)" \
        = "$old_image" ] \
      && [ "$(manifest_value "$SUCCESSOR_LINEAGE" \
        ACTION_BASE_CADDY_RESTARTS)" = "$old_restarts" ] \
      && [ "$(manifest_value "$SUCCESSOR_LINEAGE" CURRENT_CADDY_IMAGE)" \
        = "$live_image" ] \
      && [ "$(manifest_value "$SUCCESSOR_LINEAGE" CURRENT_CADDY_RESTARTS)" \
        = "$live_restarts" ] \
      || return 73
    [ "$mode" = recover ] || return 0
  else
    return 73
  fi
  clear_recreate_pending || return $?
}

verify_edge_generation() {
  local target=$1
  local edge_generation=$2
  local action=$3
  local directory="$CONTROL_DIR/edge/generations/$edge_generation"
  local owner
  local name
  local key
  local -a manifest_keys=(
    EDGE_FORMAT TARGET_COMMIT GENERATION CONTROL_MANIFEST_HASH RELEASE_ID
    RELEASE_STATE_GENERATION RELEASE_STATE_SHA256 APP_COMPOSE_SHA256
    ASSISTANT_COMPOSE_PRE_SHA256 ASSISTANT_COMPOSE_POST_SHA256
    ASSISTANT_RENDER_PRE_SHA256 ASSISTANT_RENDER_POST_SHA256
    CADDYFILE_PRE_SHA256 CADDYFILE_POST_SHA256
    AUTH_APP_CID AUTH_APP_IMAGE AUTH_APP_RESTARTS
    AUTH_FRONTEND_CID AUTH_FRONTEND_IMAGE AUTH_FRONTEND_RESTARTS
    AUTH_DB_CID AUTH_DB_IMAGE AUTH_DB_RESTARTS
    AUTH_CADDY_CID AUTH_CADDY_IMAGE AUTH_CADDY_RESTARTS
  )
  owner=$(expected_owner)
  [ -d "$directory" ] && [ ! -L "$directory" ] \
    && [ "$(stat -c '%a %U:%G' "$directory")" = "700 $owner" ] \
    || fatal "promoted edge generation directory is unsafe"
  for name in manifest.txt SHA256SUMS state.txt \
      compose.pre compose.post Caddyfile.pre Caddyfile.post; do
    safe_regular "$directory/$name" \
      && [ "$(stat -c '%a' "$directory/$name")" = 600 ] \
      || fatal "promoted edge generation artifact is unsafe"
  done
  [ "$(find "$directory" -mindepth 1 -maxdepth 1 | wc -l)" -eq 7 ] \
    || fatal "promoted edge generation has unexpected artifacts"
  (
    cd "$directory"
    sha256sum -c SHA256SUMS >/dev/null
  ) || fatal "promoted edge generation checksum failed"
  [ "$(wc -l < "$directory/manifest.txt")" \
    -eq "${#manifest_keys[@]}" ] \
    || fatal "promoted edge generation manifest schema is invalid"
  for key in "${manifest_keys[@]}"; do
    [ "$(grep -c "^${key}=" "$directory/manifest.txt")" -eq 1 ] \
      || fatal "promoted edge generation manifest schema is invalid"
  done
  [ "$(manifest_value "$directory/manifest.txt" EDGE_FORMAT)" \
    = edge-v120-1 ] \
    || fatal "promoted edge generation format is invalid"
  [ "$(cat "$directory/state.txt")" = "EDGE_STATE=promoted" ] \
    || fatal "edge generation is not promoted"
  [ "$(manifest_value "$directory/manifest.txt" TARGET_COMMIT)" \
    = "$target" ] \
    && [ "$(manifest_value "$directory/manifest.txt" GENERATION)" \
      = "$edge_generation" ] \
    && [ "$(manifest_value "$directory/manifest.txt" CONTROL_MANIFEST_HASH)" \
      = "$RUNTIME_CONTROL_MANIFEST_HASH" ] \
    && [ "$(manifest_value "$directory/manifest.txt" RELEASE_ID)" \
      = "$AUTH_RELEASE_ID" ] \
    && [ "$(manifest_value "$directory/manifest.txt" RELEASE_STATE_SHA256)" \
      = "$AUTH_STATE_SHA256" ] \
    || fatal "edge generation authority differs from HSTS authority"
  EDGE_GENERATION_MANIFEST_SHA256=$(
    sha256sum "$directory/manifest.txt" | cut -d' ' -f1
  )
  EDGE_GENERATION_STATE_SHA256=$(
    sha256sum "$directory/state.txt" | cut -d' ' -f1
  )
  EDGE_COMPOSE_POST_SHA256=$(
    manifest_value "$directory/manifest.txt" \
      ASSISTANT_COMPOSE_POST_SHA256
  )
  EDGE_CADDYFILE_POST_SHA256=$(
    manifest_value "$directory/manifest.txt" CADDYFILE_POST_SHA256
  )
  EDGE_CADDY_IMAGE=$(
    manifest_value "$directory/manifest.txt" AUTH_CADDY_IMAGE
  )
  EDGE_BASE_CADDY_CID=$(
    manifest_value "$directory/manifest.txt" AUTH_CADDY_CID
  )
  EDGE_BASE_CADDY_IMAGE=$EDGE_CADDY_IMAGE
  EDGE_BASE_CADDY_RESTARTS=$(
    manifest_value "$directory/manifest.txt" AUTH_CADDY_RESTARTS
  )
  [ "$(manifest_value "$directory/manifest.txt" AUTH_APP_CID)" \
    = "$AUTH_APP_CID" ] \
    && [ "$(manifest_value "$directory/manifest.txt" AUTH_APP_IMAGE)" \
      = "$AUTH_APP_IMAGE" ] \
    && [ "$(manifest_value "$directory/manifest.txt" AUTH_APP_RESTARTS)" \
      = "$AUTH_APP_RESTARTS" ] \
    && [ "$(manifest_value "$directory/manifest.txt" AUTH_FRONTEND_CID)" \
      = "$AUTH_FRONTEND_CID" ] \
    && [ "$(manifest_value "$directory/manifest.txt" AUTH_FRONTEND_IMAGE)" \
      = "$AUTH_FRONTEND_IMAGE" ] \
    && [ "$(manifest_value "$directory/manifest.txt" AUTH_FRONTEND_RESTARTS)" \
      = "$AUTH_FRONTEND_RESTARTS" ] \
    && [ "$(manifest_value "$directory/manifest.txt" AUTH_DB_CID)" \
      = "$AUTH_DB_CID" ] \
    && [ "$(manifest_value "$directory/manifest.txt" AUTH_DB_IMAGE)" \
      = "$AUTH_DB_IMAGE" ] \
    && [ "$(manifest_value "$directory/manifest.txt" AUTH_DB_RESTARTS)" \
      = "$AUTH_DB_RESTARTS" ] \
    || fatal "edge generation runtime authority is inconsistent"
  [ "$(sha256sum "$directory/compose.post" | cut -d' ' -f1)" \
    = "$EDGE_COMPOSE_POST_SHA256" ] \
    && [ "$(sha256sum "$directory/Caddyfile.post" | cut -d' ' -f1)" \
      = "$EDGE_CADDYFILE_POST_SHA256" ] \
    || fatal "edge generation post snapshots are inconsistent"
  [ "$(sha256sum "$CADDYFILE" | cut -d' ' -f1)" \
    = "$EDGE_CADDYFILE_POST_SHA256" ] \
    || fatal "live Caddyfile differs from promoted edge generation"
  local live_caddy_cid
  live_caddy_cid=$(docker inspect -f '{{.Id}}' personal-ai-assistant-caddy)
  local live_caddy_image live_caddy_restarts
  live_caddy_image=$(docker inspect -f '{{.Image}}' "$live_caddy_cid")
  live_caddy_restarts=$(docker inspect -f '{{.RestartCount}}' "$live_caddy_cid")
  [[ "$live_caddy_cid" =~ ^[0-9a-f]{64}$ ]] \
    && [ "$live_caddy_image" \
      = "$EDGE_CADDY_IMAGE" ] \
    && [ "$live_caddy_restarts" = 0 ] \
    || fatal "live Caddy runtime differs from promoted edge generation"
  if [ "$live_caddy_cid" = "${AUTHORITY_STATE[BASE_EDGE_CID]}" ]; then
    [ "$EDGE_BASE_CADDY_CID" = "${AUTHORITY_STATE[BASE_EDGE_CID]}" ] \
      && [ "$EDGE_BASE_CADDY_RESTARTS" \
      = "${AUTHORITY_STATE[BASE_EDGE_RESTARTS]}" ] \
      || fatal "edge generation base differs from root authority"
    ROOT_BASE_CADDY_IMAGE=$live_caddy_image
  else
    if [ "$action" != inspect ] \
        || { [ ! -e "$RECREATE_PENDING" ] \
          && [ ! -L "$RECREATE_PENDING" ]; }; then
      verify_successor_lineage "$target" "$edge_generation" "$live_caddy_cid" \
        || fatal "live Caddy lacks exact successor lineage"
    fi
    [ "$(manifest_value "$SUCCESSOR_LINEAGE" GENERATION_BASE_CADDY_CID)" \
      = "$EDGE_BASE_CADDY_CID" ] \
      && [ "$(manifest_value "$SUCCESSOR_LINEAGE" \
        GENERATION_BASE_CADDY_IMAGE)" = "$EDGE_BASE_CADDY_IMAGE" ] \
      && [ "$(manifest_value "$SUCCESSOR_LINEAGE" \
        GENERATION_BASE_CADDY_RESTARTS)" = "$EDGE_BASE_CADDY_RESTARTS" ] \
      || fatal "successor lineage differs from edge generation base"
    ROOT_BASE_CADDY_IMAGE=$(
      manifest_value "$SUCCESSOR_LINEAGE" ROOT_BASE_CADDY_IMAGE
    )
  fi
  if [ "$action" = prepare ]; then
    [ "$(sha256sum "$COMPOSE_FILE" | cut -d' ' -f1)" \
      = "$EDGE_COMPOSE_POST_SHA256" ] \
      || fatal "HSTS prepare is not based on promoted edge generation"
  fi
}

state_value() {
  local state_file=$1
  safe_regular "$state_file" \
    && [ "$(stat -c '%a' "$state_file")" = 600 ] || return 64
  [ "$(grep -c '^HSTS_STATE=' "$state_file")" -eq 1 ] || return 64
  local value
  value=$(sed -n 's/^HSTS_STATE=//p' "$state_file")
  [[ "$value" =~ ^(prepared|promoted|rolled_back)$ ]] || return 64
  printf '%s\n' "$value"
}

publish_state() {
  local generation_dir=$1
  local value=$2
  local temporary='' status
  temporary=$(mktemp -- "$generation_dir/.state.XXXXXX")
  cleanup_state() {
    status=$?
    trap - RETURN
    if [ -n "$temporary" ] && ! rm -f -- "$temporary"; then
      [ "$status" -ne 0 ] || status=97
    fi
    return "$status"
  }
  trap cleanup_state RETURN
  write_state "$temporary" "$value"
  if [ "$TEST_MODE" != 1 ]; then
    chown root:root "$temporary"
  fi
  sync -f "$temporary" || return $?
  mv -fT -- "$temporary" "$generation_dir/state.txt" || return $?
  temporary=
  sync -f "$generation_dir/state.txt" || return $?
  sync -d "$generation_dir" || return $?
  trap - RETURN
}

json_object_has_exact_keys() {
  python3 - "$@" <<'PY'
import json
import sys

try:
    value = json.loads(sys.argv[1])
except (json.JSONDecodeError, TypeError):
    raise SystemExit(1)
if not isinstance(value, dict) or set(value) != set(sys.argv[2:]):
    raise SystemExit(1)
PY
}

probe_json_health() {
  local endpoint=$1
  local expected_status=$2
  local expected_kind=$3
  case "$expected_status:$expected_kind" in
    ok:app|ok:db|ready:assistant) ;;
    *) return 64 ;;
  esac
  curl --noproxy '*' --proto '=https' --tlsv1.2 \
    --connect-timeout 5 --max-time 15 --max-redirs 0 \
    -fsS --write-out $'\n%{http_code}\n%{content_type}' "$endpoint" |
    python3 -c '
import json
import sys

raw = sys.stdin.buffer.read()
body_and_status, separator, content_type = raw.rpartition(b"\n")
if not separator:
    raise SystemExit(1)
body, separator, status = body_and_status.rpartition(b"\n")
if not separator:
    raise SystemExit(1)
try:
    status_code = int(status.decode("ascii", "strict"))
except (UnicodeDecodeError, ValueError):
    raise SystemExit(1)
if not 200 <= status_code < 300:
    raise SystemExit(1)
mime = content_type.decode("ascii", "strict").split(";", 1)[0].strip().lower()
if mime != "application/json":
    raise SystemExit(1)
try:
    payload = json.loads(body)
except (json.JSONDecodeError, UnicodeDecodeError):
    raise SystemExit(1)
if not isinstance(payload, dict) or payload.get("status") != sys.argv[1]:
    raise SystemExit(1)
if sys.argv[2] == "db" and payload.get("db") != "reachable":
    raise SystemExit(1)
' "$expected_status" "$expected_kind"
}

verify_private_host_address() {
  ip -j address show | python3 -c '
import json
import sys

records = [
    (
        info.get("family"),
        info.get("local"),
        info.get("prefixlen"),
        info.get("scope"),
    )
    for interface in json.load(sys.stdin)
    for info in interface.get("addr_info", [])
    if info.get("local") == "10.0.0.11"
]
if records != [("inet", "10.0.0.11", 22, "global")]:
    raise SystemExit(1)
' || fatal "host does not own exact private 10.0.0.11/22 address"
}

verify_runtime() {
  local expected_hsts=$1
  local assistant_health
  local headers
  local hsts_headers
  local app_cid
  local caddy_cid
  local db_cid
  local frontend_cid
  local ingress_members
  local listeners
  local networks
  local service_cid
  local service_networks
  verify_assistant_health_locator \
    || fatal "assistant health locator is unsafe"
  verify_private_host_address
  assistant_health=$(cat "$ASSISTANT_HEALTH_FILE")
  [ "$assistant_health" = "$ASSISTANT_HEALTH_URL" ] \
    || fatal "assistant health locator is invalid"
  docker inspect -f '{{.State.Running}}' personal-ai-assistant-caddy |
    grep -Fx true >/dev/null \
    || fatal "shared Caddy is not running"
  networks=$(
    docker inspect -f '{{json .NetworkSettings.Networks}}' \
      personal-ai-assistant-caddy
  ) || fatal "cannot inspect shared Caddy networks"
  json_object_has_exact_keys "$networks" \
    personal-ai-assistant-network it-spareparts-ingress \
    || fatal "shared Caddy network invariants failed"
  [ "$(
    docker network inspect -f '{{.Internal}}' it-spareparts-ingress
  )" = true ] || fatal "IT ingress network is not internal"
  frontend_cid=$(
    docker compose \
      --project-name it-spareparts \
      --env-file "$APP_ENV" \
      -f "$APP_COMPOSE" ps -q frontend
  )
  app_cid=$(
    docker compose \
      --project-name it-spareparts \
      --env-file "$APP_ENV" \
      -f "$APP_COMPOSE" ps -q app
  )
  db_cid=$(
    docker compose \
      --project-name it-spareparts \
      --env-file "$APP_ENV" \
      -f "$APP_COMPOSE" ps -q db
  )
  [[ "$frontend_cid" =~ ^[0-9a-f]{64}$ ]] \
    && [[ "$app_cid" =~ ^[0-9a-f]{64}$ ]] \
    && [[ "$db_cid" =~ ^[0-9a-f]{64}$ ]] \
    || fatal "IT container identity is incomplete"
  caddy_cid=$(
    docker inspect -f '{{.Id}}' personal-ai-assistant-caddy
  ) || fatal "cannot inspect shared Caddy identity"
  [[ "$caddy_cid" =~ ^[0-9a-f]{64}$ ]] \
    || fatal "shared Caddy identity is incomplete"
  service_networks=$(
    docker inspect -f '{{json .NetworkSettings.Networks}}' "$frontend_cid"
  ) || fatal "cannot inspect frontend networks"
  json_object_has_exact_keys "$service_networks" \
    it-spareparts_default it-spareparts-ingress \
    || fatal "frontend is not attached to ingress"
  for service_cid in "$app_cid" "$db_cid"; do
    service_networks=$(
      docker inspect -f '{{json .NetworkSettings.Networks}}' "$service_cid"
    ) || fatal "cannot inspect internal service networks"
    json_object_has_exact_keys "$service_networks" it-spareparts_default \
      || fatal "app or db is exposed to ingress"
  done
  ingress_members=$(
    docker network inspect -f '{{json .Containers}}' it-spareparts-ingress
  ) || fatal "cannot inspect ingress members"
  json_object_has_exact_keys "$ingress_members" "$caddy_cid" "$frontend_cid" \
    || fatal "IT ingress membership is not exact"
  docker exec personal-ai-assistant-caddy \
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile \
    >/dev/null || fatal "shared Caddy validation failed"
  probe_json_health "$assistant_health" ready assistant \
    || fatal "original assistant health failed"
  curl --noproxy '*' --proto '=http' \
    --connect-timeout 5 --max-time 15 --max-redirs 0 \
    -fsS http://127.0.0.1:8080/ >/dev/null \
    || fatal "IT loopback frontend health failed"
  headers=$(
    curl --noproxy '*' --proto '=https' --tlsv1.2 \
      --connect-timeout 5 --max-time 15 --max-redirs 0 \
      -fsS -D - -o /dev/null "$IT_URL"
  ) \
    || fatal "IT HTTPS probe failed"
  hsts_headers=$(
    printf '%s' "$headers" |
    tr '[:upper:]' '[:lower:]' |
    tr -d '\r' |
      grep '^strict-transport-security:' || true
  )
  [ "$hsts_headers" = \
    "strict-transport-security: max-age=$expected_hsts" ] \
    || fatal "IT HSTS runtime value mismatch"
  probe_json_health "${IT_URL%/}/health" ok app \
    || fatal "IT HTTPS health is not semantic JSON"
  probe_json_health "${IT_URL%/}/health/db" ok db \
    || fatal "IT HTTPS DB health is not semantic JSON"
  [ "$(docker compose --env-file "$ASSISTANT_ENV" -f "$COMPOSE_FILE" \
    port caddy 8080)" = 10.0.0.11:8080 ] \
    || fatal "legacy edge binding is not exact"
  headers=$(
    curl --noproxy '*' --proto '=http' \
      --connect-timeout 5 --max-time 15 --max-redirs 0 \
      -sS -D - -o /dev/null \
      "http://10.0.0.11:8080/edge-check/path?scope=1"
  ) || fatal "legacy edge redirect probe failed"
  [ "$(printf '%s' "$headers" | tr -d '\r' \
    | sed -n 's/^[Ll]ocation: *//p')" \
    = "https://hbzgc.icu/edge-check/path?scope=1" ] \
    || fatal "legacy edge redirect target changed"
  printf '%s' "$headers" | tr -d '\r' \
    | grep -Fx 'HTTP/1.1 308 Permanent Redirect' >/dev/null \
    || fatal "legacy edge redirect status changed"
  for method in POST PUT PATCH DELETE; do
    headers=$(
      curl --noproxy '*' --proto '=http' \
        --connect-timeout 5 --max-time 15 --max-redirs 0 \
        -sS -X "$method" -D - -o /dev/null \
        "http://10.0.0.11:8080/edge-check/path?scope=1"
    ) || fatal "legacy edge unsafe-method probe failed"
    printf '%s' "$headers" | tr -d '\r' \
      | grep -Fx 'HTTP/1.1 405 Method Not Allowed' >/dev/null \
      || fatal "legacy edge unsafe method changed"
    [ "$(printf '%s' "$headers" | tr '[:upper:]' '[:lower:]' \
      | grep -c '^set-cookie:' || true)" -eq 0 ] \
      || fatal "legacy edge unsafe method emitted a cookie"
  done
  headers=$(
    curl --noproxy '*' --proto '=http' \
      --connect-timeout 5 --max-time 15 --max-redirs 0 \
      -sS -D - -o /dev/null \
      "${IT_URL/https:/http:}edge-check/path?scope=1"
  ) || fatal "canonical HTTP redirect probe failed"
  [ "$(printf '%s' "$headers" | tr -d '\r' \
    | sed -n 's/^[Ll]ocation: *//p')" \
    = "${IT_URL}edge-check/path?scope=1" ] \
    || fatal "canonical HTTP redirect target changed"
  printf '%s' "$headers" | tr -d '\r' \
    | grep -Fx 'HTTP/1.1 308 Permanent Redirect' >/dev/null \
    || fatal "canonical HTTP redirect status changed"
  listeners=$(ss -H -ltnp '( sport = :8080 )') \
    || fatal "cannot verify port 8080 listeners"
  [ "$(printf '%s\n' "$listeners" | awk '{print $4}' | LC_ALL=C sort)" \
    = "$(printf '10.0.0.11:8080\n127.0.0.1:8080\n' | LC_ALL=C sort)" ] \
    || fatal "port 8080 listener set changed"
  printf '%s\n' "$listeners" \
    | grep '10.0.0.11:8080.*docker-proxy' >/dev/null \
    || fatal "legacy edge listener is not Docker-owned"
}

verify_bound_hashes() {
  local generation_dir=$1
  local side=$2
  local expected_compose
  local expected_render
  local expected_caddy
  local expected_app
  expected_compose=$(
    manifest_value "$generation_dir/manifest.txt" \
      "ASSISTANT_COMPOSE_${side}_SHA256"
  ) || return $?
  expected_render=$(
    manifest_value "$generation_dir/manifest.txt" \
      "ASSISTANT_RENDER_${side}_SHA256"
  ) || return $?
  expected_caddy=$(
    manifest_value "$generation_dir/manifest.txt" CADDYFILE_SHA256
  ) || return $?
  expected_app=$(
    manifest_value "$generation_dir/manifest.txt" APP_COMPOSE_SHA256
  ) || return $?
  [ "$(sha256sum "$COMPOSE_FILE" | cut -d' ' -f1)" = "$expected_compose" ] \
    && [ "$(render_sha256 "$COMPOSE_FILE")" = "$expected_render" ] \
    && [ "$(sha256sum "$CADDYFILE" | cut -d' ' -f1)" = "$expected_caddy" ] \
    && [ "$(sha256sum "$APP_COMPOSE" | cut -d' ' -f1)" = "$expected_app" ]
}

rename_exchange() {
  python3 - "$1" "$2" <<'PY'
import ctypes
import os
import sys

AT_FDCWD = -100
RENAME_EXCHANGE = 2
libc = ctypes.CDLL(None, use_errno=True)
renameat2 = libc.renameat2
renameat2.argtypes = (
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_uint,
)
renameat2.restype = ctypes.c_int
left = os.fsencode(sys.argv[1])
right = os.fsencode(sys.argv[2])
if renameat2(AT_FDCWD, left, AT_FDCWD, right, RENAME_EXCHANGE) != 0:
    error = ctypes.get_errno()
    raise OSError(error, os.strerror(error))
PY
}

hsts_cas_path() {
  local action=$1
  printf '%s/.hsts-cas-%s-%s-compose\n' \
    "$ASSISTANT_DIR" "$GENERATION" "$action"
}

safe_hsts_cas_file() {
  local path=$1
  safe_regular "$path" && [ "$(stat -c '%a' "$path")" -eq 600 ]
}

install_hsts_candidate() {
  local old_value=$1
  local new_value=$2
  local candidate_hash=$3
  local candidate_render=$4
  local expected_live_hash=$5
  local expected_live_render=$6
  local action=$7
  local candidate
  local status keep=0
  local candidate_identity live_identity swapped_identity
  local swapped_hash swapped_render
  candidate=$(hsts_cas_path "$action")
  [ ! -e "$candidate" ] && [ ! -L "$candidate" ] || return 73
  cleanup_candidate() {
    status=$?
    trap - RETURN
    if [ "$keep" != 1 ] && [ -n "$candidate" ] \
        && ! rm -f -- "$candidate"; then
      [ "$status" -ne 0 ] || status=97
    fi
    return "$status"
  }
  trap cleanup_candidate RETURN
  replace_hsts "$COMPOSE_FILE" "$candidate" "$old_value" "$new_value" \
    || return $?
  chmod "$(stat -c '%a' "$COMPOSE_FILE")" "$candidate"
  if [ "$TEST_MODE" != 1 ]; then
    chown root:root "$candidate"
  fi
  [ "$(sha256sum "$candidate" | cut -d' ' -f1)" = "$candidate_hash" ] \
    || return 73
  [ "$(render_sha256 "$candidate")" = "$candidate_render" ] || return 73
  sync -f "$candidate" || return $?
  candidate_identity=$(stat -Lc '%d:%i' "$candidate")
  live_identity=$(stat -Lc '%d:%i' "$COMPOSE_FILE")
  [ "$(sha256sum "$COMPOSE_FILE" | cut -d' ' -f1)" \
    = "$expected_live_hash" ] \
    && [ "$(render_sha256 "$COMPOSE_FILE")" = "$expected_live_render" ] \
    || return 73
  if [ "$TEST_MODE" = 1 ] \
      && { [ "${HSTS_TEST_FAILPOINT:-}" = cas-before-rename ] \
        || [ "${HSTS_TEST_FAILPOINT:-}" \
          = cas-after-live-check-before-exchange ]; }; then
    printf '# concurrent-cas-writer\n' >> "$COMPOSE_FILE"
  fi
  rename_exchange "$candidate" "$COMPOSE_FILE" || return $?
  swapped_identity=$(stat -Lc '%d:%i' "$candidate")
  swapped_hash=$(sha256sum "$candidate" | cut -d' ' -f1)
  swapped_render=$(render_sha256 "$candidate")
  if [ "$(stat -Lc '%d:%i' "$COMPOSE_FILE")" \
        != "$candidate_identity" ] \
      || [ "$(sha256sum "$COMPOSE_FILE" | cut -d' ' -f1)" \
        != "$candidate_hash" ] \
      || [ "$(render_sha256 "$COMPOSE_FILE")" != "$candidate_render" ] \
      || [ "$swapped_identity" != "$live_identity" ] \
      || [ "$swapped_hash" != "$expected_live_hash" ] \
      || [ "$swapped_render" != "$expected_live_render" ]; then
    if [ "$(stat -Lc '%d:%i' "$COMPOSE_FILE")" \
          != "$candidate_identity" ] \
        || [ "$(stat -Lc '%d:%i' "$candidate")" \
          != "$swapped_identity" ]; then
      keep=1
      printf 'FATAL: shared Caddy CAS inode pairing changed; retained %s\n' \
        "$candidate" >&2
      return 97
    fi
    rename_exchange "$candidate" "$COMPOSE_FILE" || {
      keep=1
      return 97
    }
    [ "$(stat -Lc '%d:%i' "$COMPOSE_FILE")" = "$swapped_identity" ] \
      && [ "$(sha256sum "$COMPOSE_FILE" | cut -d' ' -f1)" \
        = "$swapped_hash" ] \
      && [ "$(render_sha256 "$COMPOSE_FILE")" = "$swapped_render" ] \
      && [ "$(stat -Lc '%d:%i' "$candidate")" = "$candidate_identity" ] \
      && [ "$(sha256sum "$candidate" | cut -d' ' -f1)" \
        = "$candidate_hash" ] || {
          keep=1
          return 97
        }
    return 73
  fi
  sync -f "$COMPOSE_FILE" || return $?
  sync -f "$candidate" || return $?
  sync -d "$ASSISTANT_DIR" || return $?
  keep=1
  trap - RETURN
}

finalize_hsts_cas_backup() {
  local action=$1 expected_hash=$2 expected_render=$3
  local backup
  backup=$(hsts_cas_path "$action")
  if [ ! -e "$backup" ] && [ ! -L "$backup" ]; then
    return 0
  fi
  safe_hsts_cas_file "$backup" \
    && [ "$(sha256sum "$backup" | cut -d' ' -f1)" = "$expected_hash" ] \
    && [ "$(render_sha256 "$backup")" = "$expected_render" ] \
    || return 73
  rm -f -- "$backup" || return $?
  sync -d "$ASSISTANT_DIR" || return $?
}

recreate_caddy() {
  local action=$1
  local action_base_cid action_base_image action_base_restarts
  validate_recreate_pending "$TARGET_COMMIT" "$EDGE_GENERATION" \
    "$GENERATION" || return 73
  [ "$(manifest_value "$RECREATE_PENDING" ACTION)" = "$action" ] || return 73
  action_base_cid=$(manifest_value "$RECREATE_PENDING" OLD_CADDY_CID)
  action_base_image=$(manifest_value "$RECREATE_PENDING" OLD_CADDY_IMAGE)
  action_base_restarts=$(
    manifest_value "$RECREATE_PENDING" OLD_CADDY_RESTARTS
  )
  [ "$(docker inspect -f '{{.Id}}' personal-ai-assistant-caddy)" \
    = "$action_base_cid" ] || return 73
  if [ "$TEST_MODE" = 1 ] \
      && [ "${HSTS_TEST_FAILPOINT:-}" = before-caddy-recreate ]; then
    return 74
  fi
  docker compose \
    --env-file "$ASSISTANT_ENV" \
    -f "$COMPOSE_FILE" \
    up -d --no-deps --force-recreate caddy >/dev/null || return $?
  if [ "$TEST_MODE" = 1 ] \
      && [ "${HSTS_TEST_FAILPOINT:-}" \
      = after-caddy-recreate-before-lineage ]; then
    return 74
  fi
  publish_successor_lineage "$action" "$action_base_cid" \
    "$action_base_image" "$action_base_restarts" || return $?
  if [ "$TEST_MODE" = 1 ] \
      && [ "${HSTS_TEST_FAILPOINT:-}" \
      = after-successor-lineage-before-intent-clear ]; then
    return 74
  fi
  clear_recreate_pending || return $?
}

begin_recreate_transaction() {
  local action=$1 old_compose=$2 target_compose=$3 caddy_hash=$4
  local cid image restarts
  if [ -e "$RECREATE_PENDING" ] || [ -L "$RECREATE_PENDING" ]; then
    validate_recreate_pending "$TARGET_COMMIT" "$EDGE_GENERATION" \
      "$GENERATION" || return 73
    return 0
  fi
  publish_current_successor_lineage "$action" || return $?
  cid=$(docker inspect -f '{{.Id}}' personal-ai-assistant-caddy)
  image=$(docker inspect -f '{{.Image}}' "$cid")
  restarts=$(docker inspect -f '{{.RestartCount}}' "$cid")
  write_recreate_pending "$action" "$cid" "$image" "$restarts" \
    "$old_compose" "$caddy_hash" "$target_compose" "$caddy_hash"
}

promote_generation() {
  local target=$1
  local generation=$2
  local generation_dir="$GENERATIONS_DIR/$generation"
  local current_state
  local pre_hash
  local pre_render
  local post_hash
  local post_render
  validate_generation "$generation_dir" "$target" "$generation" \
    || fatal "HSTS generation validation failed"
  current_state=$(state_value "$generation_dir/state.txt") \
    || fatal "HSTS generation state is invalid"
  if verify_bound_hashes "$generation_dir" POST 2>/dev/null; then
    if [ "$current_state" = prepared ] \
        && ! (verify_runtime "$HSTS_POST") >/dev/null 2>&1; then
      recreate_caddy promote \
        || fatal "interrupted HSTS promotion could not recreate Caddy"
    fi
    verify_runtime "$HSTS_POST"
    pre_hash=$(
      manifest_value "$generation_dir/manifest.txt" \
        ASSISTANT_COMPOSE_PRE_SHA256
    )
    pre_render=$(
      manifest_value "$generation_dir/manifest.txt" \
        ASSISTANT_RENDER_PRE_SHA256
    )
    finalize_hsts_cas_backup promote "$pre_hash" "$pre_render" \
      || fatal "HSTS promotion CAS backup finalization failed"
    [ "$current_state" = promoted ] \
      || publish_state "$generation_dir" promoted
    printf 'HSTS_PROMOTE_OK generation=%s idempotent=1\n' "$generation"
    return 0
  fi
  verify_bound_hashes "$generation_dir" PRE \
    || fatal "HSTS promotion CAS precondition mismatch"
  [ "$current_state" = prepared ] \
    || fatal "HSTS promotion state is not prepared"
  post_hash=$(
    manifest_value "$generation_dir/manifest.txt" \
      ASSISTANT_COMPOSE_POST_SHA256
  )
  post_render=$(
    manifest_value "$generation_dir/manifest.txt" \
      ASSISTANT_RENDER_POST_SHA256
  )
  pre_hash=$(
    manifest_value "$generation_dir/manifest.txt" \
      ASSISTANT_COMPOSE_PRE_SHA256
  )
  pre_render=$(
    manifest_value "$generation_dir/manifest.txt" \
      ASSISTANT_RENDER_PRE_SHA256
  )
  begin_recreate_transaction promote "$pre_hash" "$post_hash" \
    "$(manifest_value "$generation_dir/manifest.txt" CADDYFILE_SHA256)" \
    || fatal "HSTS promotion intent could not be persisted"
  install_hsts_candidate "$HSTS_PRE" "$HSTS_POST" \
    "$post_hash" "$post_render" "$pre_hash" "$pre_render" promote \
    || fatal "HSTS promotion candidate failed CAS"
  if [ "$TEST_MODE" = 1 ] \
      && [ "${HSTS_TEST_FAILPOINT:-}" \
      = after-config-cas-before-lineage ]; then
    return 74
  fi
  publish_current_successor_lineage promote
  recreate_caddy promote || fatal "HSTS promotion could not recreate Caddy"
  if [ "$TEST_MODE" = 1 ] \
      && [ "${HSTS_TEST_FAILPOINT:-}" \
      = after-caddy-recreate-before-state ]; then
    return 74
  fi
  verify_bound_hashes "$generation_dir" POST \
    || fatal "HSTS promotion changed unrelated configuration"
  verify_runtime "$HSTS_POST"
  finalize_hsts_cas_backup promote "$pre_hash" "$pre_render" \
    || fatal "HSTS promotion CAS backup finalization failed"
  publish_state "$generation_dir" promoted
  printf 'HSTS_PROMOTE_OK generation=%s idempotent=0\n' "$generation"
}

rollback_generation() {
  local target=$1
  local generation=$2
  local generation_dir="$GENERATIONS_DIR/$generation"
  local current_state
  local pre_hash
  local pre_render
  local post_hash
  local post_render
  validate_generation "$generation_dir" "$target" "$generation" \
    || fatal "HSTS generation validation failed"
  current_state=$(state_value "$generation_dir/state.txt") \
    || fatal "HSTS generation state is invalid"
  if verify_bound_hashes "$generation_dir" PRE 2>/dev/null; then
    [ "$current_state" = rolled_back ] || [ "$current_state" = promoted ] \
      || fatal "pre-HSTS configuration is not a rollback state"
    if [ "$current_state" = promoted ] \
        && ! (verify_runtime "$HSTS_PRE") >/dev/null 2>&1; then
      recreate_caddy rollback \
        || fatal "interrupted HSTS rollback could not recreate Caddy"
    fi
    verify_runtime "$HSTS_PRE"
    post_hash=$(
      manifest_value "$generation_dir/manifest.txt" \
        ASSISTANT_COMPOSE_POST_SHA256
    )
    post_render=$(
      manifest_value "$generation_dir/manifest.txt" \
        ASSISTANT_RENDER_POST_SHA256
    )
    finalize_hsts_cas_backup rollback "$post_hash" "$post_render" \
      || fatal "HSTS rollback CAS backup finalization failed"
    [ "$current_state" = rolled_back ] \
      || publish_state "$generation_dir" rolled_back
    printf 'HSTS_ROLLBACK_OK generation=%s idempotent=1\n' "$generation"
    return 0
  fi
  verify_bound_hashes "$generation_dir" POST \
    || fatal "HSTS rollback CAS precondition mismatch"
  [ "$current_state" = promoted ] || [ "$current_state" = prepared ] \
    || fatal "HSTS rollback state is invalid"
  pre_hash=$(
    manifest_value "$generation_dir/manifest.txt" \
      ASSISTANT_COMPOSE_PRE_SHA256
  )
  pre_render=$(
    manifest_value "$generation_dir/manifest.txt" \
      ASSISTANT_RENDER_PRE_SHA256
  )
  post_hash=$(
    manifest_value "$generation_dir/manifest.txt" \
      ASSISTANT_COMPOSE_POST_SHA256
  )
  post_render=$(
    manifest_value "$generation_dir/manifest.txt" \
      ASSISTANT_RENDER_POST_SHA256
  )
  begin_recreate_transaction rollback "$post_hash" "$pre_hash" \
    "$(manifest_value "$generation_dir/manifest.txt" CADDYFILE_SHA256)" \
    || fatal "HSTS rollback intent could not be persisted"
  install_hsts_candidate "$HSTS_POST" "$HSTS_PRE" \
    "$pre_hash" "$pre_render" "$post_hash" "$post_render" rollback \
    || fatal "HSTS rollback candidate failed CAS"
  if [ "$TEST_MODE" = 1 ] \
      && [ "${HSTS_TEST_FAILPOINT:-}" \
      = after-config-cas-before-lineage ]; then
    return 74
  fi
  publish_current_successor_lineage rollback
  recreate_caddy rollback || fatal "HSTS rollback could not recreate Caddy"
  if [ "$TEST_MODE" = 1 ] \
      && [ "${HSTS_TEST_FAILPOINT:-}" \
      = after-caddy-recreate-before-state ]; then
    return 74
  fi
  verify_bound_hashes "$generation_dir" PRE \
    || fatal "HSTS rollback changed unrelated configuration"
  verify_runtime "$HSTS_PRE"
  finalize_hsts_cas_backup rollback "$post_hash" "$post_render" \
    || fatal "HSTS rollback CAS backup finalization failed"
  publish_state "$generation_dir" rolled_back
  printf 'HSTS_ROLLBACK_OK generation=%s idempotent=0\n' "$generation"
}

inspect_generation() {
  local target=$1
  local generation=$2
  local generation_dir="$GENERATIONS_DIR/$generation"
  local current_state pending_action
  validate_generation "$generation_dir" "$target" "$generation" \
    || {
      printf 'divergent-or-unknown\n'
      return 78
    }
  current_state=$(state_value "$generation_dir/state.txt") || {
    printf 'divergent-or-unknown\n'
    return 78
  }
  if [ -e "$RECREATE_PENDING" ] || [ -L "$RECREATE_PENDING" ]; then
    pending_action=$(manifest_value "$RECREATE_PENDING" ACTION) || {
      printf 'divergent-or-unknown\n'
      return 78
    }
    case "$pending_action" in
      promote) printf 'exact-promote-pending\n' ;;
      rollback) printf 'exact-rollback-pending\n' ;;
      *) printf 'divergent-or-unknown\n'; return 78 ;;
    esac
    return 0
  fi
  if verify_bound_hashes "$generation_dir" POST 2>/dev/null; then
    if (verify_runtime "$HSTS_POST") >/dev/null 2>&1 \
        && { [ "$current_state" = promoted ] \
          || [ "$current_state" = prepared ]; }; then
      printf 'exact-promoted\n'
      return 0
    fi
    if [ "$current_state" = prepared ]; then
      printf 'exact-promote-pending\n'
      return 0
    fi
  elif verify_bound_hashes "$generation_dir" PRE 2>/dev/null; then
    if ! (verify_runtime "$HSTS_PRE") >/dev/null 2>&1; then
      if [ "$current_state" = promoted ]; then
        printf 'exact-rollback-pending\n'
        return 0
      fi
      printf 'divergent-or-unknown\n'
      return 78
    fi
    case "$current_state" in
      prepared)
        printf 'exact-pre\n'
        return 0
        ;;
      rolled_back)
        printf 'exact-rolled-back\n'
        return 0
        ;;
      promoted)
        printf 'exact-rolled-back\n'
        return 0
        ;;
    esac
  fi
  printf 'divergent-or-unknown\n'
  return 78
}

prepare_generation() {
  local target=$1
  local generation=$2
  local generation_dir="$GENERATIONS_DIR/$generation"
  local staging=
  local candidate=
  local owner
  local existing_state
  local observed_state
  local pre_compose_hash
  local post_compose_hash
  local pre_render_hash
  local post_render_hash
  local caddy_hash
  local app_compose_hash
  owner=$(expected_owner)
  ensure_assistant_health_locator \
    || fatal "assistant health locator is unsafe or mismatched"

  if [ -e "$generation_dir" ] || [ -L "$generation_dir" ]; then
    validate_generation "$generation_dir" "$target" "$generation" \
      || fatal "existing HSTS generation is unsafe or different"
    existing_state=$(state_value "$generation_dir/state.txt") \
      || fatal "existing HSTS generation state is invalid"
    observed_state=$(inspect_generation "$target" "$generation") \
      || fatal "existing HSTS generation does not match live state"
    case "$existing_state:$observed_state" in
      prepared:exact-pre|promoted:exact-promoted|\
      rolled_back:exact-rolled-back) ;;
      *) fatal "existing HSTS generation state is inconsistent" ;;
    esac
    printf 'HSTS_PREPARE_OK generation=%s idempotent=1\n' "$generation"
    return 0
  fi

  safe_regular "$COMPOSE_FILE" || fatal "assistant compose is unsafe"
  safe_regular "$CADDYFILE" || fatal "assistant Caddyfile is unsafe"
  safe_regular "$APP_COMPOSE" || fatal "IT compose is unsafe"
  safe_app_env || fatal "IT env is unsafe"
  safe_assistant_env || fatal "assistant env is unsafe"
  verify_runtime "$HSTS_PRE"
  candidate=$(mktemp -- "$ASSISTANT_DIR/.hsts-candidate.XXXXXX")
  rm -f -- "$candidate"
  staging=$(mktemp -d -- "$GENERATIONS_DIR/.incoming-$generation.XXXXXX")
  cleanup_prepare() {
    local status=$?
    trap - RETURN
    if [ -n "$candidate" ] && ! rm -f -- "$candidate"; then
      [ "$status" -ne 0 ] || status=97
    fi
    if [ -n "$staging" ] && [ -d "$staging" ]; then
      if ! find "$staging" -depth -mindepth 1 -delete; then
        [ "$status" -ne 0 ] || status=97
      fi
      if ! rmdir "$staging"; then
        [ "$status" -ne 0 ] || status=97
      fi
    fi
    return "$status"
  }
  trap cleanup_prepare RETURN
  chmod 700 "$staging"
  replace_hsts "$COMPOSE_FILE" "$candidate" "$HSTS_PRE" "$HSTS_POST" \
    || fatal "cannot build exact HSTS promotion candidate"
  chmod "$(stat -c '%a' "$COMPOSE_FILE")" "$candidate"
  pre_compose_hash=$(sha256sum "$COMPOSE_FILE" | cut -d' ' -f1)
  post_compose_hash=$(sha256sum "$candidate" | cut -d' ' -f1)
  pre_render_hash=$(render_sha256 "$COMPOSE_FILE")
  post_render_hash=$(render_sha256 "$candidate")
  caddy_hash=$(sha256sum "$CADDYFILE" | cut -d' ' -f1)
  app_compose_hash=$(sha256sum "$APP_COMPOSE" | cut -d' ' -f1)
  {
    printf 'HSTS_SNAPSHOT_FORMAT=hsts-v120-1\n'
    printf 'TARGET_COMMIT=%s\n' "$target"
    printf 'GENERATION=%s\n' "$generation"
    printf 'CONTROL_MANIFEST_HASH=%s\n' \
      "$RUNTIME_CONTROL_MANIFEST_HASH"
    printf 'RELEASE_ID=%s\n' "$AUTH_RELEASE_ID"
    printf 'RELEASE_STATE_GENERATION=%s\n' "$AUTH_STATE_GENERATION"
    printf 'RELEASE_STATE_SHA256=%s\n' "$AUTH_STATE_SHA256"
    printf 'EDGE_GENERATION=%s\n' "$EDGE_GENERATION"
    printf 'EDGE_MANIFEST_SHA256=%s\n' \
      "$EDGE_GENERATION_MANIFEST_SHA256"
    printf 'EDGE_STATE_SHA256=%s\n' "$EDGE_GENERATION_STATE_SHA256"
    printf 'EDGE_COMPOSE_POST_SHA256=%s\n' "$EDGE_COMPOSE_POST_SHA256"
    printf 'EDGE_CADDYFILE_POST_SHA256=%s\n' \
      "$EDGE_CADDYFILE_POST_SHA256"
    printf 'ASSISTANT_COMPOSE_PRE_SHA256=%s\n' "$pre_compose_hash"
    printf 'ASSISTANT_COMPOSE_POST_SHA256=%s\n' "$post_compose_hash"
    printf 'ASSISTANT_RENDER_PRE_SHA256=%s\n' "$pre_render_hash"
    printf 'ASSISTANT_RENDER_POST_SHA256=%s\n' "$post_render_hash"
    printf 'CADDYFILE_SHA256=%s\n' "$caddy_hash"
    printf 'APP_COMPOSE_SHA256=%s\n' "$app_compose_hash"
    printf 'ASSISTANT_HEALTH_URL_SHA256=%s\n' \
      "$(assistant_health_sha256)"
    printf 'AUTH_APP_CID=%s\n' "$AUTH_APP_CID"
    printf 'AUTH_APP_IMAGE=%s\n' "$AUTH_APP_IMAGE"
    printf 'AUTH_APP_RESTARTS=%s\n' "$AUTH_APP_RESTARTS"
    printf 'AUTH_FRONTEND_CID=%s\n' "$AUTH_FRONTEND_CID"
    printf 'AUTH_FRONTEND_IMAGE=%s\n' "$AUTH_FRONTEND_IMAGE"
    printf 'AUTH_FRONTEND_RESTARTS=%s\n' "$AUTH_FRONTEND_RESTARTS"
    printf 'AUTH_DB_CID=%s\n' "$AUTH_DB_CID"
    printf 'AUTH_DB_IMAGE=%s\n' "$AUTH_DB_IMAGE"
    printf 'AUTH_DB_RESTARTS=%s\n' "$AUTH_DB_RESTARTS"
  } > "$staging/manifest.txt"
  printf 'HSTS_PRE=%s\nHSTS_POST=%s\n' "$HSTS_PRE" "$HSTS_POST" \
    > "$staging/snapshot.txt"
  write_state "$staging/state.txt" prepared
  (
    cd "$staging"
    sha256sum manifest.txt snapshot.txt > SHA256SUMS
  )
  chmod 600 "$staging/manifest.txt" "$staging/snapshot.txt" \
    "$staging/SHA256SUMS" "$staging/state.txt"
  if [ "$TEST_MODE" != 1 ]; then
    chown -R root:root "$staging"
  fi
  [ "$(stat -c '%a %U:%G' "$staging")" = "700 $owner" ] \
    || fatal "HSTS staging owner/mode mismatch"
  validate_generation "$staging" "$target" "$generation" \
    || fatal "HSTS staging validation failed"
  sync -f "$staging/manifest.txt" || return $?
  sync -f "$staging/snapshot.txt" || return $?
  sync -f "$staging/SHA256SUMS" || return $?
  sync -f "$staging/state.txt" || return $?
  sync -f "$staging" || return $?
  if [ "$TEST_MODE" = 1 ] \
      && [ "${HSTS_TEST_FAILPOINT:-}" = prepare-before-rename ]; then
    return 74
  fi
  mv -T -- "$staging" "$generation_dir" || return $?
  staging=
  sync -d "$GENERATIONS_DIR" || return $?
  rm -f -- "$candidate" || return $?
  candidate=
  trap - RETURN
  printf 'HSTS_PREPARE_OK generation=%s idempotent=0\n' "$generation"
}

if [ "${HSTS_ROOT_LIBRARY_ONLY:-0}" = 1 ]; then
  [ "$TEST_MODE" = 1 ] \
    || fatal "HSTS root library mode is test-only"
  return 0
fi

[ "$#" -eq 4 ] \
  || fatal "usage: hsts_v120_root.sh <prepare|promote|rollback|inspect> <target SHA> <generation> <edge generation>"
ACTION=$1
TARGET_COMMIT=$2
GENERATION=$3
EDGE_GENERATION=$4
[[ "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ ]] || fatal "invalid target SHA"
[[ "$GENERATION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$ ]] \
  || fatal "invalid HSTS generation"
[[ "$EDGE_GENERATION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$ ]] \
  || fatal "invalid edge generation"

acquire_lock
acquire_shared_caddy_lock
verify_packaged_control "$TARGET_COMMIT"
# This parser was included in the just-verified hash-addressed control package.
# shellcheck source=.deploy/v120_state.sh
source "$V120_STATE_LIBRARY"
verify_target_commit "$TARGET_COMMIT" "$ACTION"
verify_edge_generation "$TARGET_COMMIT" "$EDGE_GENERATION" "$ACTION"
ensure_directory "$HSTS_DIR" 700
ensure_directory "$GENERATIONS_DIR" 700

case "$ACTION" in
  prepare) prepare_generation "$TARGET_COMMIT" "$GENERATION" ;;
  promote) promote_generation "$TARGET_COMMIT" "$GENERATION" ;;
  rollback) rollback_generation "$TARGET_COMMIT" "$GENERATION" ;;
  inspect) inspect_generation "$TARGET_COMMIT" "$GENERATION" ;;
  *) fatal "unknown HSTS action" ;;
esac
