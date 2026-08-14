# 维保项目回款计划提醒设计

- 状态：设计草案已完成，等待用户书面复核
- 日期：2026-08-14
- 设计评审基线：`c431656bd2615102f053199801554191b2d88791`
- 主工作区起始 HEAD：`d7168ce3350a7524b4e1e0b28e8e115f7d8cdb80`；实现前在主工作区保留并提交 `.ai` 后，显式对齐上述设计评审基线
- 数据库基线：Alembic `d9f1a3c7e5b2`
- 样例文件 SHA-256：`a783af09fa108d366a26e10fe188be52d20a9ce1fe02121bfd683d96356c8c18`
- 视觉方向：现有 Ant Design 暖白主题下的主从分栏；左侧项目列表，右侧计划节点

## 1. 结论先行

本功能只解决一个问题：让维保负责人看见项目的计划回款月份，并能人工结束、改期或重新打开一条提醒。

它不判断是否真实到账，不连接银行或财务，不生成实收、待收、到账率、核销或凭证事实。界面中的“已处理”只表示这次提醒已经跟进完毕。

最小闭环是：

`项目经理 XLS → 零领域事实写入预览 → 人工确认订单与项目/合同绑定 → 24 组宽列转纵向计划节点 → 本月/逾期提醒 → 标记已处理或改期 → 审计记录`

## 2. 样例事实

只读画像显示：

- 工作簿有 3 个可见 Sheet；本功能只读取第一张项目表。
- 第一张表为 64 列，包含 24 组交替排列的“回款时间 N / 回款金额”。
- 3 个项目行共产生 19 组完整日期金额对；没有日期孤儿、金额孤儿或期次断档。
- 19 个时间值均为 `YYYY年M月` 文本，只能表达月份，不能表达具体到账日。
- 每个项目的计划金额合计等于订单金额和待收尾款；“已收尾款”均为零。
- 19 个计划月份均晚于样例生成日，因此这些字段是计划排期，而不是历史到账流水。
- 数据区颜色和条件格式不承载业务状态；颜色不得作为导入字段。
- “订单编号”在样例中非空且唯一，是外部订单候选键；项目名称和人员姓名不是关系键。
- 第二张表是费用样例，不进入本功能。

因此，导入后的业务语义固定为 `planned_collection_milestone`，禁止生成 `actual_receipt`。

## 3. 已有能力与真实缺口

### 3.1 复用能力

基线已经具备：

- `MaintenanceCollectionMilestone`：按项目合同和期次保存纵向计划节点。
- 项目、合同、维保负责人、项目范围权限和金额可见性。
- 经理工作簿的签名、差异预览、原子应用、版本并发和审计模式。
- 项目目录的本人/全部范围、任务类型、任务状态和到期范围筛选。
- 项目工作台、系统提醒、合同列表和项目详情路由。
- Ant Design 5、现有暖白主题、响应式抽屉和前端错误态模式。

### 3.2 必须补齐

基线尚不具备：

1. 项目全部计划节点的专用读取接口；当前只返回最近一个节点。
2. 人工“标记已处理、改期、重新打开”的持久化合同。
3. 月份精度字段；当前模型保存具体日期并按天计算逾期。
4. 当前提醒关闭依赖财务累计实收，和本设计的 reminder-only 口径冲突。
5. 当前远端基线没有可复用的“销售订单编号 → 项目/合同”稳定绑定。
6. 当前远端基线没有本样例 `.xls` 的正式解析、预览和应用链。

## 4. 业务对象与状态

### 4.1 计划节点

继续使用 `maintenance_collection_milestone` 保存计划事实，并做加法式扩展：

| 字段 | 语义 |
|---|---|
| `milestone_id` | 节点稳定 ID |
| `project_id` | 维保项目稳定 ID |
| `project_contract_id` | 项目合同关系稳定 ID |
| `sequence` | 合同期次，范围 1–24 |
| `planned_date` | 规范化日期；月份型来源保存当月 1 日 |
| `date_precision` | `day` 或 `month`；本样例固定为 `month` |
| `planned_amount` | 计划金额；不推断含税或未税口径 |
| `completeness_state` | 日期/金额字段完整度，不是提醒处理状态 |
| `source` | `direct_api`、`manager_workbook_v3` 或 `project_manager_xls_v1` |
| `source_batch_id` | 现有经理工作簿批次；仅 `manager_workbook_v3` 使用 |
| `collection_plan_import_batch_id` | 仅 XLS 来源使用的导入批次；其他来源为空 |
| `follow_up_status` | `pending` 或 `handled` |
| `follow_up_review_required` | 已处理节点的计划事实后来发生变化时为 `true` |
| `follow_up_note` | 最后一次处理说明，可空 |
| `followed_up_by` | 最后处理账号 |
| `followed_up_at` | 最后处理时间 |
| `version` | 乐观锁版本 |

约束：

- 唯一键继续使用 `(project_contract_id, sequence)`。
- `handled` 必须同时具有 `followed_up_by` 和 `followed_up_at`。
- `pending` 不得保留伪造的处理人和处理时间。
- `follow_up_review_required=true` 必须同时满足 `follow_up_status=handled`，由数据库 CheckConstraint 强制。
- Excel 再导入可以更新计划月份和金额，但不得自动清除人工处理状态。
- `source=project_manager_xls_v1` 时必须有 `collection_plan_import_batch_id`，且原有 `source_batch_id` 为空；`manager_workbook_v3` 继续只使用原有批次外键；`direct_api` 的两个批次字段都为空。迁移给存量节点回填 `date_precision=day`、`follow_up_status=pending`、`follow_up_review_required=false`。
- 已处理节点的月份或金额被新批次修改时，保留 `handled` 并设置 `follow_up_review_required=true`；`reopen` 清除此标记并重新进入提醒队列。

### 4.2 操作账本

新增不可变的 `maintenance_collection_milestone_operation`：

| 字段 | 语义 |
|---|---|
| `operation_id` | 操作 ID |
| `milestone_id` | 目标节点 |
| `action` | `handle`、`reschedule` 或 `reopen` |
| `idempotency_key` | 客户端重试幂等键 |
| `expected_version` | 客户端看到的版本 |
| `result_version` | 成功后的版本 |
| `payload_hash` | 幂等请求的规范化摘要 |
| `before_payload` | 变更前受控字段快照 |
| `after_payload` | 变更后受控字段快照 |
| `result_json` | 首次成功响应，用于相同请求安全重放 |
| `reason` | 改期和重新打开时必填 |
| `actor_user_id` | 实名操作者 |
| `created_at` | 操作时间 |

`idempotency_key` 全局唯一。同一幂等键和相同 `payload_hash` 返回首次 `result_json`；同一幂等键配不同请求返回 `409`。账本由数据库 trigger 禁止 UPDATE/DELETE。

### 4.3 提醒状态

颜色不入库。服务端根据当前日期、日期精度和 `follow_up_status` 派生：

| `reminder_state` | 判断 | 界面 |
|---|---|---|
| `needs_review` | 已处理节点后来被导入修改 | 紫色“计划有变更” |
| `handled` | 已人工处理 | 绿色“已处理” |
| `overdue` | 待处理且计划月份早于当前月份 | 红色“已逾期” |
| `due_this_month` | 待处理且计划月份等于当前月份 | 琥珀色“本月跟进” |
| `upcoming` | 待处理且计划月份晚于当前月份 | 灰色“待到期” |
| `incomplete` | 日期或金额不完整 | 橙色“信息待补” |

派生优先级固定为 `needs_review > handled > incomplete > overdue > due_this_month > upcoming`。`reschedule` 是操作记录，不是永久状态；改期成功后，节点继续按新月份进入待到期、本月或逾期。

日期比较口径：

- `date_precision=month`：只比较 `YYYY-MM`；早于当前自然月为逾期，等于当前月为本月跟进，晚于当前月为待到期。
- `date_precision=day`：先比较具体日期；早于 `as_of` 当日为逾期，当日或本月内未来日期为本月跟进，下月及以后为待到期。
- 两种精度都由服务端使用同一个显式 `as_of` 派生，前端不得按浏览器时区重新计算。

### 4.4 导入批次与原文件证据

新增 `maintenance_collection_plan_import_batch`，不复用会执行通用 loader 的 `sys_import_batch`：

| 字段 | 语义 |
|---|---|
| `batch_id` | 预览和应用共用的稳定 ID |
| `owner_user_id` | 创建预览的实名账号 |
| `contract_version` | 精确 XLS 表头合同版本 |
| `file_sha256` / `file_size` | 原文件不可变指纹和资源预算证据 |
| `original_filename` / `storage_key` | 受控上传存储中的原文件证据；不写日志 |
| `operation_key` | 文件与操作者作用域内的幂等键 |
| `semantic_hash` / `data_version` | 规范化计划与绑定基线摘要 |
| `apply_payload_hash` | 首次应用的 expected versions 与 bindings 规范化摘要 |
| `version` | 批次乐观锁版本 |
| `status` | `valid`、`error`、`applied` 或 `expired` |
| `plan_json` / `issues_json` | 脱敏差异计划与阻断/警告 |
| `result_json` | 首次应用结果 |
| `created_by/at`、`expires_at`、`applied_by/at` | 审计与有效期 |

原始 `.xls` 保存在受控 uploads 存储中并由批次引用；预览失败也保留哈希和受控原件证据，不把业务行或原文件名写入应用日志。应用代码不得删除已有上传文件。原件只允许具备导入权限的实名 admin 通过审计下载接口访问，响应强制 attachment disposition；存储按不可猜测 `storage_key` 寻址，禁止按原文件名直接访问。

## 5. 外部订单绑定

由于冻结基线没有可复用的销售订单稳定绑定，新增专用的 `maintenance_collection_plan_source_binding`：

| 字段 | 语义 |
|---|---|
| `source_system` | 固定 `project_manager_xls_v1` |
| `external_order_no` | Excel“订单编号”的规范化精确值 |
| `project_id` | 人工确认的项目 |
| `project_contract_id` | 人工确认的项目合同关系 |
| `binding_status` | 固定 `reviewed` |
| `reviewed_by` / `reviewed_at` | 绑定审核人和时间 |
| `version` | 乐观锁版本 |

规则：

- `(source_system, external_order_no)` 唯一。
- 订单号规范化只移除首尾空白；不改变大小写，不删除内部空白或标点。批次证据保留原始单元格值，正式绑定只保存规范化精确值。
- 项目名称、负责人姓名、客户名、日期范围和相似度不得自动建立关系。
- 未绑定订单在预览中必须人工选择项目和合同后才能应用。
- 绑定必须验证合同当前属于所选项目；改派必须留下审计记录。
- 改派和首次绑定复用 `maintenance_project_operation_audit`，记录受控 before/after、理由和实名操作者；不得在普通日志回显项目名称或原始 Excel 行。

## 6. XLS 导入合同

### 6.1 范围和依赖

- 使用专用 `.xls` 适配器，新增并锁定直接依赖 `xlrd==2.0.2`，同步更新 `pyproject.toml`、`uv.lock`、`requirements.lock`、egg-info 和 SBOM。
- 只读取第一张 Sheet；第二张费用表和空 Sheet 不进入本次预览。
- 最大文件、Sheet、行列和字符串长度必须设置资源预算。
- `xlrd` 不计算或执行公式，只会暴露工作簿保存的缓存结果；首期按缓存的 text/number 值预览，不宣称能够证明单元格不是公式。管理员必须在差异预览中确认计划。
- BIFF numeric cell 由 `xlrd` 暴露为 Python `float`；读取后必须立即使用 `Decimal(str(cell.value))`，不得用 float 做金额计算或直接持久化，且禁止静默四舍五入。

### 6.2 版本识别

机器可读合同固定在 `.ai/contracts/maintenance-collections/project-manager-xls-v1.yaml`，其表头签名算法和 64 个有序标签是唯一版本判断依据。正式合同锁定：

- 64 列有序表头。
- `订单编号、项目名称、项目经理、订单金额、已收尾款、待收尾款` 的精确位置。
- 24 组交替排列的 `回款时间 N` 和相邻 `回款金额`。
- 同一行最多 24 个计划节点。

表头漂移时整个文件失败关闭，不根据相似列名猜测。

Excel 财务列仅用于导入校验：`订单金额` 只产生计划合计警告；`已收尾款` 不参与关闭、处理或提醒状态；`待收尾款` 不进入系统待收事实。三者均不得写入 actual receipt、received amount、到账率或核销模型。

### 6.3 预览

`POST /api/maintenance/collection-plan-imports/preview`

预览零领域事实写入，只允许创建导入批次和不可变原文件证据。它不得写项目、合同绑定或计划节点。返回：

- 文件哈希、合同版本、项目行数、计划节点数。
- 已绑定、待绑定和阻断项目数量。
- 每个订单的项目/合同绑定结果。
- 每个节点的 `create/update/unchanged/source_missing` 差异。
- 日期金额不成对、重复订单、非法月份、非正金额等阻断项。
- 计划合计与订单金额不一致的非阻断警告。

预览响应还必须返回 `batch_version`、`data_version`、每个待绑定订单的稳定 `row_key`，以及可提交的绑定选择结构。批次内不可变 `plan_json` 记录本批涉及的项目、合同、现有绑定和计划节点 `expected_version`，`data_version` 对这些版本做统一摘要。项目/合同候选通过批次内的受控搜索接口读取，不能把全量项目塞入预览响应。

### 6.4 应用

`POST /api/maintenance/collection-plan-imports/{batch_id}/apply`

- 仅 admin 且具备专用导入权限。
- 要求预览批次未过期、文件哈希一致、既有绑定均为 reviewed，且每个待绑定订单都有本次人工选择。
- 一次事务原子应用；任何阻断项使整批零业务写入。
- 同文件、同语义内容重复应用保持幂等。
- 请求携带 `expected_batch_version`、`expected_data_version` 和用户审核后的 `bindings[]`；绑定选择在预览阶段只保存在浏览器内，首次领域写入发生在 apply，绑定与计划节点在同一事务内写入。
- apply 只消费批次中不可变的规范化计划，不重新解析客户端文件；按稳定顺序锁定项目、合同、现有绑定和节点并逐一比对预览版本，任何漂移整批返回 `409` 且零领域事实写入。
- 首次 apply 将 `expected_batch_version + expected_data_version + bindings[]` 规范化为 `apply_payload_hash`；已应用批次仅在摘要相同时返回首次 `result_json`，摘要不同返回 `409`。
- 以 `(project_contract_id, sequence)` 创建或更新节点。
- 新文件缺失旧节点时只报告 `source_missing`，不自动删除、取消或关闭。
- 已处理节点的计划被修改时保留 `handled`，持久化 `follow_up_review_required=true`，并在结果中返回 `needs_review` 数量。
- 通过版本校验后，后发生的显式操作生效。导入预览必须明确展示将覆盖人工改期的差异，用户再次确认 apply 后才可覆盖；旧预览因版本漂移一律 `409`。

### 6.5 绑定候选搜索

`GET /api/maintenance/collection-plan-imports/{batch_id}/binding-options?q=...`

- 仅批次所有者或具备同一导入权限的管理员可用。
- 返回项目稳定 ID、项目编号、项目当前版本，以及该项目下的合同稳定 ID/编号/有效性/当前版本；不做推荐分数。
- 用户必须明确选择一个项目和该项目下一个合同。项目不存在、合同不属于项目、批次过期或权限撤销时失败关闭。

## 7. 读取与操作 API

### 7.1 项目列表

`POST /api/maintenance/collection-reminders/search`

请求支持：

- `q`：项目编号、项目名称、合同编号。
- `owner_scope`：`me`；只有后端授予完整范围能力的账号可使用 `all`。
- `reminder_state`：全部、计划有变更、信息待补、逾期、本月、待到期、已处理。
- 分页和排序。

默认排序：计划有变更、逾期、本月、信息待补、待到期、已处理，再按计划月份和项目编号。

响应 DTO 固定为：

- 顶层：`rows`、`total`、`page`、`page_size`、`owner_scope`、`allowed_owner_scopes`、`as_of`、`data_version`、`amount_visibility`。
- 项目：`project_id`、`project_code`、`display_name`、`lifecycle_status`、`manager_assignment`、`service_period`、`contracts[]`。
- 摘要：`next_actionable_milestone` 和 `reminder_counts`，后者包含 `total/needs_review/incomplete/overdue/due_this_month/upcoming/handled`。
- `next_actionable_milestone` 按 `needs_review > overdue > due_this_month > incomplete > upcoming`、再按计划月份和期次确定；不得把普通已处理历史节点选为“下一条”。
- `amount_visibility` 为 `visible` 或 `restricted`；受限时所有计划金额为 `null`，不能只靠前端隐藏。
- `allowed_owner_scopes` 由后端按当前账号范围返回，只含 `me` 或 `me/all`；前端不得再用角色名推断。
- `contracts[]` 是 reminder-only 最小结构，只含 `project_contract_id/contract_no/relation_status/lifecycle_status`；不得携带 received、receivable、rate、receipt reference 或其他实收字段。

### 7.2 项目计划详情

`GET /api/maintenance/projects/stable/{project_id}/collection-milestones`

顶层返回 `project`、`summary`、`rows`、`as_of`、`data_version` 和 `amount_visibility`。其中：

- `project`：`project_id/project_code/display_name/lifecycle_status`、`manager_assignment`、`service_period`、`contracts[]`。
- `summary`：与列表相同的七类 `reminder_counts`。
- `rows` 中每行返回：

- `milestone_id`
- `project_contract_id`、`contract_no`
- `sequence`
- `planned_date`、`date_precision`、`planned_month`
- `planned_amount`；以十进制定点字符串返回，无金额权限时为 `null`
- `completeness_state`
- `follow_up_status`、`reminder_state`
- `follow_up_review_required`
- `followed_up_by`、`followed_up_at`、`follow_up_note`
- `last_operation`：`operation_id/action/reason/actor_display_name/created_at/result_version`；无记录时为 `null`
- `version`

### 7.3 节点操作

`POST /api/maintenance/collection-milestones/{milestone_id}/follow-ups`

统一请求字段：

- `expected_version`
- `idempotency_key`
- `action`
- `planned_month`，仅改期使用，格式固定 `YYYY-MM`
- `note` 或 `reason`

规则：

- `handle`：将提醒标记为已处理，可选备注。
- `reschedule`：仅待处理且完整的节点可用；新计划月份和理由必填，服务端规范化为当月 1 日并保存 `date_precision=month`，计划金额不在首期页面编辑。
- `reopen`：仅已处理节点可用，理由必填。
- `incomplete` 节点首期只读，不允许标记已处理或改期；先通过受控来源补齐日期和金额。
- `needs_review` 节点只允许 `reopen`；重新打开后才可再次处理或改期。
- 月份型节点只按自然月比较，不产生或展示伪造的“逾期天数”。所有 `reason` 去除首尾空白后必须非空；服务端按 action 判别联合校验字段，拒绝无关字段。
- 版本冲突返回 `409` 并要求刷新，不静默覆盖。
- 跨项目、越权节点返回 `404` 或 `403`，不得泄漏不可见项目。

## 8. 前端设计

本文 API 路径使用服务端完整路径 `/api/maintenance/...`。前端 Axios 已配置 `baseURL=/api`，实现时只传 `/maintenance/...`，禁止形成 `/api/api/...`。

### 8.1 路由与结构

- 新路由：`/maintenance/beta/collection-reminders`
- 菜单：`维保工作台 / 回款提醒`
- 导航同时声明 `perm: page_maintenance_beta` 和 `betaFeature: maintenance`，并更新现有导航精确数组测试。
- 桌面端采用主从分栏：左侧约 38%，右侧约 62%。
- 768–1199px 调整为约 42% / 58%，右侧表格自身横向滚动。
- 小于 768px 先显示项目列表，选择项目后使用现有 `MobileDetailDrawer` 打开详情。
- 分栏容器及两侧子项设置 `min-width: 0`；计划表只在自身启用 `scroll.x`，页面外层禁止横向滚动；移动端详情抽屉使用全屏高度。

### 8.2 左侧项目列表

字段：

- 项目名称 `display_name`
- 项目编号 `project_code`
- 维保负责人 `manager_assignment.display_name || username`
- 关联合同编号
- 下一条可跟进节点的计划月份、期次和提醒状态

控件：

- 搜索：项目编号、项目名称、合同编号。
- 状态：全部、计划有变更、信息待补、已逾期、本月跟进、待到期、已处理。
- 负责人范围：默认本人项目；只有后端返回完整范围能力时显示“全部项目”。

选择和请求规则：

- 初次加载自动选中当前结果第一页第一项。
- 搜索、筛选或翻页使当前项目离开结果集时，选择新的第一项；结果为空时清空右侧。
- 切换项目时取消或忽略旧详情请求，响应必须校验 `project_id`，禁止慢请求覆盖新选择。

### 8.3 右侧详情

项目头：

- 项目名称、项目编号、维保负责人、维保期限、关联合同。
- 次按钮“查看完整项目”。
- 固定提示：“已处理仅表示本次提醒已完成，不代表财务确认到账”。

紧凑指标：

- 计划节点数
- 计划有变更；仅数量大于零时突出显示
- 本月待跟进
- 已逾期
- 已处理

计划表列：

- 合同编号
- 期次
- 计划月份
- 计划金额
- 提醒状态
- 最近处理记录
- 操作

行操作：

- `overdue/due_this_month/upcoming`：`标记已处理`、`改期`。
- `handled`：`重新打开`。
- `needs_review`：只显示 `重新打开` 和“计划有变更”提示。
- `incomplete`：只读，提示先补齐计划字段。
- 无写权限：只读，不渲染操作按钮。

### 8.4 页面级导入流程

“导入回款计划”位于页面级工具栏，不属于某个项目头，并且只在 `canImportCollectionPlan=true` 时显示。弹窗使用四步流程：

1. **选择文件**：只接受 `.xls`；展示文件名、大小和“上传后先预检、不直接修改计划”。
2. **解析预览**：展示项目数、节点数、已绑定/待绑定/阻断数量，以及逐订单节点 `create/update/unchanged/source_missing` 差异。
3. **审核绑定**：对每个待绑定订单搜索并明确选择项目和合同；显示阻断项与非阻断警告，阻断未清零时禁用应用。
4. **确认应用**：提交 `batch_id + expected_batch_version + expected_data_version + bindings[]`；成功展示新增、更新、未变、来源缺失和计划变更待复核数量。相同批次重放展示首次结果。

`bindings[]` 判别结构固定为 `row_key/external_order_no/project_id/project_version/project_contract_id/project_contract_version/existing_binding_version/reason`。新绑定的 `existing_binding_version=null`；改派必须填写非空理由。apply 仍重新校验项目、合同有效性、归属和版本。

解析中、上传失败、批次过期、403、409 和应用失败均保留已完成步骤；409 要求刷新预览，不静默覆盖。弹窗不得展示原始整行 JSON。

### 8.5 前端能力与统一文案

`MaintenanceCapabilities` 增加：

- `canViewCollectionReminders`
- `canFollowUpCollection`
- `canImportCollectionPlan`
- 继续使用 `canViewContract` 控制计划金额

页面入口看 `canViewCollectionReminders`；行按钮只看 `canFollowUpCollection`；导入按钮只看 `canImportCollectionPlan`。所有页面标题、筛选标签、状态、按钮、空态和提示先写入 `components/maintenance/maintenanceLanguage.ts`，组件不得散落硬编码中文。

金额接口保留十进制定点字符串，前端新增只接受 `string | null` 的金额格式化器；它只做合法十进制校验、展示和脱敏，不把金额传给现有只接受 `number` 的 `money()`，也不经 JavaScript 浮点参与合计。

标记、改期、重新打开或导入应用成功后，前端重新请求当前筛选下的项目列表和当前项目详情，并按选择规则重新定位。列表计数、排序和筛选命中不得只靠局部 optimistic update 猜测。

### 8.6 明确删除的假口径

页面不得出现：

- 已到账、确认到账、实收、待收、回款率、到账率。
- 财务确认、上传凭证、核销或撤销到账。
- 客户单位、联系人、电话、催收次数。
- 含税或未税标签；来源没有批准的税口径。
- 项目经理作为独立角色；统一使用“维保负责人”。

## 9. 权限

- 页面读取依赖 `page_maintenance_beta`、maintenance Beta 开关和项目行级范围。
- 非完整范围角色默认且只能查看本人负责项目；是否具备完整范围由后端能力返回，不在前端硬编码角色名。
- 计划金额继续服从现有利润/合同金额可见性；无权限时仍可查看月份和提醒状态。
- 新增 `action_maintenance_collection_follow_up` 控制标记、改期和重新打开。
- 新增 `action_maintenance_collection_plan_import` 控制预览、绑定候选查询和应用；首期仅实名 admin，且同时要求 `data_profit`。
- 两个新增 action 在所有权限模板中默认 `false`，都依赖 `page_maintenance`、实名白名单 `page_maintenance_beta` 和 maintenance Beta 开关。
- `canImportCollectionPlan` 禁止使用普通 `isAdmin || permission` 短路；必须同时满足 Beta 能力、`role=admin`、后端显式返回 `action_maintenance_collection_plan_import=true`、实名账号校验和 `canViewContract=true`。服务端在 preview、候选搜索和 apply 时重新做同样门禁。
- 所有写操作在执行前重新校验账号、项目范围、合同归属和版本，避免 TOCTOU。

## 10. 失败与恢复

| 场景 | 行为 |
|---|---|
| 项目列表加载失败 | 显示错误和重试，保留筛选条件 |
| 详情加载失败 | 左侧仍可用，右侧显示重试 |
| 403 | 显示权限提示，不回显被拒数据 |
| 409 | 提示“数据已变化，请刷新”，不覆盖 |
| 项目没有计划 | 有导入能力时提供“导入回款计划”；否则只提示“当前项目暂无回款计划，请联系管理员导入” |
| 文件表头漂移 | 整个预览失败关闭 |
| 订单没有绑定 | 进入人工项目/合同选择，不能应用 |
| 一个订单有多个候选 | 人工选择，不能按顺序取第一个 |
| 导入缺少旧节点 | 只告警，不删除生产数据 |
| 已处理节点被新计划修改 | 保留已处理事实，标记“计划有变更”，由用户重新打开 |

## 11. 测试与验收

### 11.1 后端

- 精确识别 24 组交替列并解析 19 个样例节点。
- 月份精度保持为 `month`，不伪造到账日。
- 日期金额孤儿、非法月份、非正金额和重复订单失败关闭。
- 零个/多个绑定拒绝应用；项目名称相同也不得自动匹配。
- 同文件和同幂等键重放不重复写入。
- 同幂等键不同请求、版本冲突返回 `409`。
- 标记、改期、重新打开留下不可变操作记录。
- 导入不得覆盖已处理状态，不得删除缺失节点。
- `manager_workbook_v3` 新建节点统一写 `date_precision=day`；任一受控写入者修改 handled 节点的日期或金额，都必须设置 `follow_up_review_required=true`。
- 目录和详情提醒不再依赖财务累计实收。
- 跨项目 IDOR、撤权和合同改派在执行时失败关闭。

### 11.2 前端

- 默认选择第一条可见项目，只刷新右侧详情。
- 切换项目、筛选和翻页时旧详情请求不能覆盖当前选择。
- 搜索、状态筛选和本人/全部范围参数正确。
- 标记成功后状态变绿、按钮切换为“重新打开”。
- 改期要求新月份和理由。
- 无权限账号看不到写按钮和受限金额。
- incomplete 节点无处理按钮；needs_review 节点只能重新打开。
- 导入四步流程覆盖待绑定、阻断、警告、确认和重放结果；阻断未清零时不能应用。
- loading、空数据、403、409、500 均有明确状态。
- 页面不存在禁用的财务到账文案。
- 390、768、1024、1440 宽度无页面级横向滚动。

### 11.3 成功标准

使用已授权的真实账号可以完成：

1. 导入样例并预览 3 个项目、19 个计划节点。
2. 为未绑定订单人工选择项目和合同。
3. 原子应用后在回款提醒页看到纵向计划。
4. 标记一条提醒已处理，刷新后状态和处理人保持。
5. 将一条待处理节点改期，刷新后按新月份重新计算状态。
6. 重新打开误处理节点，重新进入提醒队列。
7. 导入修改一条已处理节点后，刷新仍显示“计划有变更”；重新打开后标记消失且节点重新进入提醒队列。

## 12. 发布边界

- 本设计是独立业务切片，不和备件仓储 G1a/G2、费用导入或财务实收一起发布。
- 新迁移从冻结基线 `d9f1a3c7e5b2` 追加，禁止在脏生产 checkout 构建。
- 主工作区实现前必须先提交正式 `.ai` 资产，再将当前分支从 `d7168ce3` 显式对齐到 `c431656b`；对齐时保留 `.ai`，不得复活已从远端删除的旧氚云项目导入链。
- 初始仍在 maintenance Beta 门内，权限和导入 allowlist 默认关闭。
- 合并前需要后端、前端、迁移和独立 reviewer 全绿。
- 生产前需要全量 DB＋uploads 备份、隔离恢复演练、迁移 rehearsal、真实账号 canary 和观察窗口。
- 本设计批准不授权直接部署生产。

## 13. 实施分片

实现顺序固定为：

1. 数据库迁移、模型和操作账本。
2. reminder-only 服务与读取/写入 API。
3. `.xls` 零写入预览、人工绑定和原子应用。
4. 前端 API 类型、主从页面、弹窗和导航。
5. 聚焦测试、全量回归、响应式验收和独立审查。
6. 合并候选；生产发布另行执行正式门禁。
