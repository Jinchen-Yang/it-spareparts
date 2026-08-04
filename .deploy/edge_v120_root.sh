#!/usr/bin/env bash
# Root-only scoped control for the legacy 8080 redirect edge.
set -Eeuo pipefail
umask 077

readonly EDGE_BINDING=10.0.0.11:8080:8080
readonly EDGE_ORIGIN=http://10.0.0.11:8080
readonly CANONICAL_ORIGIN=https://hbzgc.icu
TEST_MODE=${EDGE_V120_TEST_MODE:-0}
if [ "$TEST_MODE" = 1 ]; then
  [ "$EUID" -ne 0 ] || {
    printf 'FATAL: root may not enable edge test mode\n' >&2
    exit 1
  }
  APP_DIR=${EDGE_APP_DIR:?}
  ASSISTANT_DIR=${EDGE_ASSISTANT_DIR:?}
  CONTROL_DIR=${EDGE_CONTROL_DIR:?}
  LOCK_PATH=${EDGE_LOCK_PATH:?}
  CADDY_LOCK_PATH=${EDGE_CADDY_LOCK_PATH:?}
  COMMAND_DIR=${EDGE_COMMAND_DIR:?}
  AUTHORITY_MARKER=${EDGE_AUTHORITY_MARKER:?}
  RUNTIME_CONTROL_MANIFEST_HASH=${EDGE_CONTROL_MANIFEST_HASH:?}
  V120_STATE_LIBRARY=${EDGE_V120_STATE_LIBRARY:?}
  ASSISTANT_HEALTH_URL=${EDGE_ASSISTANT_HEALTH_URL:-https://118.25.94.90/health}
else
  [ "$EUID" -eq 0 ] || {
    printf 'FATAL: edge_v120_root.sh must run as root\n' >&2
    exit 1
  }
  APP_DIR=/home/ubuntu/apps/it-spareparts
  ASSISTANT_DIR=/opt/personal-ai-assistant
  CONTROL_DIR=/var/lib/it-spareparts-release-control
  LOCK_PATH=/run/lock/it-spareparts-v120
  CADDY_LOCK_PATH=/etc/it-spareparts/shared-caddy.lock
  COMMAND_DIR=
  AUTHORITY_MARKER=/etc/it-spareparts/v120-authority.marker
  RUNTIME_CONTROL_MANIFEST_HASH=
  V120_STATE_LIBRARY=
  ASSISTANT_HEALTH_URL=https://118.25.94.90/health
fi
readonly TEST_MODE APP_DIR ASSISTANT_DIR CONTROL_DIR LOCK_PATH \
  CADDY_LOCK_PATH COMMAND_DIR \
  AUTHORITY_MARKER ASSISTANT_HEALTH_URL
readonly EDGE_DIR="$CONTROL_DIR/edge"
readonly GENERATIONS_DIR="$EDGE_DIR/generations"
readonly SUCCESSOR_LINEAGE="$EDGE_DIR/successor-lineage.txt"
readonly RECREATE_PENDING="$EDGE_DIR/recreate-pending.txt"
readonly APP_COMPOSE="$APP_DIR/docker-compose.yml"
readonly APP_ENV="$APP_DIR/.env"
readonly COMPOSE_FILE="$ASSISTANT_DIR/compose.production.yml"
readonly CADDYFILE="$ASSISTANT_DIR/Caddyfile"
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

safe_regular() {
  local path=$1
  local owner
  owner=$(expected_owner)
  [ -f "$path" ] && [ ! -L "$path" ] \
    && [ "$(stat -c '%U:%G %h' "$path")" = "$owner 1" ] \
    && [ $((8#$(stat -c '%a' "$path") & 8#022)) -eq 0 ]
}

safe_app_authority_mirror() {
  local path=$1
  if [ "$TEST_MODE" = 1 ]; then
    safe_regular "$path" && [ "$(stat -c '%a' "$path")" = 600 ]
    return
  fi
  [ -f "$path" ] && [ ! -L "$path" ] \
    && [ "$(stat -c '%a %U:%G %h' "$path")" = "600 ubuntu:ubuntu 1" ]
}

safe_app_env() {
  if [ "$TEST_MODE" = 1 ]; then
    safe_regular "$APP_ENV" && [ "$(stat -c '%a' "$APP_ENV")" = 600 ]
    return
  fi
  [ -f "$APP_ENV" ] && [ ! -L "$APP_ENV" ] \
    && [ "$(stat -c '%a %U %h' "$APP_ENV")" = "600 ubuntu 1" ]
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
    return
  fi
  mkdir -- "$path"
  if [ "$TEST_MODE" != 1 ]; then chown root:root "$path"; fi
  chmod "$mode" "$path"
}

acquire_lock() {
  local mode=750
  local owner_group=root:ubuntu
  if [ "$TEST_MODE" = 1 ]; then
    mode=700
    owner_group="$(id -un):$(id -gn)"
  fi
  [ -d "$LOCK_PATH" ] && [ ! -L "$LOCK_PATH" ] \
    || fatal "release lock is unsafe"
  [ "$(stat -c '%a %U:%G' "$LOCK_PATH")" = "$mode $owner_group" ] \
    || fatal "release lock owner/mode mismatch"
  exec 9<"$LOCK_PATH"
  flock -n 9 || {
    printf 'EDGE_BUSY: another release operation holds the lock\n' >&2
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

verify_packaged_control() {
  local target=$1
  if [ "$TEST_MODE" = 1 ]; then
    [[ "$RUNTIME_CONTROL_MANIFEST_HASH" =~ ^[0-9a-f]{64}$ ]] \
      && [ -f "$V120_STATE_LIBRARY" ] && [ ! -L "$V120_STATE_LIBRARY" ] \
      || fatal "test control authority is invalid"
    return
  fi
  local script_path script_dir manifest_hash current_target package_target
  script_path=$(realpath -e -- "${BASH_SOURCE[0]}") \
    || fatal "cannot resolve edge root control"
  script_dir=$(dirname -- "$script_path")
  manifest_hash=$(basename -- "$script_dir")
  [[ "$manifest_hash" =~ ^[0-9a-f]{64}$ ]] \
    || fatal "edge root control is not hash-addressed"
  [ "$script_dir" = "$CONTROL_DIR/versions/$manifest_hash" ] \
    || fatal "edge root control is outside control versions"
  [ -L "$CONTROL_DIR/current" ] \
    && [ "$(stat -c '%F %U:%G %h' "$CONTROL_DIR/current")" \
      = "symbolic link root:root 1" ] \
    || fatal "current control pointer is unsafe"
  current_target=$(readlink -- "$CONTROL_DIR/current")
  [ "$current_target" = "versions/$manifest_hash" ] \
    || fatal "current control pointer does not select this package"
  [ "$(stat -c '%a %U:%G %h' "$script_path")" = "700 root:root 1" ] \
    || fatal "edge root control owner/mode is unsafe"
  "$script_dir/install-v120-control.sh" verify "$manifest_hash" >/dev/null \
    || fatal "edge control package verification failed"
  [ "$(readlink -- "$CONTROL_DIR/current")" = "$current_target" ] \
    && [ "$(realpath -e -- "$CONTROL_DIR/current/edge-v120-root.sh")" \
      = "$script_path" ] \
    || fatal "current control pointer changed under release lock"
  [ "$(grep -c '^TARGET_COMMIT=' "$script_dir/manifest.txt")" -eq 1 ] \
    || fatal "control package target is ambiguous"
  package_target=$(sed -n 's/^TARGET_COMMIT=//p' "$script_dir/manifest.txt")
  [ "$package_target" = "$target" ] \
    || fatal "control package target differs from edge target"
  RUNTIME_CONTROL_MANIFEST_HASH=$manifest_hash
  V120_STATE_LIBRARY="$script_dir/v120_state.sh"
}

app_compose() {
  docker compose --project-name it-spareparts \
    --env-file "$APP_ENV" -f "$APP_COMPOSE" "$@"
}

verify_release_authority() {
  local target=$1 action=$2
  local state="$CONTROL_DIR/v120-state.state"
  local owner app_state app_hash
  local -a marker_lines=()
  owner=$(expected_owner)
  [ -f "$AUTHORITY_MARKER" ] && [ ! -L "$AUTHORITY_MARKER" ] \
    && [ "$(stat -c '%a %U:%G %h' "$AUTHORITY_MARKER")" \
      = "600 $owner 1" ] \
    || fatal "release authority marker is unsafe"
  mapfile -t marker_lines < "$AUTHORITY_MARKER"
  [ "${#marker_lines[@]}" -eq 3 ] \
    && [ "${marker_lines[0]}" = "AUTHORITY_FORMAT=v120-authority-1" ] \
    && [[ "${marker_lines[1]}" \
      =~ ^INITIAL_CONTROL_MANIFEST_HASH=[0-9a-f]{64}$ ]] \
    && [[ "${marker_lines[2]}" \
      =~ ^INITIAL_TARGET_COMMIT=[0-9a-f]{40}$ ]] \
    || fatal "release authority marker is malformed"
  safe_regular "$state" && [ "$(stat -c '%a' "$state")" = 600 ] \
    || fatal "root release authority is unsafe"
  declare -gA AUTHORITY_STATE=()
  v120_state_parse_to_array "$state" AUTHORITY_STATE \
    || fatal "root release authority is invalid"
  [ "${AUTHORITY_STATE[TARGET_COMMIT]}" = "$target" ] \
    && [ "${AUTHORITY_STATE[RELEASE_PHASE]}" = observed ] \
    && [ "${AUTHORITY_STATE[CONTROL_MANIFEST_HASH]}" \
      = "$RUNTIME_CONTROL_MANIFEST_HASH" ] \
    || fatal "root release authority does not authorize edge"
  AUTH_RELEASE_ID=${AUTHORITY_STATE[RELEASE_ID]}
  app_state="$APP_DIR/backups/$AUTH_RELEASE_ID.state"
  if ! safe_app_authority_mirror "$app_state" \
      || ! cmp -s -- "$state" "$app_state"; then
    fatal "root and app release authority mirrors differ"
  fi
  AUTH_STATE_GENERATION=${AUTHORITY_STATE[STATE_GENERATION]}
  AUTH_STATE_SHA256=$(sha256sum "$state" | cut -d' ' -f1)
  app_hash=$(sha256sum "$APP_COMPOSE" | cut -d' ' -f1)
  [ "$app_hash" = "${AUTHORITY_STATE[APP_COMPOSE_HASH]}" ] \
    || fatal "app compose differs from root authority"
  AUTH_APP_CID=$(app_compose ps -q app)
  AUTH_FRONTEND_CID=$(app_compose ps -q frontend)
  AUTH_DB_CID=$(app_compose ps -q db)
  [ "$AUTH_APP_CID" = "${AUTHORITY_STATE[NEW_APP_CID]}" ] \
    && [ "$AUTH_FRONTEND_CID" = "${AUTHORITY_STATE[NEW_FRONTEND_CID]}" ] \
    && [ "$AUTH_DB_CID" = "${AUTHORITY_STATE[BASE_DB_CID]}" ] \
    || fatal "live containers differ from root authority"
  AUTH_APP_IMAGE=$(docker inspect -f '{{.Image}}' "$AUTH_APP_CID")
  AUTH_FRONTEND_IMAGE=$(docker inspect -f '{{.Image}}' "$AUTH_FRONTEND_CID")
  AUTH_DB_IMAGE=$(docker inspect -f '{{.Image}}' "$AUTH_DB_CID")
  [ "$AUTH_APP_IMAGE" = "${AUTHORITY_STATE[NEW_APP_IMAGE_ID]}" ] \
    && [ "$AUTH_FRONTEND_IMAGE" \
      = "${AUTHORITY_STATE[NEW_FRONTEND_IMAGE_ID]}" ] \
    && [ "$AUTH_DB_IMAGE" = "${AUTHORITY_STATE[BASE_DB_IMAGE_ID]}" ] \
    || fatal "live images differ from root authority"
  AUTH_APP_RESTARTS=$(docker inspect -f '{{.RestartCount}}' "$AUTH_APP_CID")
  AUTH_FRONTEND_RESTARTS=$(
    docker inspect -f '{{.RestartCount}}' "$AUTH_FRONTEND_CID"
  )
  AUTH_DB_RESTARTS=$(docker inspect -f '{{.RestartCount}}' "$AUTH_DB_CID")
  [ "$AUTH_APP_RESTARTS" = 0 ] \
    && [ "$AUTH_FRONTEND_RESTARTS" = 0 ] \
    && [ "$AUTH_DB_RESTARTS" = "${AUTHORITY_STATE[BASE_DB_RESTARTS]}" ] \
    || fatal "live restart counts differ from root authority"
  ROOT_BASE_CADDY_CID=${AUTHORITY_STATE[BASE_EDGE_CID]}
  ROOT_BASE_CADDY_RESTARTS=${AUTHORITY_STATE[BASE_EDGE_RESTARTS]}
  LIVE_CADDY_CID=$(docker inspect -f '{{.Id}}' personal-ai-assistant-caddy)
  [[ "$LIVE_CADDY_CID" =~ ^[0-9a-f]{64}$ ]] \
    || fatal "shared Caddy identity is invalid"
  if [ -e "$RECREATE_PENDING" ] || [ -L "$RECREATE_PENDING" ]; then
    case "$action" in
      inspect)
        if ! reconcile_recreate_pending "$target" "$GENERATION" \
            "$LIVE_CADDY_CID" inspect inspect; then
          printf 'divergent-or-unknown\n'
          exit 78
        fi
        ROOT_BASE_CADDY_IMAGE=$(
          manifest_value "$SUCCESSOR_LINEAGE" ROOT_BASE_CADDY_IMAGE
        )
        AUTH_CADDY_CID=$(
          manifest_value "$GENERATIONS_DIR/$GENERATION/manifest.txt" \
            AUTH_CADDY_CID
        )
        AUTH_CADDY_IMAGE=$(
          manifest_value "$GENERATIONS_DIR/$GENERATION/manifest.txt" \
            AUTH_CADDY_IMAGE
        )
        AUTH_CADDY_RESTARTS=$(
          manifest_value "$GENERATIONS_DIR/$GENERATION/manifest.txt" \
            AUTH_CADDY_RESTARTS
        )
        return
        ;;
      promote|rollback)
        reconcile_recreate_pending "$target" "$GENERATION" \
          "$LIVE_CADDY_CID" recover "$action" \
          || fatal "Caddy recreate pending intent is not exactly recoverable"
        ;;
      *)
        fatal "pending Caddy recreation requires explicit promote or rollback"
        ;;
    esac
  fi
  LIVE_CADDY_CID=$(docker inspect -f '{{.Id}}' personal-ai-assistant-caddy)
  if [ "$LIVE_CADDY_CID" = "$ROOT_BASE_CADDY_CID" ]; then
    AUTH_CADDY_IMAGE=$(docker inspect -f '{{.Image}}' "$LIVE_CADDY_CID")
    AUTH_CADDY_RESTARTS=$(
      docker inspect -f '{{.RestartCount}}' "$LIVE_CADDY_CID"
    )
    [ "$AUTH_CADDY_RESTARTS" = "$ROOT_BASE_CADDY_RESTARTS" ] \
      || fatal "shared Caddy restart count differs from root authority"
    ROOT_BASE_CADDY_IMAGE=$AUTH_CADDY_IMAGE
    AUTH_CADDY_CID=$LIVE_CADDY_CID
  else
    validate_successor_lineage "$target" "$LIVE_CADDY_CID" \
      || fatal "shared Caddy successor lacks exact root lineage"
    ROOT_BASE_CADDY_IMAGE=$(
      manifest_value "$SUCCESSOR_LINEAGE" ROOT_BASE_CADDY_IMAGE
    )
    if [ "$(manifest_value "$SUCCESSOR_LINEAGE" GENERATION)" = "$GENERATION" ] \
        && [ -d "$GENERATIONS_DIR/$GENERATION" ]; then
      AUTH_CADDY_CID=$(
        manifest_value "$SUCCESSOR_LINEAGE" GENERATION_BASE_CADDY_CID
      )
      AUTH_CADDY_IMAGE=$(
        manifest_value "$SUCCESSOR_LINEAGE" GENERATION_BASE_CADDY_IMAGE
      )
      AUTH_CADDY_RESTARTS=$(
        manifest_value "$SUCCESSOR_LINEAGE" GENERATION_BASE_CADDY_RESTARTS
      )
    else
      AUTH_CADDY_CID=$LIVE_CADDY_CID
      AUTH_CADDY_IMAGE=$(
        manifest_value "$SUCCESSOR_LINEAGE" CURRENT_CADDY_IMAGE
      )
      AUTH_CADDY_RESTARTS=$(
        manifest_value "$SUCCESSOR_LINEAGE" CURRENT_CADDY_RESTARTS
      )
    fi
  fi
}

render_sha256() {
  docker compose --env-file "$ASSISTANT_ENV" -f "$1" \
    config --format json | sha256sum | cut -d' ' -f1
}

make_post_compose() {
  python3 - "$1" "$2" <<'PY'
import os
import re
import sys

source, destination = sys.argv[1:]
raw = open(source, "rb").read()
text = raw.decode("utf-8")
if "\r" in text or "10.0.0.11:8080" in text:
    raise SystemExit("unexpected existing edge binding")
lines = text.splitlines(keepends=True)
service = next(
    (i for i, line in enumerate(lines) if re.match(r"^  caddy:\s*(?:#.*)?\n?$", line)),
    None,
)
if service is None:
    raise SystemExit("missing caddy service")
end = next(
    (
        i
        for i in range(service + 1, len(lines))
        if re.match(r"^  [A-Za-z0-9_.-]+:\s*(?:#.*)?\n?$", lines[i])
    ),
    len(lines),
)
ports = next(
    (
        i
        for i in range(service + 1, end)
        if re.match(r"^    ports:\s*(?:#.*)?\n?$", lines[i])
    ),
    None,
)
if ports is None:
    raise SystemExit("missing caddy ports list")
insert = ports + 1
while insert < end and (
    re.match(r"^      -\s+", lines[insert])
    or re.match(r"^\s*(?:#.*)?\n?$", lines[insert])
):
    insert += 1
lines.insert(insert, '      - "10.0.0.11:8080:8080"\n')
updated = "".join(lines)
if updated.count("10.0.0.11:8080:8080") != 1:
    raise SystemExit("edge binding is ambiguous")
fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "wb") as handle:
    handle.write(updated.encode("utf-8"))
    handle.flush()
    os.fsync(handle.fileno())
PY
}

make_post_caddyfile() {
  python3 - "$1" "$2" <<'PY'
import os
import sys

source, destination = sys.argv[1:]
raw = open(source, "rb").read()
text = raw.decode("utf-8")
if "\r" in text or "\n:8080 {" in "\n" + text:
    raise SystemExit("unexpected existing edge site")
if not text.endswith("\n"):
    text += "\n"
text += """
:8080 {
\t@safe method GET HEAD
\t@unsafe not method GET HEAD
\tredir @safe https://hbzgc.icu{uri} 308
\theader @unsafe Allow "GET, HEAD"
\trespond @unsafe 405
}
"""
fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "wb") as handle:
    handle.write(text.encode("utf-8"))
    handle.flush()
    os.fsync(handle.fileno())
PY
}

manifest_value() {
  local manifest=$1 key=$2 count value
  count=$(grep -c "^${key}=" "$manifest" || true)
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

validate_successor_lineage() {
  local target=$1 live_cid=$2
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
      = "$ROOT_BASE_CADDY_CID" ] \
    && [ "$(manifest_value "$SUCCESSOR_LINEAGE" ROOT_BASE_CADDY_RESTARTS)" \
      = "$ROOT_BASE_CADDY_RESTARTS" ] \
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
  local dir=$1 action=$2 action_base_cid=$3
  local action_base_image=$4 action_base_restarts=$5
  local current_cid current_image current_restarts temporary status
  current_cid=$(docker inspect -f '{{.Id}}' personal-ai-assistant-caddy)
  current_image=$(docker inspect -f '{{.Image}}' "$current_cid")
  current_restarts=$(docker inspect -f '{{.RestartCount}}' "$current_cid")
  [[ "$current_cid" =~ ^[0-9a-f]{64}$ ]] \
    && [ "$current_image" = "$(manifest_value "$dir/manifest.txt" AUTH_CADDY_IMAGE)" ] \
    && [ "$current_restarts" = 0 ] \
    || fatal "recreated Caddy successor is not exact"
  temporary=$(mktemp -- "$EDGE_DIR/.successor-lineage.XXXXXX")
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
    printf 'ROOT_BASE_CADDY_CID=%s\n' "$ROOT_BASE_CADDY_CID"
    printf 'ROOT_BASE_CADDY_IMAGE=%s\n' "$ROOT_BASE_CADDY_IMAGE"
    printf 'ROOT_BASE_CADDY_RESTARTS=%s\n' "$ROOT_BASE_CADDY_RESTARTS"
    printf 'GENERATION=%s\n' "$GENERATION"
    printf 'MUTATION_DOMAIN=edge\nMUTATION_GENERATION=%s\n' "$GENERATION"
    printf 'ACTION=%s\n' "$action"
    printf 'GENERATION_BASE_CADDY_CID=%s\n' \
      "$(manifest_value "$dir/manifest.txt" AUTH_CADDY_CID)"
    printf 'GENERATION_BASE_CADDY_IMAGE=%s\n' \
      "$(manifest_value "$dir/manifest.txt" AUTH_CADDY_IMAGE)"
    printf 'GENERATION_BASE_CADDY_RESTARTS=%s\n' \
      "$(manifest_value "$dir/manifest.txt" AUTH_CADDY_RESTARTS)"
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
    || fatal "successor lineage staging is invalid"
  sync -f "$temporary" || return $?
  mv -fT -- "$temporary" "$SUCCESSOR_LINEAGE" || return $?
  temporary=
  sync -f "$SUCCESSOR_LINEAGE" || return $?
  sync -d "$EDGE_DIR" || return $?
  trap - RETURN
}

publish_current_successor_lineage() {
  local dir=$1 action=$2
  local current_cid current_image current_restarts
  current_cid=$(docker inspect -f '{{.Id}}' personal-ai-assistant-caddy)
  current_image=$(docker inspect -f '{{.Image}}' "$current_cid")
  current_restarts=$(docker inspect -f '{{.RestartCount}}' "$current_cid")
  publish_successor_lineage "$dir" "$action" "$current_cid" \
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
  local target=$1 generation=$2
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
      = "$generation" ] \
    && [ "$(manifest_value "$RECREATE_PENDING" MUTATION_DOMAIN)" = edge ] \
    && [ "$(manifest_value "$RECREATE_PENDING" MUTATION_GENERATION)" \
      = "$generation" ] \
    && [ "$(manifest_value "$SUCCESSOR_LINEAGE" TARGET_COMMIT)" = "$target" ] \
    && [ "$(manifest_value "$SUCCESSOR_LINEAGE" CONTROL_MANIFEST_HASH)" \
      = "$RUNTIME_CONTROL_MANIFEST_HASH" ] \
    && [ "$(manifest_value "$SUCCESSOR_LINEAGE" GENERATION)" = "$generation" ] \
    && [ "$(manifest_value "$RECREATE_PENDING" TARGET_CADDY_IMAGE)" \
      = "$(manifest_value "$SUCCESSOR_LINEAGE" CURRENT_CADDY_IMAGE)" ]
}

clear_recreate_pending() {
  [ -e "$RECREATE_PENDING" ] || [ -L "$RECREATE_PENDING" ] || return 0
  safe_regular "$RECREATE_PENDING" \
    && [ "$(stat -c '%a' "$RECREATE_PENDING")" = 600 ] \
    || return 73
  rm -f -- "$RECREATE_PENDING" || return $?
  sync -d "$EDGE_DIR" || return $?
}

write_recreate_pending() {
  local dir=$1 action=$2 old_cid=$3 old_image=$4 old_restarts=$5
  local old_compose_hash=$6 old_caddy_hash=$7
  local target_compose_hash=$8 target_caddy_hash=$9
  local temporary='' status
  if [ -e "$RECREATE_PENDING" ] || [ -L "$RECREATE_PENDING" ]; then
    validate_recreate_pending "$TARGET_COMMIT" "$GENERATION" \
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
  temporary=$(mktemp -- "$EDGE_DIR/.recreate-pending.XXXXXX")
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
    printf 'EDGE_GENERATION=%s\n' "$GENERATION"
    printf 'MUTATION_DOMAIN=edge\nMUTATION_GENERATION=%s\n' "$GENERATION"
    printf 'ACTION=%s\n' "$action"
    printf 'OLD_CADDY_CID=%s\nOLD_CADDY_IMAGE=%s\n' "$old_cid" "$old_image"
    printf 'OLD_CADDY_RESTARTS=%s\n' "$old_restarts"
    printf 'OLD_ASSISTANT_COMPOSE_SHA256=%s\n' "$old_compose_hash"
    printf 'OLD_CADDYFILE_SHA256=%s\n' "$old_caddy_hash"
    printf 'TARGET_CADDY_IMAGE=%s\n' \
      "$(manifest_value "$dir/manifest.txt" AUTH_CADDY_IMAGE)"
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
  sync -d "$EDGE_DIR" || return $?
  trap - RETURN
}

reconcile_recreate_pending() {
  local target=$1 generation=$2 live_cid=$3 mode=${4:-recover}
  local expected_action=${5:-}
  local old_cid old_image old_restarts lineage_cid
  local live_image live_restarts networks ingress_members dir action
  local actual_compose actual_caddy old_compose old_caddy
  local target_compose target_caddy lineage_compose lineage_caddy
  [ -e "$RECREATE_PENDING" ] || [ -L "$RECREATE_PENDING" ] || return 0
  validate_recreate_pending "$target" "$generation" || return 73
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
  dir="$GENERATIONS_DIR/$generation"
  safe_regular "$dir/manifest.txt" || return 73
  if [ "$live_cid" = "$old_cid" ]; then
    lineage_compose=$(
      manifest_value "$SUCCESSOR_LINEAGE" ASSISTANT_COMPOSE_SHA256
    )
    lineage_caddy=$(manifest_value "$SUCCESSOR_LINEAGE" CADDYFILE_SHA256)
    [ "$lineage_cid" = "$old_cid" ] \
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
      publish_successor_lineage "$dir" "$action" "$old_cid" \
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
  json_exact_keys "$networks" \
    personal-ai-assistant-network it-spareparts-ingress || return 73
  [ "$(docker network inspect -f '{{.Internal}}' it-spareparts-ingress)" \
    = true ] || return 73
  ingress_members=$(docker network inspect \
    -f '{{json .Containers}}' it-spareparts-ingress) || return $?
  json_exact_keys "$ingress_members" "$live_cid" "$AUTH_FRONTEND_CID" \
    || return 73
  ROOT_BASE_CADDY_IMAGE=$(
    manifest_value "$SUCCESSOR_LINEAGE" ROOT_BASE_CADDY_IMAGE
  )
  if [ "$lineage_cid" = "$old_cid" ]; then
    [ "$(manifest_value "$SUCCESSOR_LINEAGE" CURRENT_CADDY_IMAGE)" \
      = "$old_image" ] \
      && [ "$(manifest_value "$SUCCESSOR_LINEAGE" CURRENT_CADDY_RESTARTS)" \
        = "$old_restarts" ] \
      || return 73
    [ "$mode" = recover ] || return 0
    publish_successor_lineage "$dir" "$action" "$old_cid" \
      "$old_image" "$old_restarts" || return $?
  elif [ "$lineage_cid" = "$live_cid" ]; then
    [ "$(manifest_value "$SUCCESSOR_LINEAGE" MUTATION_DOMAIN)" = edge ] \
      && [ "$(manifest_value "$SUCCESSOR_LINEAGE" MUTATION_GENERATION)" \
        = "$generation" ] \
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

validate_edge_manifest_schema() {
  python3 - "$1" <<'PY'
import re
import sys

allowed = {
    "EDGE_FORMAT",
    "TARGET_COMMIT",
    "GENERATION",
    "CONTROL_MANIFEST_HASH",
    "RELEASE_ID",
    "RELEASE_STATE_GENERATION",
    "RELEASE_STATE_SHA256",
    "APP_COMPOSE_SHA256",
    "ASSISTANT_COMPOSE_PRE_SHA256",
    "ASSISTANT_COMPOSE_POST_SHA256",
    "ASSISTANT_RENDER_PRE_SHA256",
    "ASSISTANT_RENDER_POST_SHA256",
    "CADDYFILE_PRE_SHA256",
    "CADDYFILE_POST_SHA256",
    "AUTH_APP_CID",
    "AUTH_APP_IMAGE",
    "AUTH_APP_RESTARTS",
    "AUTH_FRONTEND_CID",
    "AUTH_FRONTEND_IMAGE",
    "AUTH_FRONTEND_RESTARTS",
    "AUTH_DB_CID",
    "AUTH_DB_IMAGE",
    "AUTH_DB_RESTARTS",
    "AUTH_CADDY_CID",
    "AUTH_CADDY_IMAGE",
    "AUTH_CADDY_RESTARTS",
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
if set(values) != allowed or values["EDGE_FORMAT"] != "edge-v120-1":
    raise SystemExit(1)
PY
}

state_value() {
  safe_regular "$1" && [ "$(stat -c '%a' "$1")" = 600 ] || return 64
  [ "$(grep -c '^EDGE_STATE=' "$1")" -eq 1 ] || return 64
  local value
  value=$(sed -n 's/^EDGE_STATE=//p' "$1")
  [[ "$value" =~ ^(prepared|promoted|rolled_back)$ ]] || return 64
  printf '%s\n' "$value"
}

write_state() {
  printf 'EDGE_STATE=%s\n' "$2" > "$1"
  chmod 600 "$1"
}

publish_state() {
  local dir=$1 value=$2 temporary='' status
  temporary=$(mktemp -- "$dir/.state.XXXXXX")
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
  if [ "$TEST_MODE" != 1 ]; then chown root:root "$temporary"; fi
  sync -f "$temporary" || return $?
  mv -fT -- "$temporary" "$dir/state.txt" || return $?
  temporary=
  sync -f "$dir/state.txt" || return $?
  sync -d "$dir" || return $?
  trap - RETURN
}

validate_generation() {
  local dir=$1 target=$2 generation=$3 owner name
  owner=$(expected_owner)
  [ -d "$dir" ] && [ ! -L "$dir" ] \
    && [ "$(stat -c '%a %U:%G' "$dir")" = "700 $owner" ] \
    || return 1
  for name in manifest.txt SHA256SUMS state.txt \
      compose.pre compose.post Caddyfile.pre Caddyfile.post; do
    safe_regular "$dir/$name" \
      && [ "$(stat -c '%a' "$dir/$name")" = 600 ] || return 1
  done
  (
    cd "$dir"
    sha256sum -c SHA256SUMS >/dev/null
  ) || return 1
  validate_edge_manifest_schema "$dir/manifest.txt" || return 1
  [ "$(manifest_value "$dir/manifest.txt" TARGET_COMMIT)" = "$target" ] \
    && [ "$(manifest_value "$dir/manifest.txt" GENERATION)" = "$generation" ] \
    && [ "$(manifest_value "$dir/manifest.txt" CONTROL_MANIFEST_HASH)" \
      = "$RUNTIME_CONTROL_MANIFEST_HASH" ] \
    && [ "$(manifest_value "$dir/manifest.txt" RELEASE_ID)" \
      = "$AUTH_RELEASE_ID" ] \
    && [ "$(manifest_value "$dir/manifest.txt" RELEASE_STATE_SHA256)" \
      = "$AUTH_STATE_SHA256" ] \
    && [ "$(manifest_value "$dir/manifest.txt" AUTH_APP_CID)" \
      = "$AUTH_APP_CID" ] \
    && [ "$(manifest_value "$dir/manifest.txt" AUTH_FRONTEND_CID)" \
      = "$AUTH_FRONTEND_CID" ] \
    && [ "$(manifest_value "$dir/manifest.txt" AUTH_DB_CID)" \
      = "$AUTH_DB_CID" ] \
    && [ "$(manifest_value "$dir/manifest.txt" AUTH_CADDY_CID)" \
      = "$AUTH_CADDY_CID" ] \
    && [ "$(manifest_value "$dir/manifest.txt" AUTH_APP_IMAGE)" \
      = "$AUTH_APP_IMAGE" ] \
    && [ "$(manifest_value "$dir/manifest.txt" AUTH_APP_RESTARTS)" \
      = "$AUTH_APP_RESTARTS" ] \
    && [ "$(manifest_value "$dir/manifest.txt" AUTH_FRONTEND_IMAGE)" \
      = "$AUTH_FRONTEND_IMAGE" ] \
    && [ "$(manifest_value "$dir/manifest.txt" AUTH_FRONTEND_RESTARTS)" \
      = "$AUTH_FRONTEND_RESTARTS" ] \
    && [ "$(manifest_value "$dir/manifest.txt" AUTH_DB_IMAGE)" \
      = "$AUTH_DB_IMAGE" ] \
    && [ "$(manifest_value "$dir/manifest.txt" AUTH_DB_RESTARTS)" \
      = "$AUTH_DB_RESTARTS" ] \
    && [ "$(manifest_value "$dir/manifest.txt" AUTH_CADDY_IMAGE)" \
      = "$AUTH_CADDY_IMAGE" ] \
    && [ "$(manifest_value "$dir/manifest.txt" AUTH_CADDY_RESTARTS)" \
      = "$AUTH_CADDY_RESTARTS" ] \
    || return 1
}

side_matches() {
  local dir=$1 side=$2
  [ "$(sha256sum "$COMPOSE_FILE" | cut -d' ' -f1)" \
    = "$(manifest_value "$dir/manifest.txt" "ASSISTANT_COMPOSE_${side}_SHA256")" ] \
    && [ "$(render_sha256 "$COMPOSE_FILE")" \
      = "$(manifest_value "$dir/manifest.txt" "ASSISTANT_RENDER_${side}_SHA256")" ] \
    && [ "$(sha256sum "$CADDYFILE" | cut -d' ' -f1)" \
      = "$(manifest_value "$dir/manifest.txt" "CADDYFILE_${side}_SHA256")" ] \
    && [ "$(sha256sum "$APP_COMPOSE" | cut -d' ' -f1)" \
      = "$(manifest_value "$dir/manifest.txt" APP_COMPOSE_SHA256)" ]
}

file_side_matches() {
  local dir=$1 kind=$2 side=$3 live key
  case "$kind" in
    compose) live=$COMPOSE_FILE; key="ASSISTANT_COMPOSE_${side}_SHA256" ;;
    caddy) live=$CADDYFILE; key="CADDYFILE_${side}_SHA256" ;;
    *) return 64 ;;
  esac
  [ "$(sha256sum "$live" | cut -d' ' -f1)" \
    = "$(manifest_value "$dir/manifest.txt" "$key")" ]
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

edge_cas_path() {
  local action=$1 kind=$2
  printf '%s/.edge-cas-%s-%s-%s\n' \
    "$ASSISTANT_DIR" "$GENERATION" "$action" "$kind"
}

safe_cas_file() {
  local path=$1
  safe_regular "$path" && [ "$(stat -c '%a' "$path")" -eq 600 ]
}

install_snapshot() {
  local source=$1 destination=$2 expected_live=$3 expected_new=$4 kind=$5
  local action=$6
  local temporary status keep=0
  local candidate_identity live_identity swapped_identity swapped_hash
  temporary=$(edge_cas_path "$action" "$kind")
  [ ! -e "$temporary" ] && [ ! -L "$temporary" ] || return 73
  ( set -o noclobber; : > "$temporary" ) 2>/dev/null || return 73
  cleanup_snapshot() {
    status=$?
    trap - RETURN
    if [ "$keep" != 1 ] && [ -n "$temporary" ] \
        && ! rm -f -- "$temporary"; then
      [ "$status" -ne 0 ] || status=97
    fi
    return "$status"
  }
  trap cleanup_snapshot RETURN
  cp --reflink=auto -- "$source" "$temporary"
  chmod "$(stat -c '%a' "$destination")" "$temporary"
  if [ "$TEST_MODE" != 1 ]; then chown root:root "$temporary"; fi
  [ "$(sha256sum "$temporary" | cut -d' ' -f1)" = "$expected_new" ] \
    || return 73
  sync -f "$temporary" || return $?
  candidate_identity=$(stat -Lc '%d:%i' "$temporary")
  live_identity=$(stat -Lc '%d:%i' "$destination")
  [ "$(sha256sum "$destination" | cut -d' ' -f1)" = "$expected_live" ] \
    || return 73
  if [ "$TEST_MODE" = 1 ] \
      && { [ "${EDGE_TEST_FAILPOINT:-}" = "$kind-before-rename" ] \
        || [ "${EDGE_TEST_FAILPOINT:-}" \
          = "$kind-after-live-check-before-exchange" ]; }; then
    printf '# concurrent-edge-writer\n' >> "$destination"
  fi
  rename_exchange "$temporary" "$destination" || return $?
  swapped_identity=$(stat -Lc '%d:%i' "$temporary")
  swapped_hash=$(sha256sum "$temporary" | cut -d' ' -f1)
  if [ "$(stat -Lc '%d:%i' "$destination")" != "$candidate_identity" ] \
      || [ "$(sha256sum "$destination" | cut -d' ' -f1)" != "$expected_new" ] \
      || [ "$swapped_identity" != "$live_identity" ] \
      || [ "$swapped_hash" != "$expected_live" ]; then
    if [ "$(stat -Lc '%d:%i' "$destination")" != "$candidate_identity" ] \
        || [ "$(stat -Lc '%d:%i' "$temporary")" != "$swapped_identity" ]; then
      keep=1
      printf 'FATAL: shared Caddy CAS inode pairing changed; retained %s\n' \
        "$temporary" >&2
      return 97
    fi
    rename_exchange "$temporary" "$destination" || {
      keep=1
      return 97
    }
    [ "$(stat -Lc '%d:%i' "$destination")" = "$swapped_identity" ] \
      && [ "$(sha256sum "$destination" | cut -d' ' -f1)" \
        = "$swapped_hash" ] \
      && [ "$(stat -Lc '%d:%i' "$temporary")" = "$candidate_identity" ] \
      && [ "$(sha256sum "$temporary" | cut -d' ' -f1)" \
        = "$expected_new" ] || {
          keep=1
          return 97
        }
    return 73
  fi
  sync -f "$destination" || return $?
  sync -f "$temporary" || return $?
  sync -d "$(dirname -- "$destination")" || return $?
  keep=1
  trap - RETURN
}

finalize_edge_cas_backups() {
  local action=$1 expected_compose=$2 expected_caddy=$3
  local compose_backup caddy_backup
  compose_backup=$(edge_cas_path "$action" compose)
  caddy_backup=$(edge_cas_path "$action" caddy)
  if [ ! -e "$compose_backup" ] && [ ! -L "$compose_backup" ] \
      && [ ! -e "$caddy_backup" ] && [ ! -L "$caddy_backup" ]; then
    return 0
  fi
  safe_cas_file "$compose_backup" && safe_cas_file "$caddy_backup" \
    || return 73
  [ "$(sha256sum "$compose_backup" | cut -d' ' -f1)" \
    = "$expected_compose" ] \
    && [ "$(sha256sum "$caddy_backup" | cut -d' ' -f1)" \
      = "$expected_caddy" ] || return 73
  rm -f -- "$compose_backup" "$caddy_backup" || return $?
  sync -d "$ASSISTANT_DIR" || return $?
}

json_exact_keys() {
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

probe_json_health() {
  local endpoint=$1
  local expected_status=$2
  local expected_kind=$3
  local -a protocol=()
  case "$expected_status:$expected_kind" in
    ok:app|ok:db|ready:assistant) ;;
    *) return 64 ;;
  esac
  if [[ "$endpoint" == https://* ]]; then
    protocol=(--proto '=https' --tlsv1.2)
  else
    protocol=(--proto '=http')
  fi
  curl --noproxy '*' "${protocol[@]}" \
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

verify_runtime() {
  local networks frontend_networks service_networks ingress_members listeners
  local headers location cookie_count hsts_headers
  local runtime_caddy_cid runtime_caddy_image runtime_caddy_restarts
  runtime_caddy_cid=$(
    docker inspect -f '{{.Id}}' personal-ai-assistant-caddy
  )
  [[ "$runtime_caddy_cid" =~ ^[0-9a-f]{64}$ ]] \
    || fatal "shared Caddy runtime identity is invalid"
  runtime_caddy_image=$(docker inspect -f '{{.Image}}' "$runtime_caddy_cid")
  runtime_caddy_restarts=$(
    docker inspect -f '{{.RestartCount}}' "$runtime_caddy_cid"
  )
  [ "$(docker inspect -f '{{.State.Running}}' \
    personal-ai-assistant-caddy)" = true ] \
    || fatal "shared Caddy is not running"
  [ "$runtime_caddy_image" = "$AUTH_CADDY_IMAGE" ] \
    && [ "$runtime_caddy_restarts" = 0 ] \
    || fatal "shared Caddy runtime image or restart count changed"
  docker exec personal-ai-assistant-caddy caddy validate \
    --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null \
    || fatal "shared Caddy validation failed"
  [ "$(docker compose --env-file "$ASSISTANT_ENV" -f "$COMPOSE_FILE" \
    port caddy 8080)" = 10.0.0.11:8080 ] \
    || fatal "legacy edge binding is not exact"
  listeners=$(ss -H -ltnp '( sport = :8080 )')
  [ "$(printf '%s\n' "$listeners" | awk '{print $4}' | LC_ALL=C sort)" = "$(
    printf '10.0.0.11:8080\n127.0.0.1:8080\n' | LC_ALL=C sort
  )" ] || fatal "port 8080 listeners are not exact"
  printf '%s\n' "$listeners" | grep '10.0.0.11:8080.*docker-proxy' >/dev/null \
    || fatal "legacy edge listener is not Docker-owned"
  networks=$(docker inspect -f '{{json .NetworkSettings.Networks}}' \
    personal-ai-assistant-caddy)
  json_exact_keys "$networks" \
    personal-ai-assistant-network it-spareparts-ingress \
    || fatal "shared Caddy networks are not exact"
  frontend_networks=$(docker inspect \
    -f '{{json .NetworkSettings.Networks}}' "$AUTH_FRONTEND_CID")
  json_exact_keys "$frontend_networks" \
    it-spareparts_default it-spareparts-ingress \
    || fatal "frontend networks are not exact"
  for cid in "$AUTH_APP_CID" "$AUTH_DB_CID"; do
    service_networks=$(docker inspect \
      -f '{{json .NetworkSettings.Networks}}' "$cid")
    json_exact_keys "$service_networks" it-spareparts_default \
      || fatal "internal service networks are not exact"
  done
  [ "$(docker network inspect -f '{{.Internal}}' it-spareparts-ingress)" \
    = true ] || fatal "ingress is not internal"
  ingress_members=$(docker network inspect \
    -f '{{json .Containers}}' it-spareparts-ingress)
  json_exact_keys "$ingress_members" "$runtime_caddy_cid" \
    "$AUTH_FRONTEND_CID" \
    || fatal "ingress membership is not exact"
  probe_json_health "$ASSISTANT_HEALTH_URL" ready assistant \
    || fatal "original assistant health failed"
  probe_json_health "$CANONICAL_ORIGIN/health" ok app \
    || fatal "canonical HTTPS health is not semantic JSON"
  probe_json_health "$CANONICAL_ORIGIN/health/db" ok db \
    || fatal "canonical HTTPS DB health is not semantic JSON"
  headers=$(curl --noproxy '*' --proto '=https' --tlsv1.2 \
    --connect-timeout 5 --max-time 15 --max-redirs 0 \
    -fsS -D - -o /dev/null "$CANONICAL_ORIGIN/")
  hsts_headers=$(
    printf '%s' "$headers" | tr '[:upper:]' '[:lower:]' | tr -d '\r' \
      | grep '^strict-transport-security:' || true
  )
  [ "$hsts_headers" = "strict-transport-security: max-age=300" ] \
    || fatal "edge promotion requires exact pre-HSTS header"
  headers=$(curl --noproxy '*' --proto '=http' --connect-timeout 5 \
    --max-time 15 --max-redirs 0 -sS -D - -o /dev/null \
    "$EDGE_ORIGIN/edge-check/path?scope=1")
  [ "$(printf '%s' "$headers" | tr -d '\r' | head -1)" \
    = "HTTP/1.1 308 Permanent Redirect" ] \
    || fatal "legacy edge GET status is not 308"
  location=$(printf '%s' "$headers" | tr -d '\r' \
    | sed -n 's/^[Ll]ocation: *//p')
  [ "$location" = "$CANONICAL_ORIGIN/edge-check/path?scope=1" ] \
    || fatal "legacy edge redirect target is wrong"
  cookie_count=$(printf '%s' "$headers" | tr '[:upper:]' '[:lower:]' \
    | grep -c '^set-cookie:' || true)
  [ "$cookie_count" -eq 0 ] || fatal "legacy edge emitted a cookie"
  headers=$(curl --noproxy '*' --proto '=http' --connect-timeout 5 \
    --max-time 15 --max-redirs 0 -sS -I \
    "$EDGE_ORIGIN/edge-check/path?scope=1")
  printf '%s' "$headers" | tr -d '\r' \
    | grep -Fx 'HTTP/1.1 308 Permanent Redirect' >/dev/null \
    || fatal "legacy edge HEAD status is not 308"
  for method in POST PUT PATCH DELETE; do
    headers=$(curl --noproxy '*' --proto '=http' --connect-timeout 5 \
      --max-time 15 --max-redirs 0 -sS -X "$method" -D - -o /dev/null \
      "$EDGE_ORIGIN/edge-check/path?scope=1")
    printf '%s' "$headers" | tr -d '\r' \
      | grep -Fx 'HTTP/1.1 405 Method Not Allowed' >/dev/null \
      || fatal "legacy edge unsafe method is not rejected"
    [ "$(printf '%s' "$headers" | tr -d '\r' \
      | grep -Eic '^allow: *GET, HEAD$' || true)" -eq 1 ] \
      || fatal "legacy edge unsafe method Allow header is not exact"
    cookie_count=$(printf '%s' "$headers" | tr '[:upper:]' '[:lower:]' \
      | grep -c '^set-cookie:' || true)
    [ "$cookie_count" -eq 0 ] \
      || fatal "legacy edge unsafe method emitted a cookie"
  done
  headers=$(curl --noproxy '*' --proto '=http' --connect-timeout 5 \
    --max-time 15 --max-redirs 0 -sS -D - -o /dev/null \
    "http://hbzgc.icu/edge-check/path?scope=1")
  printf '%s' "$headers" | tr -d '\r' \
    | grep -Fx "Location: $CANONICAL_ORIGIN/edge-check/path?scope=1" \
    >/dev/null || fatal "canonical HTTP redirect changed"
}

verify_pre_runtime() {
  local runtime_caddy_cid runtime_caddy_image runtime_caddy_restarts
  local networks frontend_networks service_networks ingress_members listeners
  local headers hsts_headers
  runtime_caddy_cid=$(
    docker inspect -f '{{.Id}}' personal-ai-assistant-caddy
  )
  [[ "$runtime_caddy_cid" =~ ^[0-9a-f]{64}$ ]] \
    || fatal "pre-edge shared Caddy identity is invalid"
  runtime_caddy_image=$(docker inspect -f '{{.Image}}' "$runtime_caddy_cid")
  runtime_caddy_restarts=$(
    docker inspect -f '{{.RestartCount}}' "$runtime_caddy_cid"
  )
  [ "$(docker inspect -f '{{.State.Running}}' \
    personal-ai-assistant-caddy)" = true ] \
    && [ "$runtime_caddy_image" = "$AUTH_CADDY_IMAGE" ] \
    && [ "$runtime_caddy_restarts" = 0 ] \
    || fatal "pre-edge shared Caddy runtime is not exact"
  docker exec personal-ai-assistant-caddy caddy validate \
    --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null \
    || fatal "pre-edge shared Caddy validation failed"
  [ -z "$(docker compose --env-file "$ASSISTANT_ENV" -f "$COMPOSE_FILE" \
    port caddy 8080)" ] || fatal "pre-edge private listener is still published"
  listeners=$(ss -H -ltnp '( sport = :8080 )')
  [ "$(printf '%s\n' "$listeners" | awk '{print $4}' | LC_ALL=C sort)" \
    = "127.0.0.1:8080" ] \
    || fatal "pre-edge port 8080 listeners are not exact"
  networks=$(docker inspect -f '{{json .NetworkSettings.Networks}}' \
    personal-ai-assistant-caddy)
  json_exact_keys "$networks" \
    personal-ai-assistant-network it-spareparts-ingress \
    || fatal "pre-edge shared Caddy networks are not exact"
  frontend_networks=$(docker inspect \
    -f '{{json .NetworkSettings.Networks}}' "$AUTH_FRONTEND_CID")
  json_exact_keys "$frontend_networks" \
    it-spareparts_default it-spareparts-ingress \
    || fatal "pre-edge frontend networks are not exact"
  for cid in "$AUTH_APP_CID" "$AUTH_DB_CID"; do
    service_networks=$(docker inspect \
      -f '{{json .NetworkSettings.Networks}}' "$cid")
    json_exact_keys "$service_networks" it-spareparts_default \
      || fatal "pre-edge internal service networks are not exact"
  done
  [ "$(docker network inspect -f '{{.Internal}}' it-spareparts-ingress)" \
    = true ] || fatal "pre-edge ingress is not internal"
  ingress_members=$(docker network inspect \
    -f '{{json .Containers}}' it-spareparts-ingress)
  json_exact_keys "$ingress_members" "$runtime_caddy_cid" \
    "$AUTH_FRONTEND_CID" \
    || fatal "pre-edge ingress membership is not exact"
  probe_json_health "$ASSISTANT_HEALTH_URL" ready assistant \
    || fatal "pre-edge original assistant health failed"
  probe_json_health "$CANONICAL_ORIGIN/health" ok app \
    || fatal "pre-edge canonical HTTPS health is not semantic JSON"
  probe_json_health "$CANONICAL_ORIGIN/health/db" ok db \
    || fatal "pre-edge canonical HTTPS DB health is not semantic JSON"
  probe_json_health "http://127.0.0.1:8080/health" ok app \
    || fatal "pre-edge loopback health is not semantic JSON"
  probe_json_health "http://127.0.0.1:8080/health/db" ok db \
    || fatal "pre-edge loopback DB health is not semantic JSON"
  headers=$(curl --noproxy '*' --proto '=https' --tlsv1.2 \
    --connect-timeout 5 --max-time 15 --max-redirs 0 \
    -fsS -D - -o /dev/null "$CANONICAL_ORIGIN/")
  hsts_headers=$(
    printf '%s' "$headers" | tr '[:upper:]' '[:lower:]' | tr -d '\r' \
      | grep '^strict-transport-security:' || true
  )
  [ "$hsts_headers" = "strict-transport-security: max-age=300" ] \
    || fatal "pre-edge HSTS is not exact"
  if curl --noproxy '*' --proto '=http' --connect-timeout 2 \
      --max-time 5 --max-redirs 0 -sS -o /dev/null \
      "$EDGE_ORIGIN/edge-check/path?scope=1"; then
    fatal "pre-edge private redirect remains reachable"
  fi
  headers=$(curl --noproxy '*' --proto '=http' --connect-timeout 5 \
    --max-time 15 --max-redirs 0 -sS -D - -o /dev/null \
    "http://hbzgc.icu/edge-check/path?scope=1")
  printf '%s' "$headers" | tr -d '\r' \
    | grep -Fx "Location: $CANONICAL_ORIGIN/edge-check/path?scope=1" \
    >/dev/null || fatal "pre-edge canonical HTTP redirect changed"
}

recreate_caddy() {
  local dir=$1 action=$2
  local action_base_cid action_base_image action_base_restarts
  validate_recreate_pending "$TARGET_COMMIT" "$GENERATION" || return 73
  [ "$(manifest_value "$RECREATE_PENDING" ACTION)" = "$action" ] || return 73
  action_base_cid=$(manifest_value "$RECREATE_PENDING" OLD_CADDY_CID)
  action_base_image=$(manifest_value "$RECREATE_PENDING" OLD_CADDY_IMAGE)
  action_base_restarts=$(
    manifest_value "$RECREATE_PENDING" OLD_CADDY_RESTARTS
  )
  [ "$(docker inspect -f '{{.Id}}' personal-ai-assistant-caddy)" \
    = "$action_base_cid" ] || return 73
  if [ "$TEST_MODE" = 1 ] \
      && [ "${EDGE_TEST_FAILPOINT:-}" = before-caddy-recreate ]; then
    return 74
  fi
  docker compose --env-file "$ASSISTANT_ENV" -f "$COMPOSE_FILE" \
    up -d --no-deps --force-recreate caddy >/dev/null || return $?
  if [ "$TEST_MODE" = 1 ] \
      && [ "${EDGE_TEST_FAILPOINT:-}" \
      = after-caddy-recreate-before-lineage ]; then
    return 74
  fi
  publish_successor_lineage "$dir" "$action" "$action_base_cid" \
    "$action_base_image" "$action_base_restarts" || return $?
  if [ "$TEST_MODE" = 1 ] \
      && [ "${EDGE_TEST_FAILPOINT:-}" \
      = after-successor-lineage-before-intent-clear ]; then
    return 74
  fi
  clear_recreate_pending || return $?
}

begin_recreate_transaction() {
  local dir=$1 action=$2 old_compose=$3 old_caddy=$4
  local target_compose=$5 target_caddy=$6
  local cid image restarts
  if [ -e "$RECREATE_PENDING" ] || [ -L "$RECREATE_PENDING" ]; then
    validate_recreate_pending "$TARGET_COMMIT" "$GENERATION" || return 73
    return 0
  fi
  publish_current_successor_lineage "$dir" "$action" || return $?
  cid=$(docker inspect -f '{{.Id}}' personal-ai-assistant-caddy)
  image=$(docker inspect -f '{{.Image}}' "$cid")
  restarts=$(docker inspect -f '{{.RestartCount}}' "$cid")
  write_recreate_pending "$dir" "$action" "$cid" "$image" "$restarts" \
    "$old_compose" "$old_caddy" "$target_compose" "$target_caddy"
}

prepare_generation() {
  local target=$1 generation=$2
  local dir="$GENERATIONS_DIR/$generation"
  local staging status
  if [ -e "$dir" ] || [ -L "$dir" ]; then
    validate_generation "$dir" "$target" "$generation" \
      || fatal "existing edge generation is invalid"
    if side_matches "$dir" PRE; then
      verify_pre_runtime
    elif side_matches "$dir" POST; then
      verify_runtime
    else
      fatal "existing edge generation is divergent"
    fi
    printf 'EDGE_PREPARE_OK generation=%s idempotent=1\n' "$generation"
    return
  fi
  safe_regular "$COMPOSE_FILE" || fatal "assistant compose is unsafe"
  safe_regular "$CADDYFILE" || fatal "assistant Caddyfile is unsafe"
  safe_regular "$APP_COMPOSE" || fatal "IT compose is unsafe"
  safe_app_env || fatal "IT env is unsafe"
  safe_assistant_env || fatal "assistant env is unsafe"
  verify_pre_runtime
  staging=$(mktemp -d -- "$GENERATIONS_DIR/.incoming-$generation.XXXXXX")
  cleanup_prepare() {
    status=$?
    trap - RETURN
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
  cp -- "$COMPOSE_FILE" "$staging/compose.pre"
  cp -- "$CADDYFILE" "$staging/Caddyfile.pre"
  make_post_compose "$COMPOSE_FILE" "$staging/compose.post"
  [ "$(grep -F -c -- "$EDGE_BINDING" "$staging/compose.post")" -eq 1 ] \
    || fatal "edge Compose candidate binding is not exact"
  make_post_caddyfile "$CADDYFILE" "$staging/Caddyfile.post"
  chmod 600 "$staging"/compose.* "$staging"/Caddyfile.*
  if [ "$TEST_MODE" != 1 ]; then chown -R root:root "$staging"; fi
  docker run --rm --network none --read-only \
    --tmpfs /config:rw,nosuid,nodev,noexec \
    --tmpfs /data:rw,nosuid,nodev,noexec \
    --volume "$staging/Caddyfile.post:/etc/caddy/Caddyfile:ro" \
    "$AUTH_CADDY_IMAGE" \
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile \
    >/dev/null || fatal "edge Caddy candidate validation failed"
  {
    printf 'EDGE_FORMAT=edge-v120-1\n'
    printf 'TARGET_COMMIT=%s\nGENERATION=%s\n' "$target" "$generation"
    printf 'CONTROL_MANIFEST_HASH=%s\n' "$RUNTIME_CONTROL_MANIFEST_HASH"
    printf 'RELEASE_ID=%s\nRELEASE_STATE_GENERATION=%s\n' \
      "$AUTH_RELEASE_ID" "$AUTH_STATE_GENERATION"
    printf 'RELEASE_STATE_SHA256=%s\n' "$AUTH_STATE_SHA256"
    printf 'APP_COMPOSE_SHA256=%s\n' \
      "$(sha256sum "$APP_COMPOSE" | cut -d' ' -f1)"
    printf 'ASSISTANT_COMPOSE_PRE_SHA256=%s\n' \
      "$(sha256sum "$staging/compose.pre" | cut -d' ' -f1)"
    printf 'ASSISTANT_COMPOSE_POST_SHA256=%s\n' \
      "$(sha256sum "$staging/compose.post" | cut -d' ' -f1)"
    printf 'ASSISTANT_RENDER_PRE_SHA256=%s\n' \
      "$(render_sha256 "$staging/compose.pre")"
    printf 'ASSISTANT_RENDER_POST_SHA256=%s\n' \
      "$(render_sha256 "$staging/compose.post")"
    printf 'CADDYFILE_PRE_SHA256=%s\n' \
      "$(sha256sum "$staging/Caddyfile.pre" | cut -d' ' -f1)"
    printf 'CADDYFILE_POST_SHA256=%s\n' \
      "$(sha256sum "$staging/Caddyfile.post" | cut -d' ' -f1)"
    printf 'AUTH_APP_CID=%s\nAUTH_APP_IMAGE=%s\nAUTH_APP_RESTARTS=%s\n' \
      "$AUTH_APP_CID" "$AUTH_APP_IMAGE" "$AUTH_APP_RESTARTS"
    printf 'AUTH_FRONTEND_CID=%s\nAUTH_FRONTEND_IMAGE=%s\nAUTH_FRONTEND_RESTARTS=%s\n' \
      "$AUTH_FRONTEND_CID" "$AUTH_FRONTEND_IMAGE" "$AUTH_FRONTEND_RESTARTS"
    printf 'AUTH_DB_CID=%s\nAUTH_DB_IMAGE=%s\nAUTH_DB_RESTARTS=%s\n' \
      "$AUTH_DB_CID" "$AUTH_DB_IMAGE" "$AUTH_DB_RESTARTS"
    printf 'AUTH_CADDY_CID=%s\nAUTH_CADDY_IMAGE=%s\nAUTH_CADDY_RESTARTS=%s\n' \
      "$AUTH_CADDY_CID" "$AUTH_CADDY_IMAGE" "$AUTH_CADDY_RESTARTS"
  } > "$staging/manifest.txt"
  write_state "$staging/state.txt" prepared
  (
    cd "$staging"
    sha256sum manifest.txt compose.pre compose.post \
      Caddyfile.pre Caddyfile.post > SHA256SUMS
  )
  chmod 600 "$staging/manifest.txt" "$staging/state.txt" "$staging/SHA256SUMS"
  if [ "$TEST_MODE" != 1 ]; then chown -R root:root "$staging"; fi
  validate_generation "$staging" "$target" "$generation" \
    || fatal "edge staging validation failed"
  sync -f "$staging"/* || return $?
  sync -f "$staging" || return $?
  mv -T -- "$staging" "$dir" || return $?
  staging=
  sync -d "$GENERATIONS_DIR" || return $?
  trap - RETURN
  printf 'EDGE_PREPARE_OK generation=%s idempotent=0\n' "$generation"
}

promote_generation() {
  local target=$1 generation=$2
  local dir="$GENERATIONS_DIR/$generation"
  local state pre_compose post_compose pre_caddy post_caddy
  validate_generation "$dir" "$target" "$generation" \
    || fatal "edge generation validation failed"
  state=$(state_value "$dir/state.txt")
  pre_compose=$(manifest_value "$dir/manifest.txt" ASSISTANT_COMPOSE_PRE_SHA256)
  post_compose=$(manifest_value "$dir/manifest.txt" ASSISTANT_COMPOSE_POST_SHA256)
  pre_caddy=$(manifest_value "$dir/manifest.txt" CADDYFILE_PRE_SHA256)
  post_caddy=$(manifest_value "$dir/manifest.txt" CADDYFILE_POST_SHA256)
  if side_matches "$dir" POST; then
    if [ -e "$RECREATE_PENDING" ] || [ -L "$RECREATE_PENDING" ]; then
      recreate_caddy "$dir" promote \
        || fatal "interrupted edge promotion could not recreate Caddy"
    fi
    if [ "$state" = promoted ]; then
      verify_runtime
    elif [ "$state" = prepared ]; then
      if ! (verify_runtime) >/dev/null 2>&1; then
        begin_recreate_transaction "$dir" promote \
          "$pre_compose" "$pre_caddy" "$post_compose" "$post_caddy" \
          || fatal "edge promotion intent could not be persisted"
        recreate_caddy "$dir" promote
      fi
      verify_runtime
      finalize_edge_cas_backups promote "$pre_compose" "$pre_caddy" \
        || fatal "edge promotion CAS backup finalization failed"
      publish_state "$dir" promoted
    else
      fatal "edge promotion state is invalid"
    fi
    printf 'EDGE_PROMOTE_OK generation=%s idempotent=1\n' "$generation"
    return
  fi
  [ "$state" = prepared ] || fatal "edge promotion state is invalid"
  begin_recreate_transaction "$dir" promote \
    "$pre_compose" "$pre_caddy" "$post_compose" "$post_caddy" \
    || fatal "edge promotion intent could not be persisted"
  if file_side_matches "$dir" caddy PRE; then
    file_side_matches "$dir" compose PRE \
      || fatal "edge promotion configuration is divergent"
    install_snapshot "$dir/Caddyfile.post" "$CADDYFILE" \
      "$pre_caddy" "$post_caddy" caddy promote \
      || fatal "edge Caddy CAS failed"
    publish_current_successor_lineage "$dir" promote
    if [ "$TEST_MODE" = 1 ] \
        && [ "${EDGE_TEST_FAILPOINT:-}" = after-caddy-rename ]; then
      return 74
    fi
  else
    file_side_matches "$dir" caddy POST \
      || fatal "edge promotion Caddy state is divergent"
  fi
  if file_side_matches "$dir" compose PRE; then
    install_snapshot "$dir/compose.post" "$COMPOSE_FILE" \
      "$pre_compose" "$post_compose" compose promote \
      || fatal "edge Compose CAS failed"
    publish_current_successor_lineage "$dir" promote
  else
    file_side_matches "$dir" compose POST \
      || fatal "edge promotion Compose state is divergent"
  fi
  if [ "$TEST_MODE" = 1 ] \
      && [ "${EDGE_TEST_FAILPOINT:-}" \
      = after-final-cas-before-recreate ]; then
    return 74
  fi
  recreate_caddy "$dir" promote
  side_matches "$dir" POST || fatal "edge promotion changed unrelated data"
  verify_runtime
  finalize_edge_cas_backups promote "$pre_compose" "$pre_caddy" \
    || fatal "edge promotion CAS backup finalization failed"
  publish_state "$dir" promoted
  printf 'EDGE_PROMOTE_OK generation=%s idempotent=0\n' "$generation"
}

rollback_generation() {
  local target=$1 generation=$2
  local dir="$GENERATIONS_DIR/$generation"
  local state pre_compose post_compose pre_caddy post_caddy
  validate_generation "$dir" "$target" "$generation" \
    || fatal "edge generation validation failed"
  state=$(state_value "$dir/state.txt")
  pre_compose=$(manifest_value "$dir/manifest.txt" ASSISTANT_COMPOSE_PRE_SHA256)
  post_compose=$(manifest_value "$dir/manifest.txt" ASSISTANT_COMPOSE_POST_SHA256)
  pre_caddy=$(manifest_value "$dir/manifest.txt" CADDYFILE_PRE_SHA256)
  post_caddy=$(manifest_value "$dir/manifest.txt" CADDYFILE_POST_SHA256)
  if side_matches "$dir" PRE; then
    [ "$state" = promoted ] || [ "$state" = rolled_back ] \
      || fatal "edge rollback state is invalid"
    if [ -e "$RECREATE_PENDING" ] || [ -L "$RECREATE_PENDING" ]; then
      recreate_caddy "$dir" rollback \
        || fatal "interrupted edge rollback could not recreate Caddy"
    fi
    verify_pre_runtime
    finalize_edge_cas_backups rollback "$post_compose" "$post_caddy" \
      || fatal "edge rollback CAS backup finalization failed"
    [ "$state" = rolled_back ] || publish_state "$dir" rolled_back
    printf 'EDGE_ROLLBACK_OK generation=%s idempotent=1\n' "$generation"
    return
  fi
  [ "$state" = promoted ] || [ "$state" = prepared ] \
    || fatal "edge rollback state is invalid"
  begin_recreate_transaction "$dir" rollback \
    "$post_compose" "$post_caddy" "$pre_compose" "$pre_caddy" \
    || fatal "edge rollback intent could not be persisted"
  if file_side_matches "$dir" compose POST; then
    file_side_matches "$dir" caddy POST \
      || fatal "edge rollback configuration is divergent"
    install_snapshot "$dir/compose.pre" "$COMPOSE_FILE" \
      "$post_compose" "$pre_compose" compose rollback \
      || fatal "edge Compose rollback CAS failed"
    publish_current_successor_lineage "$dir" rollback
    if [ "$TEST_MODE" = 1 ] \
        && [ "${EDGE_TEST_FAILPOINT:-}" = after-compose-rollback ]; then
      return 74
    fi
  else
    file_side_matches "$dir" compose PRE \
      || fatal "edge rollback Compose state is divergent"
  fi
  if file_side_matches "$dir" caddy POST; then
    install_snapshot "$dir/Caddyfile.pre" "$CADDYFILE" \
      "$post_caddy" "$pre_caddy" caddy rollback \
      || fatal "edge Caddy rollback CAS failed"
    publish_current_successor_lineage "$dir" rollback
  else
    file_side_matches "$dir" caddy PRE \
      || fatal "edge rollback Caddy state is divergent"
  fi
  if [ "$TEST_MODE" = 1 ] \
      && [ "${EDGE_TEST_FAILPOINT:-}" \
      = after-final-cas-before-recreate ]; then
    return 74
  fi
  recreate_caddy "$dir" rollback
  side_matches "$dir" PRE || fatal "edge rollback changed unrelated data"
  verify_pre_runtime
  finalize_edge_cas_backups rollback "$post_compose" "$post_caddy" \
    || fatal "edge rollback CAS backup finalization failed"
  publish_state "$dir" rolled_back
  printf 'EDGE_ROLLBACK_OK generation=%s idempotent=0\n' "$generation"
}

inspect_generation() {
  local target=$1 generation=$2 state pending_action
  local dir="$GENERATIONS_DIR/$generation"
  validate_generation "$dir" "$target" "$generation" || {
    printf 'divergent-or-unknown\n'
    return 78
  }
  state=$(state_value "$dir/state.txt") || {
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
  if [ "$state" = prepared ] \
      && file_side_matches "$dir" caddy POST \
      && file_side_matches "$dir" compose PRE; then
    printf 'exact-promote-pending\n'
    return 0
  fi
  if [ "$state" = promoted ] \
      && file_side_matches "$dir" compose PRE \
      && file_side_matches "$dir" caddy POST; then
    printf 'exact-rollback-pending\n'
    return 0
  fi
  if side_matches "$dir" POST; then
    [ "$state" = promoted ] || [ "$state" = prepared ] || {
      printf 'divergent-or-unknown\n'; return 78;
    }
    if ! (verify_runtime) >/dev/null 2>&1; then
      if [ "$state" = prepared ]; then
        printf 'exact-promote-pending\n'
        return 0
      fi
      printf 'divergent-or-unknown\n'
      return 78
    fi
    printf 'exact-promoted\n'
  elif side_matches "$dir" PRE; then
    if ! (verify_pre_runtime) >/dev/null 2>&1; then
      printf 'divergent-or-unknown\n'
      return 78
    fi
    case "$state" in
      prepared) printf 'exact-pre\n' ;;
      promoted|rolled_back) printf 'exact-rolled-back\n' ;;
      *) printf 'divergent-or-unknown\n'; return 78 ;;
    esac
  else
    printf 'divergent-or-unknown\n'
    return 78
  fi
}

if [ "${EDGE_ROOT_LIBRARY_ONLY:-0}" = 1 ]; then
  [ "$TEST_MODE" = 1 ] || fatal "edge library mode is test-only"
  return 0
fi

[ "$#" -eq 3 ] \
  || fatal "usage: edge_v120_root.sh <prepare|promote|rollback|inspect> <target SHA> <generation>"
ACTION=$1
TARGET_COMMIT=$2
GENERATION=$3
[[ "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ ]] || fatal "invalid target SHA"
[[ "$GENERATION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$ ]] \
  || fatal "invalid edge generation"

acquire_lock
acquire_shared_caddy_lock
verify_packaged_control "$TARGET_COMMIT"
# shellcheck source=.deploy/v120_state.sh
source "$V120_STATE_LIBRARY"
verify_release_authority "$TARGET_COMMIT" "$ACTION"
verify_private_host_address
ensure_directory "$EDGE_DIR" 700
ensure_directory "$GENERATIONS_DIR" 700
case "$ACTION" in
  prepare) prepare_generation "$TARGET_COMMIT" "$GENERATION" ;;
  promote) promote_generation "$TARGET_COMMIT" "$GENERATION" ;;
  rollback) rollback_generation "$TARGET_COMMIT" "$GENERATION" ;;
  inspect) inspect_generation "$TARGET_COMMIT" "$GENERATION" ;;
  *) fatal "unknown edge action" ;;
esac
