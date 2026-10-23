# 共享 PN 模块 HTTP 与审计契约（设计草案）

状态：未实现。共同前缀 `/api/pn-catalog/v1`；itsp拥有权威端，shop BFF仅做认证/路由/旧响应适配。第一版发布OpenAPI 3.1并生成TypeScript client；本文不是可调用API。目标见[架构](architecture.md)，跟踪 itsp #463。

## 1. 统一约定

- 使用稳定数字 part_id；不以PN文本当更新主键。图片/条目/任务/回执为不透明ID，不能靠解析ID猜语义。
- JSON未知写字段拒422；PATCH仅更改传入字段，显式null/删除键按字段schema处理，缺失不代表清空。品牌与厂商分别字段。不可用万能CRUD透传ORM列。
- 所有写操作带 `Idempotency-Key` UUID；更新带相关expected revision/field versions，缺前置条件428；HTTP条件请求If-Match不匹配412；领域字段/身份并发冲突409，三者不混用。
- 前端不得自行连续重试冲突写；保留用户草稿，拉当前版本、展示差异，由人重新确认并生成新request_id。同一次网络未知结果保留原key。
- 共享资料普通写同步200/201；确实有耗时工作的202只表示持久受理，返回status_url、request_id、job_id，不能提示“已归档成功”。
- GET list用受限limit（默认50，最大200）和不透明cursor；排序允许列表明确登记；默认不返回停用项，include_inactive需权限。搜索跨描述/PN/品牌/厂商/别名/规格的范围明确，模糊候选永不自动绑身份。
- 身份redirect通过响应 `requested_part_id`、`canonical_part_id` 和revision说明，不把旧ID静默当新目标执行写入。
- 响应 no-store 或 private，审计/委托/回执 no-store。版本不得用更新时间代替。

## 2. 查询、新增、修改、删除接口

所有路径相对上述前缀。写权限名仅为待注册契约，不表示目前已被权限框架支持。

| Method / Path | 输入要点 | 成功结果 | 主要边界 |
|---|---|---|---|
| GET /parts | q、cursor、limit、状态/品类筛选 |200 分页主档摘要|筛选与总数同一权限范围；不强制每行返回签名图URL|
| POST /parts |pn、profile、来源引用|201 part与初始revision|近似重复给候选，不自动合并；规范名冲突409|
| GET /parts/{id} |include=profile,content,gallery_summary|200 权威快照/能力|include白名单；无权字段不返回|
| PATCH /parts/{id}/profile |expected_identity_revision、field_versions、changes|200 新值/版本/回执|不同字段可合并；同字段ABA也冲突；品类作为组合|
| GET /parts/{id}/contents/{grade} |good或bad|200 title/description/status/version|无内容是明确空对象，不从另一grade猜值|
| PATCH /parts/{id}/contents/{grade} |expected_revision、identity_revision、title/description|200 内容及回执|good/bad独立冲突域；不改SKU库存|
| POST /parts/{id}/deactivation |expected_revision、reason|200 availability=inactive+回执|可追溯停用；保留历史引用；不是物理DELETE|
| POST /parts/{id}/reactivation |expected_revision、reason|200 availability=active+回执|停用恢复与撤销合并是不同操作|
| POST /parts/{id}/aliases |alias、expected_identity_revision、来源|201待审别名|新增不自动表示同款，确认需identity权限|
| PATCH /aliases/{id} |expected_revision、允许字段|200别名回执|确认/拒绝/重挂必须使用严格身份命令|
| DELETE /aliases/{id} |expected_revision、reason|200逻辑撤销|保留历史来源，不删除业务文本|
| GET /parts/{id}/aliases |cursor|200 状态化别名|pending/rejected不当作已确认同款|
| POST /parts/{id}/identity-commands |type=rename/merge/unmerge/review_alias及各自严格schema|200/202 类型化回执|不是自由字典；权限独立；多PN锁序/全闭包校验；历史事实副作用另见宿主治理契约|
| GET /parts/{id}/gallery |用途/状态、cursor、limit|200 条目、revision、签名有效期|正式/候选/删除区分；分页绑定图组版本，过期409/410|
| POST /uploads |目标或unassigned、文件声明、client_request_id|201 上传票据/有限有效期|OSS临时前缀+大小/MIME限制，字节仍须服务端验证|
| POST /uploads/{id}/completion |上传结果引用/校验前置|202 验图/持久处理任务|浏览器自报SHA/MIME不可信；同票据同回执；无目标上传仅成为收件箱资产，不直接发布图库|
| GET /uploads/{id} |—|200 传输/校验/归档分别状态|只允许所属用户或明确管理员能力读取|
| POST /uploads/{id}/cancellation |expected_revision|200/202 取消回执|已归档不伪装可取消，返回409提示图库删除|
| POST /parts/{id}/gallery/entries |upload_id/asset_id、用途、expected_identity_revision|201/202 归档回执|验证资产使用权；不是传任意OSS key；幂等去重|
| PATCH /parts/{id}/gallery |expected_gallery_revision、primary_entry_id、ordered_entry_ids|200 图组/版本|排序列表必须与约定成员集一致，不隐式删未传图片|
| PATCH /gallery/entries/{id} |expected_revision、purpose/状态变更白名单|200条目|候选采用独立权限；瑕疵不得自动升通用|
| DELETE /gallery/entries/{id} |expected_revision（查询/If-Match）、Idempotency-Key|200 tombstone+回执|移除关联/停止展示，不删OSS；重复原key重放|
| POST /gallery/entries/{id}/restoration |expected_deleted_revision、reason|200恢复+新generation|独立人工动作，旧上传不能借用|
| POST /assets/{id}/derivatives |recipe_id、预期源revision|202任务|recipe白名单；原图不被替换|
| POST /content-candidates |类型化来源/目标/内容|201候选|AI与Excel建议不直接当有效值|
| POST /content-candidates/{id}/adoption |expected_candidate_version、字段选择与基线|200采用回执|实名确认、字段权限，写值与来源同事务|
| GET /requests/{request_id} |client域由认证确定|200原回执或404|404不证明另一个在途事务不会提交；可用原key安全重试|
| GET /jobs/{id} |—|200阶段/失败码/可重试性|无权任务不可枚举；不暴露外站密钥或原始异常|
| POST /jobs/{id}/retry |expected_revision|202新的执行attempt|保留逻辑job与历史；重新鉴权，不覆盖旧attempt|
| GET /parts/{id}/audit-events |cursor、limit、动作过滤|200审计分页|审计正文按读取者权限裁剪|

对外不提供原始 SQL、任意表 CRUD、任意URL抓取、任意OSS删除。扩展外用先覆盖稳定公开DTO，宿主内部事务对象不进入HTTP协议。

## 3. 响应与错误

成功对象：`{data, meta:{request_id, resource_revision, identity_revision, replayed}}`；异步增加 `operation_id/status_url`。列表另有 `next_cursor`、可选明确成本的total，不能用估算冒充精确数量。

错误示例：

```json
{"error":{"code":"catalog.field_conflict","message":"描述已被其他操作修改，请核对后重试","request_id":"01900000-0000-7000-8000-000000000001","retryable":false,"retry_after_seconds":null,"details":{"fields":[{"path":"profile.description","base_revision":7,"current_revision":8}],"resolution":"reload_and_confirm"}}}
```

base/current/proposed值仅对有权字段返回；对外只保证稳定机器码、details schema和request_id，message可本地化。HTTP码与业务码分别解决通用客户端行为和具体恢复。

| HTTP | 稳定业务码 | 含义/客户端动作 |
|---|---|---|
|400|request.invalid_json|请求不可解析；不重试|
|401|auth.unauthenticated / auth.delegation_expired|重新登录/更新委托；写重试沿用原key|
|403|auth.forbidden / auth.actor_unmapped / auth.actor_revoked|不自动扩大权限；展示需要授权/账号绑定|
|404|catalog.part_not_found / media.entry_not_found / request.not_found|不存在或当前用户无读取资格；不以404创建同名替代物|
|409|catalog.identity_changed|旧身份不可继续写，重取并人工确认目标|
|409|catalog.field_conflict / content.version_conflict / gallery.version_conflict|保留草稿，展示三方差异|
|409|catalog.pn_exists / catalog.binding_conflict|给有权查看的候选，不模糊自动合并|
|409|request.idempotency_mismatch|同key请求内容不同；编程错误，不自动换key掩盖|
|409|media.deleted / media.upload_already_committed|旧意图不能复活；用明确恢复或删除流程|
|409|catalog.in_use / catalog.merge_conflict|请求与依赖/身份约束冲突，不级联删除|
|409|request.in_progress|同key仍在处理中；按Retry-After查询或同key重试|
|410|media.upload_expired / pagination.cursor_expired|重新取票/首屏；已传字节先查询可恢复性，不能静默丢弃|
|412|request.precondition_failed|HTTP If-Match失败；重取基线|
|413|media.file_too_large / request.payload_too_large|解释限额，禁止无界上传|
|415|media.unsupported_type|类型不支持；不可信扩展名不能绕过|
|422|request.validation_failed / catalog.unknown_field / content.invalid_grade / media.invalid_image|逐字段错误；不落部分写|
|428|request.precondition_required|缺基线/版本，客户端先GET|
|429|request.rate_limited|带Retry-After，指数退避+jitter|
|503|auth.unavailable / catalog.unavailable / media.provider_unavailable / write.temporarily_busy|暂不可用；权限故障不能回退允许；写保留原key|
|500|internal.unexpected|仅脱敏request_id；结果可能未知，查回执后重试|

`retryable`只表示相同语义请求可能再次成功，不允许自动重发一份新命令。任务另有 `job.superseded / job.lease_lost / job.cancelled / job.awaiting_provider / job.failed` 状态；租约失效是worker不能提交的证据，不作为用户可点击“忽略冲突”的选项。

## 4. 审计：人、账号、执行者、来源分开

账号是认证身份，人可能有多个账号；AI不是一个与Excel平级的账号角色。禁止以可编辑显示名做唯一审计主键。

| 维度 | 必须记录的内容 |
|---|---|
| actor |主账号稳定ID；未迁历史保留issuer+subject；kind=human/system；显示名仅当时快照|
| executor |实际执行的服务/worker身份、client_id；代表人执行不抹去actor|
| entrypoint |itsp_ui/shop_ui/external_api/import_job/maintenance_job等受控入口|
| provenance |manual/excel/ai/upstream/migration，以及evidence_id；Excel档案hash、sheet/row/column；AI provider/model/run_id/prompt-template版本；不记录密钥|
| decision |requested_by、confirmed_by、adopted_from_candidate_id；无需人工确认的既定导入规则记录policy/version；不伪造历史确认人|
| change |稳定resource ID、action、字段patch、前后revision、受权限约束的前后值、reason、result、影响身份集合|
| correlation |request_id、correlation_id、causation_id、job_id/attempt、source_event_id、命令payload_hash|
| authorization |授权资源/动作、grant版本、policy版本、委托来源；不保存bearer token|
| time |服务端UTC发生/提交时间；来源时间另列，不用客户端时间排序并发|

示例：员工在shop点击“采用AI描述” → actor=员工主账号；executor=API；entrypoint=shop_ui；provenance=ai及run_id；confirmed_by=该员工。Excel任务 → actor=实际上传人（若有证据）；executor=导入worker；provenance=excel档案与行；同一命令可有多个字段各自不同来源。历史作者未知写unknown和原始证据，不回填成迁移操作者本人。

审计与生效变更同事务；拒绝/失败另存受控操作日志，不能制造成功业务审计。日志追加式、不能由普通编辑接口改删；幂等重放不重复写业务审计。保留期与归档策略沿现有制度，不未经确认永久保存所有AI原始对话/敏感文档。

## 5. 外部集成与版本

OpenAPI为协议规范源，双仓生成客户端与fixture校验，shop已有前端生成文档不能替代后端契约测试。小版本只加可选读字段；修改必需字段、错误语义或身份规则需新主版本和迁移说明。公开类型化命令，不暴露内部表名。

批量导入是异步operation+逐行结果（明确最大批量/字节预算），不承诺跨所有PN原子事务；itsp已有导入事务保持原来的原子边界，不能为复用HTTP而变成逐行提交。扩展webhook/export等先预留变更事件DTO，不先实现总线；投影事件使用事务outbox与逐事件ack/幂等revision，不能按已见最大自增id跳过晚提交事件。

## 6. 保留 shop 工作流的补充接口

以下属于首版必需，不是未来扩展；不支持它们就不能替换现有 shop 维护模块。多接口复用同一上传/认领/归档命令内核。

| Method / Path | 输入与前置 | 返回与语义 |
|---|---|---|
| GET /inbox-items |scope=mine/shared、cursor、group_id、状态筛选|200受权限范围约束的未归档照片；无part_id也可发现|
| POST /inbox-groups |client_group_id、capture_mode|201分组；同件继续拍/下一件保持原分组语义|
| POST /inbox-groups/{id}/items |upload_id、position、expected_group_revision|201/202照片接收回执；不创建库存|
| PATCH /inbox-groups/{id} |expected_revision、完整成员顺序|200新顺序，缺失成员不能静默删除|
| POST /inbox-items/{id}/claim |expected_revision|200当前认领和claim_revision；已被别人认领409|
| POST /inbox-items/{id}/release |expected_claim_revision|200释放；仅持有人或明确管理权限|
| POST /inbox-items/{id}/assignment |claim_revision、part_id、identity_revision、assignment_key|200/202归档；同key不重复绑定，目标变化需新确认|
| DELETE /inbox-items/{id} |expected_revision、Idempotency-Key|200软删除，取消旧归档generation；不删原图|
| POST /inbox-items/{id}/restoration |expected_deleted_revision、reason|200恢复未归档状态；不自动恢复旧归属|
| POST /upload-batches |逐文件client_request_id、目标/分组|201各文件独立upload_id；不承诺整批原子|
| POST /uploads/{id}/multipart |expected_revision|201唯一multipart会话与服务端part_size|
| GET /uploads/{id}/multipart |—|200已知分片与恢复状态；重验owner|
| POST /uploads/{id}/multipart/part-urls |part_numbers白名单、expected_revision|200短期分片URL；不允许任意key或跨票据签名|
| PUT /uploads/{id}/multipart/parts/{number} |etag、size、expected_revision|200分片回执；后端与OSS实际分片校验，不盲信客户端|
| POST /uploads/{id}/multipart/completion |规范分片清单、expected_revision|202合并/验图回执；已有completion沿原操作恢复|
| GET /legacy-galleries |迁移状态、来源命名空间、cursor|200待解决来源集合，仅授权迁移/资料人员|
| GET /legacy-galleries/{origin_id} |—|200完整历史图库/来源，未映射也可访问|
| POST /legacy-galleries/{origin_id}/resolution |mapping_manifest_id、expected_source_revision、目标或明确新建选择|200/202幂等绑定回执，需独立迁移权限；不是按PN猜合并|

未映射旧图库对象使用稳定 `origin_id=(source_namespace, source_pn_uuid)` 作为迁移来源身份，与可复用Asset、历史条目引用关联；不能捏造part_id=0。legacy-galleries供影子迁移/只读保全和映射裁决，不作为永久第二套可写主数据。正式整体切换前，可继续使用的旧商品必须已有明确part_id；仅明确批准归档的待解对象可保持只读。历史未归档收件箱照片不需要先建立PN，正常保存/认领/归档能力保留。

认领冲突使用 `inbox.claim_conflict`(409)，分组漂移 `inbox.group_conflict`(409)，非法分片 `media.invalid_part`(422)，票据不属于当前人403或不可见404；multipart取消统一走upload cancellation，不另起一个可绕过状态的abort接口。

## 7. 去重、恢复与回执的精确定义

- Asset按稳定asset_id与不可变object version标识；内容hash可索引去重，但仅知道hash或Asset ID不得取得别人文件使用权。跨来源复用asset_id必须同时验证来源当前可读且可复用、目标图库可归档、purpose兼容；目标上传权限不能绕过来源删除或访问限制。
- Entry持久来源键 `(origin_part_id, asset_content_hash, purpose_scope)`，包括已删除条目；实际唯一约束覆盖tombstone。同来源同内容不能新建另一Entry绕开删除。跨来源相同hash允许分别保留证据，组内只做展示去重。
- 同来源删除后重新上传相同图：返回 `media.deleted`，由有恢复权限者明确恢复原条目并检查删除版本；普通上传不隐式restore。更改用途也需原条目权限与版本，不能通过换scope逃避整资产隐藏。
- scope有条目删除与规范图组隐藏两层，接口必须返回影响范围。图组合并后同hash代表图被隐藏时，隐藏意图绑定图组及内容hash，隐藏绑定稳定group_id+内容hash，成员版本变化、重复导入或另一个来源重新出现均不能自动清除；只有显式取消隐藏命令可清除；撤销合并保留组级审计而不把组操作反写各来源。
- 重放/GET回执先按当前actor/client/resource重新鉴权和裁剪；旧回执不泄露被撤字段或已删除图片的访问URL。可返回原applied_revision和audit_id，若附最新data需单独标data_revision，不能把最新数据声称是旧请求结果。动态signed URL重新授权签发。
- 幂等请求优先级：认证/当前权限 → key/hash核对 → 已完成合法回执重放 → 新请求的If-Match/expected版本校验 → 执行。权限已撤销时即使以前成功也不因命中缓存绕过。
- HTTP/body前置同时存在则都须匹配各自资源；没有明确资源对应关系的混合版本拒422。排序/主图/成员调整为同一gallery编排版本；缩略图ready进度不无故使用户拖拽排序冲突。

## 8. 编辑基线、建档确认与既有辅助功能

GET主档应显式返回可编辑基线，字段含义如下；只返回本用户可读取的字段和值，隐藏字段不返回基线或冲突原文。

| 字段 | 类型 | 用途与为何需要 |
|---|---|---|
| part_id |正整数|稳定更新目标；PN改名后仍相同|
| identity_revision |非负整数|检测改名、合并、别名归组等身份变动|
| profile_revision |非负整数|整个资料版本，用于保守旧客户端和缓存|
| field_versions |字段路径→非负整数|局部并发及ABA检测；删除键也保留版本|
| profile |严格类型对象|用户可见当前字段值，与字段版本来自同一读取快照|
| specs |键→value/unit/source/version/deleted|明确参数单位、人工删除保护与逐键冲突|
| contents |good/bad→内容+revision|两种描述独立更新，未传grade不受影响|
| capabilities |已登记操作→允许状态|界面渲染；不替代后端授权|

更新示例（HTTP PATCH，另有Idempotency-Key请求头）：

```json
{"expected_identity_revision":4,"field_versions":{"description":8},"changes":{"description":"经人工核对的通用型号描述"}}
```

对应成功回执示例：

```json
{"data":{"part_id":101,"profile":{"description":"经人工核对的通用型号描述"},"field_versions":{"description":9}},"meta":{"request_id":"01900000-0000-7000-8000-000000000002","resource_revision":12,"identity_revision":4,"replayed":false}}
```

profile字段、规格键删除、content的完整schema与上限应在B1编码前落OpenAPI并经过契约测试；本B0是语义设计，不声称已经有可生成客户端的完备OpenAPI文件。

创建碰到近似候选时返回409 `catalog.possible_duplicate`，details含有权候选与短时`confirmation_token`。令牌绑定actor/client、规范化请求hash、候选身份revision与到期时间，由服务端产生；用户选择已有ID或者明确“确为独立型号”后，以新request_id+token+reason再次提交。后端重新查重，规范名精确冲突仍拒绝，新候选/旧token漂移需再次确认。`confirm_distinct`属于单独能力，不能用裸force=true绕过。创建过程中不修改原候选或库存。

保留已有辅助交互的契约与替换点：

| 现有能力 | 新接口/归属 | 不得退化的行为 |
|---|---|---|
| 搜PN联想、手机直接拍照 |GET /parts/suggestions；上传/收件箱接口|返回稳定ID和品类；选中后才定归属，不自动强绑近似|
| 贴码识别、同件连拍 |POST /recognition-jobs（upload_id或inbox_id）及GET /jobs|人工触发；候选/品类仅建议，识别失败可手动选择；不创建库存|
| 补资料、找图片 |POST /research-requests，capability=text/images，GET /jobs|人点击并带request_id/预算策略；只产候选，沿用限流/暂停恢复|
| 选择采用候选 |POST /content-candidates/{id}/adoption及图库状态命令|明确确认、当前权限/版本、保留原来源|
| 批量图片下载 |POST /gallery-bundles（entry_ids+版本），GET /jobs|异步包受读取权限；下载URL签发时再鉴权，不打包已无权/已删除图|
| 后台刷新与上传队列 |原前端流程+统一client/operation IDs|每项固定目标，切页继续、失败就近重试、暂停不擅自恢复|

研究/识别用现有AI模块adapter，不把大模型调用器或新爬虫塞进共享主数据核心；生成结果不能隐式回写profile或content。原先已生效AI资料迁移为legacy有效值，不从历史重新执行AI。

前端每资源记录owner/session generation、request sequence和最新revision：旧GET、旧搜索响应或更早保存回执晚到不得回退当前状态。退出/换账号隔离缓存和IndexedDB队列；编辑中的脏字段不被后台刷新覆盖。gallery列表分页/读签名分别处理内容版本与URL有效期，不将新URL刷新误当成编辑冲突。

## 9. 辅助流程的状态与恢复补全

- Inbox只有验图/元数据已持久化且达到received/ready时才进入共享列表；本机排队/传输中可在本人上传列表见到，不向其他人显示为“已收到”。claim是可过期的明确认领，响应claim_expires_at；过期可通过claim CAS接手，旧认领人assignment必因claim_revision过期拒409。管理员接手须reason和审计。新PN路径先调用已有查重确认建档，再用同一assignment_key归档；创建成功而归档失败可重试归档，不反向删除PN。
- 不能直接重试 cancelled/superseded/lease_lost 旧attempt并恢复其发布权限。系统lease丢失由当前合法任务调度恢复；用户只可重试允许的failed/waiting状态。输入或身份过期时返回 `job.input_stale`(409)，须新确认、新命令。显式取消后的重新请求是新request_id，不伪装旧任务继续。

| Method / Path | 前置 | 返回与恢复 |
|---|---|---|
| GET /content-candidates |part_id/grade/source/status、cursor|200权限内候选，模型/Excel证据与有效值分开|
| POST /content-candidates/{id}/rejection |expected_candidate_version、reason|200拒绝回执；不删来源，不改已生效主档|
| POST /file-match-proposals |文件名/允许的文件元数据列表、每批限额|200 PN匹配建议；先按现有文件名匹配流程再人工归类，不上传文件字节/不自动归档|
| POST /research-requests/{id}/cancellation |expected_revision|200/202取消意图，递增generation；已发云请求不保证撤回但结果不得发布|
| POST /gallery-groups/{id}/suppressions |hash或entry目标、expected_gallery_revision、reason|200持久隐藏意图；需库管理权限，成员变化不清除|
| DELETE /gallery-groups/{id}/suppressions/{suppression_id} |expected_revision、Idempotency-Key|200显式取消隐藏审计；不是物理删图|

旧路径与这组接口的兼容adapter不得增加隐式AI触发或自动采用。第一版UI可继续使用原有路由，但所有共享状态命令最终调用同一核心。接口部署与客户端启用需按能力版本协商，未交付能力不显示为已可用。
