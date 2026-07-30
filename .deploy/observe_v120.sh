#!/usr/bin/env bash
# Hold the release lock for the complete 0/5/15/30 minute observation window.
set -Eeuo pipefail
umask 077

readonly APP_DIR=/home/ubuntu/apps/it-spareparts
readonly CONTROL_DIR=/var/lib/it-spareparts-release-control
readonly CONTROL_CURRENT="$CONTROL_DIR/current"
readonly ROOT_STATE="$CONTROL_DIR/v120-state.state"
readonly LOCK_PATH=/run/lock/it-spareparts-v120
readonly EXPECTED_HTTPS_HOST=hbzgc.icu
readonly EDGE_CADDYFILE=/opt/personal-ai-assistant/Caddyfile
readonly EDGE_COMPOSE=/opt/personal-ai-assistant/compose.production.yml
SCRIPT_DIR=$(
  cd "$(dirname "${BASH_SOURCE[0]}")"
  pwd -P
)
readonly SCRIPT_DIR

fatal() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

# shellcheck source=.deploy/v120_state.sh
source "$SCRIPT_DIR/v120_state.sh"

compose() {
  sudo env \
    -u COMPOSE_FILE \
    -u COMPOSE_PROJECT_NAME \
    -u COMPOSE_PROFILES \
    docker compose \
      --project-name it-spareparts \
      --env-file "$APP_DIR/.env" \
      -f "$APP_DIR/docker-compose.yml" \
      "$@"
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

advance_state() {
  local temporary
  sudo cmp -s "$STATE" "$ROOT_STATE" || return 73
  temporary=$(mktemp -- "$APP_DIR/backups/.v120-state.next.XXXXXX")
  if ! v120_state_prepare_update "$STATE" "$temporary" "$@"; then
    rm -f -- "$temporary"
    return 1
  fi
  # shellcheck disable=SC2024
  if ! sudo "$CONTROL_CURRENT/sync-v120-root-state.sh" < "$temporary"; then
    rm -f -- "$temporary"
    return 1
  fi
  sudo cmp -s "$temporary" "$ROOT_STATE" || {
    rm -f -- "$temporary"
    return 73
  }
  if ! v120_state_commit_mirror "$STATE" "$temporary"; then
    rm -f -- "$temporary"
    return 1
  fi
  v120_state_load "$STATE"
}

load_root_snapshot() {
  local snapshot
  snapshot=$(mktemp -- "$APP_DIR/backups/.v120-root-snapshot.XXXXXX") \
    || return $?
  # shellcheck disable=SC2024
  if ! sudo cat "$ROOT_STATE" > "$snapshot"; then
    rm -f -- "$snapshot"
    return 1
  fi
  chmod 600 "$snapshot"
  if ! v120_state_load "$snapshot"; then
    rm -f -- "$snapshot"
    return 1
  fi
  ROOT_SNAPSHOT=$snapshot
}

fail_closed_from_snapshot() {
  local snapshot=$1
  local candidate
  local failed_at
  stop_business_and_verify || return $?
  failed_at=$(date --iso-8601=seconds) || return $?
  candidate=$(mktemp -- "$APP_DIR/backups/.v120-root-next.XXXXXX") \
    || return $?
  if ! v120_state_prepare_update "$snapshot" "$candidate" \
      FAILED_AT "$failed_at" RELEASE_PHASE failed_closed; then
    rm -f -- "$candidate"
    return 1
  fi
  # shellcheck disable=SC2024
  if ! sudo "$CONTROL_CURRENT/sync-v120-root-state.sh" < "$candidate"; then
    rm -f -- "$candidate"
    return 1
  fi
  sudo cmp -s "$candidate" "$ROOT_STATE" || {
    rm -f -- "$candidate"
    return 73
  }
  if ! v120_state_commit_mirror "$STATE" "$candidate"; then
    rm -f -- "$candidate"
    return 1
  fi
}

check_compose_identity() {
  local cid=$1
  [ "$(sudo docker inspect -f \
    '{{index .Config.Labels "com.docker.compose.project"}}' "$cid")" \
    = it-spareparts ]
  [ "$(sudo docker inspect -f \
    '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$cid")" \
    = "$APP_DIR/docker-compose.yml" ]
  [ "$(sudo docker inspect -f \
    '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$cid")" \
    = "$APP_DIR" ]
}

check_loopback_8080() {
  local listeners
  listeners=$(sudo ss -H -ltn '( sport = :8080 )') \
    || fatal "cannot inspect port 8080"
  [ -n "$listeners" ]
  awk '$4 != "127.0.0.1:8080" { exit 1 }' <<< "$listeners" \
    || fatal "port 8080 has a non-loopback listener"
}

check_internal_health() {
  compose exec -T app python - >/dev/null <<'PY'
import json
from urllib.request import urlopen

for path in ("/health", "/health/db"):
    with urlopen("http://127.0.0.1:8000" + path, timeout=8) as response:
        assert response.status == 200
        payload = json.load(response)
        assert payload.get("status") == "ok", payload
        if path == "/health/db":
            assert payload.get("db") == "reachable", payload
PY
}

wait_minutes() {
  local minutes=$1
  local half_minutes=$((minutes * 2))
  local _
  for _ in $(seq 1 "$half_minutes"); do
    sleep 30
  done
}

wait_for_monitor_advance() {
  local previous=$1
  local _
  local current
  for _ in $(seq 1 20); do
    [ -f "$APP_DIR/monitor.status" ] && [ ! -L "$APP_DIR/monitor.status" ] \
      || fatal "monitor status is unsafe"
    current=$(stat -c '%Y' "$APP_DIR/monitor.status")
    if [ "$current" -gt "$previous" ]; then
      grep -Eq 'ok=Y$' "$APP_DIR/monitor.status" \
        || fatal "cron monitor advanced with a failing status"
      LAST_MONITOR_MTIME=$current
      return 0
    fi
    sleep 3
  done
  return 1
}

preflight_cron_journal() {
  sudo -n journalctl -u cron --since "$SWITCHED_AT" --no-pager -n 1 \
    >/dev/null
}

capture_cron_journal() {
  local destination=$1
  local error_pattern
  local temporary
  error_pattern='permission denied|operation not permitted|'
  error_pattern+='command not found|:[^:]+: not found|'
  error_pattern+='no such file or directory|cannot execute|failed to execute|'
  error_pattern+='timed out|time limit exceeded|deadline exceeded|'
  error_pattern+='timeout:.*(failed|killed|sending signal)|'
  error_pattern+='timeout (expired|exceeded|error|failure)|'
  error_pattern+='sudo:.*(password|terminal|tty|not allowed|authentication)'
  temporary=$(
    mktemp -- "$(dirname -- "$destination")/.cron-journal.XXXXXX"
  ) || return $?
  # Redirection intentionally writes to an app-owned, private evidence file.
  # shellcheck disable=SC2024
  if ! sudo -n journalctl -u cron --since "$SWITCHED_AT" --no-pager \
      > "$temporary"; then
    rm -f -- "$temporary"
    return 1
  fi
  chmod 600 "$temporary" || {
    rm -f -- "$temporary"
    return 1
  }
  mv -fT -- "$temporary" "$destination" || {
    rm -f -- "$temporary"
    return 1
  }
  local scan_status
  if LC_ALL=C grep -Eiq \
      "$error_pattern" \
      "$destination"; then
    printf 'FATAL: cron journal contains an execution error: %s\n' \
      "$destination" >&2
    return 76
  else
    scan_status=$?
    if [ "$scan_status" -ne 1 ]; then
      printf 'FATAL: cron journal could not be scanned: %s\n' \
        "$destination" >&2
      return "$scan_status"
    fi
  fi
}

observe() {
  local minute=$1
  local require_cron_advance=$2
  local evidence="$EVIDENCE_DIR/observe-${minute}m.txt"
  local app_cid
  local frontend_cid
  local https_result
  local http_result

  app_cid=$(compose ps -q app)
  frontend_cid=$(compose ps -q frontend)
  [ "$app_cid" = "$NEW_APP_CID" ] || fatal "app CID drift at ${minute}m"
  [ "$frontend_cid" = "$NEW_FRONTEND_CID" ] \
    || fatal "frontend CID drift at ${minute}m"
  check_compose_identity "$app_cid"
  check_compose_identity "$frontend_cid"
  [ "$(sudo docker inspect -f '{{.Image}}' "$app_cid")" \
    = "$NEW_APP_IMAGE_ID" ]
  [ "$(sudo docker inspect -f '{{.Image}}' "$frontend_cid")" \
    = "$NEW_FRONTEND_IMAGE_ID" ]
  [ "$(sudo docker inspect -f '{{.RestartCount}}' "$app_cid")" = 0 ]
  [ "$(sudo docker inspect -f '{{.RestartCount}}' "$frontend_cid")" = 0 ]
  [ "$(compose ps -q db)" = "$BASE_DB_CID" ]
  check_compose_identity "$BASE_DB_CID"
  [ "$(sudo docker inspect -f '{{.Image}}' "$BASE_DB_CID")" \
    = "$BASE_DB_IMAGE_ID" ]
  [ "$(sudo docker inspect -f '{{.RestartCount}}' "$BASE_DB_CID")" \
    = "$BASE_DB_RESTARTS" ]
  [ "$(sudo docker ps -q --no-trunc \
    --filter name=^/personal-ai-assistant-caddy$)" = "$BASE_EDGE_CID" ]
  [ "$(sudo docker inspect -f '{{.RestartCount}}' "$BASE_EDGE_CID")" \
    = "$BASE_EDGE_RESTARTS" ]
  [ "$(sudo sha256sum "$EDGE_CADDYFILE" | cut -d' ' -f1)" \
    = "$EDGE_CADDY_HASH" ]
  [ "$(sudo sha256sum "$EDGE_COMPOSE" | cut -d' ' -f1)" \
    = "$EDGE_COMPOSE_HASH" ]
  [ "$(compose port frontend 80)" = 127.0.0.1:8080 ]
  check_loopback_8080
  [ "$(
    sudo docker inspect -f \
      '{{with index .NetworkSettings.Networks "it-spareparts-ingress"}}yes{{end}}' \
      "$frontend_cid"
  )" = yes ]
  check_internal_health

  https_result=$(
    curl --noproxy '*' --proto '=https' --tlsv1.2 \
      --silent --show-error --output /dev/null \
      --write-out '%{http_code} %{remote_ip} %{ssl_verify_result}' \
      --max-time 15 "https://$EXPECTED_HTTPS_HOST/"
  )
  [ "$https_result" = "200 118.25.94.90 0" ] \
    || fatal "HTTPS ${minute}m check failed: $https_result"
  http_result=$(
    curl --noproxy '*' --proto '=http' --max-redirs 0 \
      --silent --show-error --output /dev/null \
      --write-out '%{http_code} %{redirect_url}' \
      --max-time 15 "http://$EXPECTED_HTTPS_HOST/release-observe?q=1"
  )
  [ "$http_result" \
    = "308 https://$EXPECTED_HTTPS_HOST/release-observe?q=1" ] \
    || fatal "HTTP redirect ${minute}m check failed: $http_result"

  [ "$(systemctl is-active cron)" = active ] || fatal "cron is not active"
  if [ "$require_cron_advance" = 1 ]; then
    wait_for_monitor_advance "$LAST_MONITOR_MTIME" \
      || fatal "cron monitor did not advance after ${minute}m"
  else
    [ "$(stat -c '%Y' "$APP_DIR/monitor.status")" \
      -ge "$MONITOR_SWITCH_MTIME" ]
    grep -Eq 'ok=Y$' "$APP_DIR/monitor.status"
  fi
  capture_cron_journal "$EVIDENCE_DIR/cron-${minute}m.txt" \
    || fatal "${minute}m cron journal validation failed"

  # shellcheck disable=SC2024
  compose logs --since "$SWITCHED_AT" app frontend \
    > "$EVIDENCE_DIR/logs-${minute}m.txt" 2>&1
  chmod 600 "$EVIDENCE_DIR/logs-${minute}m.txt"
  if grep -E \
      'Traceback|(^|[[:space:]])ERROR([[:space:]:]|$)|HTTP/[0-9.]+\" 5[0-9]{2}' \
      "$EVIDENCE_DIR/logs-${minute}m.txt"; then
    fatal "${minute}m logs contain an error"
  fi
  {
    printf 'minute=%s\n' "$minute"
    printf 'checked_at=%s\n' "$(date --iso-8601=seconds)"
    printf 'https=%s\n' "$https_result"
    printf 'http_redirect=%s\n' "$http_result"
    printf 'monitor_mtime=%s\n' "$LAST_MONITOR_MTIME"
    printf 'result=PASS\n'
  } > "$evidence"
  chmod 600 "$evidence"
  printf 'OBSERVE_OK minute=%s at=%s\n' \
    "$minute" "$(date --iso-8601=seconds)"
}

if [ "${V120_OBSERVER_LIBRARY_ONLY:-0}" = 1 ]; then
  [ "${V120_STATE_TEST_MODE:-0}" = 1 ] \
    && [ "${BASH_SOURCE[0]}" != "$0" ] \
    || fatal "observer library mode is test-only and must be sourced"
  return 0
fi

CLEAN_EXIT=0
OBSERVATION_ARMED=0
STATE=
ROOT_SNAPSHOT=
observer_abort() {
  local status=$?
  trap - EXIT HUP INT TERM
  if [ "$CLEAN_EXIT" != 1 ] && [ "$OBSERVATION_ARMED" = 1 ]; then
    current_phase=
    if load_root_snapshot; then
      current_phase=$RELEASE_PHASE
    fi
    case "$current_phase" in
      observed)
        if ! v120_state_commit_mirror "$STATE" "$ROOT_SNAPSHOT"; then
          status=99
        else
          ROOT_SNAPSHOT=
        fi
        ;;
      switched)
        printf 'Observation failed; closing app/frontend for forward repair.\n' >&2
        fail_closed_from_snapshot "$ROOT_SNAPSHOT" || status=99
        ;;
      failed_closed)
        stop_business_and_verify >/dev/null 2>&1 || status=99
        ;;
      *)
        stop_business_and_verify >/dev/null 2>&1 || status=99
        status=99
        ;;
    esac
  fi
  [ -z "$ROOT_SNAPSHOT" ] || rm -f -- "$ROOT_SNAPSHOT" || status=97
  v120_release_lock || status=97
  [ "$status" -ne 0 ] || status=98
  exit "$status"
}
trap observer_abort EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

[ "$EUID" -ne 0 ] || fatal "observe_v120.sh must run as the app account"
[ "$#" -eq 1 ] || fatal "usage: observe_v120.sh <exact release state>"
STATE=$1
state_name=$(basename -- "$STATE")
[[ "$state_name" =~ ^v120-[0-9a-f]{12}-[0-9]{14}[.]state$ ]]
[ "$STATE" = "$APP_DIR/backups/$state_name" ]
[ "$(realpath -e -- "$STATE")" = "$STATE" ]
[ "$(stat -c '%a %U %h' "$STATE")" = "600 $(id -un) 1" ]
v120_acquire_lock "$LOCK_PATH" "750 root:$(id -gn)"
v120_state_load "$STATE"

[ "$RELEASE_PHASE" = switched ] \
  || fatal "observation requires switched, not $RELEASE_PHASE"
[[ "$RELEASE_ID" =~ ^v120-[0-9a-f]{12}-[0-9]{14}$ ]]
[[ "$NEW_APP_CID" =~ ^[0-9a-f]{64}$ ]]
[[ "$NEW_FRONTEND_CID" =~ ^[0-9a-f]{64}$ ]]
[ "$EVIDENCE_DIR" = "$APP_DIR/backups/$RELEASE_ID-release" ]
[ -d "$EVIDENCE_DIR" ] && [ ! -L "$EVIDENCE_DIR" ]
[ "$(stat -c '%a %U' "$EVIDENCE_DIR")" = "700 $(id -un)" ]
[ -f "$ROOT_STATE" ] && [ ! -L "$ROOT_STATE" ]
sudo cmp -s "$STATE" "$ROOT_STATE" \
  || fatal "app state mirror differs from root authority"
[ "$(sudo stat -c '%F %a %U:%G %h' "$CONTROL_CURRENT/manifest.txt")" \
  = "regular file 600 root:root 1" ]
[ "$(sudo sha256sum "$CONTROL_CURRENT/manifest.txt" | cut -d' ' -f1)" \
  = "$CONTROL_MANIFEST_HASH" ]
sudo "$CONTROL_CURRENT/install-v120-control.sh" \
  verify "$CONTROL_MANIFEST_HASH"
[ "$(sudo sed -n 's/^TARGET_COMMIT=//p' \
  "$CONTROL_CURRENT/manifest.txt")" = "$TARGET_COMMIT" ]

cd "$APP_DIR"
[ "$(sha256sum docker-compose.yml | cut -d' ' -f1)" \
  = "$APP_COMPOSE_HASH" ]
[ "$(stat -c '%a %U:%G' docker-compose.yml)" = "644 root:root" ]
LAST_MONITOR_MTIME=$MONITOR_SWITCH_MTIME
OBSERVATION_ARMED=1
preflight_cron_journal \
  || fatal "non-interactive root cron journal access is unavailable"

observe 0 0
wait_minutes 5
observe 5 1
wait_minutes 10
observe 15 1
wait_minutes 15
observe 30 1

OBSERVED_AT=$(date --iso-8601=seconds)
advance_state OBSERVED_AT "$OBSERVED_AT" RELEASE_PHASE observed
CLEAN_EXIT=1
v120_release_lock
trap - EXIT HUP INT TERM
printf 'OBSERVATION_COMPLETE release=%s observed_at=%s\n' \
  "$RELEASE_ID" "$OBSERVED_AT"
