# 坏件循环待修池发布控制器（只准备，不执行）

决策: D-0058

本文登记两支新的生产发布控制器和它们的阶段命令。它不是生产回执，不表示已经打标签、切流、开出站或上线。本变更不合并、不部署、不连接生产写、不生成或轮换 token、不改 `frontend/src/version.ts`、不打开闲鱼/闲管家 live 发布，也不碰 HOLD 的 #520–#522。

启用顺序（控制器守边界，本文件不执行）：先部署商城 v2（0028 然后 0029，token 仍空）→ 保存经办人 → 再配商城 token → 再部署 itsp（迁移 + 1.47.0，出站仍关）→ 授权页键 → 最后单独打开 `SHOP_CYCLE_SELL_ENABLED`。

## 不能复用的旧入口

| 文件 | 为什么不能拿来跑这次 |
| --- | --- |
| `.deploy/requirements_successor_ops.py` | `target_head` 钉死 `98afc0d2157b`，migrate 只做 heads/current/check |
| `.deploy/requirements_visibility_ops.py` | 范围是 `e5a7c9d1f3b6 → 98afc0d2157b` |
| `.deploy/shop_pn_ops.py` | 范围是 `0026_pn_master_follow_merges → 0027_catalog_delivery_receipt` |

三个文件的字节都没有改。itsp 控制器在导入时核对可见性控制器 `6b2fe7287242ff6ce5dfcd785613877ef49dd80f13fb8ea15c4d244e93d37e9d` 和接续控制器 `2b2aa5ca2ca305b9f42574b50f505199169919323eea9d7c80ed90e3f19e2d1e`。商城控制器核对 `shop_pn_ops.py` 的 `beca271f1420981e5dcf21b78c7469710ce084efc75d0247b86712b567361c48`。

itsp 的升级顺序继承可见性控制器：先证明无网络恢复出旧头，再比较冻结时的业务行，然后真正迁移，`alembic check`，再比较那些行。命令是 `alembic upgrade head`，不是接续发布的空迁移。接续发布的教训留在新文件里：Mounts 按目标/类型/源排序后再比较、`alembic current` 接受 `98afc0d2157b (head) (mergepoint)`、主站 route 接受 `terminal: true`、freeze 前必须有当次 live 快照的 dry-run、`exposure-started.json` 先于改 Caddyfile、切流不用 `POST /load`、暴露后不自动回滚。

## itsp

- 发布提交：`ccefa17cc6446f613b7237ac0cd2121256969622`（#535 的 squash merge，`APP_VERSION=1.47.0`）。不要用父提交 `b2650202c2adb9c40c8fef4663e3472963ef534a`，也不要用分支头 `7e5f15e5`。
- 现网前序：容器 `itsp-successor-e62cd60cdbcc-app` / `itsp-successor-e62cd60cdbcc-frontend`，owner `.requirements-successor-active-owner.json`。新控制器只读这个 owner，不改写、不删除。
- 新目录：`/data/spareparts/releases/cycle-pool-ccefa17cc6446f613b7237ac0cd2121256969622/`（root、0700）。新 owner：`.cycle-pool-active-owner.json`。新容器前缀：`itsp-cycle-ccefa17cc644`。
- 预期 schema：backup-old 恢复头 `98afc0d2157b`；候选 `alembic heads` 唯一为 `c3e8a1d94b27 (head)`；`alembic upgrade head` 走 `98afc0d2157b → 86f7486b9ed5 → c3e8a1d94b27`。backup-new 恢复头 `c3e8a1d94b27`，`maintenance_project` 与 `maintenance_project_contract` 行数与冻结时一致，四张新循环表为 0 行。
- 候选 app 在创建时写入三个环境变量，不靠 `environment_file` 标签，也不读 `/opt/it-spareparts/.env`：`SHOP_CYCLE_SELL_URL=https://shop.ybwznt.com/api/integrations/cycle/sell-intents`，`SHOP_CYCLE_SELL_TOKEN` 来自发布目录里 root/0600 的 `cycle-sell.token.private`（无换行），`SHOP_CYCLE_SELL_ENABLED=false`。不配置 `CYCLE_SELL_CALLBACK_TOKEN_SHA256`。
- 日备目录按现场使用 `/data/backups/spareparts`。`.deploy/restore_drill.sh` 仍查找 `/var/backups/spareparts`，本变更不改那个脚本，避免影响仍引用旧路径的说明和其他调用；这次门禁用控制器自己的 dump，并记录日备文件名和 sha256。
- 回滚：不 `downgrade`，不改写 `alembic_version`，不把 dump 盖回生产。暴露标记出现之后不能 `recover`。若升级停在 `86f7486b9ed5`，排除原因后重跑 migrate，它会再次执行 `alembic upgrade head`，不会降级。关出站是 `disable-outbound`：只把运行中的 itsp app 重建为 `SHOP_CYCLE_SELL_ENABLED=false`，不动数据库、Caddy 和商城。

入口：

```text
python3 .deploy/cycle_pool_ops.py \
  /data/spareparts/releases/cycle-pool-ccefa17cc6446f613b7237ac0cd2121256969622/manifest.json \
  PHASE
```

`PHASE` 依次为 `preflight`、`images`、`dry-run`、`freeze`、`backup-old`、`migrate`、`backup-new`、`bluegreen`、`cutover`、`verify`。切流前、且 schema 仍是 `98afc0d2157b` 时，显式恢复阶段是 `recover`。出站开关是切流和 verify 之后的独立阶段：`enable-outbound`、`disable-outbound`。只停 manifest 里的旧 app，不停 `it-spareparts-db-1`、Caddy 或商城。禁止 `docker compose down` 项目 `it-spareparts`。

`dry-run` 把当次只读快照写到发布目录，再跑 `.deploy/cycle_pool_dry_run.py` 的 19 项检查和负例。19/19 且负例 `ALL_OK` 才会留下 PASS；`freeze` 核对这份回执和控制器 sha256。也可以离线重放，不连生产：

```text
python3 .deploy/cycle_pool_dry_run.py SNAPSHOT.json .deploy/cycle_pool_ops.py
```

归档核对（只读，不加生产锁）：

```text
git -C REPO archive --format=tar.gz ccefa17cc6446f613b7237ac0cd2121256969622 > source.tar.gz
python3 .deploy/cycle_pool_ops.py --check-archive REPO source.tar.gz
```

manifest 必须带上控制器、smoke、dry-run、可见性、接续、pool、helper 的 sha256。执行时以文件字节为准；上面历史控制器的摘要是固定门禁。

## 商城

`SHOP_RELEASE` 还没有。必须从生产 `4b40bb24c6de3cc95e1361e97ca55e3b53475792` 窄移植 #89 和 #92 之后，用那个合并提交的 40 位 SHA。不要部署商城 main（已知发散头前缀 `f5481cf1`），也不要把这条 release 分支合回商城 main。本仓库不包含那次窄移植。

- 目录：`/data/shop/releases/cycle-<40位SHOP_RELEASE>/`。owner：`.shop-cycle-active-owner.json`。不改 `.shop-pn-active-owner.json`。
- `old_head=0027_catalog_delivery_receipt`，`intermediate_head` 必须匹配 `0028_*`，`target_head=0029_cycle_sell_qty`。`alembic upgrade head` 先跑 0028 再跑 0029。
- 迁移只使用这条命令形态。`--env-file` 只作为 compose 参数传入，控制器不读取、不打印 `/root/IT-shop/private/next.env`：

```text
docker compose --env-file /root/IT-shop/private/next.env -p it-shop-next \
  -f /data/shop/releases/cycle-<40位SHOP_RELEASE>/prod.compose.yml \
  --profile migration run --rm --no-deps <migration_service> alembic upgrade head
```

`migration_service` 写在 manifest 里，必须是 PRODUCTION.md 里 `--profile migration` 的服务名（预期 `migration`）。它只能是一段服务名，不能带空格或参数。

入口：

```text
python3 .deploy/shop_cycle_ops.py \
  /data/shop/releases/cycle-<40位SHOP_RELEASE>/manifest.json \
  PHASE
```

`PHASE` 依次为 `preflight`、`freeze`、`backup-old`、`migrate`、`backup-new`、`create`、`activate`、`verify`。freeze 已开始但还没有迁移/创建/暴露时，`recover-freeze` 只拉起原来的五个容器。停写范围是 backend、worker、agent、frontend 和 NocoDB bridge。不碰 Noco 界面和 Noco 的两个库，不碰 `it-shop-prod-*`、itsp、Caddy。不 `compose down`，不改 Caddyfile 里的 `shop.ybwznt.com`。`preflight` 执行候选树里的 `deploy/scripts/check-external-networks.sh`；frontend 起来之后执行 `deploy/scripts/rollback-networks.sh` 把 ingress 接回去。两份脚本的 sha256 钉在 manifest 里，脚本里若出现 `docker compose down` 则拒绝。

部署期间 `CYCLE_SELL_TOKEN_SHA256` 必须为空。不改 `CATALOG_*` / `PN_MASTER_*`。不降级。0029 上若已经有名字里带 `cycle` 的表写入行，控制器拒绝再把旧程序指回这个库。

## 非目标

- 不在本 PR 执行任一阶段，不 SSH 改生产，不打 `release-` 标签。
- 不生成、不打印、不提交 token 或 `.env`。
- 不打开 `SHOP_CYCLE_SELL_ENABLED` 的默认值；`enable-outbound` 只是以后受控阶段的代码。
- 不实现闲鱼/闲管家 live 发布，不装配真实发帖客户端。
- 不改 HOLD 的 #520–#522，不改 `.deploy/restore_drill.sh`、`shop_pn_ops.py`、`requirements_successor_ops.py`、`requirements_visibility_ops.py`。
