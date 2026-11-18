# 坏件循环待修池发布控制器（只准备，不执行）

决策: D-0058、D-0059

本文登记两支新的生产发布控制器和它们的阶段命令。它不是生产回执，不表示已经打标签、切流、开出站或上线。本变更不合并、不部署、不连接生产写、不生成或轮换 token、不改 `frontend/src/version.ts`、不打开闲鱼/闲管家 live 发布，也不碰 HOLD 的 #520–#522。

启用顺序（控制器守边界，本文件不执行）：先部署商城 v2（0028 然后 0029，token 仍空）→ 保存经办人 → 再配商城 token → 再部署 itsp（迁移 + 1.47.0，出站仍关）→ 授权页键 → 最后单独打开 `SHOP_CYCLE_SELL_ENABLED`。

## 不能复用的旧入口

| 文件 | 为什么不能拿来跑这次 |
| --- | --- |
| `.deploy/requirements_successor_ops.py` | `target_head` 钉死 `98afc0d2157b`，migrate 只做 heads/current/check |
| `.deploy/requirements_visibility_ops.py` | 范围是 `e5a7c9d1f3b6 → 98afc0d2157b` |
| `.deploy/shop_pn_ops.py` | 范围是 `0026_pn_master_follow_merges → 0027_catalog_delivery_receipt` |

三个文件的字节都没有改。itsp 控制器在导入时核对可见性控制器 `6b2fe7287242ff6ce5dfcd785613877ef49dd80f13fb8ea15c4d244e93d37e9d` 和接续控制器 `2b2aa5ca2ca305b9f42574b50f505199169919323eea9d7c80ed90e3f19e2d1e`。商城控制器核对 `shop_pn_ops.py` 的 `beca271f1420981e5dcf21b78c7469710ce084efc75d0247b86712b567361c48`。

itsp 的升级顺序继承可见性控制器：先证明无网络恢复出旧头，再比较冻结时的业务行，然后真正迁移，`alembic check`，再比较那些行。命令是 `alembic upgrade head`，不是接续发布的空迁移。候选镜像的脚本头是 `c3e8a1d94b27`，所以升级前 `alembic current` 是 `98afc0d2157b (mergepoint)`（不再带 `(head)`），停在中间修订时是不带后缀的 `86f7486b9ed5`。`86f7486b9ed5` 会把五个循环页键写成 false，因此 `sys_role_template.permissions` 与 `sys_user` 的 `template_perms` / `perm_overrides` / `permissions` 允许按该迁移的投影变化；其余列仍与冻结摘要一致。接续发布的其余教训留在新文件里：Mounts 按目标/类型/源排序后再比较、主站 route 接受 `terminal: true`、freeze 前必须有当次 live 快照的 dry-run、`exposure-started.json` 先于改 Caddyfile、切流不用 `POST /load`、暴露后不自动回滚。出站重建使用单独的审计文件名，并在停掉正在跑的 app 之前完成环境与路径检查。

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

`PHASE` 依次为 `preflight`、`images`、`dry-run`、`freeze`、`backup-old`、`migrate`、`backup-new`、`bluegreen`、`cutover`、`verify`。切流前、且 schema 仍是 `98afc0d2157b` 时，显式恢复阶段是 `recover`。`recover.intent.json` 落下之后，正常阶段不再执行；没有 PASS 回执时可以再跑一次 `recover`。出站开关是切流和 verify 之后的独立阶段：`enable-outbound`、`disable-outbound`。只停 manifest 里的旧 app，不停 `it-spareparts-db-1`、Caddy 或商城。禁止 `docker compose down` 项目 `it-spareparts`。

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

## 商城（按 D-0059 在线发布）

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

新发布的 `PHASE` 依次为 `preflight`、`backup-old`、`migrate`、`backup-new`、`create`、`activate`、`verify`。`freeze` 已禁用。这项调整只适用于商城 `.deploy/shop_cycle_ops.py`，上面的 itsp 流程仍按原有阶段执行。

1. **旧服务持续在线，先做在线备份。** `preflight` 核对原来的五个容器、候选镜像、网络、schema 与兼容性审查。`backup-old` 由 `pg_dump` 取得一个一致的 MVCC 快照，在无网络、无发布端口的 PostgreSQL 15 容器里真正恢复，核验 schema、表和恢复后的行数。在线请求可以正常写入，因此不把稍后查询到的生产行数与备份快照强行比较。备份回执记录恢复快照中的行数、dump 大小与 sha256；`migrate` 再次核对 dump 字节和恢复证明。
2. **只执行已审查的兼容迁移。** 执行 0027 → 0028 → 0029 时旧程序继续服务。新 manifest 必须声明 `strategy=online-expand-v1` 和 `online_migration_review_sha256`，摘要指向同一目录的 `online-migration-review.json`。这份审查必须将 `release`、`source_sha256`、`app_image`、`frontend_image`、`old_head`、`intermediate_head`、`target_head` 与 manifest 完全绑定，并显式声明 `old_app_compatible=true`、`cycle_writes_disabled=true`。这些声明须来自该精确候选的迁移审查与兼容验证；控制器读取声明不等于自动证明任何未来迁移都向后兼容。
3. **旁起候选，探测通过后才切流。** `create` 不重命名、删除或替换旧容器。候选使用发布专属名称和网络；新前端的 `backend:8000` 只解析到候选后端，不能复用旧栈的公共 DNS 别名。候选前端不发布宿主机端口。`activate` 先启动候选 backend/frontend，等待 Docker 健康检查并直接探测候选 `/api/health`、`/api/ready`、`/healthz`，通过后才继续。worker/agent 做单实例交接，避免两代后台进程同时消费；NocoDB bridge 保持原容器。
4. **只切商城 route，保留旧栈。** 切流只改 `shop.ybwznt.com` 对应 route 的 upstream，使用 Caddy ETag 条件更新，不做 `POST /load`；Caddyfile 同步受内容、inode 和文件锁核验。itsp 的 route、DB 和容器保持原身份。文件与运行配置不是同一事务，发布窗口须暂停其他 Caddy 配置写者；忽略本控制器文件锁的写者仍能造成并发冲突。中断时保留 intent、计划及现场证据，用显式恢复阶段处理，不无条件覆盖整份配置。
5. **显式恢复旧入口。** `recover-cutover` 在核验原容器、兼容 schema、Caddy 计划及循环表仍未写入后，还须通过旧 backend/frontend 的健康检查和直接 HTTP 探测。先把 HTTP 入口交回旧栈，再交接后台进程，避免等待 agent 退出延长故障。恢复只切应用，不 `downgrade`、不改写 `alembic_version`、不把 dump 覆盖回生产。新业务已经写入循环表时拒绝回切。恢复开始后该次发布停止向前推进；修复后另建发布。

`preflight` 仍执行候选树中的 `deploy/scripts/check-external-networks.sh`，传入显式 `--env-file` 和 `--compose`。脚本摘要固定于 manifest，脚本出现 `docker compose down` 则拒绝。新前端以专属名称接入 ingress，不再用旧的 reconnect 脚本接管旧前端名称。安全检查按完整 argv 核对受审查的命令形态，`alembic upgrade head` 中的 `head` 是合法修订参数；`cat`、`head`、`tail` 等读取私有 env 的命令仍被拒绝。

### 迁移重试与旧停机发布恢复

新控制器在迁移子进程发出前，先持久化 `migration-dispatched-*.json`。`migrate.intent.json` 包含 manifest 摘要和 `dispatch_tracking=true`。若检查阶段拒绝命令，且没有任何 dispatch 记录、迁移容器或数据库迁移会话，schema 仍为 0027 时可以重试 `migrate`。只看 schema 仍是 0027 不足以证明命令从未执行；子进程超时、启动结果不明或已有 dispatch 记录时不能按“没执行过”重试。0028/0029 的续跑仍须先排除残留迁移进程和会话。

`recover-freeze` 留作旧离线流程的恢复入口，允许 freeze 已完成但升级可证明尚未发出的情况。它核对原五个容器的 id、镜像、名称、旧 schema 和未改动入口，拉起原进程，并等待 backend/frontend 健康检查及公网 `/api/ready`、`/healthz` 通过才写 PASS；没有 PASS 的失败恢复可以重试。恢复不会恢复数据库或降级。

**这次旧控制器留下的 intent 没有新的 dispatch 跟踪协议，不能凭“没有新日志”推导旧命令未执行。** 现场恢复须保留原 manifest、owner、回执及日志，以独立事故证据核对实际未发出升级、schema 和容器身份，再按经审查的恢复步骤处置。本修复没有连接生产，也没有替现场生成“未执行”证明。不得删除 intent、伪造新字段、重算旧 manifest/owner/回执摘要来绕过门禁。旧冻结发布不能原地改成在线发布；恢复后归档该次失败证据，按新控制器摘要、新 manifest 和新兼容性证据准备一次新的发布。

部署期间 `CYCLE_SELL_TOKEN_SHA256` 必须为空。不改 `CATALOG_*` / `PN_MASTER_*`，不生成 token、不配置经办人、不打开 itsp 出站或商城 live。上述恢复和发布命令的存在不表示本 PR 已执行它们；生产恢复、重发时间和真实验收单独记录。

### 本修复的开发验证

- `python3 -m unittest discover -s .deploy/tests -p 'test_shop_cycle*.py'`：47 项通过。覆盖完整命令误拦、派发日志与超时、0027 重试门禁、在线备份、兼容审查绑定、恢复拒绝不污染 intent、候选 DNS 隔离、探测/交接顺序、Caddy CAS、并发漂移及回切。
- `python3 -m unittest discover -s .deploy/tests -p test_cycle_pool_ops.py`：15 项通过。itsp 控制器及三个历史控制器的代码字节未改。
- 本地 Docker 29.9.2 与真实 Caddy 2.11.4：通过 Docker API 创建隔离网络，确认旧网络 `backend` 只解析旧后端、候选网络只解析新后端；实际执行商城路由 ETag PATCH、持久化及反向切回，itsp 路由保持一致。官方测试镜像没有 curl，管理请求以宿主机 curl 访问本地映射端口，仍执行控制器的响应解析及条件更新逻辑。测试容器停止并保留，未删除已有资源。
- Python 编译与 `git diff --check` 通过。独立代理复核发现的输入漂移、无效恢复污染 intent、重复商城路由和回切健康/顺序问题已修复；该复核为同模型，不代称跨模型复审。

这些结果不是生产演练，也不是精确商城候选的迁移兼容性证明。重发前仍须生成上述绑定的兼容审查。备份或创建候选时若部分失败，旧 HTTP 继续服务；这两阶段的独占 intent/制品不自动清理，须保留现场并按审查后的步骤处置，不能删除标记直接重试。

## 非目标

- 不在本 PR 执行任一阶段，不 SSH 改生产，不打 `release-` 标签。
- 不生成、不打印、不提交 token 或 `.env`。
- 不打开 `SHOP_CYCLE_SELL_ENABLED` 的默认值；`enable-outbound` 只是以后受控阶段的代码。
- 不实现闲鱼/闲管家 live 发布，不装配真实发帖客户端。
- 不改 HOLD 的 #520–#522，不改 `.deploy/restore_drill.sh`、`shop_pn_ops.py`、`requirements_successor_ops.py`、`requirements_visibility_ops.py`。
