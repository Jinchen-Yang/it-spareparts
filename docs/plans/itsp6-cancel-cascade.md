# ITSP-6 第二个候选：已取消影响与人工连带作废

决策: D-34

基线 `0ff4ffe`（PR #425，含 main `32ab2a6`），分支 `feat/cancel-cascade-confirm`。
本次仅 D-34 第 3–7 款；只在 lane B 工作树开发，不推送、不开 PR、不访问生产。

## 编码前勘察（基线文件行号）

- 单头真实字段是 `FMaintenanceOrder.data_status`（`backend/app/models/maintenance.py:89`）；`data_status_raw` 属 CKD。loader 单头 upsert 写状态及批次（`backend/app/etl/loader.py:1085`），skip 不覆盖已有头。最新合法上传驱动需求可见性。
- #368 共用读侧 `_assigned_lines` 检查行有效性、归属及 `active_orders`（`backend/app/services/maintenance_project_master_workbook.py:511`），后者仅已生效（`backend/app/services/query_filters.py:123`）。管理历史查询仍保留非生效头。既有 `test_maintenance_effective_read.py:131` 覆盖未生效、恢复及系统作废不复活。
- 现有不可变 `_wbdd_source_snapshot` 仅身份、行号和成员，无状态（`backend/app/services/maintenance_wbdd_source_snapshot.py:134`）；pipeline 在业务校验前捕获，loader 成功后写 report，同事务标成功（`backend/app/etl/pipeline.py:523`、`:618`、`:697`、`:713`）。不能用归档的 skip 取消原文推断已应用状态。
- transform 对同 raw_order_id 取首个头（`backend/app/etl/transform.py:548`）。因此状态证据要检查同头多值，冲突不可执行。子行 PN/数量非法不妨碍合法单头更新（`:491`）；不能直接用缺项快照的全部 blocked_reasons 阻断状态证据。
- D-27 确认锁序及写链可参考 `backend/app/services/maintenance_wbdd_sync.py:81`、`:113`、`:127`、`:139`。精确关联以当前项目和原始明细 ID 为依据（`maintenance_wbdd_reconciliation.py:69`）；单号只用于提示，绝不参与执行集合。
- `source_line_id` 在仓库领用来源也可代表仓库行（`maintenance_warehouse_site_issue_bridge.py:362`）。新执行集合须排除 delivery 来源身份碰撞及相互冲突的原始单头身份；仅单号关联、未知来源关联保留提示。
- 复用 `maintenance_demands.void_fast`（`:1278`）创建墓碑、停用挂靠、实名审计。恢复仍为管理员实名动作（`backend/app/api/maintenance_demands.py:376`）；不更改 D-12 权限。
- 旧缺项确认重放会因恢复或后加行而 409（`maintenance_wbdd_sync.py:93`），不符合本次“返回原结果”，故新命令独立保存回执，重放不再写业务状态。
- 项目权限复用 `backend/app/api/maintenance_project_scope.py:13`、`:49`；不能把未开行键的非管理员 `maintenance_scope_project_ids=None` 误当全范围。实名复用 `maintenance_demands.py:74`。

## 三路结论及实施边界

独立三路只读检查覆盖用户场景、业务边界及技术数据。采用上述证据，尚无口径分歧。

1. 新状态证据模块，由 reader 填头后的数据捕获，在成功 loader 后绑定实际已应用单头；保存在批次私有 JSONB。skip 永不具执行资格，历史无证据需显式重传。无新表、无迁移、无历史回填。
2. 新取消服务与 API，项目内分页列表、具体候选详情、逐候选人工确认。列表包括未知非生效告警与证据不足提示，草稿/进行中/空不作为取消候选。取消不走缺项算法。
3. 当前头批次、不可变证据、项目/归属、全部需求行、精确/未精确领用及成本证据共同绑定摘要。确认持全局数据锁→项目状态/项目→单头→归属→需求行→领用，锁后新鲜复验。漂移 409，事务整体回滚。
4. D-12 动作权和实名用户必需，锁内复验项目访问。批次私有回执绑定操作者、候选、摘要、原因及幂等键；同请求返回原确认结果（明确历史），不再处理后来数据。
5. 软作废需求与精确领用、整单墓碑、P3 corrections/version、审计同事务。返还、库存、采购、报销、回款及成本证据不删除不冲减。来源回到已生效只恢复尚未系统作废的需求可见性。
6. 前端独立影响卡片放项目“备件与需求单”，展示具体影响后填写原因确认。未知状态只读，409 强制刷新重核；网络结果未知保留原请求重试；320/390px 可用。

## 公共 seam 与验收场景

沿用户要求与开发技能自主确定：`pipeline.run_import`（原件证据）、项目取消 HTTP 列表/详情/确认（鉴权/事务）、既有有效需求统计/查询、前端组件交互。真实独占 PostgreSQL，不用 SQLite 替代锁/迁移验证。

| 起点/操作 | 预期 |
|---|---|
| 有归属需求与精确、仅单号领用，upsert 已取消 | 列候选，确认前领用保留；详情分清两类 |
| 实名有权用户填写原因确认 | 只软作废需求及精确领用；返还/库存/历史成本不变 |
| 草稿/进行中/空；未知“已作废” | 前者无取消候选，后者告警不可执行 |
| skip 取消文件或旧历史无证据 | 不生成可执行候选，提示显式 upsert |
| 头/批次/需求/领用/归属/项目权限漂移 | 409 或越项目 403，整次零写 |
| 同请求再次确认，期间新增/恢复行 | 返回首次结果，不再作废 |
| 未确认取消后重传已生效 | 有效需求重新可见；已确认行/墓碑不复活 |
| 无权或跨项目 | 403；无业务写入 |
| 安装/升级代码 | 不处理历史 76 单；无迁移回填 |

## 待金臣确认

目前无新增业务口径。若发现可靠关联证据不足，只保留并提示，不猜测执行。

## 验证记录

所有测试串行，pytest 无多进程；前端 maxWorkers=2。专用 PostgreSQL 15 容器 `itsp-laneB-cancel-20261020` 绑定本机随机端口 `32877`。日志均放工作树外 `~/workspaces/reviews/lanes-20261020/laneB/`。

## 最终接口与实现位置

- `GET /api/maintenance/projects/{project_id}/source-cancellations?page=1&page_size=20`：只读分页，最多100条；当前归属下的已取消和未知非生效状态。旧证据不足可查看但不可执行。
- `GET /api/maintenance/projects/{project_id}/source-cancellations/{batch_id}/{source_order_id}`：影响详情与 `expected_digest`，包括需求行、精确领用、无法精确关联项、可读领用号/日期/关联依据；不暴露私有快照或成本字段。
- `POST .../{batch_id}/{source_order_id}/confirm`：`expected_digest/reason/idempotency_key`；每次一张明确选择的候选，同单影响全部成功或全部回滚。回执包含计数、首次确认时间及 `replayed`；幂等键绑定实名人及完整请求，跨批次误用返回409；已消费的候选换键也不能再执行。
- 状态证据捕获/固化：`backend/app/services/maintenance_source_status_snapshot.py:28`、`:70`，pipeline 两个既有位置最小接线；不修改 loader 算法。
- 只读候选与摘要：`backend/app/services/maintenance_source_cancel_cascade.py:73`、`:87`；确认锁链/软作废/回执：`:166`。摘要另外覆盖受影响领用单全部兄弟行，避免兄弟新增改变整单作废结果。
- API 与锁内新鲜鉴权：`backend/app/api/maintenance_source_cancel_cascade.py:63`。沿用项目访问守卫、账号实时权限和实名来源；同键回放先鉴权，再返回历史回执，不要求首次作废后已停用的归属仍活跃。
- 确认不向 P2 局部编辑原语塞 `is_active`（它不支持删除）。复用现有 P3 `snapshot/record_corrections` 推进版本和事件，再落完整前后审计；`void_fast` 不传内层幂等键，避免 key→global 与 global→key 反序，外层回执承担完整幂等。
- 前端 API 与 `SourceCancelImpactPanel` 均为新模块，项目 `PartsOrdersTab` 仅7行接线；页面一次确认一张，不承诺多个页面动作跨单事务。

## 开发验证过程

红→绿证据均保存在工作树外日志目录：

- 后端首测缺少状态证据和候选路由，2红；实现后2绿。
- 确认测试先修正违反既有成本配对约束的合成夹具，再确认路由404红；实现后状态证据/基本确认共18绿。
- 扩展边界首轮32绿、2归属夹具失败：触发器要求归属代际停用同步 version+1，已修正。公共归属入口仅接受已生效头，纯归属漂移反例使用遵循触发器的合成历史换代；确认仍走真实 HTTP，未放宽生产守卫。
- 前端先组件缺失红，再逐轮确认必填/409/网络未知重试转绿。相关4文件112绿。独立复核发现迟到列表解除409门禁、分页收缩困空页、Axios异步取token跨账号窗口，新增3红后修复，新2文件16绿。
- 新后端公共契约52项（状态证据16、取消API32、真实PG并发4），新前端16项。真实并发测试用 `pg_locks/pg_blocking_pids` 观察等待，未用固定延时冒充锁生效；数据库写入故障注入核对事实、审计、outbox、墓碑、revision与回执整事务回滚。
- Ruff 对6个新Python文件通过，按仓库 FastAPI 既有风格将 `fastapi.Depends` 作为不可变默认调用；`git diff --check` 通过。未对旧大模块做格式重写。
- Playwright 开发验证使用本机 Vite 和合成 API 响应，直接渲染真实组件；不宣称后端集成或用户验收。320px 页面宽320、对话框296；390px 页面宽390、对话框366，无页面横向溢出，表格内部可横滚。长PN可换行，空原因为禁用确认，填写原因后确认可用，320px按钮可滚动到屏幕内。截图 `cancel-detail-320.png`、`cancel-detail-390.png`、`cancel-confirm-320.png` 位于外部日志目录。本任务Vite进程已停止。

## 最终验证与交付

- 后端相关8文件：109 passed，288.54秒，退出码0；`backend-subset-final.log`。覆盖本次52项及既有有效读、缺项确认、字段保护和来源快照契约。
- 前端完整链 `npm ci && npx tsc && npx vitest run --maxWorkers=2 && npx vite build`：124文件、1563项通过（668.48秒），TypeScript与构建通过，整链退出码0；`frontend-full.log`。Vite既有大包提示保留，未改构建策略。
- 后端全量按后续 lane 新规则由执行方中止：1044 passed、2 skipped、0 failed，2296.83秒（38分16秒，约17%），日志明确收到 SIGTERM；`backend-full.log`。这不是后端全量通过，剩余全量由 CI 验证，不再本地重跑。
- 按用户收尾要求，仅再执行一次提交前定向验证：3个新增后端测试文件及直接相关的 `test_wbdd_source_snapshot_pipeline.py`、`test_import_platform.py`；API新增测试通过真实路由覆盖 `main.py` 接线。5文件75项通过，234.75秒，退出码0；`backend-precommit.log`。提交前 Ruff 新增6个Python文件和 `git diff --check` 均通过。
- 无新增迁移，未处理历史取消单；`alembic heads` 实测仍为 `c9d4f2a6b8e1`。交付后仍待用户实际点按验收。
- 本地代码提交：后端 `364759a`，前端 `a665f87`；文档另行提交到同一分支。未推送、未开 PR、未访问生产；egg-info、日志和截图均不提交。

## 建议 PR

标题：`feat: 增加已取消需求影响清单与实名连带作废`

正文：

> 决策: D-34
>
> 来源需求单上传为“已取消”后，项目面板展示受影响需求、精确关联领用和保留项。仅成功 upsert 批次的不可变状态证据可形成可执行候选；未知非生效状态只告警，skip 不生成候选。
>
> 实名有权用户核对具体候选并填写原因后，锁内重新鉴权和校验摘要，整次软作废需求及精确领用。漂移返回409且零写；重复请求返回原回执，不处理后来新增或恢复数据。返还、库存及历史成本证据保留。来源合法重传为已生效只恢复未系统作废需求的有效视图。
>
> 无 schema 迁移、无历史76单自动处理。本候选叠在 PR #425 的 `0ff4ffe` 上，不携入 test 实验；待前置 PR 合入 main 后检查差异及候选 SHA。
>
> 新增后端52项、前端16项契约测试。前端完整检查链通过（1563项）；后端相关回归109项、提交前定向验证75项通过，Ruff与差异格式检查通过。按 lane 新规则，后端全量由 CI 验证；本地全量在1044通过、2跳过、0失败时由执行方中止，不作为全量通过证明。用户点按验收与管理员对当前 SHA 的人工批准仍待进行。
