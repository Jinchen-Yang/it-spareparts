#!/usr/bin/env bash
# 当前 schema 恢复门禁：在 network-none 受限容器恢复最新备份并逐表核对。
# 历史 v1.20 有独立的 exact-SHA 恢复控制面，不调用本脚本。
set -Eeuo pipefail
umask 077
BASE_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH="$BASE_PATH"
if [ "${RESTORE_DRILL_TEST_MODE:-0}" = 1 ]; then
  [ "${EUID:-$(id -u)}" -ne 0 ] || {
    printf '%s\n' "RESTORE_DRILL_TEST_MODE 禁止以 root 运行" >&2
    exit 2
  }
  COMMAND_DIR=${RESTORE_DRILL_TEST_COMMAND_DIR:?missing test command directory}
  case "$COMMAND_DIR" in
    /*) ;;
    *) printf '%s\n' "RESTORE_DRILL_TEST_COMMAND_DIR 必须是绝对路径" >&2; exit 2 ;;
  esac
  [ -d "$COMMAND_DIR" ] && [ ! -L "$COMMAND_DIR" ] || {
    printf '%s\n' "RESTORE_DRILL_TEST_COMMAND_DIR 必须是真实目录" >&2
    exit 2
  }
  export PATH="$COMMAND_DIR:$BASE_PATH"
fi

readonly EXPECTED_DB_HEAD=c6f2a8e9d4b1
readonly RESTORE_CONTAINER="itdata-restore-${EXPECTED_DB_HEAD}"

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
APP_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
cd "$APP_DIR"

fatal() {
  printf 'FATAL: %s\n' "$*" >&2
  exit 1
}

compose() {
  sudo docker compose exec -T db "$@"
}

cleanup_restore_container() {
  if [ "$RESTORE_CREATED" = 0 ]; then
    claim_restore_container || return 0
  fi
  sudo docker rm -fv "$RESTORE_CID" >/dev/null || return 1
  remaining_restore=$(sudo docker ps -aq --no-trunc \
    --filter "id=${RESTORE_CID}") || return 1
  [ -z "$remaining_restore" ] || return 1
  RESTORE_CREATED=0
}

claim_restore_container() {
  [ -f "$RESTORE_CID_FILE" ] && [ ! -L "$RESTORE_CID_FILE" ] || return 1
  cid_lines=$(sudo wc -l -- "$RESTORE_CID_FILE" | awk 'NR == 1 {print $1}') \
    || return 1
  [ "$cid_lines" = 1 ] || return 1
  candidate_cid=$(sudo sed -n '1p' -- "$RESTORE_CID_FILE") || return 1
  [[ "$candidate_cid" =~ ^[0-9a-f]{64}$ ]] || return 1
  inspected_cid=$(sudo docker inspect -f '{{.Id}}' "$candidate_cid") || return 1
  [ "$inspected_cid" = "$candidate_cid" ] || return 1
  RESTORE_CID=$candidate_cid
  RESTORE_CREATED=1
}

if [ "${RESTORE_DRILL_TEST_MODE:-0}" = 1 ]; then
  DUMP=${RESTORE_DRILL_TEST_DUMP:?missing test dump}
  DOCKER_ROOT=${RESTORE_DRILL_TEST_DOCKER_ROOT:?missing test docker root}
  case "$DUMP:$DOCKER_ROOT" in
    /*:/*) ;;
    *) fatal "test dump and docker root must be absolute paths" ;;
  esac
else
  shopt -s nullglob
  dump_candidates=(/var/backups/spareparts/db-*.dump)
  shopt -u nullglob
  [ "${#dump_candidates[@]}" -gt 0 ] || fatal "no backup dump found"
  DUMP=${dump_candidates[0]}
  for candidate in "${dump_candidates[@]:1}"; do
    if [[ "$candidate" -nt "$DUMP" ]]; then
      DUMP=$candidate
    fi
  done
  unset dump_candidates candidate
  DOCKER_ROOT=/var/lib/docker
fi
[ -f "$DUMP" ] && [ ! -L "$DUMP" ] || fatal "backup dump missing or symbolic link"
[ -f "$DUMP.sha256" ] && [ ! -L "$DUMP.sha256" ] \
  || fatal "backup checksum missing or symbolic link"
[ -d "$DOCKER_ROOT" ] && [ ! -L "$DOCKER_ROOT" ] \
  || fatal "docker root missing or symbolic link"
[ "$(stat -c '%a' "$DUMP")" = 600 ] || fatal "backup dump mode must be 600"
[ "$(stat -c '%a' "$DUMP.sha256")" = 600 ] \
  || fatal "backup checksum mode must be 600"
printf '恢复演练用最新备份: %s\n' "$DUMP"
[ "$(wc -l < "$DUMP.sha256")" = 1 ] || fatal "backup checksum must be one line"
EXPECTED_HASH=$(sed -n '1{s/[[:space:]].*$//;p;}' "$DUMP.sha256")
[[ "$EXPECTED_HASH" =~ ^[0-9a-fA-F]{64}$ ]] \
  || fatal "backup checksum hash is invalid"
printf '%s  %s\n' "$EXPECTED_HASH" "$DUMP" | sha256sum -c -
compose pg_restore --list < "$DUMP" >/dev/null

SOURCE_COUNTS=$(mktemp)
RESTORED_COUNTS=$(mktemp)
RESTORE_STATE_DIR=$(mktemp -d)
RESTORE_CID_FILE="$RESTORE_STATE_DIR/container.cid"
RESTORE_CID=""
RESTORE_CREATED=0
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  cleanup_restore_container || status=97
  rm -f -- "$SOURCE_COUNTS" "$RESTORED_COUNTS" "$RESTORE_CID_FILE" \
    || status=97
  rmdir -- "$RESTORE_STATE_DIR" || status=97
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

DB_CID=$(sudo docker compose ps -q db)
[[ "$DB_CID" =~ ^[0-9a-f]{64}$ ]] || fatal "cannot resolve current compose DB container"
DB_IMAGE_ID=$(sudo docker inspect -f '{{.Image}}' "$DB_CID")
[[ "$DB_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || fatal "cannot resolve exact current compose DB image ID"

existing_restore=$(sudo docker ps -aq --no-trunc \
  --filter "name=^/${RESTORE_CONTAINER}$")
[ -z "$existing_restore" ] || fatal "restore container name already exists"

COUNT_SQL="SELECT 'dim_part|' || count(*) FROM dim_part
UNION ALL SELECT 'f_maintenance_line|' || count(*) FROM f_maintenance_line
UNION ALL SELECT 'f_project_expense|' || count(*) FROM f_project_expense
UNION ALL SELECT 'f_purchase_line|' || count(*) FROM f_purchase_line
UNION ALL SELECT 'f_sales_line|' || count(*) FROM f_sales_line
UNION ALL SELECT 'maintenance_project|' || count(*) FROM maintenance_project
UNION ALL SELECT 'maintenance_project_contract|' || count(*) FROM maintenance_project_contract
UNION ALL SELECT 'sys_import_batch|' || count(*) FROM sys_import_batch
ORDER BY 1;"

source_readonly_sql() {
  [ "$#" = 1 ] || fatal "source query must contain exactly one statement"
  statement=$1
  case "$statement" in
    "SELECT version_num FROM alembic_version;"|\
      "SELECT pg_database_size(current_database());"|\
      "$COUNT_SQL") ;;
    *) fatal "source query is not allowlisted" ;;
  esac
  sudo docker compose exec -T \
    -e PGOPTIONS="-c default_transaction_read_only=on" \
    db psql -U spareparts -d spareparts \
      -v ON_ERROR_STOP=1 -At -c "$statement"
}

SOURCE_DB_HEAD=$(source_readonly_sql "SELECT version_num FROM alembic_version;")
[ "$SOURCE_DB_HEAD" = "$EXPECTED_DB_HEAD" ] \
  || fatal "source database head is not the reviewed current head"

SOURCE_DB_SIZE=$(source_readonly_sql "SELECT pg_database_size(current_database());")
BACKUP_SIZE=$(stat -c '%s' "$DUMP")
DOCKER_FREE=$(df -PB1 "$DOCKER_ROOT" | awk 'NR == 2 {print $4}')
[[ "$SOURCE_DB_SIZE" =~ ^[0-9]+$ ]] || fatal "invalid source database size"
[[ "$BACKUP_SIZE" =~ ^[0-9]+$ ]] || fatal "invalid backup size"
[[ "$DOCKER_FREE" =~ ^[0-9]+$ ]] || fatal "invalid docker free space"
MAX_SAFE_INPUT=2000000000000000000
[ "$SOURCE_DB_SIZE" -le "$MAX_SAFE_INPUT" ] || fatal "source database size is unsafe"
[ "$BACKUP_SIZE" -le "$MAX_SAFE_INPUT" ] || fatal "backup size is unsafe"
RESTORE_PEAK_BYTES=$((SOURCE_DB_SIZE * 2))
if [ $((BACKUP_SIZE * 4)) -gt "$RESTORE_PEAK_BYTES" ]; then
  RESTORE_PEAK_BYTES=$((BACKUP_SIZE * 4))
fi
REQUIRED_FREE_BYTES=$((RESTORE_PEAK_BYTES + 2147483648))
[ "$DOCKER_FREE" -gt "$REQUIRED_FREE_BYTES" ] \
  || fatal "insufficient space for isolated restore"

source_readonly_sql "$COUNT_SQL" > "$SOURCE_COUNTS"

if sudo docker create \
    --name "$RESTORE_CONTAINER" \
    --cidfile "$RESTORE_CID_FILE" \
    --network none \
    --memory 1g \
    --memory-swap 1g \
    --cpus 1 \
    --pids-limit 256 \
    -e POSTGRES_HOST_AUTH_METHOD=trust \
    "$DB_IMAGE_ID" >/dev/null; then
  create_status=0
else
  create_status=$?
fi
if ! claim_restore_container; then
  [ "$create_status" = 0 ] \
    && fatal "cannot prove isolated restore container ownership"
  fatal "cannot create isolated restore container"
fi
[ "$create_status" = 0 ] || fatal "cannot create isolated restore container"
sudo docker start "$RESTORE_CID" >/dev/null

RESTORE_READY=0
for _ in $(seq 1 30); do
  if sudo docker exec "$RESTORE_CID" \
      pg_isready -U postgres >/dev/null 2>&1; then
    RESTORE_READY=1
    break
  fi
  sleep 1
done
[ "$RESTORE_READY" = 1 ] || fatal "isolated restore DB did not become ready"

sudo docker exec "$RESTORE_CID" createdb -U postgres restore_test
# The backup is intentionally read by the unprivileged operator account.
# shellcheck disable=SC2024
sudo docker exec -i "$RESTORE_CID" \
  pg_restore -U postgres -d restore_test --exit-on-error \
    --no-owner < "$DUMP"
RESTORED_DB_HEAD=$(sudo docker exec "$RESTORE_CID" \
  psql -U postgres -d restore_test -v ON_ERROR_STOP=1 -At \
    -c "SELECT version_num FROM alembic_version;")
[ "$RESTORED_DB_HEAD" = "$EXPECTED_DB_HEAD" ] \
  || fatal "restored database head is not the reviewed current head"

# The evidence file is intentionally written by the unprivileged operator account.
# shellcheck disable=SC2024
sudo docker exec "$RESTORE_CID" \
  psql -U postgres -d restore_test -v ON_ERROR_STOP=1 -At \
    -c "$COUNT_SQL" > "$RESTORED_COUNTS"
diff -u "$SOURCE_COUNTS" "$RESTORED_COUNTS"

cleanup_restore_container || fatal "cannot prove restore container removal"
printf '%s\n' "恢复成功：独立容器 head 正确，关键事实表（含两张稳定维保项目表）逐表行数一致"
