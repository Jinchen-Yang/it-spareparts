#!/usr/bin/env bash
# 每日数据库备份 + 完整性校验 + 14 天保留（§十）。由 cron 调用，日志见 backup.log。
set -euo pipefail
# cron 的 PATH 极简，显式补全（docker/sudo/find 等找不到是 cron 静默失败的常见根因）
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
cd "$(dirname "$0")"

DEST=/var/backups/spareparts
DATE=$(date +%Y%m%d-%H%M)
DUMP="$DEST/db-$DATE.dump"
log() { echo "[$(date '+%F %T')] $*"; }

mkdir -p "$DEST"
log "backup start -> $DUMP"

# 1) 备份（自定义格式，便于 pg_restore 选择性恢复）
sudo docker compose exec -T db pg_dump -U spareparts -Fc spareparts > "$DUMP"
SIZE=$(stat -c%s "$DUMP")

# 2) 完整性校验：dump 必须能被 pg_restore 读出对象清单，且体积合理，否则判失败并删坏档
OBJ=$(sudo docker compose exec -T db pg_restore --list < "$DUMP" 2>/dev/null | grep -c . || true)
if [ "$SIZE" -lt 10000 ] || [ "${OBJ:-0}" -lt 20 ]; then
  log "ERROR: dump 校验失败 (size=$SIZE bytes, objects=$OBJ) —— 删除坏档"
  rm -f "$DUMP"
  exit 1
fi
sha256sum "$DUMP" > "$DUMP.sha256"
log "verify OK: size=$SIZE bytes, objects=$OBJ, sha256 已写"

# 3) 异地副本（待填云存储凭证后启用其一；不启用则备份仅在本机，灾备不完整）：
#   rclone copy "$DUMP" "$DUMP.sha256" oss:spareparts-backups/   # 需先 rclone config 阿里OSS/腾讯COS
#   rsync -az "$DUMP" "$DUMP.sha256" backup-host:/backups/spareparts/   # 需异地服务器
# OFFSITE_PUSH_PLACEHOLDER

# 4) 保留 14 天
find "$DEST" -name 'db-*.dump' -mtime +14 -delete
find "$DEST" -name 'db-*.dump.sha256' -mtime +14 -delete
log "backup done（保留 14 天，当前 $(ls "$DEST"/db-*.dump 2>/dev/null | wc -l) 个备份）"
