#!/usr/bin/env bash
# Atomically commit a validated state from stdin to the single root authority.
set -Eeuo pipefail
umask 077

readonly CONTROL_DIR=/var/lib/it-spareparts-release-control
readonly ROOT_STATE="$CONTROL_DIR/v120-state.state"
readonly STATE_LOCK="$CONTROL_DIR/.state.lock"
readonly ARCHIVE_DIR="$CONTROL_DIR/archive"
readonly MARKER_DIR=/etc/it-spareparts
readonly AUTHORITY_MARKER="$MARKER_DIR/v120-authority.marker"
readonly BOOTSTRAP_AUTH="$MARKER_DIR/v120-bootstrap.authorization"
SCRIPT_DIR=$(
  cd "$(dirname "${BASH_SOURCE[0]}")"
  pwd -P
)
readonly SCRIPT_DIR
RUNTIME_CONTROL_MANIFEST_HASH=$(basename -- "$SCRIPT_DIR")
readonly RUNTIME_CONTROL_MANIFEST_HASH
readonly LIBRARY="$SCRIPT_DIR/v120_state.sh"

fatal() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

[ "$EUID" -eq 0 ] || fatal "sync helper must run as root"
[ "$#" -eq 0 ] || fatal "sync helper accepts state on stdin and no paths"
[[ "$RUNTIME_CONTROL_MANIFEST_HASH" =~ ^[0-9a-f]{64}$ ]] \
  || fatal "sync helper is not inside a hash-addressed control version"
[ "$SCRIPT_DIR" \
  = "$CONTROL_DIR/versions/$RUNTIME_CONTROL_MANIFEST_HASH" ] \
  || fatal "sync helper version path is unsafe"
"$SCRIPT_DIR/install-v120-control.sh" verify "$RUNTIME_CONTROL_MANIFEST_HASH" \
  >/dev/null
[ -d "$CONTROL_DIR" ] && [ ! -L "$CONTROL_DIR" ] \
  || fatal "unsafe control directory"
[ "$(stat -c '%a %U:%G' "$CONTROL_DIR")" = "700 root:root" ] \
  || fatal "control directory owner/mode mismatch"
[ -d "$ARCHIVE_DIR" ] && [ ! -L "$ARCHIVE_DIR" ] \
  || fatal "unsafe control archive directory"
[ "$(stat -c '%a %U:%G' "$ARCHIVE_DIR")" = "700 root:root" ] \
  || fatal "control archive owner/mode mismatch"
[ -f "$LIBRARY" ] && [ ! -L "$LIBRARY" ] \
  || fatal "trusted state library is missing"
[ "$(stat -c '%a %U:%G %h' "$LIBRARY")" = "700 root:root 1" ] \
  || fatal "trusted state library owner/mode mismatch"
# shellcheck source=.deploy/v120_state.sh
source "$LIBRARY"

if [ ! -e "$STATE_LOCK" ]; then
  ( set -o noclobber; : > "$STATE_LOCK" ) 2>/dev/null \
    || fatal "cannot create root state lock"
fi
[ -f "$STATE_LOCK" ] && [ ! -L "$STATE_LOCK" ] \
  || fatal "unsafe root state lock"
chown root:root "$STATE_LOCK"
chmod 600 "$STATE_LOCK"
exec 8>>"$STATE_LOCK"
flock -x 8

temporary=$(mktemp -- "$CONTROL_DIR/.v120-state.input.XXXXXX")
marker_temporary=
archive_temporary=
cleanup_root_sync() {
  [ -z "${temporary:-}" ] || rm -f -- "$temporary"
  [ -z "${marker_temporary:-}" ] || rm -f -- "$marker_temporary"
  [ -z "${archive_temporary:-}" ] || rm -f -- "$archive_temporary"
}
trap cleanup_root_sync EXIT
head -c 16385 > "$temporary"
[ "$(stat -c '%s' "$temporary")" -le 16384 ] \
  || fatal "state input exceeds 16 KiB"
chmod 600 "$temporary"
chown root:root "$temporary"
declare -A candidate_state=()
v120_state_parse_to_array "$temporary" candidate_state
[ "${candidate_state[CONTROL_MANIFEST_HASH]}" \
  = "$RUNTIME_CONTROL_MANIFEST_HASH" ] \
  || fatal "candidate state targets another control version"
initializing=0

if [ -e "$ROOT_STATE" ] || [ -L "$ROOT_STATE" ]; then
  [ -f "$AUTHORITY_MARKER" ] && [ ! -L "$AUTHORITY_MARKER" ] \
    || fatal "authority marker is missing or unsafe"
  [ "$(stat -c '%a %U:%G %h' "$AUTHORITY_MARKER")" \
    = "600 root:root 1" ] \
    || fatal "authority marker owner/mode mismatch"
  mapfile -t marker_lines < "$AUTHORITY_MARKER"
  [ "${#marker_lines[@]}" -eq 3 ] \
    && [ "${marker_lines[0]}" = "AUTHORITY_FORMAT=v120-authority-1" ] \
    && [[ "${marker_lines[1]}" =~ ^INITIAL_CONTROL_MANIFEST_HASH=[0-9a-f]{64}$ ]] \
    && [[ "${marker_lines[2]}" =~ ^INITIAL_TARGET_COMMIT=[0-9a-f]{40}$ ]] \
    || fatal "authority marker content mismatch"
  [ -f "$ROOT_STATE" ] && [ ! -L "$ROOT_STATE" ] \
    || fatal "unsafe root state"
  [ "$(stat -c '%a %U:%G %h' "$ROOT_STATE")" = "600 root:root 1" ] \
    || fatal "root state owner/mode mismatch"
  if cmp -s "$temporary" "$ROOT_STATE"; then
    if [ -e "$BOOTSTRAP_AUTH" ] || [ -L "$BOOTSTRAP_AUTH" ]; then
      [ -f "$BOOTSTRAP_AUTH" ] && [ ! -L "$BOOTSTRAP_AUTH" ] \
        && [ "$(stat -c '%a %U:%G %h' "$BOOTSTRAP_AUTH")" \
          = "600 root:root 1" ] \
        || fatal "stale bootstrap authorization is unsafe"
      rm -f -- "$BOOTSTRAP_AUTH"
      sync -d "$MARKER_DIR"
    fi
    rm -f -- "$temporary"
    temporary=
    exit 0
  fi
  declare -A old_state=()
  v120_state_parse_to_array "$ROOT_STATE" old_state
  if [ "${old_state[RELEASE_ID]}" = "${candidate_state[RELEASE_ID]}" ]; then
    v120_state_validate_transition old_state candidate_state \
      || fatal "root state transition conflict"
  else
    old_hash=$(sha256sum "$ROOT_STATE" | cut -d' ' -f1)
    v120_state_validate_supersession old_state candidate_state "$old_hash" \
      || fatal "root state supersession conflict"
    archive_name="state-${old_state[ATTEMPT_NO]}-${old_state[RELEASE_ID]}-${old_state[RELEASE_PHASE]}.state"
    archive_path="$ARCHIVE_DIR/$archive_name"
    if [ -e "$archive_path" ] || [ -L "$archive_path" ]; then
      if ! [ -f "$archive_path" ] || [ -L "$archive_path" ] \
          || [ "$(stat -c '%a %U:%G %h' "$archive_path")" \
            != "600 root:root 1" ] \
          || ! cmp -s "$ROOT_STATE" "$archive_path"; then
        fatal "existing root state archive conflicts"
      fi
    else
      archive_temporary=$(mktemp -- "$ARCHIVE_DIR/.state-archive.XXXXXX")
      install -m 600 -o root -g root "$ROOT_STATE" "$archive_temporary"
      sync -f "$archive_temporary"
      mv -T -- "$archive_temporary" "$archive_path"
      archive_temporary=
      sync -f "$archive_path"
      sync -d "$ARCHIVE_DIR"
    fi
  fi
else
  if [ -e "$AUTHORITY_MARKER" ] || [ -L "$AUTHORITY_MARKER" ]; then
    fatal "authority state is missing after initialization"
  fi
  if find "$ARCHIVE_DIR" -mindepth 1 -maxdepth 1 \
      -name 'state-*.state' -print -quit | grep -q .; then
    fatal "authority history exists without an active state"
  fi
  [ "${candidate_state[RELEASE_PHASE]}" = built ] \
    && [ "${candidate_state[STATE_GENERATION]}" = 0 ] \
    && [ "${candidate_state[ATTEMPT_NO]}" = 1 ] \
    && [ "${candidate_state[PARENT_RELEASE_ID]}" = none ] \
    || fatal "initial root state must be generation-0 built"
  [ -f "$BOOTSTRAP_AUTH" ] && [ ! -L "$BOOTSTRAP_AUTH" ] \
    && [ "$(stat -c '%a %U:%G %h' "$BOOTSTRAP_AUTH")" \
      = "600 root:root 1" ] \
    || fatal "one-time bootstrap authorization is missing or unsafe"
  [ "$(cat "$BOOTSTRAP_AUTH")" = "$(
    printf 'AUTHORIZATION_FORMAT=v120-bootstrap-1\n'
    printf 'CONTROL_MANIFEST_HASH=%s\n' \
      "${candidate_state[CONTROL_MANIFEST_HASH]}"
    printf 'TARGET_COMMIT=%s' "${candidate_state[TARGET_COMMIT]}"
  )" ] || fatal "bootstrap authorization does not match initial state"
  if [ -e "$MARKER_DIR" ] || [ -L "$MARKER_DIR" ]; then
    [ -d "$MARKER_DIR" ] && [ ! -L "$MARKER_DIR" ] \
      || fatal "unsafe authority marker directory"
    [ "$(stat -c '%a %U:%G' "$MARKER_DIR")" = "700 root:root" ] \
      || fatal "authority marker directory owner/mode mismatch"
  else
    mkdir -- "$MARKER_DIR"
    chown root:root "$MARKER_DIR"
    chmod 700 "$MARKER_DIR"
  fi
  marker_temporary=$(mktemp -- "$MARKER_DIR/.v120-authority.XXXXXX")
  {
    printf 'AUTHORITY_FORMAT=v120-authority-1\n'
    printf 'INITIAL_CONTROL_MANIFEST_HASH=%s\n' \
      "${candidate_state[CONTROL_MANIFEST_HASH]}"
    printf 'INITIAL_TARGET_COMMIT=%s\n' "${candidate_state[TARGET_COMMIT]}"
  } > "$marker_temporary"
  chown root:root "$marker_temporary"
  chmod 600 "$marker_temporary"
  sync -f "$marker_temporary"
  mv -T -- "$marker_temporary" "$AUTHORITY_MARKER"
  marker_temporary=
  sync -f "$AUTHORITY_MARKER"
  sync -d "$MARKER_DIR"
  initializing=1
fi

sync -f "$temporary"
mv -fT -- "$temporary" "$ROOT_STATE"
temporary=
sync -f "$ROOT_STATE"
sync -d "$CONTROL_DIR"
[ "$(stat -c '%a %U:%G %h' "$ROOT_STATE")" = "600 root:root 1" ] \
  || fatal "root state owner/mode mismatch after commit"
if [ "$initializing" = 1 ]; then
  rm -f -- "$BOOTSTRAP_AUTH"
  sync -d "$MARKER_DIR"
fi
