#!/usr/bin/env bash
# 生产健康巡检（cron 每 5 分钟）：容器 / DB / 入口 / 磁盘 / 备份新鲜度。
# 正常静默(只刷新 monitor.status)，异常追加 monitor.log 并(可选)发钉钉。
# 钉钉告警：把钉钉群机器人 webhook URL 写到 ~/apps/it-spareparts/.alert_webhook 即启用；无则只记日志。
set -uo pipefail   # 不用 -e：要收集所有问题而非首个失败就退出
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
cd "$(dirname "$0")"

PROBLEMS=()
add() { PROBLEMS+=("$1"); }

# 1) 容器：db/app/frontend 都应 Up
ps_out=$(sudo docker compose ps --format '{{.Service}} {{.Status}}' 2>/dev/null)
for svc in db app frontend; do
  line=$(printf '%s\n' "$ps_out" | grep -i "^$svc ")
  printf '%s' "$line" | grep -qi "up" || add "容器 $svc 未运行（${line:-缺失}）"
done

# 2) 数据库可达
sudo docker compose exec -T db pg_isready -U spareparts -q 2>/dev/null || add "数据库不可达（pg_isready 失败）"

# 3) 应用入口（前端 nginx 代理）
code=$(curl -s -o /dev/null -w '%{http_code}' -m 8 http://localhost:8080/ 2>/dev/null || echo 000)
[ "$code" = "200" ] || add "前端入口异常：HTTP $code"

# 4) 磁盘 < 90%
use=$(df / | awk 'NR==2{gsub("%","",$5);print $5}')
[ "${use:-0}" -ge 90 ] && add "根分区磁盘使用 ${use}%，接近写满"

# 5) 最新备份 < 26 小时（每日 3am 备份，留余量）
latest=$(ls -t /var/backups/spareparts/db-*.dump 2>/dev/null | head -1)
if [ -z "$latest" ]; then
  add "未找到任何数据库备份文件"
else
  age_h=$(( ( $(date +%s) - $(stat -c %Y "$latest") ) / 3600 ))
  [ "$age_h" -ge 26 ] && add "最新备份已 ${age_h} 小时未刷新（${latest##*/}）—— cron 可能没在跑"
fi

TS=$(date '+%F %T')
N=${#PROBLEMS[@]}
echo "$TS ok=$([ "$N" -eq 0 ] && echo Y || echo "N($N)")" > monitor.status   # 心跳：证明巡检本身在跑

[ "$N" -eq 0 ] && exit 0   # 健康：静默

MSG="⚠️ 备件系统巡检发现 $N 个问题（$TS）:"$'\n'"$(printf '  - %s\n' "${PROBLEMS[@]}")"
echo "$MSG" >> monitor.log
if [ -f .alert_webhook ]; then
  HOOK=$(cat .alert_webhook)
  payload=$(printf '{"msgtype":"text","text":{"content":%s}}' \
            "$(printf '%s' "$MSG" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')")
  curl -s -m 8 -H 'Content-Type: application/json' -d "$payload" "$HOOK" >/dev/null \
    || echo "$TS 钉钉告警发送失败" >> monitor.log
fi
exit 1
