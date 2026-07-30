#!/usr/bin/env bash
# Build a fixed-file control package for authenticated operator transfer.
set -Eeuo pipefail
umask 077
export PATH=/usr/local/bin:/usr/bin:/bin
export GIT_NO_REPLACE_OBJECTS=1

readonly -a SOURCE_PATHS=(
  .deploy/v120_state.sh
  .deploy/sync_v120_root_state.sh
  .deploy/rollback_v120.sh
  .deploy/install_v120_control.sh
  .deploy/it-spareparts.cron
)
readonly -a PACKAGE_NAMES=(
  v120_state.sh
  sync-v120-root-state.sh
  rollback-v120.sh
  install-v120-control.sh
  it-spareparts.cron
)
readonly -a MANIFEST_KEYS=(
  V120_STATE_SHA256
  ROOT_SYNC_SHA256
  ROLLBACK_SHA256
  INSTALLER_SHA256
  CRON_SHA256
)
readonly SOURCE_TAR_NAME=source.tar

fatal() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

[ "$EUID" -ne 0 ] || fatal "package builder must not run as root"
[ "$#" -eq 2 ] \
  || fatal "usage: package_v120_control.sh <target SHA> <output parent>"
TARGET_COMMIT=$1
OUTPUT_PARENT=$2
[[ "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ ]] || fatal "invalid target SHA"
[ -d "$OUTPUT_PARENT" ] && [ ! -L "$OUTPUT_PARENT" ] \
  || fatal "output parent is unsafe"
[ -w "$OUTPUT_PARENT" ] || fatal "output parent is not writable"

REPO_ROOT=$(git rev-parse --show-toplevel)
[ "$(git rev-parse "${TARGET_COMMIT}^{commit}")" = "$TARGET_COMMIT" ] \
  || fatal "target is not an exact commit"
git fsck --strict --no-reflogs "$TARGET_COMMIT" >/dev/null

STAGING=$(mktemp -d "$OUTPUT_PARENT/.v120-control-package.XXXXXX")
cleanup_package() {
  local status=$?
  trap - EXIT HUP INT TERM
  if [ -n "${STAGING:-}" ] && [ -d "$STAGING" ]; then
    find "$STAGING" -depth -mindepth 1 -delete || status=97
    rmdir "$STAGING" || status=97
  fi
  exit "$status"
}
trap cleanup_package EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

declare -a HASHES=()
git --no-replace-objects -C "$REPO_ROOT" \
  archive --format=tar "$TARGET_COMMIT" > "$STAGING/$SOURCE_TAR_NAME"
chmod 600 "$STAGING/$SOURCE_TAR_NAME"
[ "$(git get-tar-commit-id < "$STAGING/$SOURCE_TAR_NAME")" \
  = "$TARGET_COMMIT" ] || fatal "source archive commit mismatch"
SOURCE_TAR_HASH=$(
  sha256sum "$STAGING/$SOURCE_TAR_NAME" | cut -d' ' -f1
)
[[ "$SOURCE_TAR_HASH" =~ ^[0-9a-f]{64}$ ]] \
  || fatal "invalid source archive hash"
for index in "${!SOURCE_PATHS[@]}"; do
  destination="$STAGING/${PACKAGE_NAMES[$index]}"
  tar -xOf "$STAGING/$SOURCE_TAR_NAME" \
    "${SOURCE_PATHS[$index]}" > "$destination"
  if [[ "${SOURCE_PATHS[$index]}" == *.sh ]]; then
    chmod 700 "$destination"
    bash -n "$destination"
  else
    chmod 600 "$destination"
  fi
  HASHES[index]=$(sha256sum "$destination" | cut -d' ' -f1)
done
git fsck --strict --no-reflogs "$TARGET_COMMIT" >/dev/null

{
  printf 'CONTROL_FORMAT=v120-control-2\n'
  printf 'TARGET_COMMIT=%s\n' "$TARGET_COMMIT"
  for index in "${!MANIFEST_KEYS[@]}"; do
    printf '%s=%s\n' "${MANIFEST_KEYS[$index]}" "${HASHES[$index]}"
  done
  printf 'SOURCE_TAR_SHA256=%s\n' "$SOURCE_TAR_HASH"
} > "$STAGING/manifest.txt"
chmod 600 "$STAGING/manifest.txt"
MANIFEST_HASH=$(sha256sum "$STAGING/manifest.txt" | cut -d' ' -f1)
FINAL="$OUTPUT_PARENT/it-spareparts-control-$MANIFEST_HASH"
[ ! -e "$FINAL" ] && [ ! -L "$FINAL" ] \
  || fatal "control package already exists"
mv -T -- "$STAGING" "$FINAL"
STAGING=
sync -d "$OUTPUT_PARENT"
printf 'PACKAGE_OK path=%s manifest_sha256=%s target=%s\n' \
  "$FINAL" "$MANIFEST_HASH" "$TARGET_COMMIT"
