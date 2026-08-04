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
  .deploy/hsts_v120_root.sh
  .deploy/hsts_v120_operator.sh
  .deploy/edge_v120_root.sh
  .deploy/edge_v120_operator.sh
  .deploy/it-spareparts.cron
)
readonly -a PACKAGE_NAMES=(
  v120_state.sh
  sync-v120-root-state.sh
  rollback-v120.sh
  install-v120-control.sh
  hsts-v120-root.sh
  hsts-v120-operator.sh
  edge-v120-root.sh
  edge-v120-operator.sh
  it-spareparts.cron
)
readonly -a MANIFEST_KEYS=(
  V120_STATE_SHA256
  ROOT_SYNC_SHA256
  ROLLBACK_SHA256
  INSTALLER_SHA256
  HSTS_ROOT_SHA256
  HSTS_OPERATOR_SHA256
  EDGE_ROOT_SHA256
  EDGE_OPERATOR_SHA256
  CRON_SHA256
)
readonly -a PROVENANCE_PATHS=(
  backend/requirements.lock
  backend/uv.lock
  frontend/package-lock.json
  backend/dependency-sbom.cdx.json
  frontend/dependency-sbom.cdx.json
)
readonly -a PROVENANCE_KEYS=(
  BACKEND_REQUIREMENTS_SHA256
  BACKEND_UV_LOCK_SHA256
  FRONTEND_PACKAGE_LOCK_SHA256
  BACKEND_SBOM_SHA256
  FRONTEND_SBOM_SHA256
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
declare -a PROVENANCE_HASHES=()
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
for index in "${!PROVENANCE_PATHS[@]}"; do
  PROVENANCE_HASHES[index]=$(
    tar -xOf "$STAGING/$SOURCE_TAR_NAME" \
      "${PROVENANCE_PATHS[$index]}" | sha256sum | cut -d' ' -f1
  )
  [[ "${PROVENANCE_HASHES[$index]}" =~ ^[0-9a-f]{64}$ ]] \
    || fatal "invalid supply-chain artifact hash"
done
BACKEND_DOCKERFILE=$(
  tar -xOf "$STAGING/$SOURCE_TAR_NAME" backend/Dockerfile
) || fatal "cannot read backend Dockerfile"
FRONTEND_DOCKERFILE=$(
  tar -xOf "$STAGING/$SOURCE_TAR_NAME" frontend/Dockerfile
) || fatal "cannot read frontend Dockerfile"
mapfile -t BACKEND_FROM_LINES < <(
  grep -E '^FROM ' <<< "$BACKEND_DOCKERFILE"
)
mapfile -t FRONTEND_FROM_LINES < <(
  grep -E '^FROM ' <<< "$FRONTEND_DOCKERFILE"
)
[ "${#BACKEND_FROM_LINES[@]}" -eq 1 ] \
  || fatal "backend Dockerfile must have exactly one base"
[ "${#FRONTEND_FROM_LINES[@]}" -eq 2 ] \
  || fatal "frontend Dockerfile must have exactly two bases"
[[ "${BACKEND_FROM_LINES[0]}" =~ ^FROM\ --platform=linux/amd64\ python:3\.11-slim@sha256:([0-9a-f]{64})$ ]] \
  || fatal "backend base is not an immutable linux/amd64 digest"
BACKEND_BASE_DIGEST=${BASH_REMATCH[1]}
[[ "${FRONTEND_FROM_LINES[0]}" =~ ^FROM\ --platform=linux/amd64\ node:20-alpine@sha256:([0-9a-f]{64})\ AS\ build$ ]] \
  || fatal "frontend build base is not an immutable linux/amd64 digest"
FRONTEND_BUILD_BASE_DIGEST=${BASH_REMATCH[1]}
[[ "${FRONTEND_FROM_LINES[1]}" =~ ^FROM\ --platform=linux/amd64\ nginx:1\.27-alpine@sha256:([0-9a-f]{64})$ ]] \
  || fatal "frontend runtime base is not an immutable linux/amd64 digest"
FRONTEND_RUNTIME_BASE_DIGEST=${BASH_REMATCH[1]}
git fsck --strict --no-reflogs "$TARGET_COMMIT" >/dev/null

{
  printf 'CONTROL_FORMAT=v120-control-3\n'
  printf 'TARGET_COMMIT=%s\n' "$TARGET_COMMIT"
  for index in "${!MANIFEST_KEYS[@]}"; do
    printf '%s=%s\n' "${MANIFEST_KEYS[$index]}" "${HASHES[$index]}"
  done
  printf 'SOURCE_TAR_SHA256=%s\n' "$SOURCE_TAR_HASH"
  for index in "${!PROVENANCE_KEYS[@]}"; do
    printf '%s=%s\n' \
      "${PROVENANCE_KEYS[$index]}" "${PROVENANCE_HASHES[$index]}"
  done
  printf 'BACKEND_BASE_DIGEST=%s\n' "$BACKEND_BASE_DIGEST"
  printf 'FRONTEND_BUILD_BASE_DIGEST=%s\n' \
    "$FRONTEND_BUILD_BASE_DIGEST"
  printf 'FRONTEND_RUNTIME_BASE_DIGEST=%s\n' \
    "$FRONTEND_RUNTIME_BASE_DIGEST"
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
