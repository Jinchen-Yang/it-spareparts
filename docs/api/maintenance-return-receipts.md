# 返还收货台账接口

业务规则以 D-17 和实施计划为准。以下接口均须登录并具有维保页面及项目访问权限；写操作还需 `action_maintenance_bad_return_manage`，操作人由真实登录身份取得。

| 方法与路径（均以 `/api` 开头） | 用途 |
| --- | --- |
| GET `/maintenance/projects/stable/{project_id}/return-receipts` | 分页查询；支持 `line_status=active/voided/all`、`source_order_id`、`unassigned=true`、`source`、`q` |
| GET `/maintenance/projects/stable/{project_id}/return-receipt-summary` | `project_total_qty`、`unassigned_qty` 与 `by_demand` |
| GET `/maintenance/projects/stable/{project_id}/return-receipt-demands` | 正式入口的有效需求候选，按项目鉴权，支持分页与搜索 |
| POST `/maintenance/projects/stable/{project_id}/return-receipts` | 登记已收到的返件，`pn`、正整数 `qty` 必填，`wbdd_no`、件况、备注、凭证、时间可空；重试保持 `idempotency_key` |
| PATCH `/maintenance/return-receipts/{receipt_id}` | `version`、`reason` 必填；未传字段保持不变，可空字段显式 `null` 清空；跨项目需两侧权限并重新选择或清空需求单 |
| POST `/maintenance/return-receipts/{receipt_id}/void` | 版本检查后作废，必填原因；不物理删除、不提供自动恢复 |
| GET `/maintenance/return-receipts/{receipt_id}/audit` | 显示有权查看的历史；转移前后快照涉及的项目均需有访问权 |

同一幂等键不同载荷、旧版本修改、修改已作废行返回冲突。权限拒绝、请求校验失败和服务失败写入独立事务的 `sys_audit_log`；不存请求正文、令牌和异常原文。成功的前后值审计与事实变更在同一事务提交。

卡墙 `receipt_return_rate` 返回 `returned_qty`、`demand_qty`、`rate_pct`、`state`。数量采用十进制定点字符串；`basis_incomplete` 时 `rate_pct=null`。未关联需求单的收货同样计入项目分子。“未归属需求”汇总行不是项目，其 `receipt_return_rate=null`，仍保留与项目行相同的顶层字段。

领用搜索增加现场 `remark`、`demand_order_no`、当前主数据 `description/brand/category_major/category_minor/unit` 及 `return_requirement`；描述标注 `description_source=current_master_data`。`return_requirement` 包含 `requirement_status`、应返/免返/待判定数量及规则依据。

使用 V2 工作簿（`MAINTENANCE_PROJECT_MASTER_V2_ENABLED=true`）下载 06 领用表时附带“返还收货台账（只读）”快照，汇总和实际收货明细独立展示，不分配到领用行，并保留整机层级与数量待审提示。统计列与只读快照被编辑时明确拒绝；收货更正走台账页面。上传者原样保留的旧快照不阻止他人在下载后新增收货。旧 V1 回传仍支持是/否/清空同步。

## 标准入库单异步导入

专用入口为 `/api/maintenance/doc-imports/return-receipts/jobs`，使用正式维保总闸、维保页面与返还管理动作权限，不依赖 Beta 白名单。任务仅允许上传者操作；读取预览、应用和下载原件同时检查涉及项目的当前访问权限。

| 方法与相对路径 | 用途 |
| --- | --- |
| POST `/jobs` | multipart 字段 `file` 上传 `.xlsx`；必填 `Idempotency-Key`（8–128 字符）；完整原件归档后返回 202 与 `batch_id`，后台解析 |
| GET `/jobs/{batch_id}` | 返回状态、分类计数及分页明细；`offset` 从 0 起，`limit` 默认 100、上限 500 |
| POST `/jobs/{batch_id}/apply` | 提交当前 `preview_token`、`plan_hash`；存在更正时须 `confirm_changes=true` 与非空 `reason`；有疑似手工重复时还须独立 `confirm_possible_duplicates=true` |
| POST `/jobs/{batch_id}/cancel` | 取消未应用的任务；迟到的旧工作线程不能再次发布预览 |
| POST `/jobs/{batch_id}/retry` | 从已归档原件重新预览，生成新代次和新凭证；不重新上传、不恢复已作废台账 |
| GET `/jobs/{batch_id}/original` | 经身份、归属及文件哈希校验后下载原件，不暴露服务器路径 |

状态依次为 `queued → processing → ready → applied`；失败为 `failed`，取消为 `cancelled`。失败或取消后可重新预览；超出处理租期的遗留任务也可恢复。应用时锁定事实、重新计算计划并核验原件，任何一条阻断问题或失效预览均禁止整批写入；异常时整批回滚。响应丢失时先查询任务是否已应用。

解析以第二份维保部门标准导出的双表头为准：只将有效的“维保拆旧返件”和“旧库退返”纳入，所有件况均可入账。项目归属优先采用已有有效 WBDD 挂靠；无 WBDD 时要求唯一、可验证的项目名称或编码。无法确定归属时列为待关联，不以当前页面项目或 XSDD 猜测。整机主项计 1，附属明细保留原始证据但不计数；正的小数数量精确保留并标记待审，人工明确改为整数后解除待审。

来源身份使用独立来源命名空间、稳定原始主单 ID 与明细 ID。文件重复、重叠来源明细不重复计数；源值变化列为更正并展示前后值。原文件重传保留页面人工修改；已作废记录不自动恢复。旧通道中仅凭单号无法证明相同身份的记录会阻断，不能跨来源静默接管。

拟新增行与有效手工登记的项目、PN、数量、上海业务日期相同，仅作为疑似重复提示；日期缺失不猜测。响应包含全批 `possible_duplicates_count` 和行级 `possible_duplicates`，即使本页没有候选也须明确确认。候选版本进入预览指纹；确认后新增独立来源事实，手工行不会被合并、接管或改写。

应用使用固定锁序冻结项目归属、名称与候选事实，并在项目锁后重新读取上传者当前范围；权限撤销先提交则拒绝写入。导入原子应用期间，同项目的手工登记等待它提交；手工登记彼此仍可并行。

原件、原始主单与明细、来源身份、数量待审标记及每次应用的文件/任务证据均可追溯。登记事实与成功审计在同一事务内提交，权限拒绝、校验和应用失败另行持久化脱敏审计。导入不写库存、成本和 WBDD 源事实。
