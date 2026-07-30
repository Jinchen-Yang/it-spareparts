#!/bin/bash
# 恢复 HTTPS 边缘配置，但绝不把 IT 备件前端重新暴露到公网 8080。
set -Eeuo pipefail
umask 077

readonly BASE_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
readonly CADDY_CONTAINER=personal-ai-assistant-caddy
readonly INGRESS_NETWORK=it-spareparts-ingress
readonly PRODUCTION_SCRIPT_PATH=/usr/local/sbin/it-spareparts-https-rollback
export PATH="$BASE_PATH"

fail() {
  printf 'rollback failed: %s\n' "$*" >&2
  exit 1
}

if [ "${HTTPS_ROLLBACK_TEST_MODE:-0}" = "1" ]; then
  # root 永远不能进入命令注入测试通道，避免 sudo -E/SETENV 把桩命令提权。
  [ "${EUID:-$(id -u)}" -ne 0 ] \
    || fail "test mode is forbidden while running as root"
  ASSISTANT_DIR=${HTTPS_ROLLBACK_ASSISTANT_DIR:?missing test assistant directory}
  APP_DIR=${HTTPS_ROLLBACK_APP_DIR:?missing test application directory}
  LOCK_DIR=${HTTPS_ROLLBACK_LOCK_DIR:?missing test lock directory}
  COMMAND_DIR=${HTTPS_ROLLBACK_COMMAND_DIR:?missing test command directory}
  readonly ROOT_CONFIG_UID=$EUID
  ROOT_CONFIG_GID=$(id -g)
  readonly ROOT_CONFIG_GID
  readonly TRUSTED_OPERATOR_UID=$EUID
  readonly TRUSTED_OPERATOR_GID=$ROOT_CONFIG_GID
  case "$COMMAND_DIR" in
    /*) export PATH="$COMMAND_DIR:$BASE_PATH" ;;
    *) fail "test command directory must be absolute" ;;
  esac
else
  [ "${EUID:-$(id -u)}" -eq 0 ] || fail "run this script with sudo"
  [ "$(realpath -e -- "$0")" = "$PRODUCTION_SCRIPT_PATH" ] \
    || fail "production rollback must run from $PRODUCTION_SCRIPT_PATH"
  [ "$(stat -c '%u:%g:%a' -- "$PRODUCTION_SCRIPT_PATH")" = "0:0:755" ] \
    || fail "production rollback executable ownership or mode is unsafe"
  [ "$(stat -c '%u:%g:%a' -- "${PRODUCTION_SCRIPT_PATH%/*}")" = "0:0:755" ] \
    || fail "production rollback executable directory is unsafe"
  readonly TRUSTED_OPERATOR=ubuntu
  TRUSTED_OPERATOR_UID=$(id -u "$TRUSTED_OPERATOR") \
    || fail "trusted production operator account is missing"
  TRUSTED_OPERATOR_GID=$(id -g "$TRUSTED_OPERATOR") \
    || fail "trusted production operator group is missing"
  readonly TRUSTED_OPERATOR_UID
  readonly TRUSTED_OPERATOR_GID
  readonly ROOT_CONFIG_UID=0
  readonly ROOT_CONFIG_GID=0
  readonly ASSISTANT_DIR=/opt/personal-ai-assistant
  readonly APP_DIR=/home/ubuntu/apps/it-spareparts
  readonly LOCK_DIR=/run/lock/it-spareparts-https-rollback
fi

[ "$#" -eq 2 ] \
  || fail "usage: $0 /absolute/evidence-directory https://assistant-host/health"
case "$1" in
  /*) evidence_arg=$1 ;;
  *) fail "evidence directory must be an absolute path" ;;
esac
readonly ASSISTANT_SMOKE_URL=$2
[[ "$ASSISTANT_SMOKE_URL" =~ ^https://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?/health$ ]] \
  || fail "assistant smoke URL must be an HTTPS /health URL without a port"
[[ "$evidence_arg" =~ ^[A-Za-z0-9_./-]+$ ]] \
  || fail "evidence directory contains unsupported characters"

[ -d "$ASSISTANT_DIR" ] && [ ! -L "$ASSISTANT_DIR" ] \
  || fail "assistant directory is missing or unsafe"
[ -d "$APP_DIR" ] && [ ! -L "$APP_DIR" ] \
  || fail "application directory is missing or unsafe"
[ -d "$evidence_arg" ] && [ ! -L "$evidence_arg" ] \
  || fail "evidence directory is missing or unsafe"

evidence_dir=$(realpath -e -- "$evidence_arg") \
  || fail "cannot resolve evidence directory"
[ "$evidence_arg" = "$evidence_dir" ] \
  || fail "evidence directory must be canonical and contain no symlink traversal"

mode_denies_group_world_write() {
  local mode=$1
  [[ "$mode" =~ ^[0-7]+$ ]] || return 1
  (( (8#$mode & 8#022) == 0 ))
}

runtime_file_is_safe() {
  local path=$1
  local expected_uid=$2
  local expected_gid=$3
  [ -f "$path" ] && [ ! -L "$path" ] && [ -r "$path" ] || return 1
  mode_denies_group_world_write "$(stat -c '%a' -- "$path")" || return 1
  [ "$(stat -c '%u' -- "$path")" -eq "$expected_uid" ] || return 1
  [ "$(stat -c '%g' -- "$path")" -eq "$expected_gid" ] || return 1
}

evidence_mode=$(stat -c '%a' -- "$evidence_dir") \
  || fail "cannot inspect evidence directory"
[[ "$evidence_mode" =~ ^[0-7]+$ ]] && [ "${evidence_mode: -2}" = "00" ] \
  || fail "evidence directory must deny group/world access"
[ "$(stat -c '%u' -- "$evidence_dir")" -eq "$ROOT_CONFIG_UID" ] \
  || fail "evidence directory has an unexpected owner"
[ "$(stat -c '%g' -- "$evidence_dir")" -eq "$ROOT_CONFIG_GID" ] \
  || fail "evidence directory has an unexpected group"
[ "$(stat -c '%u' -- "$ASSISTANT_DIR")" -eq "$TRUSTED_OPERATOR_UID" ] \
  || fail "assistant directory is not owned by the trusted operator"
[ "$(stat -c '%g' -- "$ASSISTANT_DIR")" -eq "$TRUSTED_OPERATOR_GID" ] \
  || fail "assistant directory is not grouped to the trusted operator"
[ "$(stat -c '%a' -- "$ASSISTANT_DIR")" = 750 ] \
  || fail "assistant directory mode must be exactly 750"

readonly BACKUP_CADDY="$evidence_dir/Caddyfile.before"
readonly BACKUP_ASSISTANT_COMPOSE="$evidence_dir/assistant-compose.before.yml"
readonly BACKUP_IT_COMPOSE="$evidence_dir/it-compose.before.yml"
readonly CHECKSUM_FILE="$evidence_dir/SHA256SUMS"
readonly EXPECTED_FILES=(
  "$BACKUP_CADDY"
  "$BACKUP_ASSISTANT_COMPOSE"
  "$BACKUP_IT_COMPOSE"
  "$CHECKSUM_FILE"
)

for file in "${EXPECTED_FILES[@]}"; do
  [ -f "$file" ] && [ ! -L "$file" ] && [ -r "$file" ] \
    || fail "required evidence is missing or unsafe: ${file##*/}"
  file_mode=$(stat -c '%a' -- "$file") \
    || fail "cannot inspect evidence file: ${file##*/}"
  [[ "$file_mode" =~ ^[0-7]+$ ]] && [ "${file_mode: -2}" = "00" ] \
    || fail "evidence file must deny group/world access: ${file##*/}"
  [ "$(stat -c '%u' -- "$file")" -eq "$ROOT_CONFIG_UID" ] \
    || fail "evidence file has an unexpected owner: ${file##*/}"
  [ "$(stat -c '%g' -- "$file")" -eq "$ROOT_CONFIG_GID" ] \
    || fail "evidence file has an unexpected group: ${file##*/}"
done

# 不信任 SHA256SUMS 中的任意路径：只接受三个预期备份，且每项只能出现一次。
declare -A expected_checksums=(
  ["$BACKUP_CADDY"]=0
  ["$BACKUP_ASSISTANT_COMPOSE"]=0
  ["$BACKUP_IT_COMPOSE"]=0
)
while IFS= read -r checksum_line || [ -n "$checksum_line" ]; do
  [[ "$checksum_line" =~ ^([0-9A-Fa-f]{64})[[:space:]][[:space:]](.+)$ ]] \
    || fail "invalid SHA256SUMS format"
  expected_digest=${BASH_REMATCH[1],,}
  checksum_path=${BASH_REMATCH[2]}
  [ "${expected_checksums[$checksum_path]+present}" = present ] \
    || fail "SHA256SUMS references an unexpected path"
  [ "${expected_checksums[$checksum_path]}" -eq 0 ] \
    || fail "SHA256SUMS contains a duplicate path"
  actual_digest=$(sha256sum -- "$checksum_path")
  actual_digest=${actual_digest%% *}
  [ "$actual_digest" = "$expected_digest" ] \
    || fail "checksum mismatch: ${checksum_path##*/}"
  expected_checksums["$checksum_path"]=1
done < "$CHECKSUM_FILE"

for checksum_path in \
  "$BACKUP_CADDY" "$BACKUP_ASSISTANT_COMPOSE" "$BACKUP_IT_COMPOSE"; do
  [ "${expected_checksums[$checksum_path]}" -eq 1 ] \
    || fail "SHA256SUMS is missing: ${checksum_path##*/}"
done

# /run 会在重启后清空。缺失时用 mkdir 的“已存在即失败”语义原子创建；若
# 非特权用户抢先创建任意对象，绝不接管其所有权或权限，后续校验直接拒绝。
if [ ! -e "$LOCK_DIR" ] && [ ! -L "$LOCK_DIR" ]; then
  mkdir --mode=700 -- "$LOCK_DIR" \
    || fail "cannot create rollback lock directory"
fi
[ -d "$LOCK_DIR" ] && [ ! -L "$LOCK_DIR" ] \
  || fail "rollback lock directory is missing or unsafe"
lock_dir_real=$(realpath -e -- "$LOCK_DIR") \
  || fail "cannot resolve rollback lock directory"
[ "$LOCK_DIR" = "$lock_dir_real" ] \
  || fail "rollback lock directory must be canonical"
[ "$(stat -c '%u:%g:%a' -- "$LOCK_DIR")" = \
  "$ROOT_CONFIG_UID:$ROOT_CONFIG_GID:700" ] \
  || fail "lock directory ownership or mode is unsafe"

# 锁定 root-owned 目录本身，不创建可被预置为 symlink 的固定名称锁文件。
exec 9<"$LOCK_DIR" || fail "cannot open rollback lock directory"
flock -n 9 || fail "another HTTPS rollback is already running"

current_caddy="$ASSISTANT_DIR/Caddyfile"
current_assistant_compose="$ASSISTANT_DIR/compose.production.yml"
assistant_env="$ASSISTANT_DIR/.env"
app_compose="$APP_DIR/docker-compose.yml"
for current_file in "$current_caddy" "$current_assistant_compose" "$app_compose"; do
  runtime_file_is_safe "$current_file" "$ROOT_CONFIG_UID" "$ROOT_CONFIG_GID" \
    || fail "root runtime config is missing, writable, unowned, or unsafe: ${current_file##*/}"
done
runtime_file_is_safe \
  "$assistant_env" "$TRUSTED_OPERATOR_UID" "$TRUSTED_OPERATOR_GID" \
  || fail "assistant .env is not a safe trusted-operator file"
[ "$(stat -c '%a' -- "$assistant_env")" = 600 ] \
  || fail "assistant .env mode must be exactly 600"

# 先验证 IT 的持久主 Compose，而不读取应用密钥。容器停止时也不能让下一次启动重开 8080。
FRONTEND_PORT=8080 IT_DATA_INGRESS_NETWORK="$INGRESS_NETWORK" \
  docker compose \
    --project-directory "$APP_DIR" \
    --env-file /dev/null \
    -f "$app_compose" \
    config --format json |
  python3 -c '
import json
import sys

config = json.load(sys.stdin)
services = config["services"]
ports = services["frontend"].get("ports", [])
assert ports == [{
    "mode": "ingress",
    "target": 80,
    "published": "8080",
    "protocol": "tcp",
    "host_ip": "127.0.0.1",
}]
assert services["frontend"]["networks"]["ingress"]["aliases"] == [
    "it-spareparts-frontend"
]
assert "ingress" not in services["app"]["networks"]
assert "ingress" not in services["db"]["networks"]
assert config["networks"]["ingress"]["name"] == "it-spareparts-ingress"
assert config["networks"]["ingress"]["internal"] is True
' || fail "persistent IT compose could reopen plaintext access"

# 在替换线上文件前离线验证备份 Compose。Caddyfile 来自已校验和的运行前快照，
# 替换后仍会再次由 Caddy 自身 validate。
docker compose \
  --project-directory "$ASSISTANT_DIR" \
  --env-file "$assistant_env" \
  -f "$BACKUP_ASSISTANT_COMPOSE" \
  config --quiet \
  || fail "backup assistant compose is invalid"

failed_suffix="$(date -u +%Y%m%dT%H%M%SZ)-$$"
failed_caddy="$evidence_dir/Caddyfile.failed-$failed_suffix"
failed_assistant_compose="$evidence_dir/assistant-compose.failed-$failed_suffix.yml"
install -m 600 -- "$current_caddy" "$failed_caddy"
install -m 600 -- "$current_assistant_compose" "$failed_assistant_compose"

staged_caddy=$(mktemp "$ASSISTANT_DIR/.Caddyfile.rollback.XXXXXX")
staged_assistant_compose=$(
  mktemp "$ASSISTANT_DIR/.compose.production.rollback.XXXXXX"
)
install -m 600 -- "$BACKUP_CADDY" "$staged_caddy"
install -m 600 -- "$BACKUP_ASSISTANT_COMPOSE" "$staged_assistant_compose"

transaction_active=0
transaction_committed=0
cleanup_transaction() {
  local status=$?
  trap - EXIT
  rm -f -- "${staged_caddy:-}" "${staged_assistant_compose:-}"
  if [ "$transaction_active" -eq 1 ] && [ "$transaction_committed" -ne 1 ]; then
    printf '%s\n' \
      "rollback transaction failed; restoring the pre-rollback edge snapshot" >&2
    install -m 600 -- "$failed_caddy" "$current_caddy" || true
    install -m 600 -- "$failed_assistant_compose" \
      "$current_assistant_compose" || true
    (
      cd "$ASSISTANT_DIR" || exit
      docker compose --env-file .env -f compose.production.yml \
        config --quiet &&
        docker compose --env-file .env -f compose.production.yml \
          up -d --no-deps --force-recreate caddy
    ) >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup_transaction EXIT

# 两次 rename 均在目标文件系统内原子完成；若后续任一门禁失败，EXIT trap 会恢复成对快照。
transaction_active=1
mv -f -- "$staged_caddy" "$current_caddy"
mv -f -- "$staged_assistant_compose" "$current_assistant_compose"

cd "$ASSISTANT_DIR"
docker compose --env-file .env -f compose.production.yml config --quiet \
  || fail "restored assistant compose is invalid"
docker compose --env-file .env -f compose.production.yml \
  up -d --no-deps --force-recreate caddy \
  || fail "failed to recreate the restored Caddy service"

running=false
for _attempt in $(seq 1 30); do
  running=$(docker inspect -f '{{.State.Running}}' "$CADDY_CONTAINER" 2>/dev/null \
    || true)
  [ "$running" = true ] && break
  sleep 1
done
[ "$running" = true ] || fail "restored Caddy container did not become ready"

docker exec "$CADDY_CONTAINER" \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile \
  || fail "restored Caddy configuration failed validation"
docker exec "$CADDY_CONTAINER" wget -qO- http://api:8000/health >/dev/null \
  || fail "original personal assistant health check failed"

networks_json=$(docker inspect \
  --format '{{json .NetworkSettings.Networks}}' "$CADDY_CONTAINER")
if grep -Fq "\"$INGRESS_NETWORK\"" <<<"$networks_json"; then
  docker network disconnect "$INGRESS_NETWORK" "$CADDY_CONTAINER" \
    || fail "failed to detach the retired IT ingress network"
fi

listeners=$(ss -ltnH '( sport = :8080 )') \
  || fail "cannot verify port 8080 listeners"
listener_count=0
while IFS= read -r listener; do
  [ -z "$listener" ] && continue
  listener_count=$((listener_count + 1))
  listener_address=$(awk '{print $4}' <<<"$listener")
  [ "$listener_address" = "127.0.0.1:8080" ] \
    || fail "unsafe port 8080 listener remains: $listener_address"
done <<<"$listeners"
[ "$listener_count" -eq 1 ] \
  || fail "expected loopback port 8080 listener is missing or duplicated"

curl --noproxy '*' --proto '=http' \
  --connect-timeout 3 --max-time 8 -fsS \
  http://127.0.0.1:8080/ >/dev/null \
  || fail "loopback IT frontend is not usable"
curl --noproxy '*' --proto '=https' --tlsv1.2 \
  --connect-timeout 5 --max-time 15 -fsS \
  "$ASSISTANT_SMOKE_URL" >/dev/null \
  || fail "original personal assistant HTTPS route is not usable"

transaction_committed=1
printf '%s\n' "rollback complete; public 8080 remains closed"
