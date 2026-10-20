# ITSP-6b 检察官非阻塞建议跟进交付

决策: D-34

基线 `origin/main@91f9a41`（#427 已合并），分支 `fix/cancel-cascade-review-followups`。
实施边界见 [方案](../plans/itsp6-cancel-cascade-review-followups.md)。本轮只在指定工作树开发，
不推送、不开 PR、不访问生产、不改全局 Codex 配置。状态为开发验证候选，待用户实际点按验收。

## 五条建议与可见行为

| 建议 | 改动及验证边界 |
|---|---|
| 两处锁后复验缺专门用例 | 新增独立 HTTP 用例，分别让全局锁后/行锁前及行锁后的 `executable`、`expected_digest` 不同，断言 409、事实/归属/墓碑/审计/版本/回执完整快照零写。两处实现均保留。 |
| delivery 身份排除缺专门用例 | 构造真实 delivery 外键、匹配原需求明细且非 `site_issue_v2` 的领用，详情仅提示仓库身份，确认后领用及发货事实保留，需求正常软作废。 |
| 看不出人工数据影响 | API 增加 `manual_created`、`protected_fields`；可靠关联领用另有 `source_demand_line_id`。需求标签显示“人工新建/人工保护”，领用标签明确为“关联需求”；确认区汇总有效需求和精确领用数量，说明两类计数可重叠。 |
| 回执查找全表扫 | 保留按操作者+幂等键的全局跨批次查重，查询固定 `->` 表达式并加专用 GIN 索引。旧回执不按日期、类型、success 状态缩窗。 |
| 网络未知后无法退出 | 返回/关闭先提示“结果未知，重新打开将重新查询状态；重试使用同一次确认”。关闭保留原请求，并提供独立“重新查询确认状态”入口；重新打开查询候选、列表及历史回执，不自动 POST；显式重试使用原摘要、原因、幂等键和账号。 |

人工标记只读复用 `maintenance_field_protection.is_manual_created/protected_fields`，包括三种人工身份前缀、
历史整行保护及 PN 身份组。未修改字段保护模块或 #430 语义；不把独立人工成本账本误列为需求字段，
成本证据继续保留。无法可靠关联的领用不凭 ID 字符串碰撞继承人工需求标签。
人工标记不改变哪些行会被作废，也不改变 `executable`。

`expected_digest` 原本覆盖需求实体全部字段（含 `raw_line_id`、`edited_source`、`manual_override`）、
领用及独立成本证据。新增标签在原摘要之后派生，不加入新的哈希输入，旧请求摘要、回执键和历史回放语义不变。
保护账本真实变更仍使摘要漂移并返回 409。

新增只读接口：

```text
GET /api/maintenance/projects/{project_id}/source-cancellations/{batch_id}/{source_order_id}/confirmation-status
    ?idempotency_key=原确认键
```

沿用项目/页面范围、实名及作废动作权限，按已知批次主键取回执，再绑定操作者、项目及候选；
返回 `{"result": null}` 或 `{"result": 历史回执}`，响应 `Cache-Control: no-store`。
无回执只是当前观察，不能宣称原请求失败。即使确认成功导致候选从列表消失、详情返回 409，也能查回历史回执。
该接口不提交确认、不修改业务事实。回执中的 `replayed=true` 明确它不代表当前业务状态。

## 改动文件

- 后端服务/API：`backend/app/services/maintenance_source_cancel_cascade.py`、
  `backend/app/api/maintenance_source_cancel_cascade.py`。
- 索引 metadata/迁移：`backend/app/models/system.py`、
  `backend/alembic/versions/d8f6a2c4b9e7_source_cancel_receipt_index.py`。
- 后端新增测试：`test_maintenance_source_cancel_followups.py`、
  `test_maintenance_source_cancel_metadata.py`、`test_maintenance_source_cancel_status.py`、
  `test_source_cancel_receipt_index_migration.py`（均在 `backend/tests/`）。
- 前端：`frontend/src/api/maintenanceSourceCancelCascade.ts`、
  `frontend/src/components/maintenance/SourceCancelImpactPanel.tsx` 及各自 `__tests__` 中的对应测试。
- 文档：`CONTEXT.md`、本交付说明及跟进方案。

## 红绿证据

日志全部位于工作树外 `/home/cloudlay/workspaces/reviews/lanes-20261020/laneB/`，
本轮文件名均以 `6bfu-` 开头；不提交日志、egg-info 或依赖软链。

| 反例 | 红 | 绿 | 日志 |
|---|---|---|---|
| 第一处复验 | 仅禁用 `before` 复验，2 failed | 恢复，2 passed | `6bfu-pre-lock-{red,green}.log` |
| 第二处复验 | 仅禁用 `checked` 复验，2 failed | 恢复，2 passed | `6bfu-post-lock-{red,green}.log` |
| delivery 排除 | 仅移除 `delivery_line_id is None`，1 failed | 恢复，1 passed | `6bfu-delivery-{red,green}.log` |
| API 人工标记 | 缺 `manual_created/protected_fields`，2 failed | 相关合并验证通过 | `6bfu-api-markers-red.log`、`6bfu-api-index-green.log` |
| 状态查询 | 缺路由 404，2 failed | 2 passed | `6bfu-api-index-{red,green}.log` |
| 索引/迁移 | 缺 revision/index，计划 Seq Scan，4 failed | 4 passed，含无损迁移往返及 metadata check | 同上 |
| UI 人工标记 | 缺标签，1 failed | 1 passed | `6bfu-ui-markers-{red,green}.log` |
| UI 未知退出/查询 | 新 API 缺失、返回禁用、无法离开，4 failed | 见最终前端验证 | `6bfu-ui-recovery-red.log` |
| 回执与附属读取/旧刷新竞态 | 新增 4 failed，原 21 passed | 见最终前端验证 | `6bfu-ui-late-read-red.log` |

三处 mutation 只在本地临时修改，`6bfu-mutation-driver.log` 记录每次退出码及最后的 `SOURCE_RESTORED`，
未提交禁用版。最初人工标记夹具缺非空 `import_batch_id`，已修正并重新取得字段缺失的有效红证据。
UI 首轮实现后 18 passed、3 failed 是测试环境双 Modal 标题 ID 冲突；改为按可见标题定位所属 dialog，
保留行为断言。独立复核追加“附属读取/项目刷新挂起不得遮住回执、旧刷新迟到不得覆盖新结果”反例。

## 索引迁移与 EXPLAIN

新增且仅新增 revision `d8f6a2c4b9e7`，直接接 `c9d4f2a6b8e1`，只有一个 head。

```sql
CREATE INDEX CONCURRENTLY ix_batch_source_cancel_receipts
ON sys_import_batch USING gin ((report_json -> '_source_cancel_confirmations'));
```

默认 `jsonb_ops` 支持 `?` 键存在操作；`jsonb_path_ops` 不适用于此操作符。
索引只覆盖回执子对象，不索引整份报告。查询及 metadata 固定同一 `->` 表达式，避免 JSONB 下标渲染差异或
准备语句的路径参数导致表达式不匹配。状态查询已知 batch id，直接使用主键。

最终索引测试在同一专用 PostgreSQL 15、同一组 20,002 条合成批次上编译真实确认查询。
SAVEPOINT 内临时删除索引记录 before，回滚恢复索引记录 after；不禁用顺序扫描、不按耗时阈值判通过。
断言 before 为 Seq Scan、after 命中 `ix_batch_source_cancel_receipts`，两次均只返回目标回执。
计划改善仅代表合成数据验证，不作为生产延迟承诺。

| 同一实际查询 | 计划 | 过滤无关行 | Shared Hit Blocks | Execution Time |
|---|---|---:|---:|---:|
| 无索引 | Seq Scan | 20,001 | 450 | 15.669 ms |
| 有索引 | Bitmap Index Scan → Bitmap Heap Scan | 0 | 72 | 2.649 ms |

原始完整 JSON 计划在 `6bfu-backend-final.log`。未用两次投影不同的早期红绿日志计算性能比例。

未来另行获得部署授权后，先完成原发布 runbook 的备份、批准和同 SHA 前后端要求。
此 revision 使用 `autocommit_block`，会提交此前事务，须分两步：

```bash
alembic upgrade c9d4f2a6b8e1
alembic upgrade d8f6a2c4b9e7
```

升级后检查 `pg_index.indisvalid/indisready` 均为 true，再启动候选应用。
并发建索引失败可能留下 INVALID 索引，不使用 `IF NOT EXISTS` 将其误报为成功：
先核对索引定义、有效性与 `alembic_version`；确认仅此索引无效后，在事务外
`DROP INDEX CONCURRENTLY ix_batch_source_cancel_receipts`，再重试该 revision。
若索引有效但迁移版本未登记，先核对故障阶段，不盲目 stamp 或重复建索引。
上述是未来授权发布的说明，本轮未执行任何生产操作。

应用回退可保留此索引，不清回执或恢复业务行。若确需 schema 回退，迁移的普通事务内 `DROP INDEX`
仅删除索引、保留所有报告和回执，也保留现有历史 downgrade 守卫的事务回滚能力。
参考 [Alembic autocommit 语义](https://alembic.sqlalchemy.org/en/latest/api/runtime.html#alembic.runtime.migration.MigrationContext.autocommit_block)
及 [PostgreSQL 并发索引](https://www.postgresql.org/docs/15/sql-createindex.html)。

## 验证、验收与未做事项

每次测试前读取负载，大于 10 则等待；
pytest 单进程、Vitest `--maxWorkers=2`，仅相关子集，全量交 CI。
测试数据库仅专用容器 `itsp-laneB-6bfu-20261020`（本机随机端口 `32886`）中的隔离合成库。
前端 node_modules 仅在 lockfile 一致后复用依赖软链。

后端最终相关子集 **90 passed，157.72 秒，exit 0**，日志 `6bfu-backend-final.log`：

```bash
cd backend
PYTEST_DATABASE_BASE_URL=postgresql+psycopg://spareparts:spareparts@127.0.0.1:32886/spareparts_test \
  uv run --extra dev pytest -q -s \
    tests/test_maintenance_source_cancel_cascade.py \
    tests/test_maintenance_source_cancel_concurrency.py \
    tests/test_maintenance_source_status_snapshot.py \
    tests/test_maintenance_source_cancel_followups.py \
    tests/test_maintenance_source_cancel_metadata.py \
    tests/test_maintenance_source_cancel_status.py \
    tests/test_source_cancel_receipt_index_migration.py \
    tests/test_maintenance_wbdd_sync_api.py \
    tests/test_maintenance_effective_read.py \
    tests/test_migration_pool_seq.py::test_single_alembic_head
```

9572 条 warning 主要为既有工作簿 `copy()` 弃用，另有 TestClient 和 Alembic metadata 检查
既有警告；迁移往返、`command.check`、唯一 head 均通过。Ruff 对本次 Python 文件检查通过；
随后仅统一 import 格式，无业务实现变更。

前端最终链 **TypeScript 通过、4 文件 50 passed（30.48 秒）、Vite 构建通过（19.77 秒），exit 0**，
日志 `6bfu-frontend-final.log`：

```bash
cd frontend
npx tsc &&
npx vitest run --maxWorkers=2 \
  src/components/maintenance/__tests__/SourceCancelImpactPanel.test.tsx \
  src/api/__tests__/maintenanceSourceCancelCascade.test.ts \
  src/pages/maintenance/panel/__tests__/PartsOrdersTab.test.tsx \
  src/components/maintenance/__tests__/WbddReconciliation.test.tsx &&
npx vite build
```

Vite 保留既有大于 500 kB 的 chunk 提示，无构建失败。新增后端 13 项、前端 9 项；
独立复核发现的 4 项恢复竞态用例已绿：回执不等待附属详情/列表，已知结果立即显示，
项目刷新只更新提示尾句，旧刷新不能覆盖下一次确认。状态查询无回执或失败后，
即使详情仍未返回，也能沿用同一请求重试。

`6bfu-static-final.log` 记录 Ruff、Python 编译、`alembic heads` 和 `git diff --check` 全部通过。
三路只读分析、后端及前端独立静态复核由不同代理承担，属于同模型独立复核，不声称异构模型审查。
本轮未另做浏览器实点或移动端截图；UI 行为证据来自组件/API 定向测试，实际验收仍待用户安排。

收尾只停止并删除 `itsp-laneB-6bfu-20261020`，不触碰其他 lane 容器；删除本工作树依赖软链，
还原依赖安装生成的三个 tracked egg-info 文件，日志与构建产物不提交。

用户点按验收：核对人工标签/计数；连续 5xx 后返回和叉号退出；重开重新查询；
成功回包丢失后查得历史回执；无回执时原原因/摘要/幂等键保持不变；长 PN 和 320px/390px 弹窗可操作。
本轮没有把自动组件测试冒充用户实际点按验收。

**不实现：墓碑恢复不恢复子行/领用、缺少行级恢复入口。待金臣拍板。**
没有改变恢复行为，没有生产迁移、部署或历史数据转换。

## 建议 PR 文案

标题：`fix: 完善已取消连带作废复验、人工提示与未知结果恢复`

正文：

> 决策: D-34
>
> 补齐两处锁后复验及 delivery 身份排除的独立回归测试，临时禁用每处防护均能单独触发失败。
> 影响清单展示人工新建/人工保护需求及精确领用的关联需求标记，并在确认区汇总数量；
> 执行集合、字段保护语义、摘要及已有幂等回执保持兼容。
>
> 网络结果未知时允许带提示离开，保留同一次确认。重新打开只读查询候选和实名绑定回执，
> 已成功但候选消失时仍可显示历史结果，不自动重发 POST。
>
> 新增纯索引迁移 d8f6a2c4b9e7（父 c9d4f2a6b8e1），并发建立回执子对象 GIN，
> 保留全局跨批次查重；合成 EXPLAIN、迁移往返和 metadata 检查验证。
> 无数据迁移、历史自动处理或恢复入口改动。详细红绿和定向回归见交付说明；
> 全量交 CI，待用户点按验收及 @Jinchen-Yang 对当前候选 SHA 人工批准。未推送、未部署。
