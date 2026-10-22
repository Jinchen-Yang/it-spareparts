# catalog-v2 D1：读取与发布运行片段

决策: D-36。适用契约为 `docs/integrations/catalog-v2.md` §1–4、§6–9。
本片段是后续获准部署时的操作说明；此次开发没有生产启用或部署动作。
人工命令属于 D2，`/commands`、`/duplicate-checks` 未注册。

## 迁移与基线

遵循 `docs/BRANCH_POLICY.md`，候选基于 main；合并仍需管理员对当前 SHA
人工 GitHub APPROVED review。生产操作需另获“开始部署”授权，先备份并核验
可恢复，前后端使用同 commit，迁移成功后再启动镜像并按既有规则打 release 标签。

在 backend 工作目录、使用该环境注入的数据库连接执行：

```bash
uv run alembic heads
uv run alembic upgrade head
uv run python -m app.cli.catalog_v2 status
uv run python -m app.cli.catalog_v2 seed
uv run python -m app.cli.catalog_v2 publish-once
uv run python -m app.cli.catalog_v2 status
```

D1 迁移 `d4e8f2a6b0c1` 的父版本为 `c9d4f2a6b8e1`。合并前再次核对唯一 head。
迁移先创建捕获表和三张源表触发器，并把既有 PN 的两种 revision 初始化为1；
不会改变 PN 的业务值、现有 v1 DTO 或 `updated_at` 语义。seed 在捕获已启用后
从 RR 快照构建完整发布投影，完成才标 ready。未 seed 的读请求为503
`publication_not_ready`，状态命令可观察 `unseeded / ready / stopped`、水位及待捕获行数。

seed 每次都创建新发布世代 UUID，旧批次/投影保留；消费者旧 epoch 请求410，
必须留旧镜像并重新 staging 全量。不要将 seed 用作日常轮询命令。

## 开关和容量

- `CATALOG_V2_READ_CLIENTS` 默认 `{}`：client_id 到独立 Bearer token SHA256
  小写摘要的 JSON 映射。仅在后端环境注入，不在日志或前端保存 token；未配置返回
  404 `integration_disabled`。它不复用 `RESALE_CATALOG_TOKEN_SHA256`。
- `CATALOG_V2_CURSOR_HMAC_KEY` 可注入独立随机密钥；未配置时由应用 `SECRET_KEY`
  作域隔离派生。多进程需一致；更换密钥使原游标校验失败，消费者需新快照。
- `CATALOG_V2_PUBLISHER_ENABLED=false` 默认关闭后台循环。seed 后可手工
  `publish-once`；获准启用后台时设 true，默认间隔
  `CATALOG_V2_PUBLISHER_INTERVAL_SECONDS=30`。单发布者由数据库 advisory lock 保证。
- `CATALOG_V2_RESPONSE_MAX_BYTES=8388608`、
  `CATALOG_V2_FRAGMENT_MAX_BYTES=4194304` 为默认响应/原始片上限；测试可用较小值
  （例如4096/1537），实际仍受8/4 MiB硬上限约束。分片完整规范字节及清单持久保存；
  只在收齐后重组并核 hash。请求体64 KiB上限独立于响应配置。

用只读客户端创建快照，核 node/alias count 与 content_hash，再从其 epoch/watermark
读取增量和固定分段摘要。`/parts/{id}` 是源当前状态，仅供资料基线刷新，不能推进
消费方 checkpoint。无增量返回原水位。pending 过久时检查数据库长事务、后台开关与
报警表，不手动跳水位或把捕获自增ID当成提交序。

## 停批、保留与回滚

捕获到 PN 物理 DELETE 或不可发布身份/规格时，当前世代 stopped，写
`catalog_v2_alarm`，不推进水位或确认捕获组。由人工修复源数据并显式 seed 新世代。
代码不会自行删除/重建业务 PN，也不会以目录数据写库存或其他业务状态。

发布流至少保留30日；默认不自动清理。显式清理仅删除过期批次前缀并维护可用水位边界：

```bash
uv run python -m app.cli.catalog_v2 cleanup --retain-days 30
```

旧快照/传输元数据与捕获证据保留；本命令不删除业务行、捕获、投影或报警。
少于30日的保留配置会拒绝。

回滚首选清空 `CATALOG_V2_READ_CLIENTS`，按需关闭后台发布循环；**保留触发器、捕获、
批次、投影与报警**。v1凭据、导入链路和已发布历史不变。不要对生产做 schema downgrade，
不要倒拨 revision / epoch / 水位，也不通过删捕获或恢复整库覆盖后续事实。

## 本地验证

仅 lane D 隔离临时 PostgreSQL，不能使用5433或生产连接：

```bash
cd backend
PYTEST_DATABASE_BASE_URL=postgresql+psycopg://spareparts:spareparts@127.0.0.1:55510/spareparts_test uv run --extra dev pytest
```

测试场景见 `docs/integrations/catalog-v2-d1-validation.md`。所有数据库用例使用真实PG；
pytest 自动为每次运行建删独立数据库。开发检查通过不等于已合并、用户验收或已部署。
