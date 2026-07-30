#!/usr/bin/env bash
# Restore old business images only before the persisted public-opening boundary.
set -Eeuo pipefail
umask 077

readonly APP_DIR=/home/ubuntu/apps/it-spareparts
readonly CONTROL_DIR=/var/lib/it-spareparts-release-control
readonly ROOT_STATE="$CONTROL_DIR/v120-state.state"
readonly LOCK_PATH=/run/lock/it-spareparts-v120
readonly EXPECTED_DB_HEAD=f1c8e4a7b2d9
readonly EXPECTED_OLD_COMMIT=ab42005b5b94bf98b3db0e4bff87e5df9da2f7ca
readonly EXPECTED_HTTPS_HOST=hbzgc.icu
readonly EDGE_CADDYFILE=/opt/personal-ai-assistant/Caddyfile
readonly EDGE_COMPOSE=/opt/personal-ai-assistant/compose.production.yml
SCRIPT_DIR=$(
  cd "$(dirname "${BASH_SOURCE[0]}")"
  pwd -P
)
readonly SCRIPT_DIR
RUNTIME_CONTROL_MANIFEST_HASH=$(basename -- "$SCRIPT_DIR")
readonly RUNTIME_CONTROL_MANIFEST_HASH
readonly LIBRARY="$SCRIPT_DIR/v120_state.sh"
readonly ROOT_SYNC="$SCRIPT_DIR/sync-v120-root-state.sh"

fatal() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

[ "$EUID" -eq 0 ] || fatal "rollback_v120.sh must run as root"
[[ "$RUNTIME_CONTROL_MANIFEST_HASH" =~ ^[0-9a-f]{64}$ ]] \
  || fatal "rollback is not inside a hash-addressed control version"
[ "$SCRIPT_DIR" \
  = "$CONTROL_DIR/versions/$RUNTIME_CONTROL_MANIFEST_HASH" ] \
  || fatal "rollback control version path is unsafe"
"$SCRIPT_DIR/install-v120-control.sh" verify "$RUNTIME_CONTROL_MANIFEST_HASH" \
  >/dev/null
[ -d "$CONTROL_DIR" ] && [ ! -L "$CONTROL_DIR" ] \
  || fatal "unsafe root control directory"
[ "$(stat -c '%a %U:%G' "$CONTROL_DIR")" = "700 root:root" ] \
  || fatal "root control directory owner/mode mismatch"
[ -f "$LIBRARY" ] && [ ! -L "$LIBRARY" ] \
  || fatal "trusted state library is missing"
[ "$(stat -c '%a %U:%G %h' "$LIBRARY")" = "700 root:root 1" ] \
  || fatal "trusted state library owner/mode mismatch"
[ -f "$ROOT_SYNC" ] && [ ! -L "$ROOT_SYNC" ] \
  && [ "$(stat -c '%a %U:%G %h' "$ROOT_SYNC")" \
    = "700 root:root 1" ] \
  || fatal "trusted root state sync helper is missing or unsafe"
# shellcheck source=.deploy/v120_state.sh
source "$LIBRARY"

compose() {
  env \
    -u COMPOSE_FILE \
    -u COMPOSE_PROJECT_NAME \
    -u COMPOSE_PROFILES \
    docker compose \
      --project-name it-spareparts \
      --env-file "$APP_DIR/.env" \
      -f "$APP_DIR/docker-compose.yml" \
      "$@"
}

check_container_id() {
  [[ "$1" =~ ^[0-9a-f]{64}$ ]]
}

check_sha256_id() {
  [[ "$1" =~ ^sha256:[0-9a-f]{64}$ ]]
}

check_compose_identity() {
  local cid=$1
  [ "$(docker inspect -f \
    '{{index .Config.Labels "com.docker.compose.project"}}' "$cid")" \
    = it-spareparts ]
  [ "$(docker inspect -f \
    '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$cid")" \
    = "$APP_DIR/docker-compose.yml" ]
  [ "$(docker inspect -f \
    '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$cid")" \
    = "$APP_DIR" ]
}

check_loopback_8080() {
  local listeners
  listeners=$(ss -H -ltn '( sport = :8080 )') \
    || fatal "cannot inspect port 8080"
  [ -n "$listeners" ] || fatal "port 8080 is not listening"
  awk '$4 != "127.0.0.1:8080" { exit 1 }' <<< "$listeners" \
    || fatal "port 8080 has a non-loopback listener"
}

check_internal_health() {
  local _
  for _ in $(seq 1 30); do
    if compose exec -T app python - >/dev/null 2>&1 <<'PY'
import json
from urllib.request import urlopen

for path in ("/health", "/health/db"):
    with urlopen("http://127.0.0.1:8000" + path, timeout=5) as response:
        assert response.status == 200
        payload = json.load(response)
        assert payload.get("status") == "ok", payload
        if path == "/health/db":
            assert payload.get("db") == "reachable", payload
PY
    then
      return 0
    fi
    sleep 1
  done
  return 1
}

check_frontend_and_https() {
  local _
  for _ in $(seq 1 30); do
    if curl --noproxy '*' --fail --silent --show-error --max-time 5 \
        http://127.0.0.1:8080/ >/dev/null 2>&1 \
        && curl --noproxy '*' --proto '=https' --tlsv1.2 \
          --fail --silent --show-error --max-time 8 \
          "https://$EXPECTED_HTTPS_HOST/" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

run_monitor_with_retry() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if sudo -u ubuntu "$APP_DIR/.deploy/monitor.sh"; then
      return 0
    fi
    [ "$attempt" -lt 5 ] || break
    sleep 3
  done
  return 1
}

stop_business_and_verify() {
  local running
  local grep_status
  compose stop frontend app || return $?
  running=$(compose ps --status running --services) || return $?
  if grep -Eq '^(app|frontend)$' <<< "$running"; then
    printf 'FATAL: app/frontend remain running after stop\n' >&2
    return 1
  else
    grep_status=$?
    [ "$grep_status" -eq 1 ] || return "$grep_status"
  fi
}

commit_root_transition() {
  local candidate
  candidate=$(mktemp -- "$CONTROL_DIR/.v120-rollback-next.XXXXXX") \
    || return $?
  if ! v120_state_prepare_update "$ROOT_STATE" "$candidate" "$@"; then
    rm -f -- "$candidate"
    return 1
  fi
  if ! "$ROOT_SYNC" < "$candidate"; then
    rm -f -- "$candidate"
    return 1
  fi
  cmp -s "$candidate" "$ROOT_STATE" || {
    rm -f -- "$candidate"
    return 73
  }
  rm -f -- "$candidate"
}

mirror_root_state() {
  local destination="$APP_DIR/backups/$RELEASE_ID.state"
  local temporary
  [ -d "$APP_DIR/backups" ] && [ ! -L "$APP_DIR/backups" ] \
    || return 1
  [ "$(stat -c '%a %U:%G' "$APP_DIR/backups")" = "700 ubuntu:ubuntu" ] \
    || return 1
  if [ -e "$destination" ] || [ -L "$destination" ]; then
    [ -f "$destination" ] && [ ! -L "$destination" ] \
      && [ "$(stat -c '%a %U:%G %h' "$destination")" \
        = "600 ubuntu:ubuntu 1" ] || return 1
  fi
  temporary=$(mktemp -- "$APP_DIR/backups/.v120-root-mirror.XXXXXX") \
    || return $?
  install -m 600 -o ubuntu -g ubuntu "$ROOT_STATE" "$temporary" \
    || {
      rm -f -- "$temporary"
      return 1
    }
  # Populated through an indirect reference in v120_state_parse_to_array.
  # shellcheck disable=SC2034
  declare -A mirrored_state=()
  if ! v120_state_parse_to_array "$temporary" mirrored_state; then
    rm -f -- "$temporary"
    return 1
  fi
  mv -fT -- "$temporary" "$destination" || return $?
  sync -f "$destination" || return $?
  sync -d "$APP_DIR/backups" || return $?
}

verify_control_plane() {
  local edge_cid
  [ "$(sha256sum "$APP_DIR/docker-compose.yml" | cut -d' ' -f1)" \
    = "$APP_COMPOSE_HASH" ] || fatal "active compose hash drifted"
  [ "$(stat -c '%a %U:%G' "$APP_DIR/docker-compose.yml")" \
    = "644 root:root" ] || fatal "active compose owner/mode drifted"
  [ -f "$APP_DIR/.env" ] && [ ! -L "$APP_DIR/.env" ]
  [ "$(stat -c '%a %U' "$APP_DIR/.env")" = "600 ubuntu" ]

  [ "$(compose ps -q db)" = "$BASE_DB_CID" ] \
    || fatal "DB container changed"
  check_compose_identity "$BASE_DB_CID"
  [ "$(docker inspect -f '{{.Image}}' "$BASE_DB_CID")" \
    = "$BASE_DB_IMAGE_ID" ] || fatal "DB image changed"
  [ "$(docker inspect -f '{{.State.Running}}' "$BASE_DB_CID")" = true ]
  [ "$(docker inspect -f '{{.RestartCount}}' "$BASE_DB_CID")" \
    = "$BASE_DB_RESTARTS" ] || fatal "DB restart count changed"
  [ "$(compose exec -T db psql -U spareparts -d spareparts -At \
    -c 'SELECT version_num FROM alembic_version;')" = "$EXPECTED_DB_HEAD" ]

  edge_cid=$(
    docker ps -q --no-trunc --filter name=^/personal-ai-assistant-caddy$
  )
  [ "$edge_cid" = "$BASE_EDGE_CID" ] || fatal "HTTPS edge container changed"
  [ "$(docker inspect -f '{{.State.Running}}' "$BASE_EDGE_CID")" = true ]
  [ "$(docker inspect -f '{{.RestartCount}}' "$BASE_EDGE_CID")" \
    = "$BASE_EDGE_RESTARTS" ] || fatal "HTTPS edge restart count changed"
  [ "$(stat -c '%a %U:%G' "$EDGE_CADDYFILE")" = "644 root:root" ]
  [ "$(stat -c '%a %U:%G' "$EDGE_COMPOSE")" = "644 root:root" ]
  [ "$(sha256sum "$EDGE_CADDYFILE" | cut -d' ' -f1)" \
    = "$EDGE_CADDY_HASH" ]
  [ "$(sha256sum "$EDGE_COMPOSE" | cut -d' ' -f1)" \
    = "$EDGE_COMPOSE_HASH" ]
  [ "$(
    docker inspect -f \
      '{{with index .NetworkSettings.Networks "it-spareparts-ingress"}}yes{{end}}' \
      "$BASE_EDGE_CID"
  )" = yes ] || fatal "HTTPS edge left ingress"
}

[ "$#" -eq 0 ] || fatal "usage: rollback_v120.sh"

v120_acquire_lock "$LOCK_PATH" "750 root:ubuntu"

[ -f "$ROOT_STATE" ] && [ ! -L "$ROOT_STATE" ] \
  || fatal "root state is missing or unsafe"
[ "$(stat -c '%a %U:%G %h' "$ROOT_STATE")" = "600 root:root 1" ] \
  || fatal "root state owner/mode mismatch"
v120_state_load "$ROOT_STATE"
[ "${CONTROL_MANIFEST_HASH:-}" = "$RUNTIME_CONTROL_MANIFEST_HASH" ] \
  || fatal "root state targets another control version"

[[ "${RELEASE_ID:-}" =~ ^v120-[0-9a-f]{12}-[0-9]{14}$ ]]
[ "${OLD_COMMIT:-}" = "$EXPECTED_OLD_COMMIT" ]
[[ "${OLD_RUNNING_SOURCE_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]]
[ "$OLD_COMMIT" != "$OLD_RUNNING_SOURCE_COMMIT" ]
[ "${DB_HEAD:-}" = "$EXPECTED_DB_HEAD" ]
check_sha256_id "${OLD_APP_IMAGE_ID:-}"
check_sha256_id "${OLD_FRONTEND_IMAGE_ID:-}"
check_sha256_id "${BASE_DB_IMAGE_ID:-}"
check_container_id "${BASE_DB_CID:-}"
check_container_id "${BASE_EDGE_CID:-}"

ROLLBACK_STARTED=0
BUSINESS_RESTORED=0
rollback_abort() {
  local status=$?
  trap - EXIT HUP INT TERM
  if [ "$ROLLBACK_STARTED" = 1 ] && [ "$BUSINESS_RESTORED" != 1 ]; then
    if ! stop_business_and_verify >/dev/null 2>&1; then
      printf 'FATAL: rollback failed and business stop is unproven\n' >&2
      status=99
    fi
  fi
  v120_release_lock || status=97
  [ "$status" -ne 0 ] || status=98
  exit "$status"
}
trap rollback_abort EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

case "$RELEASE_PHASE" in
  prepared|backup_verified)
    if [ "$ROLLBACK_POLICY" = forward_only ]; then
      ROLLBACK_STARTED=1
      stop_business_and_verify \
        || fatal "could not prove forward-repair services are stopped"
      FAILED_AT=$(date --iso-8601=seconds)
      commit_root_transition \
        FAILED_AT "$FAILED_AT" RELEASE_PHASE failed_closed \
        || fatal "could not record failed-closed forward repair"
      v120_state_load "$ROOT_STATE"
      mirror_root_state || fatal "root state committed but mirror repair failed"
      fatal "forward-only release cannot restore old images; services stopped"
    fi
    ;;
  opening|switched)
    ROLLBACK_STARTED=1
    stop_business_and_verify \
      || fatal "could not prove public services are stopped"
    FAILED_AT=$(date --iso-8601=seconds)
    commit_root_transition \
      FAILED_AT "$FAILED_AT" RELEASE_PHASE failed_closed \
      || fatal "could not record failed-closed rollback refusal"
    v120_state_load "$ROOT_STATE"
    mirror_root_state || fatal "root state committed but mirror repair failed"
    fatal "old-image rollback is forbidden after public opening; services stopped"
    ;;
  failed_closed)
    ROLLBACK_STARTED=1
    stop_business_and_verify \
      || fatal "failed-closed state exists but service stop is unproven"
    mirror_root_state || fatal "failed-closed mirror repair failed"
    fatal "old-image rollback is forbidden for phase failed_closed"
    ;;
  observed)
    mirror_root_state || fatal "observed mirror repair failed"
    fatal "observed release is healthy and cannot be rolled back by v1.20 control"
    ;;
  rolled_back)
    mirror_root_state || fatal "rolled-back mirror repair failed"
    v120_release_lock
    trap - EXIT HUP INT TERM
    printf 'ROLLBACK_ALREADY_COMPLETE release=%s\n' "$RELEASE_ID"
    exit 0
    ;;
  *) fatal "old-image rollback is not allowed from phase $RELEASE_PHASE" ;;
esac

cd "$APP_DIR"
verify_control_plane
docker image inspect "$OLD_APP_IMAGE_ID" >/dev/null
docker image inspect "$OLD_FRONTEND_IMAGE_ID" >/dev/null

current_frontend=$(compose ps -q frontend)
if [ -n "$current_frontend" ]; then
  check_container_id "$current_frontend"
  current_frontend_image=$(docker inspect -f '{{.Image}}' "$current_frontend")
  [ "$current_frontend_image" != "$NEW_FRONTEND_IMAGE_ID" ] \
    || fatal "candidate frontend exists before opening; refusing old rollback"
fi

ROLLBACK_STARTED=1
compose stop frontend app
docker tag "$OLD_APP_IMAGE_ID" "$APP_IMAGE_REF"
docker tag "$OLD_FRONTEND_IMAGE_ID" "$FRONTEND_IMAGE_REF"
[ "$(docker image inspect -f '{{.Id}}' "$APP_IMAGE_REF")" \
  = "$OLD_APP_IMAGE_ID" ]
[ "$(docker image inspect -f '{{.Id}}' "$FRONTEND_IMAGE_REF")" \
  = "$OLD_FRONTEND_IMAGE_ID" ]

compose up -d --no-deps --no-build --force-recreate app
APP_CID=$(compose ps -q app)
check_container_id "$APP_CID"
check_compose_identity "$APP_CID"
[ "$(docker inspect -f '{{.Image}}' "$APP_CID")" = "$OLD_APP_IMAGE_ID" ]
[ "$(docker inspect -f '{{.RestartCount}}' "$APP_CID")" = 0 ]
check_internal_health || fatal "old app health check failed"

compose up -d --no-deps --no-build --force-recreate frontend
FRONTEND_CID=$(compose ps -q frontend)
check_container_id "$FRONTEND_CID"
check_compose_identity "$FRONTEND_CID"
[ "$(docker inspect -f '{{.Image}}' "$FRONTEND_CID")" \
  = "$OLD_FRONTEND_IMAGE_ID" ]
[ "$(docker inspect -f '{{.RestartCount}}' "$FRONTEND_CID")" = 0 ]
[ "$(compose port frontend 80)" = 127.0.0.1:8080 ]
check_loopback_8080
[ "$(
  docker inspect -f \
    '{{with index .NetworkSettings.Networks "it-spareparts-ingress"}}yes{{end}}' \
    "$FRONTEND_CID"
)" = yes ]
verify_control_plane
check_frontend_and_https || fatal "old frontend/HTTPS readiness failed"

# Core old business and the immutable control plane are now proven healthy.
# Later audit/state failures must not take the restored service back down.
BUSINESS_RESTORED=1
run_monitor_with_retry || fatal "restored business is healthy but monitor failed"

ROLLED_BACK_AT=$(date --iso-8601=seconds)
commit_root_transition \
  ROLLED_BACK_AT "$ROLLED_BACK_AT" RELEASE_PHASE rolled_back
v120_state_load "$ROOT_STATE"
mirror_root_state || fatal "root rollback committed but mirror repair failed"
v120_release_lock
trap - EXIT HUP INT TERM
printf 'ROLLBACK_OK release=%s app=%s frontend=%s at=%s\n' \
  "$RELEASE_ID" "$OLD_APP_IMAGE_ID" "$OLD_FRONTEND_IMAGE_ID" \
  "$ROLLED_BACK_AT"
