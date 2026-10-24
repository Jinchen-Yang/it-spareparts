# S3 前端对象与集合能力协议（设计，未实施）

本文件基于 S2 前端（复核基线 `68e9944`）与 [S3 项目合同](s3-project-authorization-contract.md)、[完整执行合同](s3-execution-spec.json)、[文件任务接缝](s3-artifact-task-execution-seams.md) 的实读。S3 工作只新增本文及 surface 建议 JSON；未改 app/tests。完整合同的 73 个资源、228 个活跃后端入口并非已迁移证据，也不等于前端权限门。旧 delta 与临时 fragment 仅保留推导背景；数据动作、字段和上下文闭集以完整执行合同为准。S3 executable 定义、surface 和 seed 仍由根统一冻结，本文不建立另一张授权真值表。

根已批准：在少数现有领域读入口增加封闭 `view=authorization`（含下文两个只读 POST search）。返回最小 locator 和 `authorization_context`，不查业务事实；path 对应的资源、动作、集合 purpose 由服务端固定，不接受任意 resource/action/scope/resolver。`/me.requires_context` 仅可进入解析壳。HTTP 判别联合、precondition 也已冻结；具体领域块归属仍须以完整后端执行合同对拍，均未宣称已经实施。

**当前冻结进度**：完整 spec `23e5d4d144b8ed567e17e0e76dca1bb583bcff047019646db38c22e0bb7d1497` 的 13 个基础授权视图、23 个 input 授权视图已对拍；真实 action→context kind/selector/purpose 已替换初版机械 collection refs，包含完整目标集的 bulk apply/backfill/repair。根随后批准子对象既有 GET 与逐动作既有命令的固定授权视图，后端正在补精确表；**子对象 bootstrap 尚未全部闭合**，不能把数据引用通过写成全部入口可用。`/me` 超过 PDP 单批上限时由后端在同一次 observation 内分批并 all-or-error，前端不拼多批快照。

后端共用协议以根的 [核心对象边界](s3-core-context-boundary-design.md) 及 spec `foundation_interfaces` 为准：`ContextBatch` 保存真实 carrier 和逐资源 policy subjects，`ReadSnapshotProof` 只在服务端同一活动只读事务有效，领域在该事务内完成授权、事实查询和 DTO 物化。浏览器只消费下述 wire context；不接收、缓存或重建这些证明对象，也不把 revision 相等解释为授权。写侧仍由服务端完整领域锁和当前主体裁决；前端 precondition 仅用于发现漂移。

## 1. 一个 actor Provider，两阶段领域请求

```text
AuthzProvider /me（token + generation + snapshot.version）
  └─ generated surface Rule：deny / resolve / allow
       ├─ deny：不挂内容、不请求
       └─ resolve：只挂标题/加载/错误/重试壳
            └─ 固定领域 resolver（view=authorization，无业务 payload）
                 └─ 当前对象/集合 context + 精确 action allow
                      └─ 普通领域 query → 本响应 context + block_access → 内容
                           └─ 当前对象 action allow → 单次 command/download
```

继续复用 `authz/AuthzProvider.tsx` 的 `AuthorizationCapture`、signal、`isCurrent`，以及 `authz/access.tsx` 的第一帧投影、abort、commit 和无重放 runner。禁止修改 `ruleAllowed`，把所有 `requires_context` 当作 `allow`；禁止组装一个伪 `/me` snapshot 覆盖全局 Provider；禁止每页保存 role、scope、fields 的替代状态。

需要的最小共享增量：

```ts
// 签名建议；类型由后端闭集生成，以下不是当前实现。
type EntryState = 'pending' | 'denied' | 'resolve' | 'allowed' | 'unavailable';
routeEntryState(snapshot: AuthzSnapshot | null, rule: Rule): EntryState;
useAuthorizationContext({ resolver, input, enabled }): ContextResolution;
useContextAccess(context: ContextResolution, rule: Rule): ContextAccess;
useAuthorizedQuery({ rule, context, queryKey, query, enabled });
runAuthorized({ requirements: [{ rule, context }], queryKey, request, commit });
runAuthorizedOperation({ adapter, input, inputRevision, isInputCurrent, commit });
```

| 名称 | 类型/用途 | 限制 |
| --- | --- | --- |
| `resolver` | 静态领域 adapter；固定 path、expected kind/id 和响应 validator | 不是由 URL 输入拼任意 resource 的 dispatcher；只有这个入口允许 unresolved 状态发送解析请求。 |
| `input` | 该 adapter 的已有业务选择参数 | 项目 ID/报表月/规范化筛选；不能带可信 owner、关系或 SQL。 |
| `ContextResolution` | 本次捕获的 actor 生命周期 + 领域响应 context + 请求 key | 页面局部结果，不写 localStorage，不给其他对象复用，不注入全局 actor snapshot。 |
| `ContextAccess` | 同一生成 Rule 在该 context 下的结果 | 未解析、缺资源、缺动作、`requires_context` 都不能使 `allowed=true`。 |
| `requirements` | 页面已有独立对象的 AND 检查列表；各项内部仍是 OR-of-AND Rule | 不能在浏览器拼接 A/B 的资源或字段。真实多对象命令只回一个服务端解析的 carrier；其完整父链与逐对象 subjects 由后端证明。 |
| `query` / `request` | 接收捕获的 token、snapshotVersion、generation、queryKey、signal 和 context 引用 | 不在 load 闭包再次 `getToken()`；文件保存、消息、回执写入只放 commit。 |
| `adapter` / `input` | 后端生成注册表对应的封闭操作 adapter，以及同次用户意图捕获的参数/文件 | adapter 固定 method/path/action/分支与授权视图，不接收任意 resource/purpose。input 两阶段使用同一不可变值/文件引用。 |
| `inputRevision` / `isInputCurrent` | 表单内容版本及当前性检查 | 文件、选择或命令参数变化即失效；浏览器版本只用来中止，不替代服务器计算的 digest。 |
| `commit` | 最终结果的 UI/文件发布 | 仅真实 execute 成功且 actor、input、context 仍当前时调用；authorize 结果不能冒充业务成功。 |

规则解释器仍只有一个：复用 generated actions/dependencies/surfaces 与 OR-of-AND；增加显式 context 参数，而不是新建维保布尔权限图。入口检查使用同一 Rule 的三值评估：一个完整 clause 无 deny、至少一项尚需 context，才为 resolve；它永不返回业务 allowed。全部必要动作/字段依赖都参与，embedded-only surface 仍不打开宿主路由。前端不把服务器关系 scope 编译成自己的 M/V/S 算法。

解析与普通 query 共用现有 capture/abort/identity primitive，不能复制三份生命周期实现。若需抽取 `useAuthorizedQuery` 内部执行器，只给封闭 resolver adapter 一个 `resolve` admission；普通 query/runner 的默认要求仍是 allow。仅服务器显式 runtime=legacy 的资源沿用其原页面行为；unknown、未取得 snapshot、suspended 不借 legacy 布尔补 allow。planned 资源可保留服务器显式 legacy 行为，但不进入新 contextual allow。object/collection enum 与协议校验词表同样从根后端声明生成，不另维护前端白名单。

**对象动作**：资源 runtime 必须 enabled，`/me` 对该动作不得 deny，真实对象 context 对该动作必须 allow；contextual 资源即使 `/me` 某动作 allow，仍不能跳过对象绑定。非 contextual 全局动作（例如独立 display_policy.read）继续由 actor snapshot 判断。display 和 data 的组合仍按同一 surface Rule，display 不替代数据动作；scope 的 `requires_context` 来自生成定义。

## 2. Wire DTO 与校验

项目/集合部分保持完整执行合同 `context_protocol` 形状。建议所有 S3 受控 JSON 业务响应的顶层附 `authorization_context`；列表中需要操作的行另附该行真实对象的同型 context。不要在列表 collection context 中把所有行写动作 flatten 成 allow。

```ts
type ContextRef =
  | { kind: 'project'; id: string; authorization_revision: string }
  | { kind: 'collection'; id: CollectionRef; authorization_revision: string }
  // 已冻结；子对象不能仅借父项目 allow。
  | { kind: 'object'; object_type: MaintenanceObjectType;
      id: string; authorization_revision: string };
interface AuthorizationContext {
  schema_version: 1;
  catalog_version: string;
  authorization_epoch: number;
  context: ContextRef;
  resources: Record<ResourceId, {
    grant_version: number | null;
    decisions: Partial<Record<ActionId, Decision>>;
    field_permissions: FieldId[];
  }>;
}
interface AuthorizationResolution {
  locator: { project_id: string } | { collection_ref: CollectionRef }
    | { object_type: MaintenanceObjectType; object_id: string };
  authorization_context: AuthorizationContext;
}
```

`Decision` 完全复用 S1 `{effect: allow|deny|requires_context, reason: {code,message}|null}`。resolver 的 locator 不含名称、客户、销售人员、金额、数量、成员或分页 total；project locator 只含真实稳定 ID。未知/不可访问对象按领域原 403/404 合同处理，不利用 resolver 枚举不可见业务资料。全拒绝响应可以解释权限，不能夹业务数据。

`CollectionRef` 从完整执行合同 `context_protocol.closed_collection_refs[].id` 生成，不沿用旧 delta 的九项截断清单。每个集合有固定资源绑定，不相互替代：分析使用 `maintenance_analysis_projects`，dashboard 使用 `maintenance_dashboard_projects`，展示板汇总使用 `maintenance_summary_projects`。同一集合允许多个 adapter 时仍各自固定 purpose，并在 revision 输入中绑定；不能跨域复用一个解析结果。闭集并不表示每项都新增解析端点，实际可请求的授权视图只来自 spec 的基础、input 与子对象精确注册表；前端生成器引用这些表，不维护第二份 method/path 白名单。

`MaintenanceObjectType` 由完整执行合同 `context_protocol.closed_object_types` 直接生成；不使用旧候选别名（例如 attachment 实际为 business_file）。内部 ObjectRef 使用 `closed_internal_context_kinds` 的扁平 kind/id/selector，后端显式转换成 HTTP 的 `kind=object + object_type`，前端不猜映射。实际 parent 链、old/new project、owner 和版本由服务端解析并进入 revision；客户端不能声明 parent 从而授权。

统一 validator 必须先于 setState 和 DTO 投影：

1. schema/类型/资源/actions/fields/collection ref/object type 都为服务端生成闭集；重复字段、未知值、格式无效整响应失败。完整业务对象不能靠类型断言绕过。已知但不返回的资源/action 按 deny，不从别的响应补全。
2. 响应 epoch/catalog 必须与捕获的当前 `/me` 一致；资源版本与 actor snapshot 对应。当前 `/me` 无 Grant 用 `grant_version=0`，delta context 用 `null`；仅这一种缺失对应可转换，不能把 null 当未知版本继续 allow。正数版本均为 safe integer。
3. context 字段只能来自该资源且不能超过 `/me` 对该资源的 Grant 字段；字段不可跨资源 OR 合并。**根已明确 S3 修改 `/me`：资源 probe 至少一个 allow 或 requires_context 时都暴露本 Grant 声明 fields/scope，作为上界，绝非对象许可。当前 S2 `my_snapshot` 仅 has_allow 才返回 fields/scope，不能原样用于 S3。** 只有 requires_context 的 Grant 即使字段非空仍不得请求业务数据/启用写；runtime legacy/suspended、planned/retired 不接受 contextual allow。
4. 项目/子对象 ID 要与 resolver 预期稳定 ID 一致；集合 ref 要与固定 adapter 一致。revision 只比较不透明字符串，不解析 `mpr1/mpc1` 前缀计算范围，也不拿它当授权票。
5. 所有结果还必须匹配捕获 token、generation、snapshotVersion、请求输入 key。catalog/epoch/Grant 不匹配应清本结果并刷新 actor；不得悄悄接受旧 context。

命令回传已冻结为 `authorization_precondition`：`{catalog_version, authorization_epoch, context:{kind,id,object_type?,authorization_revision}}`。后端将其作为可选输入保留旧兼容，但 S3 canonical 新前端的真实操作必须提交；变化返回 409，服务端仍在领域锁后完整重授权。不要回传 resources/decisions/fields 充当证明；实际 expected_version、签名、plan_hash、scope_version、preview_token、幂等键照旧。**多对象写仍回一个真实 carrier precondition**：其 revision 覆盖完整 parent 链和规范命令 plan，服务器逐真实 policy subject 全称检查并保存完整血缘；前端不回任意资源检查数组或多张项目票。若页面展示多个独立对象，可分别持有它们的读 context，但不能拼接代替命令 carrier。JSON/multipart 具体 transport 编码由各 adapter 与领域 owner 对齐，不另造权限 header。

新增 `object_type=operation_input` 只表示服务端已验证、精确 purpose/action 的操作输入，不是已存储 batch，也不附送其他 apply/download 能力；某个 apply 本身只有在精确注册表单独允许时才可用该类型。ID/revision 由服务器结合主体、purpose、规范命令/真实文件及完整对象集合生成，不能由浏览器 hash 文件后自行构造能力。

**根已批准 input bootstrap**：精确登记的既有 method/path 增 `view=authorization`。它纯解析实际上传或命令 plan 与完整真实对象集合，不创建 job/preview/business 行、不调用 LLM，也不返回业务 payload。返回当前 carrier context 后，前端同次用户意图可继续正式提交同一输入及 precondition；后台重新计算 digest、按完整锁序重验，变化返回 409。`runAuthorizedOperation` 复用现有 capture/current/abort：authorize 前检查同一生成 Rule 可进入 resolve，execute 前必须取得实际 context allow；两步之间以及最终 commit 均检查 actor、snapshot、inputRevision、文件引用和 signal。普通查询/写仍只接受 allow，绝不把 requires_context 升格。

这两阶段不是自动重放：一次用户意图只执行一次正式命令；authorize deny/503、输入或主体变化、execute 409 均结束本次意图，要求重新解析/确认。不要为了容错自动重发 execute、保留旧预检确认或发布迟到 Blob。已有 preview 确认流程仍照旧：input 授权解析不是业务 preview，正式 preview 的返回才能开启人工确认。

同一 runner 也适用于已存子对象的精确命令授权视图：method/path 和本次动作由 adapter 固定，服务端只依据真实主键及数据库 parent 解析，body 仅含该命令实际必需的 parent/selection。GET 的 read context 不能推导 update/apply/delete；低权动作的授权视图不能返回同类型其他动作能力。命令 button 可进入“核对操作权限”的解析流程，但实际业务请求只在该命令 context allow 后发送；未解析不标为“已获写权限”。列表行 context 可以减少重复解析，不能成为直达入口唯一的启动条件。

分支必须由服务器决定：AI propose 按已验证 doc_type 选择 document 或 ledger 资源，不能把可选资源列表当扁平 AND；warehouse apply 按实际 import 存在性返回 `warehouse_import` 或 `operation_input`，前端验证生成注册表允许的实际分支，不强求每次都返回 input。解析后 stored/new 分支或命令计划改变同样 409，不回退到另一分支重试。批量 carrier 覆盖实际完整目标集，不是 UI 当前页或已加载 prefix。

二进制下载不把 JSON context 塞进 blob：先使用 resolver/status/preview 的当前能力，download 自己再由后端实时重验；前端在保存 Blob 前校验原 capture/context 仍当前。若 context 已变化，丢弃 Blob/撤销 object URL；不自动重发导出。二进制协议是否携带版本响应头不是本阶段放行条件。

## 3. 固定解析入口与选择绑定

下面全部 path 带 `/api` 前缀，新增的仅是授权视图；不是宣称现在有这个参数。13 条 method/path、ref 和 purpose 已与 spec `context_protocol.authorization_view_resolvers` 核对。两条只读 POST 因没有等价 GET，根已批准作为封闭例外。下表是前端 adapter 的落点说明；生成器仍读取主表。

| 现有领域读入口 + `view=authorization` | context / 固定目的 | 前端 adapter |
| --- | --- | --- |
| GET `/maintenance/projects/stable/{project_id}` | project；固定项目面板各域能力。不能额外要求 project.read 才解析独立费用/回款/验收/工作簿 | `api/maintenanceProjects.ts`：`resolveMaintenanceProject` |
| GET `/maintenance/projects/stable` | project_directory；目录、需求选择的目录范围 | 同上：`resolveMaintenanceDirectory` |
| GET `/maintenance/boss-board/projects` | project_cards；卡墙当前筛选；不附送 direct project allow | `api/maintenanceBossBoard.ts` |
| GET `/maintenance/boss-board/summary` | summary_projects；展示板聚合 purpose | 同上；当前无单独活跃路由，按真实宿主使用 |
| GET `/maintenance/analytics/filter-options` | maintenance_analysis_projects；分析 purpose，携带已选项目/期间/维度相关筛选 | `api/maintenanceAnalytics.ts` |
| GET `/maintenance/analytics/maintenance-dashboard/options` | maintenance_dashboard_projects；dashboard purpose | `api/maintenanceDashboard.ts` |
| GET `/maintenance/spare-part-lines/fields` | global_rows；全局行字段/列表/export 与全局 workbook 的固定合同，不能把 fields.read 当作其他动作资格 | `api/maintenanceRowFilters.ts`；工作簿另传真实范围 |
| GET `/maintenance/project-manager/workbooks/v3/status` | manager_monthly_projects；report_month + 本人 active M 完整集合 | `api/maintenanceOperations.ts`；当前页面未挂载 |
| GET `/maintenance/project-assignments/orders` | source_order_directory；assignment_status/xsdd_project_id 等相关选择 | `api/maintenanceSourceAssignments.ts` |
| GET `/replenishment-beta/applications` | replenishment_owned_applications；本人申请目录 | `api/replenishment.ts` |
| POST `/maintenance/collection-reminders/search` | followup_directory；保留原搜索 body，授权视图不查提醒业务事实 | `api/maintenanceCollectionReminders.ts`；已批准只读 POST 例外 |
| POST `/maintenance/warehouse-documents/search` | warehouse_documents；保留原搜索 body，不以 project directory 替代 M-only 文档关联 | `api/maintenanceWarehouse.ts`；同上 |
| GET `/maintenance/as-of` | `business_calendar`；固定 purpose=business_date，`maintenance_business_date.read`，不借 source/project 关系 | 当前前端无消费者；不造 show/导航。未来调用须先授权视图再读日期，不能以 requires_context 直接取业务响应 |

23 条 input 入口唯一清单为 spec `context_protocol.input_authorization_view_resolvers`。生成器消费其中 `entry_id`、`purpose`、`selector_schema`、实际分支和 `binding_expression_NEVER_FLATTEN`；`allowed_resource_actions` 只是闭集引用，不可当扁平 AND。前端不提交 selector 内的“可信 digest”来替代原文件/命令，授权请求发送真实输入，由服务端生成 digest。已核边界包括 AI 的 doc_type 二择、warehouse 的 stored/new 分支、全局 workbook validate/apply、完整目标集的 delete/backfill/repair；原第一批十条路径清单已撤下，避免漏接后续十三项。

集合 resolver 的规范化业务选择进入服务端 revision：项目集合、生命周期、业务类型、人员/客户可授权筛选、日期/报表月、行筛选、文件 selected_fields 等实际影响内容的参数。分页/offset 不改变权限集合，但仍属于前端 queryKey。不能用当前页项目 ID、Top N 或用户上传的列表代替服务器完整集合证明。未知或越权筛选应拒绝/显式返回规范化选择；不能默默查询敏感 filter-options 再由前端过滤。

global rows 指定 project_id 时仍必须交目录范围；单项目直达 row/workbook 按自己的合同。两者即使 ID 相同也不共享 capability。batch-transfer options 与 board export options 所对应的不同集合、导出/写入 purpose 需后端逐动作定死；不能因为都在卡墙页头便借 cards.read。工作簿预检完成后实际文件对象集合以服务端 validation context 为准，用户选中的 UI 集合不覆盖文件集合。

**子对象待落表接缝**：根已批准 22 个现有 child GET 的固定授权视图，以及没有适当单项 GET 时逐当前动作登记的既有 POST/PATCH/DELETE 授权视图。后端将落精确 `child_authorization_view_resolvers`，前端不按 object_type 自行猜 URL、不杜撰 GET、不接受任意 resource/action 参数。所有授权视图只返真实 locator/context，零业务 write/job/file/LLM；不能为了权限检查先读审计、文件或明细 payload。附件下载也需要真实 business_file context。已授权列表的逐行 context 可作为当前同动作的已有证明，但不能代替 direct-link bootstrap，不能凭父项目或 GET read 推导 child 写。该表和全部实际 command 逐项核完前，fragment 保留 `all_bootstrap_closed=false`。

## 4. 生命周期、错误与写入

- render 期投影 key：actor token + generation + snapshot.version + resolver/inputKey + context kind/type/id/revision + 业务 queryKey。任一变化的**第一个 render**，data/capability/selected rows/预览/回执/Blob 都为空；不能等 effect 才清旧 A 数据。A→B→A 也不能恢复之前缓存的 allow。
- resolver 与业务 query 用各自 AbortController 并连接 Provider signal。授权、路由、tab、筛选、字段、对象、revision 变化立即取消旧请求、定时轮询和 debounce；迟到成功/失败均不能覆盖新对象。前端不能主动知道远端关系变化，focus/现有 refresh 事件/下一次 resolver 会重新取得关系 revision，服务端每次请求仍重验。
- payload 响应必须同时验证其 context；若与发出时 revision 不同，清空当前对象能力并重新解析，不将旧能力和新 payload 混用。普通读可在重新解析后显式 retry；写/download 永不自动 replay。
- 401 清当前身份；403 清对应结果/关操作抽屉并解释无权；404 保留既有不存在或无权文案；409 context/业务版本冲突清预览并要求刷新/重新预检，保留可安全重新编辑的非敏感输入，不重放。503/PDP/DB/未知 schema 整体失败关闭，不伪装 empty/deny partial success。
- 保存成功若改变 project/manager/viewer/source/receipt parent：先使原 context 和子任务失效，再发 `publishMaintenanceChange`，重解析后刷新各块。原 `useVisibleMaintenanceRefresh` 只调统一 refresh，不能直接绕授权呼叫 loader；不要全局 remount Router。
- AntD Modal/Drawer/portal 必须跟随 context lifetime，旧 portal 不能藏在已撤权页面外。取消/关闭仅取消视图结果，不代表服务端写回滚。成功回执仅当前 context 可显示；撤权及同 actor 全局 session 失效依旧按 S1 处理。
- WorkbookRoundTrip 的 download/validate/apply 分别绑定能力，旧单一 `canUpload` 不足。validate 返回真实对象/文件 context、操作摘要和原令牌；人工确认期间变化则必须重新预检。D03 managed/general 选择由服务端对同一项目选一份完整合同，不在浏览器 union 字段。

## 5. 已核前端落点与最小文件分工

当前导航只有六条活跃维保/补库路由；旧仓库、经理月报、提醒、迁移等路径在 `nav.tsx` 重定向 `/maintenance`，不是活跃页面。以下对应真实请求链，不把候选资源等同新导航。

| 包 / 活跃路由 | 实际文件与请求挂载点 | 实施要求 |
| --- | --- | --- |
| F0 共享基础 | `authz/{AuthzProvider,access,rules}.tsx/ts`、新 `authz/context.ts`、`api/authz.ts`、`nav.tsx`、`App.tsx`、生成脚本 | 一个 context validator/evaluator/lifetime；维护 `/accounts` 等现有 pending 占位；只由生成 surface 产生入口壳。AuthzProvider 继续只存 actor。 |
| F1 `/maintenance` | `MaintenanceHomePage` mount load+IntersectionObserver；`ProjectCard`；`MaintenanceProjectExportButton`、`MaintenanceBatchTransferButton`、`GlobalMaintenanceRowWorkbench` | cards/导出/批量/全局行/工作簿各自解析与授权。续页必须同 collection revision，字段撤销立刻移除金额排序、桶与提示。卡片可读不表示可直达；链接可开解析壳，不能携卡片 scope 作详情 allow。 |
| F1 `/maintenance/analytics` | `MaintenanceAnalyticsPage` mount filter-options、project label 补取、PN/spend；`analyticsExplorerModel`、Rankings/PnCompact；panel 中复用 analytics context | 分析 options 只依分析合同；禁止为 project label 自动借主档 read。维度/metric/filter/export 按实际动作与本域字段；共享 hook 收敛手写 seq、30s refresh。 |
| F1 `/maintenance/dashboard` | `MaintenanceDashboardPage` mount options；dashboard/{Overview,Projects,Parts,Consumption,QuantityChecks}Tab、ReceiptRankingPanel、ReceiptsDrillDrawer、dashboardShared export | 五 tab/细钻/估值/exports 独立，inactive tab 不请求。quantity-only 不被金额口径整页锁死；后端 restricted/null 精确显示。 |
| F2 `/maintenance/projects/:projectId` | `MaintenanceProjectPanelPage` 现 parent 并行 loadProject(stable→board)、loadWorkspace(多页回款)；EditBasics 内再次 getProject | 先 project resolver；仅在对应 data allow 后分别读主档/卡片/workspace。缺主档不封独立授权 tab。去 `can_edit_master_workbook` 与 role/admin 运行授权，manager/viewer 编辑分别消费真实操作合同。HealthBand 不靠一个 summary.read 合并财务。 |
| F2 项目7个 tab | panel/{Overview,PartsOrders,Expense,Collection,SiteReturn,MaintenanceProjectAnalyticsContext,Acceptance}Tab/组件；PanelActionBar | 显示/数据/操作各门。Overview 来源候选/assign/auto分别；PartsOrders 的 board orders、采购、rows、row-fields 不互授；Expense 与 Collection 不从 master 完整字段借权；Acceptance记录/附件/清单分别。 |
| F2 实收/现场子对象 | panel/{ReturnReceiptsSection,ReturnReceiptBatchEntry,ReturnReceiptBatchMaintenance,SiteIssueEditDrawer,SiteIssueReturnBatch,CollectionBatchMaintenance}；SiteIssueWorkflowPanel、ManualSiteIssueSection、ProjectProcurementPanel、SourceCancelImpactPanel、MaintenanceAcceptancePanel | 每行/子对象 context；原父链/新目标分别检查；选中行跨版本清空；批量逐项能力不能从 collection allow 推导。新增按钮的 parent allow 仅适用于 create，不能替代既有 child edit/delete/download。 |
| F3 `/maintenance/demands` | MaintenanceDemandsPage mount search/WBDD missing；DemandLinesEditor、DemandLine{Create,BatchCreate,BatchEdit}；WbddReconciliation、ReturnStatusColumn | 目录范围先解析；line/detail/source cancel/restore/import 分别。两个项目 picker 需目录合同，不用 direct project 资格扩展搜索。批量部分成功业务语义保留，禁止请求前省略子对象门。 |
| F3 `/maintenance/replenishment` | ReplenishmentBetaPage；api/replenishment.ts capabilities/projects/catalog/cart/application | owner application 与项目关系独立；旧 capabilities flags 不能第二授权真值。cart/project/application 各绑定当前对象，历史 review 的 410 不复活；下载原资格逐动作。 |
| F4 文件/公共协作 | WorkbookRoundTrip、ProjectMaintenanceRowFilters、MaintenanceBatchTransferButton、ReturnReceiptImport、api/maintenanceWorkbooks、maintenanceReturnReceiptImports、utils/maintenanceRefresh | 与 F1/F2 共享文件由一个前端 owner 顺序改。poll/retry/cancel/apply/original/error 下载完整接 capture/context；上传前后/preview后撤权反例。ImportPage 只处理本阶段真实维保共享调用，不借此迁移整个通用导入域。 |

所有 `api/maintenance*.ts` 与 `api/replenishment.ts` 的受控请求收统一 captured transport（显式 token、signal 和 context precondition）；axios 全局 interceptor 不能从新身份给旧回调补 token。API adapter 只校验/规范类型与当前响应投影，不能读 `localStorage.role` 或 `readMaintenanceCapabilities` 再造权限。具体已确认 `maintenanceOperations.ts:normalizeOperationsDirectory/normalizeWorkspace` 在这样做，必须在迁移包内移除。

**workspace 最小 DTO 修订**：目前 `project` 为完整对象，collections/requisitions/expenses total 为 number，`normalizeWorkspace` 会把未授权 expenses 变 total=0。`project_identity` 拒绝时 `project` 仅保留输入对应的稳定 `project_id` locator，描述、指派、合同值不随 identity 返回；独立已准 summary/contracts 块仍按其固定位置投影，不能要求主档 read。固定 block_access 与当前 context 同时检查；拒绝行块为 rows=[]、total=null、restricted=true，金额/比率/成本证据/推导状态为 null。不要把 unknown/restricted 冒充零。完整合同的六个实际块为 `project_identity→maintenance_project.read`、`operation_summary→maintenance_project_operations.read_workspace`、`contracts→maintenance_contract.read`、`collection_snapshots→maintenance_collection.read_snapshots`、`requisitions→maintenance_issue.read_workspace`、`approved_expenses→maintenance_expense.read_workspace`。`workbook_preview` 仅派生：overview.row_count 需要 contracts 和 collection_snapshots 同时允许，site_requisitions 数量取 requisitions，approved_expenses 数量取其本域 allow+F，manager_tracking 数量取 operation_summary；其余受限 row_count=null。它不是文件下载能力。当前 payload 没有 cost_gaps 顶层，不因存在该资源就新增查询或块。operations 自身 C/F 控制原有 HealthBand/reminders/return_rate/completeness 合法聚合，不能要求原始 collection/expense/issue read 才保留聚合，也不能反向据聚合授权明细。collection 的金额/receipt_reference/remark、requisition 的 unit_cost/双税cost/evidence、expense 的 rows 各取实际本资源 fields，不由 `canViewContract/canViewCost/canViewExpense` 用户全局布尔决定。

**税口径**：S2 已建立 display_policy.read 的独立共享依赖。S3 只对实际 TaxMoney/选口径金额块使用；固定“含税/未税”双列本就有明确口径时不引入整页依赖。quantity-only、目录/标签、附件控制页不能因无 shared read 被封。只按当前对象本资源 fields 判是否挂金额，不能按 `/me` 全局字段或 managed workbook grant 推断。

**当前未挂载组件**（App 相对静态/动态 import 图检查，并非删除建议）：BadReturnPanel、CollectionMilestoneFollowUpModal、CollectionPlanImportModal、CollectionReminderDetail、ContractPortfolio、FrontStockPanel、MaintenanceProjectCard、ProjectFinancialProgress、ProjectWorkbookActions、RecoverySummaryPanel、SalvagePanel、WorkbookFourSheetPreview；maintenanceCollectionReminders/Migration/Warehouse API 当前也无 App 导入链。S3 必须处理它们已有共享 transport 或显式保留不可达状态，不为覆盖率重新打开旧导航。后端活跃但 UI 不可达的入口由后端包验证，前端报告标 API-only。

## 6. 实施前需要冻结的最小项与反例

HTTP object 联合、两个只读 POST、authorization_precondition 已由根冻结。完整 surface 建议见 [manifest fragment](s3-frontend-manifest-surfaces-fragment.json)：73 个资源中登记 106 个 surface（40 个真实入口、53 个 embedded、13 个休眠），资源、数据动作、字段与当前后端 spec 闭合，保留输入 SHA-256 供漂移检查；这是设计而非实施计数。13 base + 23 input 已对拍，**子对象 direct-link 与 command 授权视图尚待精确表闭合，不能以 73/106 引用闭合代替全部 bootstrap 可用**。`data_requirements` 是平铺引用元数据，真实挂载和请求必须评估 `data_rule.anyOf`，不能把两份表达合成扁平 AND。实施时还需对齐具体 multipart precondition 编码和旧显示资格 seed 的精确布尔映射；新账号绝不因 data allow 自动获得 show。根生成目录是唯一权威，S3 新动作/show、字段依赖和 context 词表均由后端生成，禁止前端手抄白名单。

必须用真实 Provider 的独立反例验收（不是现在已通过的测试）：

- `/me` 全为 requires_context：菜单可见解析壳，只有授权视图请求，无业务 GET/POST、无写/导出；resolver deny/503 不走 fallback。
- assigned 目录 vs canonical-sales direct：无目录资格但直接项目合法；cards all 不能附送主档；warehouse M-only 不由 V/S 放行。
- A→慢B→A、epoch/catalog/Grant/context revision/筛选变化，用 layout-effect 记录第一帧没有旧 data/allow；取消信号实际传到底层 transport。
- A 是经理、B 是销售且 F=false：A managed 总表可用，B 普通金额/总表不从 A 获得；master M 例外不解锁 finance、monthly、read_cost_rows。
- collection allow + row action deny：批量/写不启用；receipt 改挂到新项目，旧项目 capability 不能重用；导入 job owner 同名换账号不继承。
- 只有 child 命令资格而无父项目/read 资格：固定命令授权视图可解析，但 GET read 仍拒绝；低权 command 不探测其他 action；直达页不要求先打开列表。多对象 command 只回一个服务器 carrier，不能拼接旧申请与新项目 context。
- 同一项目某子块 allow、其他块 deny：独立可用、denied 不查不挂；quantity-only 无税口径可用，金额不可泄露。
- preview 后撤权/关系改变、Blob 迟到、poll 迟到、portal 残留、503、错误后 retry：不发布旧结果、不重放写/导出；原 expected_version/签名/幂等断言全部保留。

本文是可实施接口建议与源码落点检查，不是 S3 完工、CI 或生产证据。
