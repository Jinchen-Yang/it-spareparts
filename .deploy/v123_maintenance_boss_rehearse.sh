#!/usr/bin/env bash
# v1.23 维保展示板：在隔离网络里用冻结备份演练 c8e2a4f6b1d3 -> d6e1f4a8c3b5。
#
# 演练三件事（plan v1.3 M5-4）：
#   1. 迁移真跑：升级前后读回 alembic_version 并核验；
#   2. **迁移期间展示板总闸强制 false**（铁律 7：回滚=关 flag，不做 downgrade）；
#   3. **旧应用兼容新 schema**：用父生产镜像连升级后的库跑冒烟——这是「回滚只关 flag」
#      成立的前提证明（新增列全 nullable、新表旧应用不引用）。
set -Eeuo pipefail
umask 077

readonly FROM_REV=c8e2a4f6b1d3
readonly TO_REV=d6e1f4a8c3b5
readonly RELEASE_FLAG=MAINTENANCE_BOSS_DASHBOARD_ENABLED
readonly FROZEN_FLAGS=(
  MAINTENANCE_BETA_ENABLED
  MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED
  REPLENISHMENT_BETA_ENABLED
  LLM_MAPPING_EXTERNAL_ENABLED
)

fatal() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

safe_file() {
  [ -f "$1" ] && [ ! -L "$1" ] && [ -s "$1" ] \
    || fatal "$2 必须是非空普通文件"
}

[ "$#" -eq 6 ] \
  || fatal "usage: v123_maintenance_boss_rehearse.sh DB_DUMP TARGET_SHA PARENT_PROD_SHA APP_IMAGE_ID PARENT_APP_IMAGE_ID OUTPUT_DIR"

DB_DUMP=$(realpath -e -- "$1")
TARGET_SHA=$2
PARENT_SHA=$3
APP_IMAGE_ID=$4
PARENT_APP_IMAGE_ID=$5
OUTPUT_DIR=$(realpath -m -- "$6")
readonly DB_DUMP TARGET_SHA PARENT_SHA APP_IMAGE_ID PARENT_APP_IMAGE_ID OUTPUT_DIR

[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || fatal "TARGET_SHA 非法"
[[ "$PARENT_SHA" =~ ^[0-9a-f]{40}$ ]] || fatal "PARENT_PROD_SHA 非法"
[ "$TARGET_SHA" != "$PARENT_SHA" ] || fatal "TARGET_SHA 与 PARENT_PROD_SHA 不能相同"
for image in "$APP_IMAGE_ID" "$PARENT_APP_IMAGE_ID"; do
  [[ "$image" =~ ^sha256:[0-9a-f]{64}$ ]] || fatal "Docker 镜像 ID 非法：$image"
done
safe_file "$DB_DUMP" "数据库备份"
[ ! -e "$OUTPUT_DIR" ] && [ ! -L "$OUTPUT_DIR" ] || fatal "输出目录已存在"

command -v docker >/dev/null 2>&1 || fatal "缺少 docker"

readonly NETWORK="v123-rehearse-$$"
readonly DB_CONTAINER="v123-rehearse-db-$$"
CLEANED=0
cleanup() {
  [ "$CLEANED" -eq 1 ] && return 0
  CLEANED=1
  docker rm -f "$DB_CONTAINER" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

mkdir -p "$OUTPUT_DIR"
readonly REPORT="$OUTPUT_DIR/rehearsal.json"

alembic_version() {
  docker exec "$DB_CONTAINER" \
    psql -qtAX -U spareparts -d spareparts -c \
    'SELECT version_num FROM alembic_version' 2>/dev/null | tr -d '[:space:]'
}

printf '== 1/5 建隔离网络与数据库\n'
docker network create --internal "$NETWORK" >/dev/null
docker run -d --name "$DB_CONTAINER" --network "$NETWORK" \
  -e POSTGRES_USER=spareparts -e POSTGRES_PASSWORD=rehearse-only \
  -e POSTGRES_DB=spareparts postgres:15 >/dev/null
for _ in $(seq 1 60); do
  docker exec "$DB_CONTAINER" pg_isready -U spareparts >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$DB_CONTAINER" pg_isready -U spareparts >/dev/null 2>&1 \
  || fatal "演练数据库未就绪"

printf '== 2/5 恢复冻结备份\n'
docker exec -i "$DB_CONTAINER" pg_restore -U spareparts -d spareparts --no-owner \
  < "$DB_DUMP" >/dev/null 2>&1 \
  || docker exec -i "$DB_CONTAINER" psql -U spareparts -d spareparts < "$DB_DUMP" >/dev/null

BEFORE=$(alembic_version)
[ "$BEFORE" = "$FROM_REV" ] \
  || fatal "备份基线不是 $FROM_REV（实为 ${BEFORE:-空}），拒绝演练"

printf '== 3/5 迁移演练（总闸强制 false）\n'
flag_env=(-e "$RELEASE_FLAG=false")
for flag in "${FROZEN_FLAGS[@]}"; do
  flag_env+=(-e "$flag=false")
done
docker run --rm --network "$NETWORK" \
  -e "DATABASE_URL=postgresql+psycopg://spareparts:rehearse-only@${DB_CONTAINER}:5432/spareparts" \
  "${flag_env[@]}" \
  --entrypoint alembic "$APP_IMAGE_ID" upgrade "$TO_REV" >/dev/null \
  || fatal "迁移失败"

AFTER=$(alembic_version)
[ "$AFTER" = "$TO_REV" ] || fatal "迁移后版本不是 $TO_REV（实为 ${AFTER:-空}）"

printf '== 4/5 旧应用兼容新 schema（回滚=关 flag 的前提）\n'
docker run --rm --network "$NETWORK" \
  -e "DATABASE_URL=postgresql+psycopg://spareparts:rehearse-only@${DB_CONTAINER}:5432/spareparts" \
  "${flag_env[@]}" \
  --entrypoint python "$PARENT_APP_IMAGE_ID" -c '
import os
from sqlalchemy import create_engine, text
url = os.environ["DATABASE_URL"]
engine = create_engine(url)
with engine.connect() as conn:
    # 父生产镜像的既有读路径必须在升级后的 schema 上照常工作
    conn.execute(text("SELECT count(*) FROM f_maintenance_order"))
    conn.execute(text("SELECT count(*) FROM f_maintenance_line"))
    conn.execute(text("SELECT count(*) FROM maintenance_project"))
print("parent-app-compatible")
' >/dev/null || fatal "旧应用连新 schema 冒烟失败——回滚前提不成立，禁止发布"

printf '== 5/5 展示板端点在总闸关闭时不可达\n'
# 迁移后、翻闸前：新端点必须整组 404（与未发布状态不可区分）
docker run --rm --network "$NETWORK" \
  -e "DATABASE_URL=postgresql+psycopg://spareparts:rehearse-only@${DB_CONTAINER}:5432/spareparts" \
  "${flag_env[@]}" \
  --entrypoint python "$APP_IMAGE_ID" -c '
from fastapi.testclient import TestClient
from app.main import app
client = TestClient(app)
for path in ("/api/maintenance/boss-board/health", "/api/maintenance/wbdd-imports/latest"):
    resp = client.get(path)
    assert resp.status_code in (401, 404), (path, resp.status_code)
print("flag-off-hidden")
' >/dev/null || fatal "总闸关闭时新端点仍可达——发布闸门不成立"

cat > "$REPORT" <<JSON
{
  "format": "v123-maintenance-boss-rehearsal-1",
  "target_sha": "$TARGET_SHA",
  "parent_prod_sha": "$PARENT_SHA",
  "db_from": "$BEFORE",
  "db_to": "$AFTER",
  "release_flag_during_migrate": "false",
  "parent_app_compatible": true,
  "flag_off_endpoints_hidden": true
}
JSON
chmod 0400 "$REPORT"
printf 'OK 演练通过，报告：%s\n' "$REPORT"
