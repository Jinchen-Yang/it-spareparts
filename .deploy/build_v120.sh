#!/usr/bin/env bash
# v1.20 exact-SHA candidate build.  It never changes the online services.
set -Eeuo pipefail
umask 077
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

readonly EXPECTED_RUNNING_SOURCE_COMMIT=a1cf00910f08da7f27a9e6e0faaacc3a3cce9bab
readonly EXPECTED_CHECKOUT_COMMIT=ab42005b5b94bf98b3db0e4bff87e5df9da2f7ca
readonly EXPECTED_DB_HEAD=f1c8e4a7b2d9
readonly EXPECTED_APP_COMPOSE_HASH=d9846dee51822e6385055faa86f3256452a3385435e96baf624c169d51b313af
readonly EXPECTED_MIGRATION_FILE_COUNT=32
readonly EXPECTED_MIGRATION_INVENTORY_SHA256=\
6a338c1efb99d41c72ce1e097f2cb1bbf64e79ffe968e8763e8fdee4c798d326
readonly APP_DIR=/home/ubuntu/apps/it-spareparts
readonly BUILD_ROOT=/var/lib/it-spareparts-v120-build
readonly LOCK_PATH=/run/lock/it-spareparts-v120
readonly CONTROL_DIR=/var/lib/it-spareparts-release-control
readonly CONTROL_CURRENT="$CONTROL_DIR/current"
readonly ROOT_STATE="$CONTROL_DIR/v120-state.state"
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

RELEASE_SRC=
STATE_TEMP=
ROOT_SNAPSHOT_TEMP=
SOURCE_TEMP=
SOURCE_SUM_TEMP=
cleanup_build() {
  local status=$?
  trap - EXIT INT TERM
  if [ -n "$STATE_TEMP" ]; then
    rm -f -- "$STATE_TEMP" || status=97
  fi
  if [ -n "$ROOT_SNAPSHOT_TEMP" ]; then
    rm -f -- "$ROOT_SNAPSHOT_TEMP" || status=97
  fi
  if [ -n "$SOURCE_TEMP" ]; then
    sudo unlink -- "$SOURCE_TEMP" || status=97
  fi
  if [ -n "$SOURCE_SUM_TEMP" ]; then
    sudo unlink -- "$SOURCE_SUM_TEMP" || status=97
  fi
  if [ -n "$RELEASE_SRC" ] && sudo test -d "$RELEASE_SRC"; then
    case "$RELEASE_SRC" in
      "$BUILD_ROOT"/v120-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9])
        if ! sudo find "$RELEASE_SRC" -xdev -depth -mindepth 1 -delete \
            || ! sudo rmdir "$RELEASE_SRC"; then
          printf 'FATAL: failed to clean build directory %s\n' \
            "$RELEASE_SRC" >&2
          status=97
        fi
        ;;
      *)
        printf 'FATAL: refusing unexpected build cleanup path %s\n' \
          "$RELEASE_SRC" >&2
        status=97
        ;;
    esac
  fi
  v120_release_lock || status=97
  exit "$status"
}
trap cleanup_build EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

[ "$EUID" -ne 0 ] || fatal "build_v120.sh must run as the app account"
case "$#" in
  1)
    TARGET_COMMIT=$1
    SUPERSEDES_REQUEST=
    ;;
  3)
    TARGET_COMMIT=$1
    [ "$2" = --supersedes ] \
      || fatal "usage: build_v120.sh <target SHA> [--supersedes <release ID>]"
    SUPERSEDES_REQUEST=$3
    ;;
  *)
    fatal "usage: build_v120.sh <target SHA> [--supersedes <release ID>]"
    ;;
esac
readonly TARGET_COMMIT
[[ "$TARGET_COMMIT" =~ ^[0-9a-f]{40}$ ]] || fatal "invalid target commit"
if [ -n "$SUPERSEDES_REQUEST" ]; then
  [[ "$SUPERSEDES_REQUEST" =~ ^v120-[0-9a-f]{12}-[0-9]{14}$ ]] \
    || fatal "invalid superseded release ID"
fi

v120_acquire_lock "$LOCK_PATH" "750 root:$(id -gn)"
[ -d "$APP_DIR/backups" ] && [ ! -L "$APP_DIR/backups" ] \
  || fatal "release artifact directory is unsafe"
[ "$(stat -c '%U' "$APP_DIR/backups")" = "$(id -un)" ] \
  || fatal "release artifact directory owner mismatch"
chmod 700 "$APP_DIR/backups"
[ "$(stat -c '%a %U' "$APP_DIR/backups")" = "700 $(id -un)" ] \
  || fatal "release artifact directory owner/mode mismatch"
[ "$(sudo stat -c '%F %a %U:%G %h' "$CONTROL_CURRENT/manifest.txt")" \
  = "regular file 600 root:root 1" ] \
  || fatal "root control manifest is missing or unsafe"
CONTROL_MANIFEST_HASH=$(
  sudo sha256sum "$CONTROL_CURRENT/manifest.txt" | cut -d' ' -f1
)
[[ "$CONTROL_MANIFEST_HASH" =~ ^[0-9a-f]{64}$ ]] \
  || fatal "invalid root control manifest hash"
sudo "$CONTROL_CURRENT/install-v120-control.sh" \
  verify "$CONTROL_MANIFEST_HASH"
CONTROL_TARGET=$(
  sudo sed -n 's/^TARGET_COMMIT=//p' "$CONTROL_CURRENT/manifest.txt"
)
[ "$CONTROL_TARGET" = "$TARGET_COMMIT" ] \
  || fatal "root control package targets a different commit"
TRUSTED_SOURCE_HASH=$(
  sudo sed -n 's/^SOURCE_TAR_SHA256=//p' "$CONTROL_CURRENT/manifest.txt"
)
[[ "$TRUSTED_SOURCE_HASH" =~ ^[0-9a-f]{64}$ ]] \
  || fatal "root control manifest lacks a trusted source hash"
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

ATTEMPT_NO=1
PARENT_RELEASE_ID=none
PARENT_STATE_HASH=\
0000000000000000000000000000000000000000000000000000000000000000
ROLLBACK_POLICY=old_allowed
FORWARD_REPAIR=0
OLD_RUNNING_SOURCE_COMMIT=$EXPECTED_RUNNING_SOURCE_COMMIT
declare -A PREVIOUS_STATE=()
declare -A SUPERSESSION_BASE=()
if [ -n "$SUPERSEDES_REQUEST" ]; then
  [ "$(sudo stat -c '%F %a %U:%G %h' "$ROOT_STATE")" \
    = "regular file 600 root:root 1" ] \
    || fatal "root authority is missing or unsafe"
  ROOT_SNAPSHOT_TEMP=$(
    mktemp -- "$APP_DIR/backups/.v120-parent-state.XXXXXX"
  )
  # shellcheck disable=SC2024
  sudo cat "$ROOT_STATE" > "$ROOT_SNAPSHOT_TEMP"
  chmod 600 "$ROOT_SNAPSHOT_TEMP"
  v120_state_parse_to_array "$ROOT_SNAPSHOT_TEMP" PREVIOUS_STATE \
    || fatal "cannot validate parent root authority"
  [ "${PREVIOUS_STATE[RELEASE_ID]}" = "$SUPERSEDES_REQUEST" ] \
    || fatal "superseded release does not match root authority"
  v120_state_select_supersession_base PREVIOUS_STATE SUPERSESSION_BASE \
    || fatal "only observed, rolled-back or failed-closed releases may be superseded"
  ATTEMPT_NO=$((10#${PREVIOUS_STATE[ATTEMPT_NO]} + 1))
  [ "$ATTEMPT_NO" -le 999 ] || fatal "release attempt limit exceeded"
  PARENT_RELEASE_ID=${PREVIOUS_STATE[RELEASE_ID]}
  PARENT_STATE_HASH=$(sha256sum "$ROOT_SNAPSHOT_TEMP" | cut -d' ' -f1)
  OLD_RUNNING_SOURCE_COMMIT=${SUPERSESSION_BASE[RUNNING_SOURCE_COMMIT]}
  ROLLBACK_POLICY=${SUPERSESSION_BASE[ROLLBACK_POLICY]}
  if [ "${SUPERSESSION_BASE[REQUIRE_RUNNING]}" = 0 ]; then
    FORWARD_REPAIR=1
  fi
fi

cd "$APP_DIR"
[ -f .env ] && [ ! -L .env ] || fatal "production .env is missing or unsafe"
[ "$(stat -c '%U' .env)" = "$(id -un)" ] || fatal ".env owner mismatch"
chmod 600 .env
[ -f "$APP_DIR/docker-compose.yml" ] \
  && [ ! -L "$APP_DIR/docker-compose.yml" ] \
  || fatal "active compose file is unsafe"
[ "$(stat -c '%a %U:%G %h' "$APP_DIR/docker-compose.yml")" \
  = "644 root:root 1" ] || fatal "active compose owner/mode mismatch"
APP_COMPOSE_HASH=$(
  sha256sum "$APP_DIR/docker-compose.yml" | cut -d' ' -f1
)
[ "$APP_COMPOSE_HASH" = "$EXPECTED_APP_COMPOSE_HASH" ] \
  || fatal "active compose differs from the fixed HTTPS baseline"

CURRENT_DB_HEAD=$(
  compose exec -T db \
    psql -U spareparts -d spareparts -At \
      -c 'SELECT version_num FROM alembic_version;'
)
[ "$CURRENT_DB_HEAD" = "$EXPECTED_DB_HEAD" ] \
  || fatal "production database head mismatch"

if [ "$FORWARD_REPAIR" = 1 ]; then
  running_services=$(compose ps --status running --services) \
    || fatal "cannot inspect fail-closed services"
  if grep -Eq '^(app|frontend)$' <<< "$running_services"; then
    fatal "forward repair requires app/frontend to remain stopped"
  fi
  OLD_APP_IMAGE_ID=${PREVIOUS_STATE[OLD_APP_IMAGE_ID]}
  OLD_FRONTEND_IMAGE_ID=${PREVIOUS_STATE[OLD_FRONTEND_IMAGE_ID]}
  APP_IMAGE_REF=${PREVIOUS_STATE[APP_IMAGE_REF]}
  FRONTEND_IMAGE_REF=${PREVIOUS_STATE[FRONTEND_IMAGE_REF]}
else
  OLD_APP_CID=$(compose ps -q app)
  OLD_FRONTEND_CID=$(compose ps -q frontend)
  [[ "$OLD_APP_CID" =~ ^[0-9a-f]{64}$ ]]
  [[ "$OLD_FRONTEND_CID" =~ ^[0-9a-f]{64}$ ]]
  [ "$(sudo docker inspect -f '{{.State.Running}}' "$OLD_APP_CID")" = true ]
  [ "$(sudo docker inspect -f '{{.State.Running}}' "$OLD_FRONTEND_CID")" = true ]
  OLD_APP_IMAGE_ID=$(sudo docker inspect -f '{{.Image}}' "$OLD_APP_CID")
  OLD_FRONTEND_IMAGE_ID=$(sudo docker inspect -f '{{.Image}}' "$OLD_FRONTEND_CID")
  APP_IMAGE_REF=$(sudo docker inspect -f '{{.Config.Image}}' "$OLD_APP_CID")
  FRONTEND_IMAGE_REF=$(sudo docker inspect -f '{{.Config.Image}}' "$OLD_FRONTEND_CID")
  if [ -n "$SUPERSEDES_REQUEST" ]; then
    [ "$OLD_APP_IMAGE_ID" = "${SUPERSESSION_BASE[APP_IMAGE_ID]}" ] \
      || fatal "running app image differs from superseded root authority"
    [ "$OLD_FRONTEND_IMAGE_ID" \
      = "${SUPERSESSION_BASE[FRONTEND_IMAGE_ID]}" ] \
      || fatal "running frontend image differs from superseded root authority"
  fi
fi
[[ "$OLD_APP_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$OLD_FRONTEND_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
[ "$APP_IMAGE_REF" = it-spareparts-app ] || fatal "unexpected app image ref"
[ "$FRONTEND_IMAGE_REF" = it-spareparts-frontend ] \
  || fatal "unexpected frontend image ref"

RELEASE_ID="v120-${TARGET_COMMIT:0:12}-$(date +%Y%m%d%H%M%S)"
STATE="$APP_DIR/backups/$RELEASE_ID.state"
[ ! -e "$STATE" ] && [ ! -L "$STATE" ] || fatal "release state already exists"

OLD_APP_ROLLBACK_TAG="it-spareparts-release/app:rollback-$RELEASE_ID"
OLD_FRONTEND_ROLLBACK_TAG="it-spareparts-release/frontend:rollback-$RELEASE_ID"
NEW_APP_CANDIDATE_TAG="it-spareparts-release/app:candidate-$RELEASE_ID"
NEW_FRONTEND_CANDIDATE_TAG="it-spareparts-release/frontend:candidate-$RELEASE_ID"
sudo docker tag "$OLD_APP_IMAGE_ID" "$OLD_APP_ROLLBACK_TAG"
sudo docker tag "$OLD_FRONTEND_IMAGE_ID" "$OLD_FRONTEND_ROLLBACK_TAG"
[ "$(sudo docker image inspect -f '{{.Id}}' "$OLD_APP_ROLLBACK_TAG")" \
    = "$OLD_APP_IMAGE_ID" ]
[ "$(sudo docker image inspect -f '{{.Id}}' "$OLD_FRONTEND_ROLLBACK_TAG")" \
    = "$OLD_FRONTEND_IMAGE_ID" ]

if sudo test -e "$BUILD_ROOT" || sudo test -L "$BUILD_ROOT"; then
  [ "$(sudo stat -c '%F %a %U:%G' "$BUILD_ROOT")" \
    = "directory 700 root:root" ] \
    || fatal "trusted build root is unsafe"
else
  sudo mkdir -- "$BUILD_ROOT"
  sudo chown root:root "$BUILD_ROOT"
  sudo chmod 700 "$BUILD_ROOT"
fi

SOURCE_TAR="$APP_DIR/backups/$RELEASE_ID-source.tar"
SOURCE_SUM="$SOURCE_TAR.sha256"
[ ! -e "$SOURCE_TAR" ] && [ ! -L "$SOURCE_TAR" ] \
  || fatal "trusted source destination already exists"
[ ! -e "$SOURCE_SUM" ] && [ ! -L "$SOURCE_SUM" ] \
  || fatal "trusted source checksum already exists"
SOURCE_TEMP=$(sudo mktemp "$BUILD_ROOT/.v120-source.root.XXXXXX")
sudo install -m 600 -o root -g root \
  "$CONTROL_CURRENT/source.tar" "$SOURCE_TEMP"
[ "$(sudo sha256sum "$SOURCE_TEMP" | cut -d' ' -f1)" \
  = "$TRUSTED_SOURCE_HASH" ] || fatal "trusted source archive hash mismatch"
[ "$(sudo cat "$SOURCE_TEMP" | git get-tar-commit-id)" = "$TARGET_COMMIT" ] \
  || fatal "trusted source archive commit mismatch"
sudo sync -f "$SOURCE_TEMP"
sudo mv -T -- "$SOURCE_TEMP" "$SOURCE_TAR"
SOURCE_TEMP=
SOURCE_SUM_TEMP=$(
  sudo mktemp "$BUILD_ROOT/.v120-source-sum.root.XXXXXX"
)
printf '%s  %s\n' "$TRUSTED_SOURCE_HASH" "$SOURCE_TAR" |
  sudo tee "$SOURCE_SUM_TEMP" >/dev/null
sudo chown root:root "$SOURCE_SUM_TEMP"
sudo chmod 600 "$SOURCE_SUM_TEMP"
sudo sync -f "$SOURCE_SUM_TEMP"
sudo mv -T -- "$SOURCE_SUM_TEMP" "$SOURCE_SUM"
SOURCE_SUM_TEMP=
sudo sync -f "$SOURCE_TAR"
sudo sync -f "$SOURCE_SUM"
sudo sync -d "$APP_DIR/backups"
printf '%s  %s\n' "$TRUSTED_SOURCE_HASH" "$SOURCE_TAR" |
  sudo sha256sum -c -
# Read indirectly by v120_state_write_file.
# shellcheck disable=SC2034
SOURCE_HASH=$TRUSTED_SOURCE_HASH

for protected_artifact in \
  .deploy/Caddyfile.it-data.example \
  .deploy/docker-compose.https.yml
do
  sudo tar -xOf "$CONTROL_CURRENT/source.tar" "$protected_artifact" |
    sudo cmp - "$protected_artifact" \
    || fatal "$protected_artifact differs from trusted release source"
done

RELEASE_SRC_CANDIDATE="$BUILD_ROOT/$RELEASE_ID"
sudo mkdir -- "$RELEASE_SRC_CANDIDATE"
RELEASE_SRC=$RELEASE_SRC_CANDIDATE
sudo chown root:root "$RELEASE_SRC"
sudo chmod 700 "$RELEASE_SRC"
BUILD_PROJECT="v120build${TARGET_COMMIT:0:12}"
BUILD_OVERRIDE="$RELEASE_SRC/docker-compose.build-override.yml"

sudo tar -xOf "$CONTROL_CURRENT/source.tar" .deploy/v120_state.sh |
  cmp - "$SCRIPT_DIR/v120_state.sh" \
  || fatal "bootstrap state library differs from target"
sudo tar --no-same-owner --no-same-permissions \
  -xf "$CONTROL_CURRENT/source.tar" -C "$RELEASE_SRC"
sudo env \
  BUILD_OVERRIDE="$BUILD_OVERRIDE" \
  NEW_APP_CANDIDATE_TAG="$NEW_APP_CANDIDATE_TAG" \
  NEW_FRONTEND_CANDIDATE_TAG="$NEW_FRONTEND_CANDIDATE_TAG" \
  sh -c '
    set -eu
    umask 077
    {
      printf "services:\n"
      printf "  app:\n"
      printf "    image: %s\n" "$NEW_APP_CANDIDATE_TAG"
      printf "  frontend:\n"
      printf "    image: %s\n" "$NEW_FRONTEND_CANDIDATE_TAG"
    } > "$BUILD_OVERRIDE"
    chown root:root "$BUILD_OVERRIDE"
    chmod 400 "$BUILD_OVERRIDE"
  '
sudo find "$RELEASE_SRC" -xdev -type d -exec chmod 555 {} +
sudo find "$RELEASE_SRC" -xdev -type f \
  ! -path "$BUILD_OVERRIDE" -exec chmod 444 {} +
sudo grep -q 'APP_VERSION = "1.20.0"' \
  "$RELEASE_SRC/frontend/src/version.ts"

verify_release_source_hash() {
  local relative=$1
  local expected=$2
  [ "$(sudo sha256sum "$RELEASE_SRC/$relative" | cut -d' ' -f1)" \
    = "$expected" ] || fatal "$relative differs from the control manifest"
}

verify_release_source_hash \
  backend/requirements.lock "$BACKEND_REQUIREMENTS_SHA256"
verify_release_source_hash backend/uv.lock "$BACKEND_UV_LOCK_SHA256"
verify_release_source_hash \
  frontend/package-lock.json "$FRONTEND_PACKAGE_LOCK_SHA256"
verify_release_source_hash \
  backend/dependency-sbom.cdx.json "$BACKEND_SBOM_SHA256"
verify_release_source_hash \
  frontend/dependency-sbom.cdx.json "$FRONTEND_SBOM_SHA256"
grep -Fx \
  "FROM --platform=linux/amd64 python:3.11-slim@sha256:$BACKEND_BASE_DIGEST" \
  "$RELEASE_SRC/backend/Dockerfile" >/dev/null \
  || fatal "backend base digest differs from the control manifest"
grep -Fx \
  "FROM --platform=linux/amd64 node:20-alpine@sha256:$FRONTEND_BUILD_BASE_DIGEST AS build" \
  "$RELEASE_SRC/frontend/Dockerfile" >/dev/null \
  || fatal "frontend build base digest differs from the control manifest"
grep -Fx \
  "FROM --platform=linux/amd64 nginx:1.27-alpine@sha256:$FRONTEND_RUNTIME_BASE_DIGEST" \
  "$RELEASE_SRC/frontend/Dockerfile" >/dev/null \
  || fatal "frontend runtime base digest differs from the control manifest"
python3 "$RELEASE_SRC/.deploy/generate_dependency_sbom.py" \
  --check "$RELEASE_SRC" \
  || fatal "dependency SBOM does not match the committed locks"

MIGRATION_DIR="$RELEASE_SRC/backend/alembic/versions"
[ "$(sudo find "$MIGRATION_DIR" -mindepth 1 -maxdepth 1 \
  -type f -printf x | wc -c)" = "$EXPECTED_MIGRATION_FILE_COUNT" ] \
  || fatal "v1.20 contains an unexpected DB migration count"
UNEXPECTED_MIGRATION_ENTRY=$(
  sudo find "$MIGRATION_DIR" -mindepth 1 -maxdepth 1 \
    ! -type f -print -quit
)
[ -z "$UNEXPECTED_MIGRATION_ENTRY" ] \
  || fatal "v1.20 migration directory contains a non-file entry"
MIGRATION_INVENTORY_SHA256=$(
  sudo find "$MIGRATION_DIR" -mindepth 1 -maxdepth 1 \
      -type f -printf '%f\n' |
    LC_ALL=C sort |
    while IFS= read -r migration_file; do
      migration_hash=$(
        sudo sha256sum "$MIGRATION_DIR/$migration_file" | cut -d' ' -f1
      )
      printf '%s  backend/alembic/versions/%s\n' \
        "$migration_hash" "$migration_file"
    done |
    sha256sum | cut -d' ' -f1
)
[ "$MIGRATION_INVENTORY_SHA256" \
  = "$EXPECTED_MIGRATION_INVENTORY_SHA256" ] \
  || fatal "v1.20 contains an unexpected DB migration"

sudo env \
  -u COMPOSE_FILE \
  -u COMPOSE_PROJECT_NAME \
  -u COMPOSE_PROFILES \
  docker compose \
    --project-name "$BUILD_PROJECT" \
    --env-file "$APP_DIR/.env" \
    --project-directory "$RELEASE_SRC" \
    -f "$RELEASE_SRC/docker-compose.yml" \
    -f "$BUILD_OVERRIDE" \
    build --pull app frontend

NEW_APP_IMAGE_ID=$(
  sudo docker image inspect -f '{{.Id}}' "$NEW_APP_CANDIDATE_TAG"
)
NEW_FRONTEND_IMAGE_ID=$(
  sudo docker image inspect -f '{{.Id}}' "$NEW_FRONTEND_CANDIDATE_TAG"
)
[[ "$NEW_APP_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$NEW_FRONTEND_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]

STATE_FORMAT=$V120_STATE_FORMAT
STATE_GENERATION=0
OLD_COMMIT=$EXPECTED_CHECKOUT_COMMIT
# Read indirectly by v120_state_write_file.
# shellcheck disable=SC2034
DB_HEAD=$EXPECTED_DB_HEAD
RELEASE_PHASE=built
STATE_TEMP=$(mktemp "$APP_DIR/backups/.v120-state.new.XXXXXX")
v120_state_write_file "$STATE_TEMP"
chmod 600 "$STATE_TEMP"
sync -f "$STATE_TEMP"
v120_state_publish_new "$STATE_TEMP" "$STATE" \
  || fatal "release state name was concurrently created or could not publish"
STATE_TEMP=

sudo find "$RELEASE_SRC" -xdev -depth -mindepth 1 -delete
sudo rmdir "$RELEASE_SRC"
RELEASE_SRC=
rm -f -- "$ROOT_SNAPSHOT_TEMP"
ROOT_SNAPSHOT_TEMP=
v120_release_lock
trap - EXIT HUP INT TERM
printf 'BUILD_OK state=%s target=%s app=%s frontend=%s\n' \
  "$STATE" "$TARGET_COMMIT" "$NEW_APP_IMAGE_ID" "$NEW_FRONTEND_IMAGE_ID"
