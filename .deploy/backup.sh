#!/usr/bin/env bash
# 每日数据库备份 + 完整性校验 + 14 天保留（§十）。由 cron 调用，日志见 backup.log。
set -euo pipefail
umask 077

# cron 的 PATH 极简，显式补全（docker/sudo/find 等找不到是 cron 静默失败的常见根因）
BASE_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH="$BASE_PATH"

# 仅供无副作用的隔离测试注入临时目录与命令桩；生产 cron 不设置这些变量。
if [ "${BACKUP_TEST_MODE:-0}" = "1" ]; then
  [ "${EUID:-$(id -u)}" -ne 0 ] || {
    echo "BACKUP_TEST_MODE 禁止以 root 运行" >&2
    exit 2
  }
  DEST=${BACKUP_TEST_DEST:?missing test backup destination}
  COMMAND_DIR=${BACKUP_TEST_COMMAND_DIR:?missing test command directory}
  case "$DEST" in
    /*) ;;
    *) echo "BACKUP_TEST_DEST 必须是绝对路径" >&2; exit 2 ;;
  esac
  case "$COMMAND_DIR" in
    /*) export PATH="$COMMAND_DIR:$BASE_PATH" ;;
    *) echo "BACKUP_TEST_COMMAND_DIR 必须是绝对路径" >&2; exit 2 ;;
  esac
else
  DEST=/var/backups/spareparts
fi

cd "$(dirname "$0")"

log() { echo "[$(date '+%F %T')] $*"; }

if [ -L "$DEST" ]; then
  log "ERROR: 备份目录不能是符号链接"
  exit 1
fi
mkdir -p -- "$DEST"
chmod 700 -- "$DEST"

LOCK_FILE="$DEST/.backup.lock"
if [ -L "$LOCK_FILE" ]; then
  log "ERROR: 备份锁文件不能是符号链接"
  exit 1
fi
if ! exec 9>>"$LOCK_FILE"; then
  log "ERROR: 无法打开备份锁文件"
  exit 1
fi
chmod 600 -- "$LOCK_FILE"
if ! flock -n 9; then
  log "ERROR: 上一次备份仍在运行，本轮已拒绝重叠执行"
  exit 75
fi

# 每次运行同时修复旧备份的历史宽权限，避免只保护新文件而遗留已泄露面。
find "$DEST" -maxdepth 1 -type f \
  \( -name 'db-*.dump' -o -name 'db-*.dump.sha256' \) \
  -exec chmod 600 -- {} +

DATE=$(date +%Y%m%d-%H%M)
DUMP="$DEST/db-$DATE.dump"
# 手工同分钟重跑不覆盖既有恢复点；后缀只在同名 dump/校验和已存在时启用。
if [ -e "$DUMP" ] || [ -e "$DUMP.sha256" ]; then
  DUMP="$DEST/db-$DATE-$$.dump"
fi
CHECKSUM="$DUMP.sha256"
TMP_DUMP=
TMP_CHECKSUM=
TMP_TOC=
cleanup() {
  [ -z "${TMP_DUMP:-}" ] || rm -f -- "$TMP_DUMP"
  [ -z "${TMP_CHECKSUM:-}" ] || rm -f -- "$TMP_CHECKSUM"
  [ -z "${TMP_TOC:-}" ] || rm -f -- "$TMP_TOC"
}
trap cleanup EXIT
TMP_DUMP=$(mktemp -- "$DEST/.$(basename "$DUMP").tmp.XXXXXX")
TMP_CHECKSUM=$(mktemp -- "$DEST/.$(basename "$CHECKSUM").tmp.XXXXXX")
TMP_TOC=$(mktemp -- "$DEST/.$(basename "$DUMP").toc.tmp.XXXXXX")
log "backup start -> $DUMP"

# 1) 备份（自定义格式，便于 pg_restore 选择性恢复）
# 重定向必须由非特权备份用户执行，不能由 root 创建备份文件。
# shellcheck disable=SC2024
sudo docker compose exec -T db pg_dump -U spareparts -Fc spareparts > "$TMP_DUMP"
chmod 600 -- "$TMP_DUMP"
SIZE=$(stat -c%s "$TMP_DUMP")

# 2) 完整性校验：dump 必须能被 pg_restore 读出对象清单，且体积合理，否则丢弃临时文件
# shellcheck disable=SC2024
if sudo docker compose exec -T db pg_restore --list \
    < "$TMP_DUMP" > "$TMP_TOC" 2>/dev/null; then
  :
else
  TOC_STATUS=$?
  log "ERROR: pg_restore TOC 校验命令失败 (status=$TOC_STATUS) —— 丢弃本次临时文件"
  exit "$TOC_STATUS"
fi
OBJ=$(grep -c . "$TMP_TOC" || true)
if [ "$SIZE" -lt 10000 ] || [ "${OBJ:-0}" -lt 20 ]; then
  log "ERROR: dump 校验失败 (size=$SIZE bytes, objects=$OBJ) —— 丢弃本次临时文件"
  exit 1
fi
HASH_OUTPUT=$(sha256sum -- "$TMP_DUMP")
HASH=${HASH_OUTPUT%% *}
printf '%s  %s\n' "$HASH" "$TMP_DUMP" | sha256sum -c - >/dev/null
printf '%s  %s\n' "$HASH" "$DUMP" > "$TMP_CHECKSUM"
chmod 600 -- "$TMP_CHECKSUM"
# 临时文件与正式文件同目录。先发布 checksum、最后才让 dump 可见：
# 进程在任一 rename 处失败时，监控都不会看到缺少 checksum 的新 dump。
if mv -- "$TMP_CHECKSUM" "$CHECKSUM"; then
  TMP_CHECKSUM=
else
  PUBLISH_STATUS=$?
  log "ERROR: checksum 原子发布失败 (status=$PUBLISH_STATUS)"
  exit "$PUBLISH_STATUS"
fi
if mv -- "$TMP_DUMP" "$DUMP"; then
  TMP_DUMP=
else
  PUBLISH_STATUS=$?
  rm -f -- "$CHECKSUM"
  log "ERROR: dump 原子发布失败 (status=$PUBLISH_STATUS)"
  exit "$PUBLISH_STATUS"
fi
if ! sha256sum -c "$CHECKSUM" >/dev/null 2>&1; then
  rm -f -- "$DUMP" "$CHECKSUM"
  log "ERROR: 正式恢复点发布后 checksum 复核失败"
  exit 1
fi
log "verify OK: size=$SIZE bytes, objects=$OBJ, sha256 已写"

# 3) 异地副本（待填云存储凭证后启用其一；不启用则备份仅在本机，灾备不完整）：
#   rclone copy "$DUMP" "$DUMP.sha256" oss:spareparts-backups/   # 需先 rclone config 阿里OSS/腾讯COS
#   rsync -az "$DUMP" "$DUMP.sha256" backup-host:/backups/spareparts/   # 需异地服务器
# OFFSITE_PUSH_PLACEHOLDER

# 4) 保留 14 天
find "$DEST" -name 'db-*.dump' -mtime +14 -delete
find "$DEST" -name 'db-*.dump.sha256' -mtime +14 -delete
BACKUP_COUNT=$(find "$DEST" -maxdepth 1 -type f -name 'db-*.dump' -printf '.\n' | wc -l)
log "backup done（保留 14 天，当前 $BACKUP_COUNT 个备份）"
