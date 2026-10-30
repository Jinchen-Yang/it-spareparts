#!/bin/bash
# 隔离库只读：f_maintenance_order / f_maintenance_line 逐列非空覆盖率
# 口径：A=全部；Y=单头制单日期在 2025-10-31..2026-10-30（近一年）；仅 is_active 行另列
cd "$(dirname "$0")/.."
Q() { docker exec -i maint-survey-pg-20261030 psql -U survey -d itsp -At -F"|" "$@"; }
for t in f_maintenance_order f_maintenance_line; do
  cols=$(Q -c "select column_name from information_schema.columns where table_name='$t' order by ordinal_position")
  sel=""
  for c in $cols; do
    if [ "$t" = f_maintenance_order ]; then
      sel="$sel union all select '$c', count(*), count(\"$c\"), count(*) filter (where o.order_date between '2025-10-31' and '2026-10-30'), count(\"$c\") filter (where o.order_date between '2025-10-31' and '2026-10-30') from f_maintenance_order o"
    else
      sel="$sel union all select '$c', count(*), count(l.\"$c\"), count(*) filter (where o.order_date between '2025-10-31' and '2026-10-30'), count(l.\"$c\") filter (where o.order_date between '2025-10-31' and '2026-10-30') from f_maintenance_line l join f_maintenance_order o on o.id=l.order_id where l.is_active"
    fi
  done
  echo "## $t (col|all|nonnull|lastyear|lastyear_nonnull)"
  Q -c "${sel# union all }"
done
