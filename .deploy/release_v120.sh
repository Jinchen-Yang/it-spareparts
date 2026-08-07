#!/usr/bin/env bash
# v1.20 exact-artifact production switch with a fail-closed public boundary.
set -Eeuo pipefail
umask 077

readonly APP_DIR=/home/ubuntu/apps/it-spareparts
readonly CONTROL_DIR=/var/lib/it-spareparts-release-control
readonly CONTROL_CURRENT="$CONTROL_DIR/current"
readonly LOCK_PATH=/run/lock/it-spareparts-v120
readonly EXPECTED_DB_HEAD=c6f2a8e9d4b1
readonly EXPECTED_OLD_COMMIT=ab42005b5b94bf98b3db0e4bff87e5df9da2f7ca
readonly EXPECTED_HTTPS_HOST=hbzgc.icu
readonly ASSISTANT_HEALTH_URL=https://118.25.94.90/health
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

read_control_manifest_hash() {
  local key=$1
  local output_name=$2
  local value
  [ "$(sudo grep -c "^${key}=" "$CONTROL_CURRENT/manifest.txt")" = 1 ] \
    || fatal "root control manifest lacks $key"
  value=$(
    sudo sed -n "s/^${key}=//p" "$CONTROL_CURRENT/manifest.txt"
  )
  [[ "$value" =~ ^[0-9a-f]{64}$ ]] \
    || fatal "root control manifest has invalid $key"
  printf -v "$output_name" '%s' "$value"
}

# This is executable code from the verified release archive, never release data.
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

check_sha256_id() {
  [[ "$1" =~ ^sha256:[0-9a-f]{64}$ ]]
}

check_container_id() {
  [[ "$1" =~ ^[0-9a-f]{64}$ ]]
}

check_compose_identity() {
  local cid=$1
  [ "$(sudo docker inspect -f \
    '{{index .Config.Labels "com.docker.compose.project"}}' "$cid")" \
    = it-spareparts ] || return 1
  [ "$(sudo docker inspect -f \
    '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$cid")" \
    = "$APP_DIR/docker-compose.yml" ] \
    || return 1
  [ "$(sudo docker inspect -f \
    '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$cid")" \
    = "$APP_DIR" ] || return 1
}

check_loopback_8080() {
  local listeners
  if ! listeners=$(sudo ss -H -ltn '( sport = :8080 )'); then
    fatal "cannot inspect port 8080 listeners"
  fi
  [ -n "$listeners" ] || fatal "frontend port 8080 is not listening"
  if ! awk '$4 != "127.0.0.1:8080" { exit 1 }' <<< "$listeners"; then
    fatal "port 8080 has a non-loopback listener"
  fi
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

check_external_health_semantics() {
  probe_json_health "https://$EXPECTED_HTTPS_HOST/health" ok app \
    && probe_json_health "https://$EXPECTED_HTTPS_HOST/health/db" ok db
}

check_rollback_frontend_readiness() {
  local _
  local https_result
  for _ in $(seq 1 30); do
    if curl --noproxy '*' --fail --silent --show-error --max-time 5 \
        http://127.0.0.1:8080/ >/dev/null 2>&1 \
        && https_result=$(
          curl --noproxy '*' --proto '=https' --tlsv1.2 \
            --silent --show-error --output /dev/null \
            --write-out '%{http_code} %{ssl_verify_result}' \
            --max-time 8 "https://$EXPECTED_HTTPS_HOST/"
        ) \
        && [ "$https_result" = "200 0" ]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

check_candidate_external_readiness() {
  check_rollback_frontend_readiness \
    && check_external_health_semantics \
    && probe_json_health "$ASSISTANT_HEALTH_URL" ready assistant
}

run_monitor_with_retry() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if "$APP_DIR/.deploy/monitor.sh"; then
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

verify_legacy_cron_absent() (
  set -Eeuo pipefail
  local current=
  local error_file=
  local status
  current=$(mktemp)
  error_file=$(mktemp)
  # shellcheck disable=SC2329
  cleanup_legacy_cron_check() {
    rm -f -- "$current" "$error_file"
  }
  trap cleanup_legacy_cron_check EXIT

  if LC_ALL=C crontab -l > "$current" 2> "$error_file"; then
    :
  else
    status=$?
    if [ "$status" -eq 1 ] \
        && [ "$(cat "$error_file")" = "no crontab for $(id -un)" ] \
        && [ ! -s "$current" ]; then
      : > "$current"
    else
      cat "$error_file" >&2
      return "$status"
    fi
  fi
  if grep -F \
      -e "$APP_DIR/backup.sh" \
      -e "$APP_DIR/.deploy/backup.sh" \
      -e "$APP_DIR/monitor.sh" \
      -e "$APP_DIR/.deploy/monitor.sh" \
      "$current"; then
    printf 'FATAL: legacy user crontab duplicates dedicated cron.d jobs\n' >&2
    return 75
  else
    status=$?
    [ "$status" -eq 1 ] || return "$status"
  fi
)

install_host_artifacts_grouped() (
  set -Eeuo pipefail
  local source
  local destination
  local name
  local temporary
  local -a sources=(
    "$SCRIPT_DIR/backup.sh"
    "$SCRIPT_DIR/monitor.sh"
    "$SCRIPT_DIR/backup.sh"
  )
  local -a destinations=(
    "$APP_DIR/.deploy/backup.sh"
    "$APP_DIR/.deploy/monitor.sh"
    "$APP_DIR/backup.sh"
  )
  local -a temporaries=()
  local -a existed=()
  local committed=0

  mkdir "$EVIDENCE_DIR/host-artifacts-before"
  chmod 700 "$EVIDENCE_DIR/host-artifacts-before"
  # shellcheck disable=SC2329
  cleanup_host_install() {
    local index
    for temporary in "${temporaries[@]:-}"; do
      [ -z "$temporary" ] || rm -f -- "$temporary"
    done
    if [ "$committed" != 1 ]; then
      for index in "${!destinations[@]}"; do
        [ -n "${existed[$index]+x}" ] || continue
        destination=${destinations[$index]}
        name="artifact-$index"
        if [ "${existed[$index]:-0}" = 1 ]; then
          temporary=$(mktemp -- "$(dirname "$destination")/.rollback.XXXXXX")
          install -m 700 \
            "$EVIDENCE_DIR/host-artifacts-before/$name" "$temporary"
          mv -fT -- "$temporary" "$destination"
        elif [ "${existed[$index]:-0}" = 0 ]; then
          rm -f -- "$destination"
        fi
      done
    fi
  }
  trap cleanup_host_install EXIT

  for index in "${!destinations[@]}"; do
    source=${sources[$index]}
    destination=${destinations[$index]}
    name="artifact-$index"
    if [ -e "$destination" ] || [ -L "$destination" ]; then
      [ -f "$destination" ] && [ ! -L "$destination" ] \
        || fatal "unsafe host artifact destination: $destination"
      cp -- "$destination" "$EVIDENCE_DIR/host-artifacts-before/$name"
      chmod 600 "$EVIDENCE_DIR/host-artifacts-before/$name"
      existed[index]=1
    else
      existed[index]=0
    fi
    temporary=$(mktemp -- "$(dirname "$destination")/.release.XXXXXX")
    install -m 700 "$source" "$temporary"
    cmp -s "$source" "$temporary"
    temporaries[index]=$temporary
  done
  for index in "${!destinations[@]}"; do
    mv -fT -- "${temporaries[$index]}" "${destinations[$index]}"
    temporaries[index]=
  done
  committed=1
  sync -d "$APP_DIR"
  sync -d "$APP_DIR/.deploy"
)

verify_root_control() {
  [ "$(sudo stat -c '%F %a %U:%G' "$CONTROL_DIR")" \
    = "directory 700 root:root" ] \
    || fatal "root control directory owner/mode mismatch"
  [ "$(sudo stat -c '%a %U:%G %h' \
    "$CONTROL_CURRENT/install-v120-control.sh")" = "700 root:root 1" ] \
    || fatal "root control installer owner/mode mismatch"
  [ "$(sudo sha256sum "$CONTROL_CURRENT/manifest.txt" | cut -d' ' -f1)" \
    = "$CONTROL_MANIFEST_HASH" ] \
    || fatal "root control manifest hash mismatch"
  sudo "$CONTROL_CURRENT/install-v120-control.sh" \
    verify "$CONTROL_MANIFEST_HASH"
  [ "$(sudo sed -n 's/^TARGET_COMMIT=//p' \
    "$CONTROL_CURRENT/manifest.txt")" = "$TARGET_COMMIT" ] \
    || fatal "root control package targets a different commit"
}

sync_root_state() {
  # Redirection intentionally reads an app-owned, already validated data file.
  # shellcheck disable=SC2024
  sudo "$CONTROL_CURRENT/sync-v120-root-state.sh" < "$STATE"
}

advance_state() {
  local temporary
  sudo cmp -s "$STATE" "$CONTROL_DIR/v120-state.state" || {
    printf 'STATE_ERROR: app mirror differs from root authority\n' >&2
    return 73
  }
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
  sudo cmp -s "$temporary" "$CONTROL_DIR/v120-state.state" || {
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
  # The app account owns the private destination; sudo only reads root state.
  # shellcheck disable=SC2024
  if ! sudo cat "$CONTROL_DIR/v120-state.state" > "$snapshot"; then
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

commit_root_transition() {
  local base_state=$1
  shift
  local candidate
  candidate=$(mktemp -- "$APP_DIR/backups/.v120-root-next.XXXXXX") \
    || return $?
  if ! v120_state_prepare_update "$base_state" "$candidate" "$@"; then
    rm -f -- "$candidate"
    return 1
  fi
  # shellcheck disable=SC2024
  if ! sudo "$CONTROL_CURRENT/sync-v120-root-state.sh" < "$candidate"; then
    rm -f -- "$candidate"
    return 1
  fi
  sudo cmp -s "$candidate" "$CONTROL_DIR/v120-state.state" || {
    rm -f -- "$candidate"
    return 73
  }
  if ! v120_state_commit_mirror "$STATE" "$candidate"; then
    rm -f -- "$candidate"
    return 1
  fi
  v120_state_load "$STATE"
}

restore_old_business_inline() {
  local authority_file=$1
  local app_cid
  local frontend_cid
  local rolled_back_at

  v120_state_load "$authority_file" || return $?
  case "$RELEASE_PHASE" in
    prepared|backup_verified) ;;
    *) return 73 ;;
  esac
  [ "$ROLLBACK_POLICY" = old_allowed ] || return 73
  stop_business_and_verify || return $?
  sudo docker tag "$OLD_APP_IMAGE_ID" "$APP_IMAGE_REF" || return $?
  sudo docker tag "$OLD_FRONTEND_IMAGE_ID" "$FRONTEND_IMAGE_REF" \
    || return $?
  [ "$(sudo docker image inspect -f '{{.Id}}' "$APP_IMAGE_REF")" \
    = "$OLD_APP_IMAGE_ID" ] || return 1
  [ "$(sudo docker image inspect -f '{{.Id}}' "$FRONTEND_IMAGE_REF")" \
    = "$OLD_FRONTEND_IMAGE_ID" ] || return 1

  compose up -d --no-deps --no-build --force-recreate app || return $?
  app_cid=$(compose ps -q app) || return $?
  check_container_id "$app_cid" || return $?
  check_compose_identity "$app_cid" || return $?
  [ "$(sudo docker inspect -f '{{.Image}}' "$app_cid")" \
    = "$OLD_APP_IMAGE_ID" ] || return 1
  [ "$(sudo docker inspect -f '{{.RestartCount}}' "$app_cid")" = 0 ] \
    || return 1
  check_internal_health || return $?

  compose up -d --no-deps --no-build --force-recreate frontend || return $?
  frontend_cid=$(compose ps -q frontend) || return $?
  check_container_id "$frontend_cid" || return $?
  check_compose_identity "$frontend_cid" || return $?
  [ "$(sudo docker inspect -f '{{.Image}}' "$frontend_cid")" \
    = "$OLD_FRONTEND_IMAGE_ID" ] || return 1
  [ "$(sudo docker inspect -f '{{.RestartCount}}' "$frontend_cid")" = 0 ] \
    || return 1
  [ "$(compose port frontend 80)" = 127.0.0.1:8080 ] || return 1
  check_loopback_8080 || return $?
  [ "$(
    sudo docker inspect -f \
      '{{with index .NetworkSettings.Networks "it-spareparts-ingress"}}yes{{end}}' \
      "$frontend_cid"
  )" = yes ] || return 1
  [ "$(compose ps -q db)" = "$BASE_DB_CID" ] || return 1
  [ "$(sudo docker inspect -f '{{.Image}}' "$BASE_DB_CID")" \
    = "$BASE_DB_IMAGE_ID" ] || return 1
  [ "$(sudo docker ps -q --no-trunc \
    --filter name=^/personal-ai-assistant-caddy$)" = "$BASE_EDGE_CID" ] \
    || return 1
  [ "$(sudo sha256sum "$EDGE_CADDYFILE" | cut -d' ' -f1)" \
    = "$EDGE_CADDY_HASH" ] || return 1
  [ "$(sudo sha256sum "$EDGE_COMPOSE" | cut -d' ' -f1)" \
    = "$EDGE_COMPOSE_HASH" ] || return 1
  check_rollback_frontend_readiness || return $?
  run_monitor_with_retry || return $?

  rolled_back_at=$(date --iso-8601=seconds) || return $?
  commit_root_transition "$authority_file" \
    ROLLED_BACK_AT "$rolled_back_at" RELEASE_PHASE rolled_back || return $?
}

fail_closed_from_root() {
  local authority_file=$1
  local failed_at
  v120_state_load "$authority_file" || return $?
  case "$RELEASE_PHASE" in
    prepared|backup_verified|opening|switched)
      [ "$ROLLBACK_POLICY" = forward_only ] \
        || [[ "$RELEASE_PHASE" =~ ^(opening|switched)$ ]] \
        || return 73
      stop_business_and_verify || return $?
      failed_at=$(date --iso-8601=seconds) || return $?
      commit_root_transition "$authority_file" \
        FAILED_AT "$failed_at" RELEASE_PHASE failed_closed || return $?
      ;;
    failed_closed)
      stop_business_and_verify || return $?
      ;;
    *) return 73 ;;
  esac
}

v120_evidence_reset_authorized() (
  set -Eeuo pipefail
  local app_state_file=$1
  local snapshot=
  local root_hash
  local -A app_state=()
  local -A root_state=()

  v120_state_parse_to_array "$app_state_file" app_state || return $?
  if sudo test ! -e "$CONTROL_DIR/v120-state.state" \
      && sudo test ! -L "$CONTROL_DIR/v120-state.state"; then
    return 0
  fi
  [ "$(sudo stat -c '%F %a %U:%G %h' \
    "$CONTROL_DIR/v120-state.state")" = "regular file 600 root:root 1" ] \
    || return 74
  snapshot=$(mktemp -- "$APP_DIR/backups/.v120-evidence-root.XXXXXX") \
    || return $?
  # shellcheck disable=SC2329
  cleanup_evidence_authority_snapshot() {
    [ -z "$snapshot" ] || rm -f -- "$snapshot"
  }
  trap cleanup_evidence_authority_snapshot EXIT
  # shellcheck disable=SC2024
  sudo cat "$CONTROL_DIR/v120-state.state" > "$snapshot" || return $?
  chmod 600 "$snapshot" || return $?
  v120_state_parse_to_array "$snapshot" root_state || return $?
  if [ "${root_state[RELEASE_ID]}" = "${app_state[RELEASE_ID]}" ]; then
    cmp -s "$app_state_file" "$snapshot" || return 73
  else
    root_hash=$(sha256sum "$snapshot" | cut -d' ' -f1) || return $?
    v120_state_validate_supersession root_state app_state "$root_hash" \
      || return $?
  fi
)

v120_evidence_dir_valid() {
  local directory=$1
  local release_id=$2
  local target_commit=$3
  local state_hash=$4
  local marker="$directory/.v120-evidence.marker"
  local mount_status
  local -a marker_lines=()

  [ -d "$directory" ] && [ ! -L "$directory" ] || return 74
  [ "$(realpath -e -- "$directory")" = "$directory" ] || return 74
  [ "$(stat -c '%a %U' "$directory")" = "700 $(id -un)" ] || return 74
  if mountpoint -q -- "$directory"; then
    return 74
  else
    mount_status=$?
    # util-linux mountpoint uses 32 for "not a mount point"; BusyBox uses 1.
    [ "$mount_status" -eq 32 ] || [ "$mount_status" -eq 1 ] \
      || return "$mount_status"
  fi
  [ -f "$marker" ] && [ ! -L "$marker" ] \
    && [ "$(stat -c '%a %U %h' "$marker")" = "600 $(id -un) 1" ] \
    || return 74
  mapfile -t marker_lines < "$marker" || return $?
  [ "${#marker_lines[@]}" -eq 4 ] \
    && [ "${marker_lines[0]}" = "EVIDENCE_FORMAT=v120-evidence-1" ] \
    && [ "${marker_lines[1]}" = "RELEASE_ID=$release_id" ] \
    && [ "${marker_lines[2]}" = "TARGET_COMMIT=$target_commit" ] \
    && [ "${marker_lines[3]}" = "STATE_HASH=$state_hash" ] \
    || return 74
  [ "$(tail -c 1 -- "$marker" | od -An -tu1 | tr -d '[:space:]')" = 10 ] \
    || return 74
}

v120_remove_marked_evidence_dir() {
  local directory=$1
  local release_id=$2
  local target_commit=$3
  local state_hash=$4
  local marker="$directory/.v120-evidence.marker"
  local parent

  parent=$(dirname -- "$directory") || return $?
  v120_evidence_dir_valid \
    "$directory" "$release_id" "$target_commit" "$state_hash" || return $?
  find -P "$directory" -xdev -depth -mindepth 1 \
    ! -path "$marker" -delete || return $?
  rm -f -- "$marker" || return $?
  rmdir -- "$directory" || return $?
  sync -d "$parent" || return $?
}

v120_remove_evidence_recovery_dir() {
  local directory=$1
  local release_id=$2
  local target_commit=$3
  local state_hash=$4
  local parent
  local first_entry

  parent=$(dirname -- "$directory") || return $?
  if [ -e "$directory/.v120-evidence.marker" ] \
      || [ -L "$directory/.v120-evidence.marker" ]; then
    v120_remove_marked_evidence_dir \
      "$directory" "$release_id" "$target_commit" "$state_hash"
    return $?
  fi
  [ -d "$directory" ] && [ ! -L "$directory" ] || return 74
  [ "$(realpath -e -- "$directory")" = "$directory" ] || return 74
  [ "$(stat -c '%a %U' "$directory")" = "700 $(id -un)" ] || return 74
  first_entry=$(
    find -P "$directory" -mindepth 1 -maxdepth 1 -print -quit
  ) || return $?
  [ -z "$first_entry" ] || return 74
  rmdir -- "$directory" || return $?
  sync -d "$parent" || return $?
}

v120_write_evidence_marker() {
  local marker=$1
  local release_id=$2
  local target_commit=$3
  local state_hash=$4

  : > "$marker" || return $?
  printf 'EVIDENCE_FORMAT=v120-evidence-1\n' >> "$marker" || return $?
  printf 'RELEASE_ID=%s\n' "$release_id" >> "$marker" || return $?
  printf 'TARGET_COMMIT=%s\n' "$target_commit" >> "$marker" || return $?
  printf 'STATE_HASH=%s\n' "$state_hash" >> "$marker" || return $?
  chmod 600 "$marker" || return $?
  sync -f "$marker" || return $?
}

v120_discard_evidence_staging() {
  local staging=$1
  [ -n "$staging" ] || return 0
  [ -d "$staging" ] && [ ! -L "$staging" ] || return 74
  find -P "$staging" -xdev -depth -mindepth 1 -delete || return $?
  rmdir -- "$staging" || return $?
}

v120_prepare_evidence_dir() {
  local directory=$1
  local release_id=$2
  local target_commit=$3
  local state_hash=$4
  local parent
  local quarantine
  local staging=
  local marker
  local status

  [[ "$release_id" =~ ^v120-[0-9a-f]{12}-[0-9]{14}$ ]] || return 74
  [[ "$target_commit" =~ ^[0-9a-f]{40}$ ]] || return 74
  [[ "$state_hash" =~ ^[0-9a-f]{64}$ ]] || return 74
  parent=$(dirname -- "$directory") || return $?
  [ "$(basename -- "$directory")" = "$release_id-release" ] || return 74
  [ -d "$parent" ] && [ ! -L "$parent" ] || return 74
  [ "$(realpath -e -- "$parent")" = "$parent" ] || return 74
  [ "$(stat -c '%a %U' "$parent")" = "700 $(id -un)" ] || return 74
  quarantine="$parent/.${release_id}-evidence.reset"

  if [ -e "$quarantine" ] || [ -L "$quarantine" ]; then
    v120_remove_evidence_recovery_dir \
      "$quarantine" "$release_id" "$target_commit" "$state_hash" \
      || return $?
  fi
  if [ -e "$directory" ] || [ -L "$directory" ]; then
    v120_evidence_dir_valid \
      "$directory" "$release_id" "$target_commit" "$state_hash" \
      || return $?
    mv -T -- "$directory" "$quarantine" || return $?
    sync -d "$parent" || return $?
    if [ "${V120_STATE_TEST_MODE:-0}" = 1 ] \
        && [ "${V120_STATE_TEST_FAILPOINT:-}" \
          = after_evidence_quarantine ]; then
      kill -KILL "$BASHPID"
    fi
    v120_remove_marked_evidence_dir \
      "$quarantine" "$release_id" "$target_commit" "$state_hash" \
      || return $?
  fi

  staging=$(mktemp -d "$parent/.${release_id}-evidence.new.XXXXXX") \
    || return $?
  chmod 700 "$staging" || {
    status=$?
    v120_discard_evidence_staging "$staging" || :
    return "$status"
  }
  marker="$staging/.v120-evidence.marker"
  v120_write_evidence_marker \
    "$marker" "$release_id" "$target_commit" "$state_hash" || {
    status=$?
    v120_discard_evidence_staging "$staging" || :
    return "$status"
  }
  sync -f "$staging" || {
    status=$?
    v120_discard_evidence_staging "$staging" || :
    return "$status"
  }
  mv -T -- "$staging" "$directory" || {
    status=$?
    v120_discard_evidence_staging "$staging" || :
    return "$status"
  }
  staging=
  sync -f "$directory" || return $?
  sync -d "$parent" || return $?
  v120_evidence_dir_valid \
    "$directory" "$release_id" "$target_commit" "$state_hash"
}

cleanup_restore_container() {
  local remaining
  [ -n "${RESTORE_CONTAINER:-}" ] || return 0
  sudo docker rm -f "$RESTORE_CONTAINER" >/dev/null 2>&1 || return 1
  if ! remaining=$(
    sudo docker ps -aq --no-trunc \
      --filter "name=^/${RESTORE_CONTAINER}$"
  ); then
    return 1
  fi
  [ -z "$remaining" ] || return 1
  RESTORE_CONTAINER=
}

v120_publish_exact_evidence() (
  set -Eeuo pipefail
  local source=$1
  local destination=$2
  local destination_parent
  local status
  destination_parent=$(dirname -- "$destination")
  cleanup_source() {
    rm -f -- "$source" || return 97
  }
  [ -f "$source" ] && [ ! -L "$source" ] \
    && [ "$(stat -c '%a %U %h' "$source")" \
      = "600 $(id -un) 1" ] || {
    cleanup_source
    return 73
  }
  [ -d "$destination_parent" ] && [ ! -L "$destination_parent" ] || {
    cleanup_source
    return 73
  }
  if ln -T -- "$source" "$destination" 2>/dev/null; then
    chmod 600 "$destination"
    sync -f "$destination"
    cleanup_source
    sync -d "$destination_parent"
    return 0
  fi
  status=$?
  if [ -f "$destination" ] && [ ! -L "$destination" ] \
      && [ "$(stat -c '%a %U %h' "$destination")" \
        = "600 $(id -un) 1" ] \
      && cmp -s "$source" "$destination"; then
    cleanup_source
    sync -f "$destination"
    sync -d "$destination_parent"
    return 0
  fi
  cleanup_source || return $?
  [ "$status" -ne 0 ] || status=73
  return 73
)

if [ "${V120_RELEASE_LIBRARY_ONLY:-0}" = 1 ]; then
  [ "${V120_STATE_TEST_MODE:-0}" = 1 ] \
    && [ "${BASH_SOURCE[0]}" != "$0" ] \
    || fatal "release library mode is test-only and must be sourced"
  return 0
fi

RELEASE_COMPLETE=0
BUSINESS_MUTATING=0
RESTORE_CONTAINER=
IMAGE_BUNDLE_TMP=
SUPPLY_CHAIN_TEMP=
ROOT_SNAPSHOT=
release_abort() {
  local status=$?
  local authority_phase=
  trap - EXIT HUP INT TERM
  if [ -n "$IMAGE_BUNDLE_TMP" ]; then
    rm -f -- "$IMAGE_BUNDLE_TMP" || status=97
  fi
  if [ -n "$SUPPLY_CHAIN_TEMP" ]; then
    rm -f -- "$SUPPLY_CHAIN_TEMP" || status=97
  fi
  if ! cleanup_restore_container; then
    printf 'FATAL: failed to remove isolated restore container\n' >&2
    status=97
  fi
  if [ "$RELEASE_COMPLETE" != 1 ] && [ "$BUSINESS_MUTATING" = 1 ]; then
    if load_root_snapshot; then
      authority_phase=$RELEASE_PHASE
      case "$authority_phase" in
        built)
          printf 'Root state remained built; old business was not mutated.\n' >&2
          ;;
        prepared|backup_verified)
          if [ "$ROLLBACK_POLICY" = old_allowed ]; then
            printf 'Pre-public failure; restoring old business inline.\n' >&2
            restore_old_business_inline "$ROOT_SNAPSHOT" || status=99
          else
            printf 'Forward-repair failure; preserving failed-closed boundary.\n' >&2
            fail_closed_from_root "$ROOT_SNAPSHOT" || status=99
          fi
          ;;
        opening|switched|failed_closed)
          printf 'Public boundary reached; failing closed for forward repair.\n' >&2
          fail_closed_from_root "$ROOT_SNAPSHOT" || status=99
          ;;
        *)
          stop_business_and_verify >/dev/null 2>&1 || status=99
          status=99
          ;;
      esac
    else
      printf 'Cannot read root authority; failing closed.\n' >&2
      stop_business_and_verify >/dev/null 2>&1 || status=99
      status=99
    fi
  fi
  [ -z "$ROOT_SNAPSHOT" ] || rm -f -- "$ROOT_SNAPSHOT" || status=97
  v120_release_lock || status=97
  [ "$status" -ne 0 ] || status=98
  exit "$status"
}
trap release_abort EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

[ "$EUID" -ne 0 ] || fatal "release_v120.sh must run as the app account"
[ "$#" -eq 1 ] || fatal "usage: release_v120.sh <exact build state>"
STATE=$1
state_name=$(basename -- "$STATE")
[[ "$state_name" =~ ^v120-[0-9a-f]{12}-[0-9]{14}[.]state$ ]] \
  || fatal "invalid state filename"
[ "$STATE" = "$APP_DIR/backups/$state_name" ] \
  || fatal "state path must be canonical and inside app backups"
[ "$(realpath -e -- "$STATE")" = "$STATE" ] \
  || fatal "state path is not canonical"
[ "$(stat -c '%a %U %h' "$STATE")" = "600 $(id -un) 1" ] \
  || fatal "state owner/mode mismatch"
v120_acquire_lock "$LOCK_PATH" "750 root:$(id -gn)"
v120_state_load "$STATE"

[[ "${RELEASE_ID:-}" =~ ^v120-[0-9a-f]{12}-[0-9]{14}$ ]] \
  || fatal "invalid release ID"
[ "$STATE" = "$APP_DIR/backups/$RELEASE_ID.state" ] \
  || fatal "state filename and release ID differ"
[[ "${TARGET_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]] || fatal "invalid target commit"
[ "${OLD_COMMIT:-}" = "$EXPECTED_OLD_COMMIT" ] \
  || fatal "old checkout baseline mismatch"
[[ "${OLD_RUNNING_SOURCE_COMMIT:-}" =~ ^[0-9a-f]{40}$ ]] \
  || fatal "old business source baseline is invalid"
[ "$OLD_COMMIT" != "$OLD_RUNNING_SOURCE_COMMIT" ] \
  || fatal "checkout and old business source must remain distinct"
[ "${DB_HEAD:-}" = "$EXPECTED_DB_HEAD" ] || fatal "database head mismatch"
[ "${RELEASE_PHASE:-}" = built ] \
  || fatal "release must start from built, not ${RELEASE_PHASE:-missing}"
[[ "${ATTEMPT_NO:-}" =~ ^[1-9][0-9]{0,2}$ ]] \
  || fatal "invalid release attempt"
[[ "${ROLLBACK_POLICY:-}" =~ ^(old_allowed|forward_only)$ ]] \
  || fatal "invalid rollback policy"
FORWARD_REPAIR=0
if [ "$ROLLBACK_POLICY" = forward_only ]; then
  [ "$ATTEMPT_NO" -gt 1 ] && [ "$PARENT_RELEASE_ID" != none ] \
    || fatal "forward repair lacks a parent release"
  FORWARD_REPAIR=1
fi
check_sha256_id "${OLD_APP_IMAGE_ID:-}" || fatal "invalid old app image ID"
check_sha256_id "${OLD_FRONTEND_IMAGE_ID:-}" \
  || fatal "invalid old frontend image ID"
check_sha256_id "${NEW_APP_IMAGE_ID:-}" || fatal "invalid new app image ID"
check_sha256_id "${NEW_FRONTEND_IMAGE_ID:-}" \
  || fatal "invalid new frontend image ID"
[ "${APP_IMAGE_REF:-}" = it-spareparts-app ] || fatal "invalid app ref"
[ "${FRONTEND_IMAGE_REF:-}" = it-spareparts-frontend ] \
  || fatal "invalid frontend ref"
[ "${OLD_APP_ROLLBACK_TAG:-}" \
  = "it-spareparts-release/app:rollback-$RELEASE_ID" ]
[ "${OLD_FRONTEND_ROLLBACK_TAG:-}" \
  = "it-spareparts-release/frontend:rollback-$RELEASE_ID" ]
[ "${NEW_APP_CANDIDATE_TAG:-}" \
  = "it-spareparts-release/app:candidate-$RELEASE_ID" ]
[ "${NEW_FRONTEND_CANDIDATE_TAG:-}" \
  = "it-spareparts-release/frontend:candidate-$RELEASE_ID" ]
[ "${SOURCE_TAR:-}" = "$APP_DIR/backups/$RELEASE_ID-source.tar" ]
[ "${SOURCE_SUM:-}" = "$SOURCE_TAR.sha256" ]
[[ "${APP_COMPOSE_HASH:-}" =~ ^[0-9a-f]{64}$ ]] \
  || fatal "invalid compose hash"
[[ "${SOURCE_HASH:-}" =~ ^[0-9a-f]{64}$ ]] \
  || fatal "invalid source hash"
for artifact in "$SOURCE_TAR" "$SOURCE_SUM"; do
  [ -f "$artifact" ] && [ ! -L "$artifact" ] \
    || fatal "unsafe release source artifact"
  [ "$(sudo stat -c '%a %U:%G %h' "$artifact")" \
    = "600 root:root 1" ] \
    || fatal "release source artifact owner/mode mismatch"
done
v120_evidence_reset_authorized "$STATE" \
  || fatal "root authority does not authorize this release state"

cd "$APP_DIR"
[ "$(sha256sum docker-compose.yml | cut -d' ' -f1)" \
  = "$APP_COMPOSE_HASH" ] \
  || fatal "active compose hash differs from build state"
[ "$(stat -c '%a %U:%G' docker-compose.yml)" = "644 root:root" ] \
  || fatal "active compose owner/mode mismatch"
[ -f .env ] && [ ! -L .env ] || fatal "unsafe production .env"
[ "$(stat -c '%a %U' .env)" = "600 $(id -un)" ] \
  || fatal "production .env owner/mode mismatch"

[ "$(sudo cat "$SOURCE_TAR" | git get-tar-commit-id)" = "$TARGET_COMMIT" ]
SOURCE_SUM_LINES=$(sudo sed -n '$=' "$SOURCE_SUM")
SOURCE_EXPECTED_HASH=$(
  sudo sed -n '1{s/[[:space:]].*$//;p;}' "$SOURCE_SUM"
)
[ "$SOURCE_SUM_LINES" = 1 ] \
  && [[ "$SOURCE_EXPECTED_HASH" =~ ^[0-9a-fA-F]{64}$ ]] \
  || fatal "invalid source checksum"
[ "$SOURCE_EXPECTED_HASH" = "$SOURCE_HASH" ] \
  || fatal "source hash differs from build state"
printf '%s  %s\n' "$SOURCE_EXPECTED_HASH" "$SOURCE_TAR" |
  sudo sha256sum -c -
grep -q 'APP_VERSION = "1.20.0"' \
  <(sudo tar -xOf "$SOURCE_TAR" frontend/src/version.ts)

for protected_artifact in \
  .deploy/Caddyfile.it-data.example \
  .deploy/docker-compose.https.yml
do
  [ ! -L "$protected_artifact" ]
  [ "$(sudo stat -c '%a %U:%G' "$protected_artifact")" = "600 root:root" ]
  sudo tar -xOf "$SOURCE_TAR" "$protected_artifact" |
    sudo cmp - "$protected_artifact"
done

for script_name in \
  backup.sh \
  monitor.sh \
  build_v120.sh \
  release_v120.sh \
  observe_v120.sh \
  rollback_v120.sh \
  install_v120_control.sh \
  hsts_v120_root.sh \
  hsts_v120_operator.sh \
  package_v120_control.sh \
  v120_state.sh \
  sync_v120_root_state.sh
do
  [ -f "$SCRIPT_DIR/$script_name" ] || fatal "missing release artifact $script_name"
  bash -n "$SCRIPT_DIR/$script_name"
  archive_hash=$(
    sudo tar -xOf "$SOURCE_TAR" ".deploy/$script_name" |
      sha256sum | cut -d' ' -f1
  )
  local_hash=$(sha256sum "$SCRIPT_DIR/$script_name" | cut -d' ' -f1)
  [ "$archive_hash" = "$local_hash" ] \
    || fatal "$script_name differs from exact source archive"
done
cron_archive_hash=$(
  sudo tar -xOf "$SOURCE_TAR" .deploy/it-spareparts.cron |
    sha256sum | cut -d' ' -f1
)
cron_local_hash=$(sha256sum "$SCRIPT_DIR/it-spareparts.cron" | cut -d' ' -f1)
[ "$cron_archive_hash" = "$cron_local_hash" ] \
  || fatal "it-spareparts.cron differs from exact source archive"

BASE_DB_CID=$(compose ps -q db)
BASE_EDGE_CID=$(
  sudo docker ps -q --no-trunc \
    --filter name=^/personal-ai-assistant-caddy$
)
check_container_id "$BASE_DB_CID"
check_container_id "$BASE_EDGE_CID"
check_compose_identity "$BASE_DB_CID"
BASE_DB_IMAGE_ID=$(sudo docker inspect -f '{{.Image}}' "$BASE_DB_CID")
check_sha256_id "$BASE_DB_IMAGE_ID"
BASE_DB_RESTARTS=$(sudo docker inspect -f '{{.RestartCount}}' "$BASE_DB_CID")
BASE_EDGE_RESTARTS=$(sudo docker inspect -f '{{.RestartCount}}' "$BASE_EDGE_CID")
[[ "$BASE_DB_RESTARTS" =~ ^[0-9]+$ ]]
[[ "$BASE_EDGE_RESTARTS" =~ ^[0-9]+$ ]]
if [ "$FORWARD_REPAIR" = 1 ]; then
  stop_business_and_verify \
    || fatal "forward repair could not prove app/frontend are stopped"
  forward_listeners=$(sudo ss -H -ltn '( sport = :8080 )') \
    || fatal "cannot inspect port 8080 for forward repair"
  if [ -n "$forward_listeners" ]; then
    fatal "forward repair requires port 8080 to remain closed"
  fi
else
  OLD_APP_CID=$(compose ps -q app)
  OLD_FRONTEND_CID=$(compose ps -q frontend)
  check_container_id "$OLD_APP_CID"
  check_container_id "$OLD_FRONTEND_CID"
  check_compose_identity "$OLD_APP_CID"
  check_compose_identity "$OLD_FRONTEND_CID"
  [ "$(sudo docker inspect -f '{{.Image}}' "$OLD_APP_CID")" \
    = "$OLD_APP_IMAGE_ID" ]
  [ "$(sudo docker inspect -f '{{.Image}}' "$OLD_FRONTEND_CID")" \
    = "$OLD_FRONTEND_IMAGE_ID" ]
  [ "$(compose port frontend 80)" = 127.0.0.1:8080 ]
  check_loopback_8080
  [ "$(
    sudo docker inspect -f \
      '{{with index .NetworkSettings.Networks "it-spareparts-ingress"}}yes{{end}}' \
      "$OLD_FRONTEND_CID"
  )" = yes ] || fatal "frontend is not attached to ingress"
fi
[ "$(sudo docker inspect -f '{{.State.Running}}' "$BASE_DB_CID")" = true ]
[ "$(sudo docker inspect -f '{{.State.Running}}' "$BASE_EDGE_CID")" = true ]
[ "$(sudo docker image inspect -f '{{.Id}}' "$NEW_APP_CANDIDATE_TAG")" \
  = "$NEW_APP_IMAGE_ID" ]
[ "$(sudo docker image inspect -f '{{.Id}}' "$NEW_FRONTEND_CANDIDATE_TAG")" \
  = "$NEW_FRONTEND_IMAGE_ID" ]
[ "$(
  sudo docker inspect -f \
    '{{with index .NetworkSettings.Networks "it-spareparts-ingress"}}yes{{end}}' \
    "$BASE_EDGE_CID"
)" = yes ] || fatal "edge is not attached to ingress"

[ ! -L "$EDGE_CADDYFILE" ] && [ ! -L "$EDGE_COMPOSE" ]
[ "$(sudo stat -c '%a %U:%G' "$EDGE_CADDYFILE")" = "644 root:root" ]
[ "$(sudo stat -c '%a %U:%G' "$EDGE_COMPOSE")" = "644 root:root" ]
EDGE_CADDY_HASH=$(sudo sha256sum "$EDGE_CADDYFILE" | cut -d' ' -f1)
EDGE_COMPOSE_HASH=$(sudo sha256sum "$EDGE_COMPOSE" | cut -d' ' -f1)
[[ "$EDGE_CADDY_HASH" =~ ^[0-9a-f]{64}$ ]]
[[ "$EDGE_COMPOSE_HASH" =~ ^[0-9a-f]{64}$ ]]

[ "$(compose exec -T db psql -U spareparts -d spareparts -At \
  -c 'SELECT version_num FROM alembic_version;')" = "$EXPECTED_DB_HEAD" ]
[ "$(compose exec -T db psql -U spareparts -d spareparts -At \
  -c "SELECT count(*) FROM sys_import_batch WHERE status = 'processing';")" = 0 ] \
  || fatal "an import is still processing"

EVIDENCE_DIR="$APP_DIR/backups/$RELEASE_ID-release"
STATE_HASH=$(sha256sum "$STATE" | cut -d' ' -f1)
[[ "$STATE_HASH" =~ ^[0-9a-f]{64}$ ]]
v120_prepare_evidence_dir \
  "$EVIDENCE_DIR" "$RELEASE_ID" "$TARGET_COMMIT" "$STATE_HASH" \
  || fatal "cannot prepare retry-safe release evidence"
verify_root_control
for supply_key in \
  BACKEND_REQUIREMENTS_SHA256 \
  BACKEND_UV_LOCK_SHA256 \
  FRONTEND_PACKAGE_LOCK_SHA256 \
  BACKEND_SBOM_SHA256 \
  FRONTEND_SBOM_SHA256 \
  BACKEND_BASE_DIGEST \
  FRONTEND_BUILD_BASE_DIGEST \
  FRONTEND_RUNTIME_BASE_DIGEST
do
  read_control_manifest_hash "$supply_key" "$supply_key"
done
SUPPLY_CHAIN_EVIDENCE="$EVIDENCE_DIR/supply-chain-provenance.txt"
SUPPLY_CHAIN_TEMP=$(mktemp "$EVIDENCE_DIR/.supply-chain.XXXXXX")
{
  printf 'EVIDENCE_FORMAT=v120-supply-chain-1\n'
  printf 'TARGET_COMMIT=%s\n' "$TARGET_COMMIT"
  printf 'CONTROL_MANIFEST_HASH=%s\n' "$CONTROL_MANIFEST_HASH"
  for supply_key in \
    BACKEND_REQUIREMENTS_SHA256 \
    BACKEND_UV_LOCK_SHA256 \
    FRONTEND_PACKAGE_LOCK_SHA256 \
    BACKEND_SBOM_SHA256 \
    FRONTEND_SBOM_SHA256 \
    BACKEND_BASE_DIGEST \
    FRONTEND_BUILD_BASE_DIGEST \
    FRONTEND_RUNTIME_BASE_DIGEST
  do
    printf '%s=%s\n' "$supply_key" "${!supply_key}"
  done
  printf 'NEW_APP_IMAGE_ID=%s\n' "$NEW_APP_IMAGE_ID"
  printf 'NEW_FRONTEND_IMAGE_ID=%s\n' "$NEW_FRONTEND_IMAGE_ID"
} > "$SUPPLY_CHAIN_TEMP"
chmod 600 "$SUPPLY_CHAIN_TEMP"
sync -f "$SUPPLY_CHAIN_TEMP"
v120_publish_exact_evidence \
  "$SUPPLY_CHAIN_TEMP" "$SUPPLY_CHAIN_EVIDENCE" \
  || fatal "supply-chain evidence conflicts with existing evidence"
SUPPLY_CHAIN_TEMP=

image_bytes=0
for image in \
  "$OLD_APP_ROLLBACK_TAG" \
  "$OLD_FRONTEND_ROLLBACK_TAG" \
  "$NEW_APP_CANDIDATE_TAG" \
  "$NEW_FRONTEND_CANDIDATE_TAG"
do
  size=$(sudo docker image inspect -f '{{.Size}}' "$image")
  [[ "$size" =~ ^[0-9]+$ ]]
  image_bytes=$((image_bytes + size))
done
docker_free=$(df -PB1 /var/lib/docker | awk 'NR==2 {print $4}')
[[ "$docker_free" =~ ^[0-9]+$ ]]
[ "$docker_free" -gt $((image_bytes + 1073741824)) ] \
  || fatal "insufficient space for durable image bundle"
IMAGE_BUNDLE_TMP=$(mktemp "$EVIDENCE_DIR/.images.tar.XXXXXX")
# shellcheck disable=SC2024
sudo docker save \
  "$OLD_APP_ROLLBACK_TAG" \
  "$OLD_FRONTEND_ROLLBACK_TAG" \
  "$NEW_APP_CANDIDATE_TAG" \
  "$NEW_FRONTEND_CANDIDATE_TAG" > "$IMAGE_BUNDLE_TMP"
chmod 600 "$IMAGE_BUNDLE_TMP"
tar -tf "$IMAGE_BUNDLE_TMP" >/dev/null
IMAGE_BUNDLE="$EVIDENCE_DIR/images.tar"
mv -fT -- "$IMAGE_BUNDLE_TMP" "$IMAGE_BUNDLE"
IMAGE_BUNDLE_TMP=
sha256sum "$IMAGE_BUNDLE" > "$IMAGE_BUNDLE.sha256"
chmod 600 "$IMAGE_BUNDLE.sha256"
sha256sum -c "$IMAGE_BUNDLE.sha256"
IMAGE_BUNDLE_HASH=$(
  sed -n '1{s/[[:space:]].*$//;p;}' "$IMAGE_BUNDLE.sha256"
)
[[ "$IMAGE_BUNDLE_HASH" =~ ^[0-9a-f]{64}$ ]]

install_host_artifacts_grouped
touch "$APP_DIR/backup.log"
chmod 600 "$APP_DIR/backup.log"
sha256sum \
  "$APP_DIR/.deploy/backup.sh" \
  "$APP_DIR/.deploy/monitor.sh" \
  "$APP_DIR/backup.sh" > "$EVIDENCE_DIR/host-artifacts.sha256"
chmod 600 "$EVIDENCE_DIR/host-artifacts.sha256"
sha256sum -c "$EVIDENCE_DIR/host-artifacts.sha256"
verify_legacy_cron_absent
[ "$(systemctl is-active cron)" = active ] || fatal "cron service is not active"
if [ "$FORWARD_REPAIR" = 0 ]; then
  run_monitor_with_retry || fatal "pre-switch monitor did not pass"
else
  stop_business_and_verify \
    || fatal "forward-repair preflight lost the failed-closed boundary"
fi

verify_root_control
sudo "$CONTROL_CURRENT/install-v120-control.sh" \
  verify-cron "$CONTROL_MANIFEST_HASH"
sync_root_state
BUSINESS_MUTATING=1
advance_state \
  BASE_DB_CID "$BASE_DB_CID" \
  BASE_DB_IMAGE_ID "$BASE_DB_IMAGE_ID" \
  BASE_EDGE_CID "$BASE_EDGE_CID" \
  BASE_DB_RESTARTS "$BASE_DB_RESTARTS" \
  BASE_EDGE_RESTARTS "$BASE_EDGE_RESTARTS" \
  EDGE_CADDY_HASH "$EDGE_CADDY_HASH" \
  EDGE_COMPOSE_HASH "$EDGE_COMPOSE_HASH" \
  IMAGE_BUNDLE "$IMAGE_BUNDLE" \
  IMAGE_BUNDLE_HASH "$IMAGE_BUNDLE_HASH" \
  EVIDENCE_DIR "$EVIDENCE_DIR" \
  RELEASE_PHASE prepared

stop_business_and_verify || fatal "app/frontend did not fully stop"
if ! running_services=$(compose ps --status running --services); then
  fatal "cannot inspect stopped services"
fi
if grep -Eq '^(app|frontend)$' <<< "$running_services"; then
  fatal "app/frontend did not fully stop"
fi
[ "$(compose ps -q db)" = "$BASE_DB_CID" ] || fatal "DB container changed"
ACTIVE_TX=$(
  compose exec -T db psql -U spareparts -d spareparts -At -c "
    SELECT count(*) FROM pg_stat_activity
    WHERE datname='spareparts'
      AND pid <> pg_backend_pid()
      AND state <> 'idle';"
)
[ "$ACTIVE_TX" = 0 ] || fatal "active DB transactions remain after stop"

COUNT_SQL="SELECT 'dim_part|' || count(*) FROM dim_part
UNION ALL SELECT 'f_maintenance_line|' || count(*) FROM f_maintenance_line
UNION ALL SELECT 'f_project_expense|' || count(*) FROM f_project_expense
UNION ALL SELECT 'f_purchase_line|' || count(*) FROM f_purchase_line
UNION ALL SELECT 'f_sales_line|' || count(*) FROM f_sales_line
UNION ALL SELECT 'maintenance_contract_workbook_state|' || count(*) FROM maintenance_contract_workbook_state
UNION ALL SELECT 'sys_import_batch|' || count(*) FROM sys_import_batch
ORDER BY 1;"
# shellcheck disable=SC2024
compose exec -T db psql -U spareparts -d spareparts -At -c "$COUNT_SQL" \
  > "$EVIDENCE_DIR/source-counts.txt"
chmod 600 "$EVIDENCE_DIR/source-counts.txt"

if ! latest_before=$(
  find /var/backups/spareparts -maxdepth 1 -type f \
    -name 'db-*.dump' -printf '%T@ %p\n' |
    sort -nr | head -1 | cut -d' ' -f2-
); then
  fatal "cannot inspect previous backups"
fi
"$APP_DIR/backup.sh" | tee "$EVIDENCE_DIR/backup.log"
chmod 600 "$EVIDENCE_DIR/backup.log"
if ! BACKUP=$(
  find /var/backups/spareparts -maxdepth 1 -type f \
    -name 'db-*.dump' -printf '%T@ %p\n' |
    sort -nr | head -1 | cut -d' ' -f2-
); then
  fatal "cannot inspect new backup"
fi
[ -n "$BACKUP" ] && [ "$BACKUP" != "$latest_before" ] \
  || fatal "backup did not publish a new restore point"
[ "$(stat -c '%a' /var/backups/spareparts)" = 700 ]
[ "$(stat -c '%a' "$BACKUP")" = 600 ]
[ "$(stat -c '%a' "$BACKUP.sha256")" = 600 ]
EXPECTED_BACKUP_HASH=$(sed -n '1{s/[[:space:]].*$//;p;}' "$BACKUP.sha256")
[ "$(wc -l < "$BACKUP.sha256")" = 1 ]
[[ "$EXPECTED_BACKUP_HASH" =~ ^[0-9a-fA-F]{64}$ ]]
printf '%s  %s\n' "$EXPECTED_BACKUP_HASH" "$BACKUP" | sha256sum -c -
# shellcheck disable=SC2024
compose exec -T db pg_restore --list < "$BACKUP" \
  > "$EVIDENCE_DIR/backup.toc"
[ "$(grep -c . "$EVIDENCE_DIR/backup.toc")" -gt 20 ]
chmod 600 "$EVIDENCE_DIR/backup.toc"

BACKUP_SIZE=$(stat -c '%s' "$BACKUP")
DOCKER_FREE=$(df -PB1 /var/lib/docker | awk 'NR==2 {print $4}')
[[ "$DOCKER_FREE" =~ ^[0-9]+$ ]]
[ "$DOCKER_FREE" -gt $((BACKUP_SIZE * 4 + 1073741824)) ] \
  || fatal "insufficient space for isolated restore"

RESTORE_CONTAINER="itdata-restore-${RELEASE_ID}"
if ! existing_restore=$(
  sudo docker ps -aq --no-trunc --filter "name=^/${RESTORE_CONTAINER}$"
); then
  fatal "cannot inspect restore containers"
fi
[ -z "$existing_restore" ] || fatal "restore container name already exists"
sudo docker run -d \
  --name "$RESTORE_CONTAINER" \
  --network none \
  --memory 1g \
  --memory-swap 1g \
  --cpus 1 \
  --pids-limit 256 \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  "$BASE_DB_IMAGE_ID" >/dev/null
RESTORE_READY=0
for _ in $(seq 1 30); do
  if sudo docker exec "$RESTORE_CONTAINER" \
      pg_isready -U postgres >/dev/null 2>&1; then
    RESTORE_READY=1
    break
  fi
  sleep 1
done
[ "$RESTORE_READY" = 1 ] || fatal "isolated restore DB did not become ready"
sudo docker exec "$RESTORE_CONTAINER" createdb -U postgres restore_test
# shellcheck disable=SC2024
sudo docker exec -i "$RESTORE_CONTAINER" \
  pg_restore -U postgres -d restore_test \
    --no-owner --exit-on-error < "$BACKUP"
[ "$(sudo docker exec "$RESTORE_CONTAINER" \
  psql -U postgres -d restore_test -At \
    -c 'SELECT version_num FROM alembic_version;')" = "$EXPECTED_DB_HEAD" ]
# shellcheck disable=SC2024
sudo docker exec "$RESTORE_CONTAINER" \
  psql -U postgres -d restore_test -At -c "$COUNT_SQL" \
  > "$EVIDENCE_DIR/restored-counts.txt"
chmod 600 "$EVIDENCE_DIR/restored-counts.txt"
diff -u \
  "$EVIDENCE_DIR/source-counts.txt" \
  "$EVIDENCE_DIR/restored-counts.txt" \
  > "$EVIDENCE_DIR/restore-counts.diff"
chmod 600 "$EVIDENCE_DIR/restore-counts.diff"
cleanup_restore_container || fatal "cannot prove restore container removal"

advance_state \
  BACKUP "$BACKUP" \
  BACKUP_HASH "$EXPECTED_BACKUP_HASH" \
  RELEASE_PHASE backup_verified

sudo docker tag "$NEW_APP_IMAGE_ID" "$APP_IMAGE_REF"
sudo docker tag "$NEW_FRONTEND_IMAGE_ID" "$FRONTEND_IMAGE_REF"
[ "$(sudo docker image inspect -f '{{.Id}}' "$APP_IMAGE_REF")" \
  = "$NEW_APP_IMAGE_ID" ]
[ "$(sudo docker image inspect -f '{{.Id}}' "$FRONTEND_IMAGE_REF")" \
  = "$NEW_FRONTEND_IMAGE_ID" ]

compose up -d --no-deps --no-build --force-recreate app
NEW_APP_CID=$(compose ps -q app)
check_container_id "$NEW_APP_CID"
check_compose_identity "$NEW_APP_CID"
[ "$(sudo docker inspect -f '{{.Image}}' "$NEW_APP_CID")" \
  = "$NEW_APP_IMAGE_ID" ]
[ "$(sudo docker inspect -f '{{.RestartCount}}' "$NEW_APP_CID")" = 0 ]
check_internal_health || fatal "new app health check failed"

# From this persisted boundary onward, the old app is never started against a
# database that may have accepted v1.20 writes.
PUBLIC_OPENED_AT=$(date --iso-8601=seconds)
advance_state \
  NEW_APP_CID "$NEW_APP_CID" \
  PUBLIC_OPENED_AT "$PUBLIC_OPENED_AT" \
  ROLLBACK_POLICY forward_only \
  RELEASE_PHASE opening

compose up -d --no-deps --no-build --force-recreate frontend
NEW_FRONTEND_CID=$(compose ps -q frontend)
check_container_id "$NEW_FRONTEND_CID"
check_compose_identity "$NEW_FRONTEND_CID"
[ "$(sudo docker inspect -f '{{.Image}}' "$NEW_FRONTEND_CID")" \
  = "$NEW_FRONTEND_IMAGE_ID" ]
[ "$(sudo docker inspect -f '{{.RestartCount}}' "$NEW_FRONTEND_CID")" = 0 ]
[ "$(compose port frontend 80)" = 127.0.0.1:8080 ]
check_loopback_8080
[ "$(
  sudo docker inspect -f \
    '{{with index .NetworkSettings.Networks "it-spareparts-ingress"}}yes{{end}}' \
    "$NEW_FRONTEND_CID"
)" = yes ]
check_candidate_external_readiness \
  || fatal "candidate frontend/HTTPS semantic readiness failed"

[ "$(compose ps -q db)" = "$BASE_DB_CID" ]
[ "$(sudo docker ps -q --no-trunc \
  --filter name=^/personal-ai-assistant-caddy$)" = "$BASE_EDGE_CID" ]
[ "$(sudo docker inspect -f '{{.RestartCount}}' "$BASE_DB_CID")" \
  = "$BASE_DB_RESTARTS" ]
[ "$(sudo docker inspect -f '{{.RestartCount}}' "$BASE_EDGE_CID")" \
  = "$BASE_EDGE_RESTARTS" ]
[ "$(sudo sha256sum "$EDGE_CADDYFILE" | cut -d' ' -f1)" = "$EDGE_CADDY_HASH" ]
[ "$(sudo sha256sum "$EDGE_COMPOSE" | cut -d' ' -f1)" \
  = "$EDGE_COMPOSE_HASH" ]
run_monitor_with_retry || fatal "post-switch monitor did not pass"
sudo "$CONTROL_CURRENT/install-v120-control.sh" \
  verify-cron "$CONTROL_MANIFEST_HASH"
MONITOR_SWITCH_MTIME=$(stat -c '%Y' "$APP_DIR/monitor.status")
[[ "$MONITOR_SWITCH_MTIME" =~ ^[0-9]+$ ]]
grep -Eq 'ok=Y$' "$APP_DIR/monitor.status"

SWITCHED_AT=$(date --iso-8601=seconds)
advance_state \
  NEW_FRONTEND_CID "$NEW_FRONTEND_CID" \
  MONITOR_SWITCH_MTIME "$MONITOR_SWITCH_MTIME" \
  SWITCHED_AT "$SWITCHED_AT" \
  RELEASE_PHASE switched

RELEASE_COMPLETE=1
v120_release_lock
trap - EXIT HUP INT TERM
printf 'RELEASE_SWITCH_OK release=%s target=%s app=%s frontend=%s backup=%s\n' \
  "$RELEASE_ID" "$TARGET_COMMIT" "$NEW_APP_IMAGE_ID" \
  "$NEW_FRONTEND_IMAGE_ID" "$BACKUP"
