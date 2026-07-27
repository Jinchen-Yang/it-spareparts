#!/usr/bin/env bash
# 生产健康巡检（cron 每 5 分钟）：容器 / DB / 入口 / 磁盘 / 备份新鲜度。
# 正常静默(只刷新 monitor.status)，异常追加 monitor.log 并(可选)发钉钉。
# 钉钉告警：把钉钉群机器人 webhook URL 写到 ~/apps/it-spareparts/.alert_webhook 即启用；无则只记日志。
set -uo pipefail   # 不用 -e：要收集所有问题而非首个失败就退出
BASE_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# 仅供隔离测试注入无副作用命令桩；生产 cron 不设置此变量。
if [ -n "${MONITOR_COMMAND_DIR:-}" ]; then
  case "$MONITOR_COMMAND_DIR" in
    /*) export PATH="$MONITOR_COMMAND_DIR:$BASE_PATH" ;;
    *) echo "MONITOR_COMMAND_DIR 必须是绝对路径" >&2; exit 2 ;;
  esac
else
  export PATH="$BASE_PATH"
fi
# 本脚本在 .deploy/ 下，但 docker compose 需在含 docker-compose.yml 的应用根目录跑；
# 故 cd 到脚本上一级（应用根）。monitor.status/monitor.log/.alert_webhook 也落在应用根。
cd "$(dirname "$0")/.." || {
  echo "无法进入应用根目录" >&2
  exit 2
}

BACKUP_DIR=${MONITOR_BACKUP_DIR:-/var/backups/spareparts}
DOCKER_TIMEOUT=${MONITOR_DOCKER_TIMEOUT:-12s}
DOCKER_KILL_AFTER=${MONITOR_DOCKER_KILL_AFTER:-2s}
APP_HEALTH_BASE_URL=http://127.0.0.1:8000
if [ -n "${MONITOR_APP_HEALTH_BASE_URL:-}" ]; then
  if [ -z "${MONITOR_COMMAND_DIR:-}" ] \
      || [[ ! "$MONITOR_APP_HEALTH_BASE_URL" =~ ^http://127[.]0[.]0[.]1:[0-9]{1,5}$ ]]; then
    echo "MONITOR_APP_HEALTH_BASE_URL 仅允许隔离测试使用本机回环地址" >&2
    exit 2
  fi
  APP_HEALTH_BASE_URL=$MONITOR_APP_HEALTH_BASE_URL
fi
PROBLEMS=()
add() { PROBLEMS+=("$1"); }

compose() {
  timeout --kill-after="$DOCKER_KILL_AFTER" "$DOCKER_TIMEOUT" \
    sudo -n docker compose "$@"
}

probe_app() {
  local endpoint=$1
  # app 未映射宿主机端口，只能从 app 容器内部探测。除 HTTP 码外还校验安全 JSON 状态，
  # 因为 /health/db 在数据库不可达时仍可能返回 HTTP 200。
  compose exec -T app python - "$endpoint" "$APP_HEALTH_BASE_URL" >/dev/null 2>&1 <<'PY'
import json
import sys
from urllib.request import urlopen

endpoint = sys.argv[1]
base_url = sys.argv[2]
try:
    with urlopen(f"{base_url}{endpoint}", timeout=8) as response:
        if response.status != 200:
            raise SystemExit(1)
        payload = json.load(response)
except Exception:
    raise SystemExit(1) from None

if payload.get("status") != "ok":
    raise SystemExit(1)
if endpoint == "/health/db" and payload.get("db") != "reachable":
    raise SystemExit(1)
PY
}

write_status() {
  local value=$1
  local tmp
  tmp=$(mktemp .monitor.status.tmp.XXXXXX) || return 1
  if ! printf '%s\n' "$value" > "$tmp"; then
    rm -f -- "$tmp"
    return 1
  fi
  if ! mv -f -- "$tmp" monitor.status; then
    rm -f -- "$tmp"
    return 1
  fi
}

# cron 每 5 分钟触发一次；上一轮卡住时不得叠加 Docker/HTTP 探针进程。
if ! exec 9>.monitor.lock; then
  echo "无法创建巡检锁" >&2
  exit 2
fi
if ! flock -n 9; then
  TS=$(date '+%Y-%m-%dT%H:%M:%S%:z')
  write_status "$TS ok=N(1)" || exit 2
  printf '%s\n' "$TS 上一次巡检仍在运行，本轮已拒绝重叠执行" >> monitor.log
  exit 1
fi

WEBHOOK_SAFE=N
if [ -e .alert_webhook ] || [ -L .alert_webhook ]; then
  webhook_mode=$(stat -c '%a' -- .alert_webhook 2>/dev/null || true)
  if [ -L .alert_webhook ] || [ ! -f .alert_webhook ] || [ ! -r .alert_webhook ] \
      || [[ ! "$webhook_mode" =~ ^[0-7]+$ ]] || [ "${webhook_mode: -2}" != "00" ]; then
    add "告警 webhook 权限不安全，已拒绝读取（要求普通文件且 group/world 无权限）"
  else
    WEBHOOK_SAFE=Y
  fi
fi

# 1) 容器：db/app/frontend 都应 Up
ps_out=$(compose ps --format '{{.Service}} {{.Status}}' 2>/dev/null)
for svc in db app frontend; do
  line=$(printf '%s\n' "$ps_out" | grep -i "^$svc ")
  printf '%s' "$line" | grep -qi "up" || add "容器 $svc 未运行（${line:-缺失}）"
done

# 2) 数据库可达
compose exec -T db pg_isready -U spareparts -q 2>/dev/null || add "数据库不可达（pg_isready 失败）"

# 3) 应用本体（容器内部）与前端入口
probe_app /health || add "应用存活探针异常（容器内 /health）"
probe_app /health/db || add "应用数据库探针异常（容器内 /health/db）"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 8 http://localhost:8080/ 2>/dev/null || echo 000)
[ "$code" = "200" ] || add "前端入口异常：HTTP $code"

# 4) 磁盘 < 90%
use=$(df / | awk 'NR==2{gsub("%","",$5);print $5}')
[ "${use:-0}" -ge 90 ] && add "根分区磁盘使用 ${use}%，接近写满"

# 5) 最新备份 < 26 小时（每日 3am 备份，留余量）
latest=$(
  find "$BACKUP_DIR" -maxdepth 1 -type f -name 'db-*.dump' -printf '%T@ %p\n' \
    2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-
)
if [ -z "$latest" ]; then
  add "未找到任何数据库备份文件"
else
  age_h=$(( ( $(date +%s) - $(stat -c %Y "$latest") ) / 3600 ))
  [ "$age_h" -ge 26 ] && add "最新备份已 ${age_h} 小时未刷新（${latest##*/}）—— cron 可能没在跑"
fi

TS=$(date '+%Y-%m-%dT%H:%M:%S%:z')
N=${#PROBLEMS[@]}
STATUS="ok=$([ "$N" -eq 0 ] && echo Y || echo "N($N)")"
if ! write_status "$TS $STATUS"; then
  printf '%s\n' "$TS 巡检心跳原子写入失败" >> monitor.log
  exit 2
fi

[ "$N" -eq 0 ] && exit 0   # 健康：静默

MSG="⚠️ 备件系统巡检发现 $N 个问题（$TS）:"$'\n'"$(printf '  - %s\n' "${PROBLEMS[@]}")"
echo "$MSG" >> monitor.log
if [ "$WEBHOOK_SAFE" = Y ]; then
  HOOK=$(cat .alert_webhook)
  payload=$(printf '{"msgtype":"text","text":{"content":%s}}' \
            "$(printf '%s' "$MSG" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')")
  curl -s -m 8 -H 'Content-Type: application/json' -d "$payload" "$HOOK" >/dev/null \
    || echo "$TS 钉钉告警发送失败" >> monitor.log
fi
exit 1
