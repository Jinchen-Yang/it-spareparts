#!/usr/bin/env bash
# Build exact-SHA v1.21 candidate images and an offline image bundle on a trusted host.
set -Eeuo pipefail
umask 077

fatal() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

[ "$#" -eq 3 ] || fatal "usage: v121_beta_build.sh REPO TARGET_SHA OUTPUT_DIR"
REPO=$(realpath -e -- "$1")
TARGET_SHA=$2
OUTPUT_DIR=$(realpath -m -- "$3")
readonly REPO TARGET_SHA OUTPUT_DIR
[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || fatal "target SHA must be full lowercase hex"
[ "$(git -C "$REPO" rev-parse "$TARGET_SHA^{commit}")" = "$TARGET_SHA" ] \
  || fatal "target commit cannot be resolved exactly"
[ "$(git -C "$REPO" rev-parse refs/remotes/origin/main)" = "$TARGET_SHA" ] \
  || fatal "target SHA is not the fetched origin/main head"
[ ! -e "$OUTPUT_DIR" ] && [ ! -L "$OUTPUT_DIR" ] \
  || fatal "output directory already exists"

STAGING=$(mktemp -d)
SOURCE_DIR="$STAGING/source"
SOURCE_TAR="$STAGING/source.tar"
mkdir -m 700 -- "$SOURCE_DIR"
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  rm -rf -- "$STAGING"
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

git -C "$REPO" archive --format=tar --prefix=source/ "$TARGET_SHA" >"$SOURCE_TAR"
[ "$(git get-tar-commit-id <"$SOURCE_TAR")" = "$TARGET_SHA" ] \
  || fatal "source archive is not bound to target SHA"
tar -xf "$SOURCE_TAR" -C "$STAGING"

APP_TAG="it-spareparts-v121/app:$TARGET_SHA"
FRONTEND_TAG="it-spareparts-v121/frontend:$TARGET_SHA"
readonly APP_TAG FRONTEND_TAG
docker build --pull --tag "$APP_TAG" "$SOURCE_DIR/backend"
docker build --pull --tag "$FRONTEND_TAG" "$SOURCE_DIR/frontend"
APP_ID=$(docker image inspect -f '{{.Id}}' "$APP_TAG")
FRONTEND_ID=$(docker image inspect -f '{{.Id}}' "$FRONTEND_TAG")
readonly APP_ID FRONTEND_ID
[[ "$APP_ID" =~ ^sha256:[0-9a-f]{64}$ ]] || fatal "invalid app image ID"
[[ "$FRONTEND_ID" =~ ^sha256:[0-9a-f]{64}$ ]] || fatal "invalid frontend image ID"

mkdir -m 700 -- "$STAGING/output"
mv -- "$SOURCE_TAR" "$STAGING/output/source.tar"
docker save --output "$STAGING/output/images.tar" "$APP_TAG" "$FRONTEND_TAG"
chmod 600 "$STAGING/output/source.tar" "$STAGING/output/images.tar"
SOURCE_SHA=$(sha256sum "$STAGING/output/source.tar" | awk '{print $1}')
IMAGES_SHA=$(sha256sum "$STAGING/output/images.tar" | awk '{print $1}')
python3 - "$STAGING/output/build-evidence.json" "$TARGET_SHA" "$SOURCE_SHA" \
  "$APP_TAG" "$APP_ID" "$FRONTEND_TAG" "$FRONTEND_ID" "$IMAGES_SHA" <<'PY'
import datetime as dt
import json
import os
import sys

path=sys.argv[1]
payload={
  "format":"v121-beta-build-v1",
  "target_sha":sys.argv[2],
  "source_tar_sha256":sys.argv[3],
  "app_tag":sys.argv[4],
  "app_image_id":sys.argv[5],
  "frontend_tag":sys.argv[6],
  "frontend_image_id":sys.argv[7],
  "image_bundle_sha256":sys.argv[8],
  "built_at":dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
}
with open(path,"w",encoding="utf-8") as stream:
    json.dump(payload,stream,sort_keys=True,separators=(",",":"))
    stream.write("\n")
os.chmod(path,0o600)
PY
mv -T -- "$STAGING/output" "$OUTPUT_DIR"
printf 'APP_IMAGE_ID=%s\n' "$APP_ID"
printf 'FRONTEND_IMAGE_ID=%s\n' "$FRONTEND_ID"
printf 'IMAGE_BUNDLE_SHA256=%s\n' "$IMAGES_SHA"
