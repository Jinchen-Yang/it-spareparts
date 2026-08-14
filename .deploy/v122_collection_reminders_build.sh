#!/usr/bin/env bash
# Build exact-origin/main v1.22 images and immutable source evidence off-host.
set -Eeuo pipefail
umask 077

readonly FROM_REV=d9f1a3c7e5b2
readonly TO_REV=c8e2a4f6b1d3
readonly DEFAULT_PRODUCTION_DIR=/home/ubuntu/apps/it-spareparts

fatal() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

[ "$#" -eq 3 ] \
  || fatal "usage: v122_collection_reminders_build.sh REPO TARGET_SHA OUTPUT_DIR"
REPO=$(realpath -e -- "$1")
TARGET_SHA=$2
OUTPUT_DIR=$(realpath -m -- "$3")
PRODUCTION_DIR=$(realpath -m -- "${V122_PRODUCTION_APP_DIR:-$DEFAULT_PRODUCTION_DIR}")
readonly REPO TARGET_SHA OUTPUT_DIR PRODUCTION_DIR

[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || fatal "target SHA must be full lowercase hex"
[ -d "$REPO/.git" ] || git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 \
  || fatal "REPO is not a Git worktree"
[ "$REPO" != "$PRODUCTION_DIR" ] || fatal "production checkout must never build release images"
[ "$(git -C "$REPO" rev-parse "$TARGET_SHA^{commit}")" = "$TARGET_SHA" ] \
  || fatal "target commit cannot be resolved exactly"
git -C "$REPO" show-ref --verify --quiet refs/remotes/origin/main \
  || fatal "fetched origin/main reference is missing"
[ "$(git -C "$REPO" rev-parse refs/remotes/origin/main)" = "$TARGET_SHA" ] \
  || fatal "target SHA is not the fetched origin/main head"
[ -z "$(git -C "$REPO" status --porcelain=v1 --untracked-files=all)" ] \
  || fatal "worktree is not completely clean (tracked and untracked files are forbidden)"
[ ! -e "$OUTPUT_DIR" ] && [ ! -L "$OUTPUT_DIR" ] \
  || fatal "output directory already exists"

WORK=$(mktemp -d -t v122-build.XXXXXXXX)
SOURCE_TAR="$WORK/source.tar"
SOURCE_DIR="$WORK/source"
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  rm -rf -- "$WORK"
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

git -C "$REPO" archive --format=tar --prefix=source/ "$TARGET_SHA" >"$SOURCE_TAR"
[ "$(git get-tar-commit-id <"$SOURCE_TAR")" = "$TARGET_SHA" ] \
  || fatal "source archive is not bound to target SHA"
mkdir -m 700 -- "$SOURCE_DIR"
tar -xf "$SOURCE_TAR" -C "$WORK"

# Parse the complete Alembic graph with Python AST.  A filename/grep match is
# not migration evidence: aliases, duplicate revisions and extra heads fail.
python3 - "$SOURCE_DIR/backend/alembic/versions" "$FROM_REV" "$TO_REV" <<'PY'
import ast
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
from_rev, to_rev = sys.argv[2:4]
rows = {}
for path in sorted(root.glob("*.py")):
    if path.name == "__init__.py":
        continue
    tree = ast.parse(path.read_bytes(), filename=str(path))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                try:
                    values[target.id] = ast.literal_eval(value)
                except (TypeError, ValueError) as exc:
                    raise SystemExit(f"non-literal migration metadata in {path}: {exc}")
    revision = values.get("revision")
    down = values.get("down_revision")
    if not isinstance(revision, str) or not revision:
        raise SystemExit(f"missing revision in {path}")
    if revision in rows:
        raise SystemExit(f"duplicate Alembic revision: {revision}")
    if down is None:
        parents = []
    elif isinstance(down, str):
        parents = [down]
    elif isinstance(down, (tuple, list)) and all(isinstance(v, str) for v in down):
        parents = list(down)
    else:
        raise SystemExit(f"invalid down_revision in {path}")
    rows[revision] = parents
for revision, parents in rows.items():
    for parent in parents:
        if parent not in rows:
            raise SystemExit(f"unknown Alembic parent {parent} from {revision}")
heads = sorted(set(rows) - {parent for parents in rows.values() for parent in parents})
if heads != [to_rev]:
    raise SystemExit(f"expected unique Alembic head {to_rev}, got {heads}")
seen = set()
stack = [to_rev]
while stack:
    current = stack.pop()
    if current in seen:
        continue
    seen.add(current)
    stack.extend(rows[current])
if from_rev not in seen:
    raise SystemExit(f"{from_rev} is not an ancestor of {to_rev}")
if rows[to_rev] != [from_rev]:
    raise SystemExit(f"release migration must be the direct {from_rev}->{to_rev} edge")
PY

grep -Fq 'MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED: ${MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED:-false}' \
  "$SOURCE_DIR/docker-compose.yml" || fatal "candidate compose does not default apply false"
grep -Fq 'MAINTENANCE_COLLECTION_CANARY_PROJECT_ID: ${MAINTENANCE_COLLECTION_CANARY_PROJECT_ID:-}' \
  "$SOURCE_DIR/docker-compose.yml" || fatal "candidate compose does not wire canary project id"
python3 "$SOURCE_DIR/.deploy/generate_dependency_sbom.py" --check "$SOURCE_DIR" \
  || fatal "committed CycloneDX SBOMs are stale"

APP_TAG="it-spareparts-v122-collection/app:$TARGET_SHA"
FRONTEND_TAG="it-spareparts-v122-collection/frontend:$TARGET_SHA"
readonly APP_TAG FRONTEND_TAG
docker build --pull --tag "$APP_TAG" "$SOURCE_DIR/backend"
docker build --pull --tag "$FRONTEND_TAG" "$SOURCE_DIR/frontend"
APP_ID=$(docker image inspect --format '{{.Id}}' "$APP_TAG")
FRONTEND_ID=$(docker image inspect --format '{{.Id}}' "$FRONTEND_TAG")
[[ "$APP_ID" =~ ^sha256:[0-9a-f]{64}$ ]] || fatal "invalid app image ID"
[[ "$FRONTEND_ID" =~ ^sha256:[0-9a-f]{64}$ ]] || fatal "invalid frontend image ID"

mkdir -m 700 -- "$WORK/output"
mv -- "$SOURCE_TAR" "$WORK/output/source.tar"
cp -- "$SOURCE_DIR/docker-compose.yml" "$WORK/output/candidate-compose.yml"
docker save --output "$WORK/output/images.tar" "$APP_TAG" "$FRONTEND_TAG"
python3 - "$SOURCE_DIR" "$WORK/output/dependency-sbom.cdx.json" <<'PY'
import json
import pathlib
import sys

root, output = map(pathlib.Path, sys.argv[1:3])
sources = [
    json.loads((root / "backend/dependency-sbom.cdx.json").read_text()),
    json.loads((root / "frontend/dependency-sbom.cdx.json").read_text()),
]
components = []
seen = set()
for source in sources:
    if source.get("bomFormat") != "CycloneDX" or source.get("specVersion") != "1.5":
        raise SystemExit("invalid committed CycloneDX input")
    for component in source.get("components", []):
        reference = component.get("bom-ref")
        if not reference or reference in seen:
            raise SystemExit("duplicate/missing CycloneDX bom-ref")
        seen.add(reference)
        components.append(component)
payload = {
    "bomFormat": "CycloneDX",
    "specVersion": "1.5",
    "version": 1,
    "metadata": {"component": {"type": "application", "name": "it-spareparts-v1.22"}},
    "components": sorted(components, key=lambda row: row["bom-ref"]),
}
output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
PY
chmod 600 "$WORK/output"/*
SOURCE_SHA=$(sha256sum "$WORK/output/source.tar" | awk '{print $1}')
IMAGES_SHA=$(sha256sum "$WORK/output/images.tar" | awk '{print $1}')
COMPOSE_SHA=$(sha256sum "$WORK/output/candidate-compose.yml" | awk '{print $1}')
SBOM_SHA=$(sha256sum "$WORK/output/dependency-sbom.cdx.json" | awk '{print $1}')
python3 - "$WORK/output/build-evidence.json" "$TARGET_SHA" "$SOURCE_SHA" \
  "$APP_TAG" "$APP_ID" "$FRONTEND_TAG" "$FRONTEND_ID" "$IMAGES_SHA" \
  "$COMPOSE_SHA" "$SBOM_SHA" <<'PY'
import datetime as dt
import json
import os
import sys

payload = {
    "format": "v122-collection-reminders-build-v2",
    "target_sha": sys.argv[2],
    "source_archive_commit": sys.argv[2],
    "source_tar_sha256": sys.argv[3],
    "app_tag": sys.argv[4],
    "app_image_id": sys.argv[5],
    "frontend_tag": sys.argv[6],
    "frontend_image_id": sys.argv[7],
    "image_bundle_sha256": sys.argv[8],
    "candidate_compose_sha256": sys.argv[9],
    "sbom_sha256": sys.argv[10],
    "alembic_from": "d9f1a3c7e5b2",
    "alembic_to": "c8e2a4f6b1d3",
    "alembic_unique_head": True,
    "built_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
}
with open(sys.argv[1], "x", encoding="utf-8") as stream:
    json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.chmod(sys.argv[1], 0o600)
PY
(cd "$WORK/output" && sha256sum build-evidence.json candidate-compose.yml dependency-sbom.cdx.json images.tar source.tar >sha256sums)
[ -z "$(git -C "$REPO" status --porcelain=v1 --untracked-files=all)" ] \
  || fatal "worktree changed during build"
mv -T -- "$WORK/output" "$OUTPUT_DIR"
printf 'APP_IMAGE_ID=%s\n' "$APP_ID"
printf 'FRONTEND_IMAGE_ID=%s\n' "$FRONTEND_ID"
printf 'IMAGE_BUNDLE_SHA256=%s\n' "$IMAGES_SHA"
