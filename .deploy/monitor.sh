#!/usr/bin/env bash
# 生产健康巡检（cron 每 5 分钟）：容器 / DB / HTTPS、跳转、证书 / 磁盘 / 备份。
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

HTTPS_MONITOR_SAFE=N
HTTPS_MONITOR_URL=
HTTPS_MONITOR_HOST=
if [ -e .https_monitor_url ] || [ -L .https_monitor_url ]; then
  https_monitor_mode=$(stat -c '%a' -- .https_monitor_url 2>/dev/null || true)
  if [ -L .https_monitor_url ] || [ ! -f .https_monitor_url ] \
      || [ ! -r .https_monitor_url ] \
      || [[ ! "$https_monitor_mode" =~ ^[0-7]+$ ]] \
      || [ "${https_monitor_mode: -2}" != "00" ]; then
    add "HTTPS 监控配置权限不安全，已拒绝读取（要求普通文件且 group/world 无权限）"
  else
    HTTPS_MONITOR_URL=$(cat .https_monitor_url)
    if [[ "$HTTPS_MONITOR_URL" =~ ^https://([A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?)/$ ]]; then
      HTTPS_MONITOR_HOST=${BASH_REMATCH[1]}
      if [[ "$HTTPS_MONITOR_HOST" == *.* \
          && "$HTTPS_MONITOR_HOST" =~ [A-Za-z] \
          && "$HTTPS_MONITOR_HOST" != *..* \
          && "$HTTPS_MONITOR_HOST" != *.-* \
          && "$HTTPS_MONITOR_HOST" != *-.* ]]; then
        HTTPS_MONITOR_SAFE=Y
      else
        add "HTTPS 监控地址非法（仅允许正式 FQDN 的根路径）"
      fi
    else
      add "HTTPS 监控地址非法（仅允许 https://正式域名/）"
    fi
  fi
else
  add "HTTPS 监控配置缺失，无法验证正式入口、跳转和证书"
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
code=$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' -m 8 \
  http://127.0.0.1:8080/ 2>/dev/null || echo 000)
[ "$code" = "200" ] || add "前端入口异常：HTTP $code"

# 4) 正式 HTTPS 边缘、HTTP 跳转与证书续期余量
if [ "$HTTPS_MONITOR_SAFE" = Y ]; then
  if ! https_code=$(curl --noproxy '*' --proto '=https' --tlsv1.2 \
      --connect-timeout 5 --max-time 12 -sS -o /dev/null \
      -w '%{http_code}' "$HTTPS_MONITOR_URL" 2>/dev/null); then
    https_code=000
  fi
  [ "$https_code" = "200" ] \
    || add "HTTPS 正式入口异常：HTTP $https_code"

  HTTP_MONITOR_URL="http://$HTTPS_MONITOR_HOST/"
  if ! redirect_result=$(curl --noproxy '*' --proto '=http' \
      --connect-timeout 5 --max-time 12 --max-redirs 0 \
      -sS -o /dev/null -w '%{http_code} %{redirect_url}' \
      "$HTTP_MONITOR_URL" 2>/dev/null); then
    redirect_result="000 -"
  fi
  read -r redirect_code redirect_url <<< "$redirect_result"
  case "$redirect_code" in
    301|302|307|308) ;;
    *) add "HTTP 到 HTTPS 跳转异常：HTTP ${redirect_code:-000}" ;;
  esac
  [ "$redirect_url" = "$HTTPS_MONITOR_URL" ] \
    || add "HTTP 跳转目标异常（必须回到同域 HTTPS 根路径）"

  if ! timeout --kill-after=2s 12s \
      openssl s_client -connect "$HTTPS_MONITOR_HOST:443" \
        -servername "$HTTPS_MONITOR_HOST" </dev/null 2>/dev/null |
      timeout --kill-after=2s 12s \
        openssl x509 -checkend 604800 -noout >/dev/null 2>&1; then
    add "HTTPS 证书将在 7 天内到期或证书链读取失败"
  fi
fi

# 5) 磁盘 < 90%
use=$(df / | awk 'NR==2{gsub("%","",$5);print $5}')
[ "${use:-0}" -ge 90 ] && add "根分区磁盘使用 ${use}%，接近写满"

# 6) 最新备份 < 26 小时（每日 3am 备份，留余量）
latest=$(
  find "$BACKUP_DIR" -maxdepth 1 -type f -name 'db-*.dump' -printf '%T@ %p\n' \
    2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-
)
if [ -z "$latest" ]; then
  add "未找到任何数据库备份文件"
else
  latest_checksum="$latest.sha256"
  if [ -L "$latest_checksum" ] || [ ! -f "$latest_checksum" ]; then
    add "最新备份缺少有效 checksum（${latest##*/}）"
  else
    checksum_line_count=$(wc -l < "$latest_checksum" 2>/dev/null || true)
    expected_hash=$(
      sed -n '1{s/[[:space:]].*$//;p;}' "$latest_checksum" 2>/dev/null
    )
    if [ "$checksum_line_count" != 1 ] \
        || [[ ! "$expected_hash" =~ ^[0-9a-fA-F]{64}$ ]]; then
      add "最新备份 checksum 格式非法（${latest##*/}）"
    elif ! printf '%s  %s\n' "$expected_hash" "$latest" \
        | timeout --kill-after=2s 30s sha256sum -c - >/dev/null 2>&1; then
      add "最新备份 checksum 校验失败（${latest##*/}）"
    else
      age_h=$(( ( $(date +%s) - $(stat -c %Y "$latest") ) / 3600 ))
      [ "$age_h" -ge 26 ] && add "最新备份已 ${age_h} 小时未刷新（${latest##*/}）—— cron 可能没在跑"
    fi
  fi
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
