# B4 历史状态接管协议

决策：D-20261023-PN。本文对应隔离开发实现，不是生产冻结、OSS 校验或切换通过证明。

迁移依赖顺序为：冻结副本及写入隔离证据 → 全来源数量与指纹守恒 → 明确 PN/账号绑定 → 资料及素材实体 → 上传、收件箱和人工继续操作 → 历史任务/审计与在途恢复 → 对账。原 `LegacySourceRecord` 始终是不可变证据，新的状态转换另记 ledger，不能重写历史 payload 或原映射以消除冲突。

## 状态与幂等

每个 snapshot 登记全部 17 类来源的数量和有序摘要，包括零记录的类型。摘要按 `(source_id, payload_sha256)` 排序后的规范 JSON 计算；同一冻结副本的所有分页必须使用同一 snapshot ID、exported_at 和 source schema。源库 read-only repeatable-read 导出与第二次副本核对提供证据；目标库按全部已留档记录重新计算，缺页、重复来源、不同快照或漂移均不能进入接管。

逐记录转换状态另存 `MigrationRecordApplication`：

- `applied`：已写入新模块可继续操作的对象，并保存对象 ID、代次、源指纹和实际账号映射版本。
- `historical_terminal`：原本已经结束的任务、run、审计等，仅作为历史证据；不等于存在可执行的新任务。
- `recovery_required`：有不明外部结果或活动状态，必须显式核对再恢复。
- `dependency_blocked`：缺少 PN、账号或引用对象的明确绑定。
- `conflict`：目标已有不同人工值、排序或后续编辑，禁止覆盖。

记录转换需要同 snapshot 的固定指纹、当前 application revision 和命令幂等键；同来源重放只核对现状，不重设用户后续修改。恢复决定单独不可变留痕，含操作者、理由、来源及外部核对证据；不把决定写回旧历史事件。

`materialization_complete` 只表示所有来源已有明确处理结果。`live_complete` 另外要求可继续操作的对象确实接入新接口，且没有活动 owner 未映射、未解决外部结果、冲突或依赖缺失。生产切换还需要独立恢复演练、实际 OSS/服务验证和用户验收；隔离副本报告不得声称已冻结生产或可以发布。

## 各来源的处理

| 来源 | 新落点和核对 |
| --- | --- |
| PN | 使用 explicit ExternalReference；summary/brand/manufacturer/category 及规格逐字段比较有效值与人工覆盖。保留人工空值和 field_sources；目标相异人工值进入冲突，不能最后写覆盖。合法自定义规格按原 JSON 类型转入 attributes，标准规格不推测单位。catalog_active 保持 Shop 本地展示意图，不自动改共享 availability。 |
| content / image | 延续已有材质化流程；保留 AI 原状态、Image UUID、原 OSS key、删除状态、用途和来源。新人工改动/排序不被重跑覆盖。 |
| upload / multipart | upload UUID、client_request_id、实际 staging key、到期、分片及 storage_upload_id 保留。活动 owner 必须来自显式 B3 subject binding。已确定 uploading 可继续；初始化/完成/取消的外呼结果不明则进入 recovery_required，不自动再调用云端。旧 lease/token 不作为新执行权。 |
| upload_intent / image_recovery | 保留原 image/etag/attempt/error。prepared 或 waiting_cloud 等不明结果必须核对最终对象和原幂等意图，再由明确恢复命令处理；不自动生成“已验图”结论。 |
| inbox | unassigned 资产、upload ID、group/order、claim/expiry、assignment_key、原文件来源、删除状态均保留。活动 owner/claimer 未映射阻断。assigning 需要核对 image 和 assignment 结果；不以清空认领掩盖未完成归档。 |
| inbox_job / agent_job / agent_run | 终态按真实状态留档，保留 attempt、原 actor；模型及成本仅留在 legacy_evidence 敏感证据，不进入普通历史投影；活动 lease 必须 drain 或由独立 fence 证据撤销。不得把未知旧授权版本改成当前版本并继续跑。新联网/重算任务由当前用户明确启动。 |
| manual_research / bundle | 历史请求、保留预算、次数、结果包引用和到期完整留存。终态成果按当前资源权限投影；新请求有独立预算和授权，不重演未知外呼。未结束任务列入恢复清单。 |
| audit / identity_event | 原事件 UUID、actor subject、快照与合并关系保留，不伪造 canonical actor/人工确认；未完成的身份变更阻断，不能直接重放事实归属变化。 |
| catalog_link / catalog_conflict | 通过根模块的绑定/裁决记录对接；原 archive mapping 不改。只有已核对来源 ID 才可绑定，不能按 PN 显示名近似合并。 |

## 回归与恢复检查

每类至少覆盖：同一来源重放、源摘要漂移、丢页、目标并发变化、未映射账号、保留旧 UUID、事务回滚、历史真来源及终态/活动状态区分。上传需在新 owner 会话下恢复原 upload_id 和分片；收件箱需通过新 API 列出、认领、归档，并保留分组排序；活动旧 worker 晚完成不得得到新执行权。迁移失败恢复只重跑未完成转换，不重发未知云端动作。

新 authority 已接收写入后不能恢复旧数据库抹去新事实；回退只能使用继续消费新 authority 的兼容代码，反向迁移必须另行冻结、演练和对账。当前所有验证仅使用隔离合成数据。

## 已实现的第一条生效链

`migration_apply.apply_source_records` 每次最多处理 100 条，要求已冻结的完整快照清单与当前迁移权限。
锁顺序统一为迁移 namespace → 外部引用 → 身份图 → 排序后的 PN，避免多个来源页与引用解析互相倒序等待。
每条转换使用 savepoint；业务冲突记入独立 application 状态，命令、转换和审计仍由外层事务统一提交。

PN 的有效资料、人工空值保护、自定义参数及 fill_status 已转换。迁移者保留为执行者，历史字段的
manual/AI/Excel/unknown 来源另存，不把历史 AI 文案伪装成人工确认。目标已存在不同值时保留目标并报告冲突。
标准规格仅接受具备明确合法单位/数值证据的值；没有单位的标准文本仍可沿用标准键。其它合法 JSON 原型进
attributes，不推测单位。历史完整 payload、未生效 AI 候选与其他来源覆盖仍可通过 legacy_evidence 查证。

图片/好坏件文案可使用已复核的 ExternalReference 转换，原 archive mapping 不修改。先前归档时未解析的
图片可在精确绑定后归入目标；已被人工编辑或排序的图库不能被导入顺序覆盖。全部 17 类来源现有明确的转换器；业务实体转换为当前可操作对象，历史活动转换为受限历史索引，活动或不明状态进入有命令和页面的恢复流程。缺引用、相异人工值和未知合法性仍明确阻断，不能作为完成结果。

## 上传与收件箱的生效链

`upload`、`multipart`、`upload_intent`、`inbox` 已接入同一 application ledger。
账号必须通过当前配置的来源 namespace、issuer、UUID subject 与有效 canonical account 绑定，禁止按姓名猜测。
保留上传 UUID、原对象位置、文件声明、分片编号、ETag、分组及捕获顺序；这些存储内部字段不出现在普通响应。
迁入后的票据若已被当前用户操作，后续依赖转换检查 revision 并报告冲突，不能用旧状态覆盖。

原 Shop 分片使用固定 `sha256("multipart:" + upload UUID)` 指纹；迁移仅保存这一已知算法的结果。
新上传继续原有指纹协议。分片完成任务保存实际预期指纹，重试先核对已完成对象，因此原上传不因新指纹不同而重复合并。
该指纹没有任何浏览器输入字段。初始化、合并、取消结果不明，或仍有旧 operation token 的会话不转交执行权，
不产生新外呼任务、不发起 abort，记录为 recovery_required。

已完成旧归档的收件箱可以显示其图片归属，但旧记录没有 authority identity/claim CAS 和原审计回执。
因此 assignment_intent 保持空值，明确进入 assignment_receipt_recovery；不能凭后续可变图片用途伪造原成功请求。
prepared 上传意图和 receiving/assigning 状态也保留恢复要求。原先确定的 ready、deleted 与上传终态按真实状态投影。


## 历史活动、成果与明确恢复

新增 `GET /legacy-history` 只展示当前账号可归属的安全历史摘要。SQL 先按 owner、来源 namespace、PN/inbox 范围筛选，
固定 source_record_id keyset 排序，每次最多扫描 200 条、返回 100 条。逐条重新检查当前 PN 或收件箱权限；
即使一页无可见记录，只要 next_cursor 非空仍需继续。原 provider payload、账单、模型成本、object key 和历史身份快照
不进入此接口。未知历史 canonical owner 不猜测、不向普通账号开放，原记录仍由具备 legacy_evidence 权限的迁移人员核对。

旧收件箱成功识别结果保持可用：仅投影候选 PN、置信度和品类，去掉旧库匹配对象及其私有字段；当前服务重新查询匹配。
原来源未记录模型时标为 legacy / unrecorded，不编造模型名或费用。当前代次变化后不再展示旧识别结果。
历史捕获序号保留原 int32 范围；新上传命令仍限制 0..1999，组大小限额不增加。

旧成功 bundle 通过 `GET /legacy-history/{record_id}/bundle-downloads` 按当前 owner、图库、图片可见性与用途重新核对。
逐图比较原对象/hash/bytes/content_type，不使用旧权限或旧图库数字版本授予访问。只签固定旧算法的
`bundle/{source UUID}/part-{number}.zip`，签名为本地计算；原到期时间和创建后 24 小时上限均保留，迁移不续期。
撤权、图片隐藏/删除、用途或对象变化时拒绝下载；过期时提示按当前图库重新打包。URL 不入数据库。

原生 ITSP 管理页 `/pn-migration` 使用新框架的 read_legacy/materialize_migration 与 legacy_evidence 字段权限。
所有 `/migration/*` 路由是 native-only，并标记 x-itsp-only 以排除 Shop BFF/browser 生成。
原始证据 detail 只在用户明确打开时读取，每条来源原有限额 512 KiB；普通编辑者或 Shop 委托不能调用。

恢复 POST `/migration/records/{record_id}/recovery` 是闭合的三种命令，均要求 source SHA、application CAS、
核对证据引用、理由、外部结果核对结论与冻结副本的旧租约隔离证据：

- `retire_fenced`：结束旧执行并保留全部历史，不做云端 abort/copy/complete，也不声称已实际核验远端结果。
  尚未被新用户操作的未知上传变为 cancelled/failed；已经归档的 inbox 必须走下面的归属确认，不能退回。
- `confirm_assignment`：重新核对当前 identity/inbox/entry 基线，建立新的 assignment key、claim fence 和当前审计；
  原 assignment key 与原回执不伪造。新确认的跨请求重放只引用这次审计。
- `restart_current`：用当前已验证账号和当前权限明确发起 research/recognize/bundle；只入当前队列，不在事务中外呼。
  必须确实存在新 pending job 并校验其完整当前 ActorContext 后，才记录 replacement_job_id。旧 lease 和旧授权 epoch 不转移。

恢复操作与新任务、不可变恢复决定、审计、回执在同一事务中。并发决定只有一个 application CAS 成功；
响应未知时页面锁定原 payload/request ID，继续核对原请求，不生成新决定。撤权后不能回放旧结果。
此处的结束/保留是迁移人员的新审计决定，不会重写原历史状态；所有决定均可回溯到原 source_record_id。

## 本阶段验证边界

合成隔离 PostgreSQL 覆盖 17 类转换、真实并发恢复、回滚、来源漂移、引用及账号缺失、原始 UUID/删除状态/AI 来源、
旧 multipart 指纹、旧已归档确认、int32 捕获序号、识别安全投影、bundle 当前权限与到期。
原生管理路由另用真实 Cerbos 验证敏感字段撤权与 Shop 委托拒绝；前端覆盖仅读不写、证据按需读取和响应丢失固定请求重试。
这些不构成真实 OSS 字节校验、生产冻结、外部成果人工复核或生产切换验收；cutover_ready 始终 false。
