#!/usr/bin/env bash
# v1.20 inert state codec and transition rules.  State is never executable.

readonly V120_APP_DIR=/home/ubuntu/apps/it-spareparts
readonly V120_STATE_FORMAT=v120-1
V120_STATE_KEYS=(
  STATE_FORMAT
  STATE_GENERATION
  ATTEMPT_NO
  RELEASE_ID
  PARENT_RELEASE_ID
  PARENT_STATE_HASH
  ROLLBACK_POLICY
  TARGET_COMMIT
  OLD_COMMIT
  OLD_RUNNING_SOURCE_COMMIT
  DB_HEAD
  OLD_APP_IMAGE_ID
  OLD_FRONTEND_IMAGE_ID
  APP_IMAGE_REF
  FRONTEND_IMAGE_REF
  OLD_APP_ROLLBACK_TAG
  OLD_FRONTEND_ROLLBACK_TAG
  NEW_APP_IMAGE_ID
  NEW_FRONTEND_IMAGE_ID
  NEW_APP_CANDIDATE_TAG
  NEW_FRONTEND_CANDIDATE_TAG
  SOURCE_TAR
  SOURCE_SUM
  SOURCE_HASH
  CONTROL_MANIFEST_HASH
  RELEASE_PHASE
  APP_COMPOSE_HASH
  BASE_DB_CID
  BASE_DB_IMAGE_ID
  BASE_EDGE_CID
  BASE_DB_RESTARTS
  BASE_EDGE_RESTARTS
  EDGE_CADDY_HASH
  EDGE_COMPOSE_HASH
  IMAGE_BUNDLE
  IMAGE_BUNDLE_HASH
  EVIDENCE_DIR
  BACKUP
  BACKUP_HASH
  NEW_APP_CID
  NEW_FRONTEND_CID
  MONITOR_SWITCH_MTIME
  PUBLIC_OPENED_AT
  SWITCHED_AT
  OBSERVED_AT
  FAILED_AT
  ROLLED_BACK_AT
)
declare -Ag V120_STATE=()

v120_state_error() {
  printf 'STATE_ERROR: %s\n' "$*" >&2
  return 1
}

v120_state_key_allowed() {
  local wanted=$1
  local key
  for key in "${V120_STATE_KEYS[@]}"; do
    [ "$key" != "$wanted" ] || return 0
  done
  return 1
}

v120_state_is_timestamp() {
  local value=$1
  [[ "$value" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(Z|[+-][0-9]{2}:[0-9]{2})$ ]] \
    && date --date="$value" >/dev/null 2>&1
}

v120_state_value_valid() {
  local key=$1
  local value=$2
  [ -n "$value" ] || return 1
  [[ "$value" =~ ^[A-Za-z0-9_./:@+-]+$ ]] || return 1
  case "$key" in
    STATE_FORMAT) [ "$value" = "$V120_STATE_FORMAT" ] ;;
    STATE_GENERATION)
      [[ "$value" =~ ^(0|[1-9][0-9]{0,2})$ ]]
      ;;
    ATTEMPT_NO)
      [[ "$value" =~ ^[1-9][0-9]{0,2}$ ]]
      ;;
    RELEASE_ID)
      [[ "$value" =~ ^v120-[0-9a-f]{12}-[0-9]{14}$ ]]
      ;;
    PARENT_RELEASE_ID)
      [ "$value" = none ] \
        || [[ "$value" =~ ^v120-[0-9a-f]{12}-[0-9]{14}$ ]]
      ;;
    PARENT_STATE_HASH)
      [[ "$value" =~ ^[0-9a-f]{64}$ ]]
      ;;
    ROLLBACK_POLICY)
      [[ "$value" =~ ^(old_allowed|forward_only)$ ]]
      ;;
    TARGET_COMMIT|OLD_COMMIT|OLD_RUNNING_SOURCE_COMMIT|SOURCE_HASH|\
    APP_COMPOSE_HASH|CONTROL_MANIFEST_HASH|EDGE_CADDY_HASH|EDGE_COMPOSE_HASH|\
    IMAGE_BUNDLE_HASH|BACKUP_HASH)
      [[ "$value" =~ ^[0-9a-f]{64}$ ]] \
        || [[ "$key" =~ ^(TARGET_COMMIT|OLD_COMMIT|OLD_RUNNING_SOURCE_COMMIT)$ \
          && "$value" =~ ^[0-9a-f]{40}$ ]]
      ;;
    DB_HEAD)
      [ "$value" = c6f2a8e9d4b1 ]
      ;;
    OLD_APP_IMAGE_ID|OLD_FRONTEND_IMAGE_ID|NEW_APP_IMAGE_ID|\
    NEW_FRONTEND_IMAGE_ID|BASE_DB_IMAGE_ID)
      [[ "$value" =~ ^sha256:[0-9a-f]{64}$ ]]
      ;;
    APP_IMAGE_REF)
      [ "$value" = it-spareparts-app ]
      ;;
    FRONTEND_IMAGE_REF)
      [ "$value" = it-spareparts-frontend ]
      ;;
    OLD_APP_ROLLBACK_TAG|OLD_FRONTEND_ROLLBACK_TAG|\
    NEW_APP_CANDIDATE_TAG|NEW_FRONTEND_CANDIDATE_TAG)
      [[ "$value" =~ ^it-spareparts-release/(app|frontend):(rollback|candidate)-v120-[0-9a-f]{12}-[0-9]{14}$ ]]
      ;;
    SOURCE_TAR|SOURCE_SUM|IMAGE_BUNDLE|EVIDENCE_DIR|BACKUP)
      [[ "$value" == /* && "$value" != *'/../'* && "$value" != *'/./'* ]]
      ;;
    RELEASE_PHASE)
      [[ "$value" =~ ^(built|prepared|backup_verified|opening|switched|observed|failed_closed|rolled_back)$ ]]
      ;;
    BASE_DB_CID|BASE_EDGE_CID|NEW_APP_CID|NEW_FRONTEND_CID)
      [[ "$value" =~ ^[0-9a-f]{64}$ ]]
      ;;
    BASE_DB_RESTARTS|BASE_EDGE_RESTARTS|MONITOR_SWITCH_MTIME)
      [[ "$value" =~ ^(0|[1-9][0-9]*)$ ]]
      ;;
    PUBLIC_OPENED_AT|SWITCHED_AT|OBSERVED_AT|FAILED_AT|ROLLED_BACK_AT)
      v120_state_is_timestamp "$value"
      ;;
    *) return 1 ;;
  esac
}

v120_state_key_permitted_for_phase() {
  local phase=$1
  local key=$2
  case "$key" in
    STATE_FORMAT|STATE_GENERATION|ATTEMPT_NO|RELEASE_ID|\
    PARENT_RELEASE_ID|PARENT_STATE_HASH|ROLLBACK_POLICY|\
    TARGET_COMMIT|OLD_COMMIT|\
    OLD_RUNNING_SOURCE_COMMIT|DB_HEAD|OLD_APP_IMAGE_ID|\
    OLD_FRONTEND_IMAGE_ID|APP_IMAGE_REF|FRONTEND_IMAGE_REF|\
    OLD_APP_ROLLBACK_TAG|OLD_FRONTEND_ROLLBACK_TAG|NEW_APP_IMAGE_ID|\
    NEW_FRONTEND_IMAGE_ID|NEW_APP_CANDIDATE_TAG|\
    NEW_FRONTEND_CANDIDATE_TAG|SOURCE_TAR|SOURCE_SUM|SOURCE_HASH|\
    CONTROL_MANIFEST_HASH|RELEASE_PHASE|APP_COMPOSE_HASH)
      return 0
      ;;
    BASE_DB_CID|BASE_DB_IMAGE_ID|BASE_EDGE_CID|BASE_DB_RESTARTS|\
    BASE_EDGE_RESTARTS|EDGE_CADDY_HASH|EDGE_COMPOSE_HASH|IMAGE_BUNDLE|\
    IMAGE_BUNDLE_HASH|EVIDENCE_DIR)
      [ "$phase" != built ]
      ;;
    BACKUP|BACKUP_HASH)
      [[ "$phase" =~ ^(backup_verified|opening|switched|observed|failed_closed|rolled_back)$ ]]
      ;;
    NEW_APP_CID|PUBLIC_OPENED_AT)
      [[ "$phase" =~ ^(opening|switched|observed|failed_closed)$ ]]
      ;;
    NEW_FRONTEND_CID|MONITOR_SWITCH_MTIME|SWITCHED_AT)
      [[ "$phase" =~ ^(switched|observed|failed_closed)$ ]]
      ;;
    OBSERVED_AT)
      [ "$phase" = observed ]
      ;;
    FAILED_AT)
      [ "$phase" = failed_closed ]
      ;;
    ROLLED_BACK_AT)
      [ "$phase" = rolled_back ]
      ;;
    *) return 1 ;;
  esac
}

v120_state_required_keys() {
  local phase=$1
  local -a required=(
    STATE_FORMAT STATE_GENERATION ATTEMPT_NO RELEASE_ID PARENT_RELEASE_ID
    PARENT_STATE_HASH ROLLBACK_POLICY TARGET_COMMIT OLD_COMMIT
    OLD_RUNNING_SOURCE_COMMIT DB_HEAD OLD_APP_IMAGE_ID
    OLD_FRONTEND_IMAGE_ID APP_IMAGE_REF FRONTEND_IMAGE_REF
    OLD_APP_ROLLBACK_TAG OLD_FRONTEND_ROLLBACK_TAG NEW_APP_IMAGE_ID
    NEW_FRONTEND_IMAGE_ID NEW_APP_CANDIDATE_TAG
    NEW_FRONTEND_CANDIDATE_TAG SOURCE_TAR SOURCE_SUM SOURCE_HASH
    CONTROL_MANIFEST_HASH RELEASE_PHASE APP_COMPOSE_HASH
  )
  if [ "$phase" != built ]; then
    required+=(
      BASE_DB_CID BASE_DB_IMAGE_ID BASE_EDGE_CID BASE_DB_RESTARTS
      BASE_EDGE_RESTARTS EDGE_CADDY_HASH EDGE_COMPOSE_HASH IMAGE_BUNDLE
      IMAGE_BUNDLE_HASH EVIDENCE_DIR
    )
  fi
  case "$phase" in
    backup_verified|opening|switched|observed)
      required+=(BACKUP BACKUP_HASH)
      ;;
  esac
  case "$phase" in
    opening|switched|observed)
      required+=(NEW_APP_CID PUBLIC_OPENED_AT)
      ;;
  esac
  case "$phase" in
    switched|observed)
      required+=(NEW_FRONTEND_CID MONITOR_SWITCH_MTIME SWITCHED_AT)
      ;;
  esac
  case "$phase" in
    observed) required+=(OBSERVED_AT) ;;
    failed_closed) required+=(FAILED_AT) ;;
    rolled_back) required+=(ROLLED_BACK_AT) ;;
  esac
  printf '%s\n' "${required[@]}"
}

v120_state_validate_schema() {
  local array_name=$1
  local -n state_ref=$array_name
  local phase=${state_ref[RELEASE_PHASE]:-}
  local key
  local required

  [ "${state_ref[STATE_FORMAT]:-}" = "$V120_STATE_FORMAT" ] \
    || return 1
  [[ "$phase" =~ ^(built|prepared|backup_verified|opening|switched|observed|failed_closed|rolled_back)$ ]] \
    || return 1
  for key in "${!state_ref[@]}"; do
    v120_state_key_allowed "$key" || return 1
    v120_state_value_valid "$key" "${state_ref[$key]}" || return 1
    v120_state_key_permitted_for_phase "$phase" "$key" || return 1
  done
  while IFS= read -r required; do
    [ -n "${state_ref[$required]+x}" ] || return 1
  done < <(v120_state_required_keys "$phase")

  local release_id=${state_ref[RELEASE_ID]}
  local target=${state_ref[TARGET_COMMIT]}
  local attempt_no=${state_ref[ATTEMPT_NO]}
  local parent_release=${state_ref[PARENT_RELEASE_ID]}
  local parent_hash=${state_ref[PARENT_STATE_HASH]}
  [ "${release_id:5:12}" = "${target:0:12}" ] || return 1
  if [ "$attempt_no" = 1 ]; then
    [ "$parent_release" = none ] \
      && [ "$parent_hash" = \
        0000000000000000000000000000000000000000000000000000000000000000 ] \
      || return 1
    if [[ "$phase" =~ ^(built|prepared|backup_verified|rolled_back)$ ]]; then
      [ "${state_ref[ROLLBACK_POLICY]}" = old_allowed ] || return 1
    fi
  else
    [ "$parent_release" != none ] \
      && [ "$parent_hash" != \
        0000000000000000000000000000000000000000000000000000000000000000 ] \
      || return 1
  fi
  [ "${state_ref[OLD_COMMIT]}" \
    = ab42005b5b94bf98b3db0e4bff87e5df9da2f7ca ] || return 1
  if [ "$attempt_no" = 1 ]; then
    [ "${state_ref[OLD_RUNNING_SOURCE_COMMIT]}" \
      = a1cf00910f08da7f27a9e6e0faaacc3a3cce9bab ] || return 1
  fi
  [ "${state_ref[OLD_COMMIT]}" != "${state_ref[OLD_RUNNING_SOURCE_COMMIT]}" ] \
    || return 1
  [ "${state_ref[OLD_APP_ROLLBACK_TAG]}" \
    = "it-spareparts-release/app:rollback-$release_id" ] || return 1
  [ "${state_ref[OLD_FRONTEND_ROLLBACK_TAG]}" \
    = "it-spareparts-release/frontend:rollback-$release_id" ] || return 1
  [ "${state_ref[NEW_APP_CANDIDATE_TAG]}" \
    = "it-spareparts-release/app:candidate-$release_id" ] || return 1
  [ "${state_ref[NEW_FRONTEND_CANDIDATE_TAG]}" \
    = "it-spareparts-release/frontend:candidate-$release_id" ] || return 1
  [ "${state_ref[SOURCE_TAR]}" \
    = "$V120_APP_DIR/backups/$release_id-source.tar" ] || return 1
  [ "${state_ref[SOURCE_SUM]}" = "${state_ref[SOURCE_TAR]}.sha256" ] \
    || return 1

  if [ "$phase" != built ]; then
    [ "${state_ref[EVIDENCE_DIR]}" \
      = "$V120_APP_DIR/backups/$release_id-release" ] || return 1
    [ "${state_ref[IMAGE_BUNDLE]}" \
      = "${state_ref[EVIDENCE_DIR]}/images.tar" ] || return 1
  fi
  if [ -n "${state_ref[BACKUP]+x}" ]; then
    [ -n "${state_ref[BACKUP_HASH]+x}" ] || return 1
    [[ "${state_ref[BACKUP]}" =~ ^/var/backups/spareparts/db-[0-9]{8}-[0-9]{4}(-[0-9]+)?[.]dump$ ]] \
      || return 1
  elif [ -n "${state_ref[BACKUP_HASH]+x}" ]; then
    return 1
  fi
  if [ -n "${state_ref[PUBLIC_OPENED_AT]+x}" ]; then
    [ -n "${state_ref[NEW_APP_CID]+x}" ] \
      && [ -n "${state_ref[BACKUP]+x}" ] || return 1
  elif [ -n "${state_ref[NEW_APP_CID]+x}" ]; then
    return 1
  elif [ "$phase" = failed_closed ]; then
    [ "$attempt_no" -gt 1 ] \
      && [ "${state_ref[ROLLBACK_POLICY]}" = forward_only ] \
      && [ "$parent_release" != none ] || return 1
  fi
  if [ -n "${state_ref[SWITCHED_AT]+x}" ]; then
    [ -n "${state_ref[NEW_FRONTEND_CID]+x}" ] \
      && [ -n "${state_ref[MONITOR_SWITCH_MTIME]+x}" ] \
      && [ -n "${state_ref[PUBLIC_OPENED_AT]+x}" ] || return 1
  elif [ -n "${state_ref[NEW_FRONTEND_CID]+x}" ] \
      || [ -n "${state_ref[MONITOR_SWITCH_MTIME]+x}" ]; then
    return 1
  fi
  if [ "$phase" = rolled_back ] \
      && [ "${state_ref[ROLLBACK_POLICY]}" != old_allowed ]; then
    return 1
  fi
  return 0
}

v120_state_parse_to_array() {
  local state_file=$1
  local output_name=$2
  local -n output_ref=$output_name
  local line
  local key
  local value
  local line_count=0
  local size
  local last_byte

  output_ref=()
  [ -f "$state_file" ] && [ ! -L "$state_file" ] || {
    v120_state_error "state is not a safe regular file"
    return 64
  }
  [ "$(stat -Lc '%h' -- "$state_file")" = 1 ] || {
    v120_state_error "state must have exactly one hard link"
    return 64
  }
  size=$(stat -Lc '%s' -- "$state_file") || return 64
  [ "$size" -gt 0 ] && [ "$size" -le 16384 ] || {
    v120_state_error "state size is outside 1..16384 bytes"
    return 64
  }
  cmp -s -- "$state_file" <(LC_ALL=C tr -d '\000' < "$state_file") || {
    v120_state_error "state contains NUL"
    return 64
  }
  if LC_ALL=C grep -q $'[\001-\011\013-\037\177]' -- "$state_file"; then
    v120_state_error "state contains a forbidden control byte"
    return 64
  fi
  last_byte=$(
    tail -c 1 -- "$state_file" |
      od -An -tu1 |
      tr -d '[:space:]'
  )
  [ "$last_byte" = 10 ] || {
    v120_state_error "state must end with LF"
    return 64
  }

  while IFS= read -r line; do
    line_count=$((line_count + 1))
    [ "$line_count" -le 48 ] && [ "${#line}" -le 1024 ] || {
      v120_state_error "state line limit exceeded"
      return 64
    }
    [ -n "$line" ] || {
      v120_state_error "blank state line"
      return 64
    }
    [[ "$line" == *=* ]] && [ "${line#*=}" != "$line" ] \
      && [[ "${line#*=}" != *=* ]] || {
      v120_state_error "state line must contain exactly one '='"
      return 64
    }
    key=${line%%=*}
    value=${line#*=}
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || {
      v120_state_error "invalid state key"
      return 64
    }
    v120_state_key_allowed "$key" || {
      v120_state_error "unknown state key '$key'"
      return 64
    }
    [ -z "${output_ref[$key]+x}" ] || {
      v120_state_error "duplicate state key '$key'"
      return 64
    }
    v120_state_value_valid "$key" "$value" || {
      v120_state_error "invalid value for '$key'"
      return 64
    }
    output_ref["$key"]=$value
  done < "$state_file"
  v120_state_validate_schema "$output_name" || {
    v120_state_error "state schema or cross-field validation failed"
    return 64
  }
}

v120_state_load() {
  local state_file=$1
  local key
  v120_state_parse_to_array "$state_file" V120_STATE || return $?
  for key in "${V120_STATE_KEYS[@]}"; do
    unset "$key"
    [ -n "${V120_STATE[$key]+x}" ] || continue
    printf -v "$key" '%s' "${V120_STATE[$key]}"
  done
}

v120_state_render_array() {
  local array_name=$1
  local destination=$2
  local -n state_ref=$array_name
  local key
  v120_state_validate_schema "$array_name" || return 64
  : > "$destination" || return $?
  for key in "${V120_STATE_KEYS[@]}"; do
    [ -n "${state_ref[$key]+x}" ] || continue
    printf '%s=%s\n' "$key" "${state_ref[$key]}" >> "$destination" \
      || return $?
  done
  return 0
}

v120_state_write_file() {
  local destination=$1
  local key
  # Populated through an indirect reference in v120_state_render_array.
  local -A initial=()
  for key in "${V120_STATE_KEYS[@]}"; do
    [ -n "${!key+x}" ] || continue
    # shellcheck disable=SC2034
    initial["$key"]=${!key}
  done
  v120_state_render_array initial "$destination" || return $?
}

v120_state_publish_new() {
  local candidate=$1
  local destination=$2
  local state_dir
  local move_status=0
  # Populated through an indirect reference in v120_state_parse_to_array.
  # shellcheck disable=SC2034
  local -A candidate_state=()

  state_dir=$(dirname -- "$destination") || return $?
  [ "$(dirname -- "$candidate")" = "$state_dir" ] || return 74
  v120_state_parse_to_array "$candidate" candidate_state || return $?
  [ ! -e "$destination" ] && [ ! -L "$destination" ] || return 74

  # GNU mv -n maps to renameat2(RENAME_NOREPLACE) on Linux, but its collision
  # exit status differs between coreutils releases and downstream builds.
  # Normalize only the observable no-clobber outcome (both names survive);
  # preserve every other mv failure so genuine I/O errors are never hidden.
  mv -nT -- "$candidate" "$destination" || move_status=$?
  if [ "$move_status" -ne 0 ]; then
    if { [ -e "$candidate" ] || [ -L "$candidate" ]; } \
        && { [ -e "$destination" ] || [ -L "$destination" ]; }; then
      return 74
    fi
    return "$move_status"
  fi
  if [ -e "$candidate" ] || [ -L "$candidate" ]; then
    return 74
  fi
  [ -f "$destination" ] && [ ! -L "$destination" ] \
    && [ "$(stat -c '%h' "$destination")" = 1 ] || return 74
  sync -f "$destination" || return $?
  sync -d "$state_dir" || return $?
}

v120_state_transition_allowed() {
  local old_phase=$1
  local new_phase=$2
  case "$old_phase:$new_phase" in
    built:prepared|prepared:backup_verified|prepared:rolled_back|\
    prepared:failed_closed|\
    backup_verified:opening|backup_verified:rolled_back|\
    backup_verified:failed_closed|\
    opening:switched|opening:failed_closed|\
    switched:observed|switched:failed_closed)
      return 0
      ;;
    *) return 1 ;;
  esac
}

v120_state_validate_transition() {
  local old_name=$1
  local new_name=$2
  local -n old_ref=$old_name
  local -n new_ref=$new_name
  local key
  [ "${new_ref[STATE_GENERATION]}" \
    -eq $((10#${old_ref[STATE_GENERATION]} + 1)) ] || return 73
  v120_state_transition_allowed \
    "${old_ref[RELEASE_PHASE]}" "${new_ref[RELEASE_PHASE]}" || return 73
  if [ "${old_ref[ROLLBACK_POLICY]}" = forward_only ] \
      && [ "${new_ref[ROLLBACK_POLICY]}" != forward_only ]; then
    return 73
  fi
  if [ "${old_ref[ROLLBACK_POLICY]}" != "${new_ref[ROLLBACK_POLICY]}" ]; then
    [ "${old_ref[ROLLBACK_POLICY]}" = old_allowed ] \
      && [ "${new_ref[ROLLBACK_POLICY]}" = forward_only ] \
      && [ "${old_ref[RELEASE_PHASE]}" = backup_verified ] \
      && [ "${new_ref[RELEASE_PHASE]}" = opening ] \
      || return 73
  fi
  if [ "${new_ref[RELEASE_PHASE]}" = rolled_back ] \
      && [ "${old_ref[ROLLBACK_POLICY]}" != old_allowed ]; then
    return 73
  fi
  if [[ "${old_ref[RELEASE_PHASE]}:${new_ref[RELEASE_PHASE]}" \
      =~ ^(prepared|backup_verified):failed_closed$ ]] \
      && [ "${old_ref[ROLLBACK_POLICY]}" != forward_only ]; then
    return 73
  fi
  for key in "${!old_ref[@]}"; do
    [ -n "${new_ref[$key]+x}" ] || return 73
    case "$key" in
      STATE_GENERATION|RELEASE_PHASE|ROLLBACK_POLICY) ;;
      *) [ "${new_ref[$key]}" = "${old_ref[$key]}" ] || return 73 ;;
    esac
  done
  return 0
}

v120_state_validate_supersession() {
  local old_name=$1
  local new_name=$2
  local old_hash=$3
  local -n old_ref=$old_name
  local -n new_ref=$new_name

  [[ "$old_hash" =~ ^[0-9a-f]{64}$ ]] || return 73
  [[ "${old_ref[RELEASE_PHASE]}" \
    =~ ^(observed|rolled_back|failed_closed)$ ]] \
    || return 73
  [ "${new_ref[RELEASE_PHASE]}" = built ] \
    && [ "${new_ref[STATE_GENERATION]}" = 0 ] || return 73
  [ "${new_ref[ATTEMPT_NO]}" \
    -eq $((10#${old_ref[ATTEMPT_NO]} + 1)) ] || return 73
  [ "${new_ref[PARENT_RELEASE_ID]}" = "${old_ref[RELEASE_ID]}" ] \
    && [ "${new_ref[PARENT_STATE_HASH]}" = "$old_hash" ] || return 73
  [ "${new_ref[RELEASE_ID]}" != "${old_ref[RELEASE_ID]}" ] || return 73
  case "${old_ref[RELEASE_PHASE]}" in
    observed)
      [ "${new_ref[ROLLBACK_POLICY]}" = old_allowed ] || return 73
      [ "${new_ref[OLD_RUNNING_SOURCE_COMMIT]}" \
        = "${old_ref[TARGET_COMMIT]}" ] || return 73
      [ "${new_ref[OLD_APP_IMAGE_ID]}" \
        = "${old_ref[NEW_APP_IMAGE_ID]}" ] || return 73
      [ "${new_ref[OLD_FRONTEND_IMAGE_ID]}" \
        = "${old_ref[NEW_FRONTEND_IMAGE_ID]}" ] || return 73
      ;;
    rolled_back)
      [ "${new_ref[ROLLBACK_POLICY]}" = old_allowed ] || return 73
      [ "${new_ref[OLD_RUNNING_SOURCE_COMMIT]}" \
        = "${old_ref[OLD_RUNNING_SOURCE_COMMIT]}" ] || return 73
      [ "${new_ref[OLD_APP_IMAGE_ID]}" \
        = "${old_ref[OLD_APP_IMAGE_ID]}" ] || return 73
      [ "${new_ref[OLD_FRONTEND_IMAGE_ID]}" \
        = "${old_ref[OLD_FRONTEND_IMAGE_ID]}" ] || return 73
      ;;
    failed_closed)
      [ "${new_ref[ROLLBACK_POLICY]}" = forward_only ] || return 73
      [ "${new_ref[OLD_RUNNING_SOURCE_COMMIT]}" \
        = "${old_ref[OLD_RUNNING_SOURCE_COMMIT]}" ] || return 73
      [ "${new_ref[OLD_APP_IMAGE_ID]}" \
        = "${old_ref[OLD_APP_IMAGE_ID]}" ] || return 73
      [ "${new_ref[OLD_FRONTEND_IMAGE_ID]}" \
        = "${old_ref[OLD_FRONTEND_IMAGE_ID]}" ] || return 73
      ;;
  esac
  [ "${new_ref[APP_IMAGE_REF]}" = "${old_ref[APP_IMAGE_REF]}" ] \
    && [ "${new_ref[FRONTEND_IMAGE_REF]}" \
      = "${old_ref[FRONTEND_IMAGE_REF]}" ] || return 73
  return 0
}

v120_state_select_supersession_base() {
  local parent_name=$1
  local output_name=$2
  local -n parent_ref=$parent_name
  # Both nameref targets are caller-declared associative arrays.
  # shellcheck disable=SC2178
  local -n output_ref=$output_name
  local key
  for key in "${!output_ref[@]}"; do
    unset 'output_ref[$key]'
  done
  case "${parent_ref[RELEASE_PHASE]:-}" in
    observed)
      output_ref[RUNNING_SOURCE_COMMIT]=${parent_ref[TARGET_COMMIT]}
      output_ref[APP_IMAGE_ID]=${parent_ref[NEW_APP_IMAGE_ID]}
      output_ref[FRONTEND_IMAGE_ID]=${parent_ref[NEW_FRONTEND_IMAGE_ID]}
      output_ref[ROLLBACK_POLICY]=old_allowed
      output_ref[REQUIRE_RUNNING]=1
      ;;
    rolled_back)
      output_ref[RUNNING_SOURCE_COMMIT]=${parent_ref[OLD_RUNNING_SOURCE_COMMIT]}
      output_ref[APP_IMAGE_ID]=${parent_ref[OLD_APP_IMAGE_ID]}
      output_ref[FRONTEND_IMAGE_ID]=${parent_ref[OLD_FRONTEND_IMAGE_ID]}
      output_ref[ROLLBACK_POLICY]=old_allowed
      output_ref[REQUIRE_RUNNING]=1
      ;;
    failed_closed)
      output_ref[RUNNING_SOURCE_COMMIT]=${parent_ref[OLD_RUNNING_SOURCE_COMMIT]}
      output_ref[APP_IMAGE_ID]=${parent_ref[OLD_APP_IMAGE_ID]}
      output_ref[FRONTEND_IMAGE_ID]=${parent_ref[OLD_FRONTEND_IMAGE_ID]}
      output_ref[ROLLBACK_POLICY]=forward_only
      output_ref[REQUIRE_RUNNING]=0
      ;;
    *) return 73 ;;
  esac
}

v120_state_prepare_update() {
  local state_file=$1
  local candidate=$2
  shift 2
  local key
  local value
  local -A updated=()
  local -A old_state=()
  local -A next_state=()

  [ "$#" -gt 0 ] && [ $(( $# % 2 )) -eq 0 ] || return 64
  v120_state_parse_to_array "$state_file" old_state || return $?
  for key in "${!old_state[@]}"; do
    next_state["$key"]=${old_state[$key]}
  done
  while [ "$#" -gt 0 ]; do
    key=$1
    value=$2
    shift 2
    v120_state_key_allowed "$key" || return 64
    case "$key" in
      STATE_FORMAT|STATE_GENERATION) return 64 ;;
    esac
    [ -z "${updated[$key]+x}" ] || return 64
    updated["$key"]=1
    if [ -n "${old_state[$key]+x}" ] \
        && [ "$key" != RELEASE_PHASE ] \
        && [ "$key" != ROLLBACK_POLICY ]; then
      [ "${old_state[$key]}" = "$value" ] || return 73
    fi
    next_state["$key"]=$value
  done
  next_state[STATE_GENERATION]=$((10#${old_state[STATE_GENERATION]} + 1))
  v120_state_validate_schema next_state || return 64
  v120_state_validate_transition old_state next_state || return $?
  v120_state_render_array next_state "$candidate" || return $?
  chmod 600 "$candidate" || return $?
  sync -f "$candidate" || return $?
  # Populated through an indirect reference in v120_state_parse_to_array.
  # shellcheck disable=SC2034
  local -A rendered=()
  v120_state_parse_to_array "$candidate" rendered || return $?
  v120_state_validate_transition old_state rendered || return $?
  return 0
}

v120_state_commit_candidate() {
  local state_file=$1
  local candidate=$2
  local state_dir
  local -A old_state=()
  # Populated through an indirect reference in v120_state_parse_to_array.
  # shellcheck disable=SC2034
  local -A next_state=()
  state_dir=$(dirname -- "$state_file")
  [ "$(dirname -- "$candidate")" = "$state_dir" ] || return 74
  v120_state_parse_to_array "$state_file" old_state || return $?
  v120_state_parse_to_array "$candidate" next_state || return $?
  v120_state_validate_transition old_state next_state || return $?
  if [ "${V120_STATE_TEST_MODE:-0}" = 1 ] \
      && [ "${V120_STATE_TEST_FAILPOINT:-}" = before_rename ]; then
    return 74
  fi
  mv -fT -- "$candidate" "$state_file" || return $?
  sync -f "$state_file" || return $?
  sync -d "$state_dir" || return $?
}

v120_state_commit_mirror() {
  local state_file=$1
  local candidate=$2
  local state_dir
  local -A current_state=()
  local -A authority_state=()

  state_dir=$(dirname -- "$state_file")
  [ "$(dirname -- "$candidate")" = "$state_dir" ] || return 74
  v120_state_parse_to_array "$state_file" current_state || return $?
  v120_state_parse_to_array "$candidate" authority_state || return $?
  [ "${current_state[RELEASE_ID]}" = "${authority_state[RELEASE_ID]}" ] \
    || return 73
  [ "${authority_state[STATE_GENERATION]}" \
    -ge "${current_state[STATE_GENERATION]}" ] || return 73
  if [ "${V120_STATE_TEST_MODE:-0}" = 1 ] \
      && [ "${V120_STATE_TEST_FAILPOINT:-}" = before_mirror_rename ]; then
    return 74
  fi
  mv -fT -- "$candidate" "$state_file" || return $?
  sync -f "$state_file" || return $?
  sync -d "$state_dir" || return $?
}

v120_state_update_atomic() (
  set -Eeuo pipefail
  local state_file=$1
  shift
  local state_dir
  local temporary=
  state_dir=$(dirname -- "$state_file")
  temporary=$(mktemp -- "$state_dir/.v120-state.next.XXXXXX")
  # shellcheck disable=SC2329
  cleanup_state_update() {
    [ -z "$temporary" ] || rm -f -- "$temporary"
  }
  trap cleanup_state_update EXIT
  v120_state_prepare_update "$state_file" "$temporary" "$@" || return $?
  v120_state_commit_candidate "$state_file" "$temporary" || return $?
  temporary=
)

v120_acquire_lock() {
  local lock_path=$1
  local expected=${2:-750 root:ubuntu}
  [ -d "$lock_path" ] && [ ! -L "$lock_path" ] || {
    v120_state_error "release lock is not a safe directory"
    return 75
  }
  [ "$(stat -c '%a %U:%G' "$lock_path")" = "$expected" ] || {
    v120_state_error "release lock owner/mode mismatch"
    return 75
  }
  exec {V120_LOCK_FD}<"$lock_path"
  if ! flock -n "$V120_LOCK_FD"; then
    printf 'RELEASE_BUSY: another v1.20 operation holds %s\n' \
      "$lock_path" >&2
    exec {V120_LOCK_FD}>&-
    unset V120_LOCK_FD
    return 75
  fi
}

v120_release_lock() {
  [ -n "${V120_LOCK_FD:-}" ] || return 0
  flock -u "$V120_LOCK_FD" || return 1
  exec {V120_LOCK_FD}>&-
  unset V120_LOCK_FD
}
