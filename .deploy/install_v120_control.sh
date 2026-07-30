#!/usr/bin/env bash
# Install root control files from a hash-addressed operator-transfer package.
set -Eeuo pipefail
umask 077
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
unset CDPATH ENV BASH_ENV GIT_DIR GIT_WORK_TREE GIT_OBJECT_DIRECTORY \
  GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_REPLACE_REF_BASE

readonly APP_DIR=/home/ubuntu/apps/it-spareparts
readonly CONTROL_DIR=/var/lib/it-spareparts-release-control
readonly VERSIONS_DIR="$CONTROL_DIR/versions"
readonly CURRENT_LINK="$CONTROL_DIR/current"
readonly ARCHIVE_DIR="$CONTROL_DIR/archive"
readonly LOCK_PATH=/run/lock/it-spareparts-v120
readonly CRON_DEST=/etc/cron.d/it-spareparts
readonly MARKER_DIR=/etc/it-spareparts
readonly AUTHORITY_MARKER="$MARKER_DIR/v120-authority.marker"
readonly BOOTSTRAP_AUTH="$MARKER_DIR/v120-bootstrap.authorization"
readonly ROOT_STATE="$CONTROL_DIR/v120-state.state"
readonly -a PACKAGE_NAMES=(
  v120_state.sh
  sync-v120-root-state.sh
  rollback-v120.sh
  install-v120-control.sh
  it-spareparts.cron
  source.tar
)
readonly -a MANIFEST_KEYS=(
  V120_STATE_SHA256
  ROOT_SYNC_SHA256
  ROLLBACK_SHA256
  INSTALLER_SHA256
  CRON_SHA256
  SOURCE_TAR_SHA256
)
readonly -a VERSION_MODES=(700 700 700 700 600 600)
readonly SOURCE_TAR_LIMIT=67108864

fatal() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

ensure_new_or_exact_directory() {
  local path=$1
  local mode=$2
  local owner=$3
  local group=$4
  if [ -e "$path" ] || [ -L "$path" ]; then
    [ -d "$path" ] && [ ! -L "$path" ] \
      || fatal "unsafe directory: $path"
    [ "$(stat -c '%a %U:%G' "$path")" = "$mode $owner:$group" ] \
      || fatal "directory owner/mode mismatch: $path"
    return 0
  fi
  mkdir -- "$path" || fatal "cannot create directory: $path"
  chown "$owner:$group" "$path"
  chmod "$mode" "$path"
  [ "$(stat -c '%a %U:%G' "$path")" = "$mode $owner:$group" ] \
    || fatal "new directory owner/mode mismatch: $path"
}

check_control_directories() {
  local path
  for path in "$CONTROL_DIR" "$VERSIONS_DIR" "$ARCHIVE_DIR"; do
    [ -d "$path" ] && [ ! -L "$path" ] \
      || fatal "unsafe control directory: $path"
    [ "$(stat -c '%a %U:%G' "$path")" = "700 root:root" ] \
      || fatal "control directory owner/mode mismatch: $path"
  done
}

prepare_install_directories() {
  ensure_new_or_exact_directory "$LOCK_PATH" 750 root ubuntu
  ensure_new_or_exact_directory "$CONTROL_DIR" 700 root root
  ensure_new_or_exact_directory "$VERSIONS_DIR" 700 root root
  ensure_new_or_exact_directory "$ARCHIVE_DIR" 700 root root
  ensure_new_or_exact_directory "$MARKER_DIR" 700 root root
}

acquire_release_lock() {
  [ -d "$LOCK_PATH" ] && [ ! -L "$LOCK_PATH" ] \
    || fatal "release lock is unsafe"
  [ "$(stat -c '%a %U:%G' "$LOCK_PATH")" = "750 root:ubuntu" ] \
    || fatal "release lock owner/mode mismatch"
  exec 9<"$LOCK_PATH"
  flock -n 9 || {
    printf 'RELEASE_BUSY: another v1.20 operation holds %s\n' \
      "$LOCK_PATH" >&2
    exit 75
  }
}

manifest_key_allowed() {
  case "$1" in
    CONTROL_FORMAT|TARGET_COMMIT|V120_STATE_SHA256|ROOT_SYNC_SHA256|\
    ROLLBACK_SHA256|INSTALLER_SHA256|CRON_SHA256|SOURCE_TAR_SHA256)
      return 0
      ;;
    *) return 1 ;;
  esac
}

parse_manifest() {
  local manifest_path=$1
  local output_name=$2
  local -n output_ref=$output_name
  local line
  local key
  local value
  local count=0
  output_ref=()
  [ -f "$manifest_path" ] && [ ! -L "$manifest_path" ] \
    && [ "$(stat -c '%h' "$manifest_path")" = 1 ] \
    && [ "$(stat -c '%s' "$manifest_path")" -le 4096 ] \
    || return 64
  while IFS= read -r line; do
    count=$((count + 1))
    [[ "$line" == *=* ]] && [[ "${line#*=}" != *=* ]] || return 64
    key=${line%%=*}
    value=${line#*=}
    manifest_key_allowed "$key" || return 64
    [ -z "${output_ref[$key]+x}" ] || return 64
    output_ref["$key"]=$value
  done < "$manifest_path"
  [ "$count" -eq 8 ] || return 64
  [ "${output_ref[CONTROL_FORMAT]:-}" = v120-control-2 ] || return 64
  [[ "${output_ref[TARGET_COMMIT]:-}" =~ ^[0-9a-f]{40}$ ]] || return 64
  for key in "${MANIFEST_KEYS[@]}"; do
    [[ "${output_ref[$key]:-}" =~ ^[0-9a-f]{64}$ ]] || return 64
  done
}

copy_bounded_nofollow() {
  local source=$1
  local destination=$2
  local limit=$3
  local size
  [ -f "$source" ] && [ ! -L "$source" ] \
    && [ "$(stat -c '%h' "$source")" = 1 ] || return 1
  size=$(stat -c '%s' "$source") || return 1
  [ "$size" -gt 0 ] && [ "$size" -le "$limit" ] || return 1
  timeout 10 dd if="$source" of="$destination" \
    iflag=nofollow,fullblock bs=$((limit + 1)) count=1 status=none \
    || return 1
  size=$(stat -c '%s' "$destination") || return 1
  [ "$size" -gt 0 ] && [ "$size" -le "$limit" ] || return 1
  chown root:root "$destination"
  chmod 600 "$destination"
}

validate_package_directory() {
  local package_dir=$1
  local expected_manifest_hash=$2
  local expected_owner="root:root"
  local index
  local actual
  local limit
  local source
  declare -A manifest=()
  if [ "${V120_STATE_TEST_MODE:-0}" = 1 ]; then
    expected_owner="$(id -un):$(id -gn)"
  fi

  [ -d "$package_dir" ] && [ ! -L "$package_dir" ] \
    || fatal "control package directory is unsafe"
  [ "$(stat -c '%a %U:%G' "$package_dir")" \
    = "700 $expected_owner" ] \
    || fatal "control package directory owner/mode mismatch"
  [ -f "$package_dir/manifest.txt" ] \
    && [ ! -L "$package_dir/manifest.txt" ] \
    && [ "$(stat -c '%a %U:%G %h' "$package_dir/manifest.txt")" \
      = "600 $expected_owner 1" ] \
    || fatal "control manifest is unsafe"
  [ "$(sha256sum "$package_dir/manifest.txt" | cut -d' ' -f1)" \
    = "$expected_manifest_hash" ] || fatal "control manifest hash mismatch"
  parse_manifest "$package_dir/manifest.txt" manifest \
    || fatal "invalid control manifest"
  for index in "${!PACKAGE_NAMES[@]}"; do
    source="$package_dir/${PACKAGE_NAMES[$index]}"
    [ -f "$source" ] && [ ! -L "$source" ] \
      && [ "$(stat -c '%a %U:%G %h' "$source")" \
        = "${VERSION_MODES[$index]} $expected_owner 1" ] \
      || fatal "unsafe packaged control file"
    limit=262144
    [ "${PACKAGE_NAMES[$index]}" != source.tar ] \
      || limit=$SOURCE_TAR_LIMIT
    [ "$(stat -c '%s' "$source")" -gt 0 ] \
      && [ "$(stat -c '%s' "$source")" -le "$limit" ] \
      || fatal "packaged control file size is unsafe"
    actual=$(sha256sum "$source" | cut -d' ' -f1)
    [ "$actual" = "${manifest[${MANIFEST_KEYS[$index]}]}" ] \
      || fatal "packaged control file hash mismatch"
    if [[ "${PACKAGE_NAMES[$index]}" == *.sh ]]; then
      bash -n "$source"
    fi
  done
}

stage_inbox_package() {
  local expected_manifest_hash=$1
  local inbox="/var/tmp/it-spareparts-control-$expected_manifest_hash"
  local staging
  local index
  local limit
  [ -d "$inbox" ] && [ ! -L "$inbox" ] \
    || fatal "operator-transfer package is missing or unsafe"
  staging=$(mktemp -d "$CONTROL_DIR/.incoming-control.XXXXXX")
  STAGED_PACKAGE=$staging
  chmod 700 "$staging"
  chown root:root "$staging"
  copy_bounded_nofollow "$inbox/manifest.txt" \
    "$staging/manifest.txt" 4096 \
    || fatal "cannot stage control manifest"
  for index in "${!PACKAGE_NAMES[@]}"; do
    limit=262144
    [ "${PACKAGE_NAMES[$index]}" != source.tar ] \
      || limit=$SOURCE_TAR_LIMIT
    copy_bounded_nofollow "$inbox/${PACKAGE_NAMES[$index]}" \
      "$staging/${PACKAGE_NAMES[$index]}" "$limit" \
      || fatal "cannot stage packaged control file"
    chmod "${VERSION_MODES[$index]}" \
      "$staging/${PACKAGE_NAMES[$index]}"
  done
  validate_package_directory "$staging" "$expected_manifest_hash"
}

persist_version() {
  local expected_manifest_hash=$1
  local destination="$VERSIONS_DIR/$expected_manifest_hash"
  if [ -e "$destination" ] || [ -L "$destination" ]; then
    validate_package_directory "$destination" "$expected_manifest_hash"
    find "$STAGED_PACKAGE" -depth -mindepth 1 -delete
    rmdir "$STAGED_PACKAGE"
  else
    sync -f "$STAGED_PACKAGE/manifest.txt"
    for artifact in "${PACKAGE_NAMES[@]}"; do
      sync -f "$STAGED_PACKAGE/$artifact"
    done
    sync -f "$STAGED_PACKAGE"
    mv -T -- "$STAGED_PACKAGE" "$destination"
    sync -f "$destination"
    sync -d "$VERSIONS_DIR"
  fi
  STAGED_PACKAGE=
  VERSION_DIR=$destination
}

publish_current_pointer() (
  set -Eeuo pipefail
  local control_dir=$1
  local versions_dir=$2
  local expected_manifest_hash=$3
  local current="$control_dir/current"
  local temporary=
  local version="$versions_dir/$expected_manifest_hash"

  [[ "$expected_manifest_hash" =~ ^[0-9a-f]{64}$ ]] || return 64
  [ -d "$control_dir" ] && [ ! -L "$control_dir" ] || return 74
  [ -d "$versions_dir" ] && [ ! -L "$versions_dir" ] || return 74
  [ -d "$version" ] && [ ! -L "$version" ] || return 74
  temporary=$(mktemp -- "$control_dir/.current.XXXXXX") || return $?
  rm -f -- "$temporary" || return $?
  trap '[ -z "$temporary" ] || rm -f -- "$temporary"' EXIT
  ln -s -- "versions/$expected_manifest_hash" "$temporary" || return $?
  if [ "${V120_STATE_TEST_MODE:-0}" = 1 ] \
      && [ "${V120_INSTALLER_TEST_FAILPOINT:-}" = before_current_rename ]; then
    return 74
  fi
  if [ "${V120_STATE_TEST_MODE:-0}" = 1 ] \
      && [ "${V120_INSTALLER_TEST_FAILPOINT:-}" \
        = kill_before_current_rename ]; then
    kill -KILL "$BASHPID"
  fi
  mv -fT -- "$temporary" "$current" || return $?
  temporary=
  sync -d "$control_dir" || return $?
)

verify_current_version() {
  local expected_manifest_hash=$1
  local expected_target="versions/$expected_manifest_hash"
  [ -L "$CURRENT_LINK" ] \
    && [ "$(stat -c '%F %U:%G %h' "$CURRENT_LINK")" \
      = "symbolic link root:root 1" ] \
    || fatal "current control pointer is unsafe"
  [ "$(readlink -- "$CURRENT_LINK")" = "$expected_target" ] \
    || fatal "current control pointer targets another version"
  validate_package_directory \
    "$VERSIONS_DIR/$expected_manifest_hash" "$expected_manifest_hash"
}

validate_bootstrap_authorization() {
  local authorization
  local manifest_file
  local expected_manifest_hash
  local expected_identity="600 root:root 1"
  local target
  declare -A manifest=()

  authorization=$1
  manifest_file=$2
  expected_manifest_hash=$3
  if [ "${V120_STATE_TEST_MODE:-0}" = 1 ]; then
    expected_identity="600 $(id -un):$(id -gn) 1"
  fi
  [ -f "$authorization" ] && [ ! -L "$authorization" ] \
    && [ "$(stat -c '%a %U:%G %h' "$authorization")" \
      = "$expected_identity" ] \
    || return 77
  [ "$(sha256sum "$manifest_file" | cut -d' ' -f1)" \
    = "$expected_manifest_hash" ] || return 77
  parse_manifest "$manifest_file" manifest || return 77
  target=${manifest[TARGET_COMMIT]}
  [ "$(cat "$authorization")" = "$(
    printf 'AUTHORIZATION_FORMAT=v120-bootstrap-1\n'
    printf 'CONTROL_MANIFEST_HASH=%s\n' "$expected_manifest_hash"
    printf 'TARGET_COMMIT=%s' "$target"
  )" ] || return 77
}

authority_evidence_mode() {
  local marker_exists=$1
  local state_exists=$2
  local control_exists=$3
  local authorization_exists=$4
  [[ "$marker_exists$state_exists$control_exists$authorization_exists" \
    =~ ^[01]{4}$ ]] || return 64
  if [ "$marker_exists:$state_exists:$control_exists" = 1:1:1 ]; then
    printf 'existing\n'
    return 0
  fi
  if [ "$marker_exists:$state_exists:$control_exists:$authorization_exists" \
      = 0:0:0:1 ]; then
    printf 'initializing\n'
    return 0
  fi
  return 77
}

preflight_authority_evidence() {
  local control_was_present=$1
  local authorization_exists=0
  local marker_exists=0
  local mode
  local state_exists=0
  [ ! -e "$AUTHORITY_MARKER" ] && [ ! -L "$AUTHORITY_MARKER" ] \
    || marker_exists=1
  [ ! -e "$ROOT_STATE" ] && [ ! -L "$ROOT_STATE" ] \
    || state_exists=1
  [ ! -e "$BOOTSTRAP_AUTH" ] && [ ! -L "$BOOTSTRAP_AUTH" ] \
    || authorization_exists=1
  mode=$(
    authority_evidence_mode \
      "$marker_exists" "$state_exists" "$control_was_present" \
      "$authorization_exists"
  ) || fatal "authority evidence is incomplete; recovery must fail closed"

  if [ "$mode" = existing ]; then
    [ -f "$AUTHORITY_MARKER" ] && [ ! -L "$AUTHORITY_MARKER" ] \
      && [ "$(stat -c '%a %U:%G %h' "$AUTHORITY_MARKER")" \
        = "600 root:root 1" ] \
      || fatal "authority marker is unsafe"
    AUTHORITY_INITIALIZING=0
    return 0
  fi
  [ -f "$BOOTSTRAP_AUTH" ] && [ ! -L "$BOOTSTRAP_AUTH" ] \
    || fatal "explicit one-time bootstrap authorization is required"
  AUTHORITY_INITIALIZING=1
}

validate_authority_for_version() {
  local expected_manifest_hash=$1
  local version_dir=$2
  if [ -e "$AUTHORITY_MARKER" ] || [ -L "$AUTHORITY_MARKER" ]; then
    return 0
  fi
  [ "${AUTHORITY_INITIALIZING:-0}" = 1 ] \
    || fatal "authority evidence disappeared during control installation"
  validate_bootstrap_authorization \
    "$BOOTSTRAP_AUTH" "$version_dir/manifest.txt" "$expected_manifest_hash" \
    || fatal "explicit bootstrap authorization conflicts with control package"
}

activate_version() {
  local expected_manifest_hash=$1
  validate_package_directory "$VERSION_DIR" "$expected_manifest_hash"
  publish_current_pointer \
    "$CONTROL_DIR" "$VERSIONS_DIR" "$expected_manifest_hash" \
    || fatal "cannot atomically switch current control version"
  verify_current_version "$expected_manifest_hash"
}

legacy_cron_absent() {
  local current
  local errors
  local status
  current=$(mktemp) || return $?
  errors=$(mktemp) || {
    status=$?
    rm -f -- "$current"
    return "$status"
  }
  if LC_ALL=C crontab -u ubuntu -l > "$current" 2> "$errors"; then
    :
  else
    status=$?
    if [ "$status" -eq 1 ] \
        && [ "$(cat "$errors")" = "no crontab for ubuntu" ] \
        && [ ! -s "$current" ]; then
      :
    else
      cat "$errors" >&2
      rm -f -- "$current" "$errors"
      return "$status"
    fi
  fi
  rm -f -- "$errors"
  if grep -F \
      -e "$APP_DIR/backup.sh" \
      -e "$APP_DIR/.deploy/backup.sh" \
      -e "$APP_DIR/monitor.sh" \
      -e "$APP_DIR/.deploy/monitor.sh" \
      "$current"; then
    rm -f -- "$current"
    return 75
  else
    status=$?
    rm -f -- "$current"
    [ "$status" -eq 1 ] || return "$status"
  fi
}

scheduler_file_has_project_reference() {
  local file
  local status
  file=$1
  if timeout --kill-after=1s 5s grep -Fq \
      -e "$APP_DIR/backup.sh" \
      -e "$APP_DIR/.deploy/backup.sh" \
      -e "$APP_DIR/monitor.sh" \
      -e "$APP_DIR/.deploy/monitor.sh" \
      -- "$file"; then
    return 0
  else
    status=$?
    [ "$status" -eq 1 ] || return "$status"
  fi
  if timeout --kill-after=1s 5s grep -Fq -- "$APP_DIR" "$file"; then
    timeout --kill-after=1s 5s grep -Eq \
      '(^|[=[:space:]/])([.]/)?([.]deploy/)?(backup|monitor)[.]sh([;[:space:]]|$)' \
      -- "$file"
  else
    status=$?
    [ "$status" -eq 1 ] || return "$status"
    return 1
  fi
}

scheduler_root_path() {
  local root=$1
  local path=$2
  if [ "$root" = / ]; then
    printf '%s\n' "$path"
  else
    printf '%s%s\n' "${root%/}" "$path"
  fi
}

static_scheduler_duplicates_absent() {
  local root=${1:-/}
  local cron_destination
  local etc_directory
  local direct
  local directory
  local file
  local inventory
  local status
  local -a direct_files=()
  local -a cron_directories=()
  local -a scan_directories=()
  local -a home_unit_directories=()

  [ -d "$root" ] && [ ! -L "$root" ] || return 64
  cron_destination=$(scheduler_root_path "$root" "$CRON_DEST")
  etc_directory=$(scheduler_root_path "$root" /etc)
  direct_files+=(
    "$(scheduler_root_path "$root" /etc/crontab)"
    "$(scheduler_root_path "$root" /etc/anacrontab)"
  )
  for directory in \
    /etc/cron.d \
    /etc/cron.hourly \
    /etc/cron.daily \
    /etc/cron.weekly \
    /etc/cron.monthly \
    /var/spool/cron \
    /var/spool/cron/crontabs \
    /etc/systemd/system \
    /etc/systemd/user \
    /lib/systemd/system \
    /lib/systemd/user \
    /usr/lib/systemd/system \
    /usr/lib/systemd/user \
    /usr/local/lib/systemd/system \
    /usr/local/lib/systemd/user \
    /run/systemd/system \
    /run/systemd/transient \
    /run/systemd/generator \
    /run/systemd/generator.early \
    /run/systemd/generator.late \
    /root/.config/systemd/user \
    /root/.local/share/systemd/user
  do
    scan_directories+=("$(scheduler_root_path "$root" "$directory")")
  done
  shopt -s nullglob
  cron_directories=("$etc_directory"/cron.*)
  home_unit_directories=(
    "$(scheduler_root_path "$root" /home)"/*/.config/systemd/user
    "$(scheduler_root_path "$root" /home)"/*/.local/share/systemd/user
  )
  shopt -u nullglob
  scan_directories+=(
    "${cron_directories[@]}"
    "${home_unit_directories[@]}"
  )

  inventory=$(mktemp) || return $?
  for direct in "${direct_files[@]}"; do
    [ -f "$direct" ] || continue
    printf '%s\0' "$direct" >> "$inventory" || {
      rm -f -- "$inventory"
      return 1
    }
  done
  for directory in "${scan_directories[@]}"; do
    [ -d "$directory" ] || continue
    if ! timeout --kill-after=2s 15s \
        find "$directory" -maxdepth 4 -type f -print0 \
        >> "$inventory"; then
      rm -f -- "$inventory"
      return 1
    fi
  done
  # The inventory remains readable through its open descriptor when early
  # returns unlink the private temporary pathname.
  # shellcheck disable=SC2094
  while IFS= read -r -d '' file; do
    [ "$file" != "$cron_destination" ] || continue
    if scheduler_file_has_project_reference "$file"; then
      printf 'FATAL: duplicate scheduler entry in %s\n' "$file" >&2
      rm -f -- "$inventory"
      return 75
    else
      status=$?
      [ "$status" -eq 1 ] || {
        rm -f -- "$inventory"
        return "$status"
      }
    fi
  done < "$inventory"
  rm -f -- "$inventory"
}

active_timer_scope_duplicates_absent() {
  local scope=$1
  shift
  local details
  local listing
  local status
  local unit
  local units

  listing=$(mktemp) || return $?
  units=$(mktemp) || {
    status=$?
    rm -f -- "$listing"
    return "$status"
  }
  details=$(mktemp) || {
    status=$?
    rm -f -- "$listing" "$units"
    return "$status"
  }
  if ! "$@" list-timers --all --full --no-legend --no-pager --plain \
      > "$listing"; then
    rm -f -- "$listing" "$units" "$details"
    return 1
  fi
  if ! awk 'NF >= 2 { print $(NF - 1); print $NF }' "$listing" \
      | LC_ALL=C sort -u > "$units"; then
    rm -f -- "$listing" "$units" "$details"
    return 1
  fi
  # The units file remains readable through its open descriptor when early
  # returns unlink the private temporary pathname.
  # shellcheck disable=SC2094
  while IFS= read -r unit; do
    [ -n "$unit" ] || continue
    [[ "$unit" =~ ^[A-Za-z0-9_.:@\\-]+[.][A-Za-z0-9_-]+$ ]] || {
      rm -f -- "$listing" "$units" "$details"
      return 64
    }
    if ! "$@" show "$unit" --no-pager \
        --property FragmentPath \
        --property SourcePath \
        --property Unit \
        --property WorkingDirectory \
        --property ExecStart > "$details"; then
      rm -f -- "$listing" "$units" "$details"
      return 1
    fi
    if scheduler_file_has_project_reference "$details"; then
      printf 'FATAL: duplicate scheduler entry in active %s unit %s\n' \
        "$scope" "$unit" >&2
      rm -f -- "$listing" "$units" "$details"
      return 75
    else
      status=$?
      [ "$status" -eq 1 ] || {
        rm -f -- "$listing" "$units" "$details"
        return "$status"
      }
    fi
  done < "$units"
  rm -f -- "$listing" "$units" "$details"
}

active_systemd_timer_duplicates_absent() {
  local bus
  local entry
  local runtime
  local uid
  local user
  local -a user_buses=()

  active_timer_scope_duplicates_absent system \
    timeout --kill-after=2s 15s systemctl --system \
    || return $?
  shopt -s nullglob
  user_buses=(/run/user/[0-9]*/bus)
  shopt -u nullglob
  for bus in "${user_buses[@]}"; do
    [ -S "$bus" ] || continue
    runtime=$(dirname -- "$bus")
    uid=$(basename -- "$runtime")
    [[ "$uid" =~ ^(0|[1-9][0-9]*)$ ]] || return 64
    entry=$(getent passwd "$uid") || return 75
    user=${entry%%:*}
    [ -n "$user" ] || return 75
    active_timer_scope_duplicates_absent "user:$user" \
      timeout --kill-after=2s 15s \
        runuser -u "$user" -- \
          env "XDG_RUNTIME_DIR=$runtime" \
            "DBUS_SESSION_BUS_ADDRESS=unix:path=$bus" \
            systemctl --user \
      || return $?
  done
}

other_scheduler_duplicates_absent() {
  static_scheduler_duplicates_absent || return $?
  active_systemd_timer_duplicates_absent || return $?
}

install_cron() {
  local expected_manifest_hash=$1
  local package_dir="$VERSIONS_DIR/$expected_manifest_hash"
  local temporary
  validate_package_directory "$package_dir" "$expected_manifest_hash"
  legacy_cron_absent \
    || fatal "legacy user cron must be removed before cron.d activation"
  other_scheduler_duplicates_absent \
    || fatal "another scheduler still runs backup or monitor"
  if [ -e "$CRON_DEST" ] || [ -L "$CRON_DEST" ]; then
    [ -f "$CRON_DEST" ] && [ ! -L "$CRON_DEST" ] \
      || fatal "unsafe cron destination"
  fi
  temporary=$(mktemp -- /etc/cron.d/.it-spareparts.XXXXXX)
  install -m 644 -o root -g root \
    "$package_dir/it-spareparts.cron" "$temporary"
  mv -fT -- "$temporary" "$CRON_DEST"
  sync -f "$CRON_DEST"
  sync -d /etc/cron.d
}

verify_cron() {
  local expected_manifest_hash=$1
  local package_dir="$VERSIONS_DIR/$expected_manifest_hash"
  validate_package_directory "$package_dir" "$expected_manifest_hash"
  if ! [ -f "$CRON_DEST" ] || [ -L "$CRON_DEST" ] \
      || [ "$(stat -c '%a %U:%G %h' "$CRON_DEST")" \
        != "644 root:root 1" ] \
      || ! cmp -s "$package_dir/it-spareparts.cron" "$CRON_DEST"; then
    fatal "dedicated cron installation differs from package"
  fi
  legacy_cron_absent || fatal "legacy user cron still duplicates cron.d"
  other_scheduler_duplicates_absent \
    || fatal "another scheduler duplicates dedicated cron.d"
}

if [ "${V120_INSTALLER_LIBRARY_ONLY:-0}" = 1 ]; then
  [ "${V120_STATE_TEST_MODE:-0}" = 1 ] \
    && [ "${BASH_SOURCE[0]}" != "$0" ] \
    || fatal "installer library mode is test-only and must be sourced"
  return 0
fi

[ "$EUID" -eq 0 ] || fatal "installer must run as root"
[ "$#" -eq 2 ] \
  || fatal "usage: install_v120_control.sh <install|verify|install-cron|verify-cron> <manifest SHA>"
ACTION=$1
EXPECTED_MANIFEST_HASH=$2
[[ "$EXPECTED_MANIFEST_HASH" =~ ^[0-9a-f]{64}$ ]] \
  || fatal "invalid manifest SHA"
CONTROL_WAS_PRESENT=0
if [ -e "$CONTROL_DIR" ] || [ -L "$CONTROL_DIR" ]; then
  CONTROL_WAS_PRESENT=1
fi
STAGED_PACKAGE=
VERSION_DIR=
INSTALL_COMPLETE=0
cleanup_installer() {
  local status=$?
  trap - EXIT HUP INT TERM
  if [ -n "${STAGED_PACKAGE:-}" ] && [ -d "$STAGED_PACKAGE" ]; then
    find "$STAGED_PACKAGE" -depth -mindepth 1 -delete || status=97
    rmdir "$STAGED_PACKAGE" || status=97
  fi
  if [ "$INSTALL_COMPLETE" != 1 ] && [ "$CONTROL_WAS_PRESENT" = 0 ] \
      && [ ! -e "$CURRENT_LINK" ] && [ ! -L "$CURRENT_LINK" ] \
      && [ ! -e "$ROOT_STATE" ] && [ ! -L "$ROOT_STATE" ]; then
    rmdir "$VERSIONS_DIR" "$ARCHIVE_DIR" "$CONTROL_DIR" 2>/dev/null || true
  fi
  exit "$status"
}
trap cleanup_installer EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
case "$ACTION" in
  install)
    ensure_new_or_exact_directory "$LOCK_PATH" 750 root ubuntu
    acquire_release_lock
    preflight_authority_evidence "$CONTROL_WAS_PRESENT"
    prepare_install_directories
    check_control_directories
    stage_inbox_package "$EXPECTED_MANIFEST_HASH"
    validate_authority_for_version \
      "$EXPECTED_MANIFEST_HASH" "$STAGED_PACKAGE"
    persist_version "$EXPECTED_MANIFEST_HASH"
    activate_version "$EXPECTED_MANIFEST_HASH"
    INSTALL_COMPLETE=1
    ;;
  verify)
    check_control_directories
    verify_current_version "$EXPECTED_MANIFEST_HASH"
    ;;
  install-cron)
    prepare_install_directories
    acquire_release_lock
    check_control_directories
    verify_current_version "$EXPECTED_MANIFEST_HASH"
    install_cron "$EXPECTED_MANIFEST_HASH"
    verify_cron "$EXPECTED_MANIFEST_HASH"
    INSTALL_COMPLETE=1
    ;;
  verify-cron)
    check_control_directories
    verify_current_version "$EXPECTED_MANIFEST_HASH"
    verify_cron "$EXPECTED_MANIFEST_HASH"
    ;;
  *) fatal "unknown installer action" ;;
esac
trap - EXIT HUP INT TERM
printf 'CONTROL_%s_OK manifest=%s\n' \
  "${ACTION^^}" "$EXPECTED_MANIFEST_HASH"
