# 生产发布 Runbook（PR #90 后续修复）

本文只描述发布步骤；本次修复不执行生产部署。生产发布前必须先在 staging 使用生产快照完整演练。

约定：服务名为 `db`、`app`、`frontend`，数据库名为 `spareparts`。所有命令在服务器上的项目目录执行，并把 `BACKUP` 指向本次唯一备份文件；不要使用 `backup-*.dump` 这类模糊 glob。

## 1. 拉取目标 commit，记录回滚基线

```bash
cd ~/apps/it-spareparts
OLD_COMMIT=$(git rev-parse HEAD)
git fetch origin fix/boss-dashboard-p0-followup
git checkout fix/boss-dashboard-p0-followup
git pull --ff-only origin fix/boss-dashboard-p0-followup
TARGET_COMMIT=$(git rev-parse HEAD)
echo "TARGET_COMMIT=$TARGET_COMMIT"

# 先记下当前运行版本，回滚时恢复这两个镜像和这个 commit。
OLD_APP_IMAGE_ID=$(sudo docker compose images -q app)
OLD_FRONTEND_IMAGE_ID=$(sudo docker compose images -q frontend)
echo "OLD_COMMIT=$OLD_COMMIT"
echo "OLD_APP_IMAGE_ID=$OLD_APP_IMAGE_ID"
echo "OLD_FRONTEND_IMAGE_ID=$OLD_FRONTEND_IMAGE_ID"
```

如果 `git pull` 得到的 commit 不是已批准的目标 commit，立即停止，不要继续构建。

## 2. 备份并由 db 容器校验

```bash
mkdir -p backups
BACKUP="$PWD/backups/it-spareparts-$(date +%Y%m%d-%H%M%S).dump"
export BACKUP

sudo docker compose exec -T db pg_dump -U spareparts -Fc spareparts > "$BACKUP"
test -s "$BACKUP"
ls -lh "$BACKUP"

# pg_restore 从 db 容器执行，宿主机不需要安装 PostgreSQL 客户端。
sudo docker compose exec -T db pg_restore --list < "$BACKUP" | head -40
```

备份文件为空或 `pg_restore --list` 失败，立即中止。不得进入迁移或重算。

## 3. 重算前导出 KPI 与 40 单基线

下面的命令从当前 app 容器导出 KPI、按营收最高的 20 单和随机 20 单；文件保存在宿主机，供重算后逐项对账。

```bash
BASELINE="$PWD/backups/baseline-before-${TARGET_COMMIT}.json"
sudo docker compose exec -T app python -c '
import json
from sqlalchemy import func, select
from app.db import SessionLocal
from app.models.sales import FSalesLine, FSalesOrder
from app.services import dashboard

db = SessionLocal()
try:
    def row(o):
        revenue, gross = db.execute(
            select(func.coalesce(func.sum(FSalesLine.revenue_amount), 0),
                   func.coalesce(func.sum(FSalesLine.gross_profit), 0))
            .where(FSalesLine.order_id == o.id)
        ).one()
        return {"order_no": o.order_no, "order_date": str(o.order_date),
                "revenue": float(revenue), "gross_profit": float(gross)}

    high = db.scalars(
        select(FSalesOrder).join(FSalesLine)
        .group_by(FSalesOrder.id)
        .order_by(func.sum(FSalesLine.revenue_amount).desc(), FSalesOrder.id.desc())
        .limit(20)
    ).all()
    # 用稳定的伪随机顺序，保证重算前后是同一批 20 单。
    random = db.scalars(select(FSalesOrder).order_by(func.md5(FSalesOrder.order_no), FSalesOrder.id).limit(20)).all()
    print(json.dumps({"commit": "'"$TARGET_COMMIT"'", "kpi": dashboard.kpi(db, None, None),
                      "high_20": [row(o) for o in high],
                      "random_20": [row(o) for o in random]}, ensure_ascii=False, default=str))
finally:
    db.close()
' > "$BASELINE"
test -s "$BASELINE"
```

## 4. 先构建新镜像，再停止 app 写入

```bash
sudo docker compose build app frontend
NEW_APP_IMAGE_ID=$(sudo docker compose images -q app)
NEW_FRONTEND_IMAGE_ID=$(sudo docker compose images -q frontend)
echo "NEW_APP_IMAGE_ID=$NEW_APP_IMAGE_ID"
echo "NEW_FRONTEND_IMAGE_ID=$NEW_FRONTEND_IMAGE_ID"

# 到这里才停止 app；frontend 仍可继续提供旧页面，直到新镜像验证完成。
sudo docker compose stop app
```

## 5. 用新 app 镜像执行迁移、利润重算和池重建

不能 `exec` 旧 app 容器跑新迁移。以下全部是基于刚构建的新 app 镜像的 one-off 容器，并且不启动旧依赖：

```bash
sudo docker compose run --rm --no-deps app alembic upgrade head
sudo docker compose run --rm --no-deps app python -c \
  'from app.db import SessionLocal; from app.services import profit; db=SessionLocal(); print(profit.recompute(db)); db.close()'
sudo docker compose run --rm --no-deps app python -c \
  'from app.db import SessionLocal; from app.services import pool; db=SessionLocal(); print(pool.rebuild(db)); db.close()'
```

每一步出现异常栈都中止，按第 8 节恢复，不要启动新 app。

迁移后核对稳定池序列；SQL 同时打印存活最大 ID、序列高水位和 `is_called`，并输出明确结论：

```bash
sudo docker compose exec -T db psql -U spareparts -d spareparts -v ON_ERROR_STOP=1 -c '
WITH live AS (SELECT max(group_id) AS max_group_id FROM part_pool),
seq AS (SELECT last_value, is_called FROM part_pool_group_id_seq)
SELECT live.max_group_id, seq.last_value, seq.is_called,
       CASE WHEN seq.is_called THEN seq.last_value + 1 ELSE seq.last_value END AS next_value,
       CASE WHEN (CASE WHEN seq.is_called THEN seq.last_value + 1 ELSE seq.last_value END)
                  > coalesce(live.max_group_id, 0)
            THEN ''PASS: next_value > max(group_id)''
            ELSE ''FAIL: sequence may collide'' END AS conclusion
FROM live CROSS JOIN seq;
'
```

## 6. 重算后 40 单与 KPI 对账，再启动新版本

```bash
AFTER="$PWD/backups/baseline-after-${TARGET_COMMIT}.json"
sudo docker compose run --rm --no-deps app python -c '
import json
from sqlalchemy import func, select
from app.db import SessionLocal
from app.models.sales import FSalesLine, FSalesOrder
from app.services import dashboard

db = SessionLocal()
try:
    def row(o):
        revenue, gross = db.execute(
            select(func.coalesce(func.sum(FSalesLine.revenue_amount), 0),
                   func.coalesce(func.sum(FSalesLine.gross_profit), 0))
            .where(FSalesLine.order_id == o.id)
        ).one()
        return {"order_no": o.order_no, "order_date": str(o.order_date),
                "revenue": float(revenue), "gross_profit": float(gross)}
    high = db.scalars(select(FSalesOrder).join(FSalesLine).group_by(FSalesOrder.id)
                      .order_by(func.sum(FSalesLine.revenue_amount).desc(), FSalesOrder.id.desc()).limit(20)).all()
    random = db.scalars(select(FSalesOrder).order_by(func.md5(FSalesOrder.order_no), FSalesOrder.id).limit(20)).all()
    print(json.dumps({"commit": "'"$TARGET_COMMIT"'", "kpi": dashboard.kpi(db, None, None),
                      "high_20": [row(o) for o in high],
                      "random_20": [row(o) for o in random]}, ensure_ascii=False, default=str))
finally:
    db.close()
' > "$AFTER"

# 可执行阈值：40 单逐单 revenue/gross_profit 变化均不得超过 ¥0.01；订单集合必须各 20 条。
python3 - "$BASELINE" "$AFTER" <<'PY'
import json, sys

before, after = (json.load(open(p)) for p in sys.argv[1:])
assert len(before["high_20"]) == len(after["high_20"]) == 20
assert len(before["random_20"]) == len(after["random_20"]) == 20
for group in ("high_20", "random_20"):
    b = {x["order_no"]: x for x in before[group]}
    a = {x["order_no"]: x for x in after[group]}
    assert set(b) == set(a), f"{group}: order set changed"
    for order_no in b:
        for field in ("revenue", "gross_profit"):
            delta = abs(b[order_no][field] - a[order_no][field])
            assert delta <= 0.01, f"{group} {order_no} {field} delta={delta} > 0.01"
print("PASS: 40 orders reconcile within ¥0.01")
PY

sudo docker compose up -d app frontend
sudo docker compose ps
```

若对账失败，不得继续放量；差异必须能逐笔解释为本次税价口径变更，否则按回滚处理。

## 7. 生产放行门槛

以下条件全部满足后才允许生产放量：

1. staging 使用生产快照完整演练本 runbook。
2. `BACKUP` 非空，且 db 容器内 `pg_restore --list` 通过。
3. `alembic current` 为目标 head；序列 SQL 输出 `PASS`。
4. KPI 无异常；20 高额 + 20 随机订单逐单差异 ≤ ¥0.01，且无不明差额。
5. RBAC 验收：无采购成本权限不能从池顺序推断节省排名；无利润权限显示“无利润权限”。
6. UI 验收：订单分页/搜索/受限排序语义、池列表/详情快速切换、供应窗口提示。
7. 新版本观察至少 30 分钟：无新增 500、迁移错误或权限异常。

因此，合并 PR 不等于可以生产；本 PR 的预期结论是“可合并但不可生产”，直到上述 staging、备份、40 单对账、RBAC/UI 验收和观察门槛完成。

## 8. 回滚：恢复干净 DB/schema 与旧镜像

利润重算和迁移已经改库时，不能只回滚镜像，也不能只对现存数据库执行 `pg_restore --clean`；后者不会删除备份中不存在的新对象。必须停止写入、重建干净数据库，再恢复旧镜像/commit：

```bash
sudo docker compose stop app frontend

# 以 postgres 数据库连接执行，先终止业务连接，再删除并重建目标库。
sudo docker compose exec -T db psql -U spareparts -d postgres -v ON_ERROR_STOP=1 -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
   WHERE datname='spareparts' AND pid <> pg_backend_pid();"
sudo docker compose exec -T db psql -U spareparts -d postgres -v ON_ERROR_STOP=1 -c \
  'DROP DATABASE spareparts;'
sudo docker compose exec -T db psql -U spareparts -d postgres -v ON_ERROR_STOP=1 -c \
  'CREATE DATABASE spareparts OWNER spareparts;'
sudo docker compose exec -T db pg_restore -U spareparts -d spareparts --no-owner < "$BACKUP"

# 恢复旧代码记录，并把旧镜像 ID 重新标回 compose 使用的镜像名。
git checkout "$OLD_COMMIT"
APP_IMAGE_REF=$(sudo docker compose config --images | awk '/app/ {print; exit}')
FRONTEND_IMAGE_REF=$(sudo docker compose config --images | awk '/frontend/ {print; exit}')
sudo docker tag "$OLD_APP_IMAGE_ID" "$APP_IMAGE_REF"
sudo docker tag "$OLD_FRONTEND_IMAGE_ID" "$FRONTEND_IMAGE_REF"
sudo docker compose up -d app frontend
sudo docker compose ps
```

恢复完成后重新执行健康检查、`alembic current` 和关键页面验收；恢复失败时保持 app/frontend 停止，升级值班人员处理。
