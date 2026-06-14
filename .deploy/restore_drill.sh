#!/usr/bin/env bash
# 恢复演练：把最新备份还原到临时库 restore_test，比对行数，然后删除临时库。不碰生产库。
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
cd "$(dirname "$0")"

DUMP=$(ls -t /var/backups/spareparts/db-*.dump | head -1)
echo "恢复演练用最新备份: $DUMP"

dc() { sudo docker compose exec -T db "$@"; }

dc psql -U spareparts -d postgres -qc "DROP DATABASE IF EXISTS restore_test;" >/dev/null
dc psql -U spareparts -d postgres -qc "CREATE DATABASE restore_test;" >/dev/null
dc pg_restore -U spareparts -d restore_test --no-owner < "$DUMP" 2>/tmp/rdrill.err || true
if grep -iE 'error|fatal' /tmp/rdrill.err >/dev/null 2>&1; then
  echo "还原告警/错误（前5行）:"; grep -iE 'error|fatal' /tmp/rdrill.err | head -5
else
  echo "还原无错误"
fi

CNT_SQL="select 'sales_line='||count(*) from f_sales_line union all select 'dim_part='||count(*) from dim_part union all select 'inventory='||count(*) from inventory union all select 'purchase_line='||count(*) from f_purchase_line;"
echo "--- 还原库 restore_test 行数 ---"
dc psql -U spareparts -d restore_test -tAc "$CNT_SQL"
echo "--- 生产库 spareparts 行数 ---"
dc psql -U spareparts -d spareparts -tAc "$CNT_SQL"

dc psql -U spareparts -d postgres -qc "DROP DATABASE restore_test;" >/dev/null
echo "临时库已清理"
