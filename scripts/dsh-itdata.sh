#!/usr/bin/env bash
# dsh-itdata — 企业定制版 DeepSeek Harness 启动脚本（与官方版完全隔离）。
#
# 官方版（~/.dsh，端口 3080）用：dsh web / dsh --profile web
# 企业版（~/.dsh-itdata，默认端口 3081）用：本脚本
#
# 隔离内容：settings.yaml（模型配置）、profiles（组合与插件）、
# agent-presets（it-data 企业 preset）、sessions、storages 全部独立。
set -euo pipefail

DSH_HOME="${DSH_ITDATA_HOME:-$HOME/.dsh-itdata}"
PORT="${DSH_ITDATA_PORT:-3081}"
DSH_BIN="${DSH_ITDATA_BIN:-/Users/yangjinchen/.npm/_npx/1e7f6d9597241db0/node_modules/.bin/dsh}"

if [ ! -d "$DSH_HOME/profiles/web" ]; then
  echo "错误：企业版 DSH_HOME 不存在：$DSH_HOME" >&2
  echo "请先运行 dsh-plugins 的安装步骤或检查 DSH_ITDATA_HOME。" >&2
  exit 1
fi

echo "[dsh-itdata] DSH_HOME=$DSH_HOME 端口=$PORT"
export DSH_HOME
exec "$DSH_BIN" --profile web --port "$PORT" --host 127.0.0.1 "$@"
