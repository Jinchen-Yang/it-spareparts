#!/usr/bin/env bash
# v1.23 维保展示板：阶段闸发布状态机（plan v1.3 M5-3）。
#
# 阶段顺序强制（跳阶段/重复/回退一律拒绝，且在任何 docker 命令之前拒绝）：
#   preflight → backup → migrate → deploy → canary → observe → commit-release
#
# 三条不可协商的规则：
#   1. migrate 阶段**强制 MAINTENANCE_BOSS_DASHBOARD_ENABLED=false**——迁移与
#      功能开放解耦（铁律 7）；
#   2. canary 阶段翻闸后**从运行容器读回环境变量核验**，不信任 staged .env；
#      失败走 emergency trap 立即复位为 false；
#   3. rollback 只做「关 flag」，**永不执行 alembic downgrade**。
set -Eeuo pipefail
umask 077

readonly FROM_REV=c8e2a4f6b1d3
readonly TO_REV=d6e1f4a8c3b5
readonly RELEASE_FLAG=MAINTENANCE_BOSS_DASHBOARD_ENABLED
readonly PHASES=(preflight backup migrate deploy canary observe commit-release)

fatal() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat >&2 <<EOF
usage: v123_maintenance_boss_release.sh COMMAND STATE_FILE [ARGS...]
commands: ${PHASES[*]} rollback status
EOF
  exit 2
}

[ "$#" -ge 2 ] || usage
COMMAND=$1
STATE_FILE=$(realpath -m -- "$2")
shift 2
readonly COMMAND STATE_FILE

phase_index() {
  local needle=$1 i=0
  for phase in "${PHASES[@]}"; do
    [ "$phase" = "$needle" ] && { printf '%s' "$i"; return 0; }
    i=$((i + 1))
  done
  printf -- '-1'
}

read_state() {
  [ -f "$STATE_FILE" ] || { printf ''; return 0; }
  sed -n 's/^phase=//p' "$STATE_FILE" | tail -1
}

write_state() {
  local phase=$1
  printf 'phase=%s\nat=%s\n' "$phase" "$(date -u +%FT%TZ)" > "$STATE_FILE"
  chmod 0600 "$STATE_FILE"
}

# 阶段闸：必须恰好是下一个阶段（在任何 docker 调用之前判定）
require_next_phase() {
  local target=$1
  local target_idx current current_idx
  target_idx=$(phase_index "$target")
  [ "$target_idx" -ge 0 ] || fatal "未知阶段：$target"
  current=$(read_state)
  if [ -z "$current" ]; then
    current_idx=-1
  else
    current_idx=$(phase_index "$current")
    [ "$current_idx" -ge 0 ] || fatal "状态文件已损坏：$current"
  fi
  [ "$target_idx" -eq $((current_idx + 1)) ] \
    || fatal "阶段顺序错误：当前=${current:-未开始}，本次=$target（只允许顺序推进，不得跳过/重复/回退）"
}

compose() {
  docker compose "$@"
}

# 从**运行容器**读回 flag（不信任 staged .env）
readback_flag() {
  compose exec -T app sh -ceu "printf '%s' \"\${$RELEASE_FLAG:-unset}\""
}

update_env_key() {
  local key=$1 value=$2 env_file=${3:-.env}
  [ -f "$env_file" ] || fatal "缺少 $env_file"
  if grep -q "^${key}=" "$env_file"; then
    sed -i "s/^${key}=.*/${key}=${value}/" "$env_file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$env_file"
  fi
}

emergency_close_flag() {
  printf 'EMERGENCY: 复位 %s=false\n' "$RELEASE_FLAG" >&2
  update_env_key "$RELEASE_FLAG" false || true
  compose up -d --force-recreate app >/dev/null 2>&1 || true
}

case "$COMMAND" in
  status)
    current=$(read_state)
    printf 'phase=%s\n' "${current:-未开始}"
    ;;

  preflight)
    require_next_phase preflight
    command -v docker >/dev/null 2>&1 || fatal "缺少 docker"
    [ "$#" -ge 1 ] || fatal "preflight 需要发布包目录"
    PACKAGE_DIR=$(realpath -e -- "$1")
    python3 "$PACKAGE_DIR/v123_maintenance_boss_manifest.py" verify \
      --package-dir "$PACKAGE_DIR" || fatal "发布包校验失败"
    write_state preflight
    printf 'OK preflight\n'
    ;;

  backup)
    require_next_phase backup
    [ "$#" -ge 1 ] || fatal "backup 需要输出目录"
    BACKUP_DIR=$(realpath -m -- "$1")
    mkdir -p "$BACKUP_DIR"
    compose exec -T db pg_dump -U spareparts -Fc spareparts \
      > "$BACKUP_DIR/pre-v123.dump" || fatal "备份失败"
    [ -s "$BACKUP_DIR/pre-v123.dump" ] || fatal "备份文件为空"
    write_state backup
    printf 'OK backup -> %s\n' "$BACKUP_DIR/pre-v123.dump"
    ;;

  migrate)
    require_next_phase migrate
    current_rev=$(compose exec -T db psql -qtAX -U spareparts -d spareparts \
      -c 'SELECT version_num FROM alembic_version' | tr -d '[:space:]')
    [ "$current_rev" = "$FROM_REV" ] \
      || fatal "生产基线不是 $FROM_REV（实为 ${current_rev:-空}），拒绝迁移"
    # 迁移与功能开放解耦：本阶段强制关闭总闸。
    # 不加 --no-build：生产机的 Ubuntu 打包版 compose（2.40.3+ds1）的 `run` 子命令
    # 不认这个参数（只有 `up` 有），加了会以 "unknown flag" 直接失败。省略它并不会
    # 触发构建——`run` 只在镜像缺失时才构建，而发布流程要求先显式 `compose build`。
    compose run --rm --no-deps \
      -e "$RELEASE_FLAG=false" app alembic upgrade "$TO_REV" \
      || fatal "迁移失败"
    new_rev=$(compose exec -T db psql -qtAX -U spareparts -d spareparts \
      -c 'SELECT version_num FROM alembic_version' | tr -d '[:space:]')
    [ "$new_rev" = "$TO_REV" ] || fatal "迁移后版本不是 $TO_REV（实为 ${new_rev:-空}）"
    write_state migrate
    printf 'OK migrate %s -> %s（总闸保持 false）\n' "$FROM_REV" "$TO_REV"
    ;;

  deploy)
    require_next_phase deploy
    # 部署新镜像但**不开闸**：此时展示板端点仍整组 404，与未发布不可区分
    update_env_key "$RELEASE_FLAG" false
    compose up -d --force-recreate app frontend || fatal "部署失败"
    value=$(readback_flag)
    [ "$value" = "false" ] || fatal "部署后总闸读回值应为 false，实为 $value"
    write_state deploy
    printf 'OK deploy（总闸 false，端点仍隐藏）\n'
    ;;

  canary)
    require_next_phase canary
    update_env_key "$RELEASE_FLAG" true
    # 注意：`cmd || fatal` 属条件上下文，**不触发 ERR trap**——紧急复位必须显式调用，
    # 否则翻闸失败会把 .env 停在 true 上（release-control 测试锁死此不变量）。
    if ! compose up -d --force-recreate app; then
      emergency_close_flag
      fatal "翻闸后重建失败（已紧急复位为 false）"
    fi
    value=$(readback_flag) || value=readback-failed
    if [ "$value" != "true" ]; then
      emergency_close_flag
      fatal "总闸读回值应为 true，实为 $value（已紧急复位为 false）"
    fi
    write_state canary
    printf 'OK canary（总闸 true，容器读回已核验）\n'
    ;;

  observe)
    require_next_phase observe
    [ "$#" -ge 1 ] || fatal "observe 需要观察期分钟数"
    minutes=$1
    [[ "$minutes" =~ ^[1-9][0-9]*$ ]] || fatal "观察期必须是正整数分钟"
    value=$(readback_flag)
    [ "$value" = "true" ] || fatal "观察期内总闸不是 true（实为 $value）"
    write_state observe
    printf 'OK observe（%s 分钟观察期已登记）\n' "$minutes"
    ;;

  commit-release)
    require_next_phase commit-release
    value=$(readback_flag)
    [ "$value" = "true" ] || fatal "提交发布前总闸必须为 true（实为 $value）"
    write_state commit-release
    printf 'OK commit-release\n'
    ;;

  rollback)
    # 回滚 = 关 flag（铁律 7）：**不做 downgrade**，schema 保留
    update_env_key "$RELEASE_FLAG" false
    compose up -d --force-recreate app || fatal "回滚重建失败"
    value=$(readback_flag)
    [ "$value" = "false" ] || fatal "回滚后总闸读回值应为 false，实为 $value"
    printf 'OK rollback（总闸 false；schema 保留，未执行 downgrade）\n'
    ;;

  *)
    usage
    ;;
esac
