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
| PN | 使用 explicit ExternalReference；summary/brand/manufacturer/category 及规格逐字段比较有效值与人工覆盖。保留人工空值和 field_sources；目标相异人工值进入冲突，不能最后写覆盖。未知规格键必须明确映射或扩展后才算 live-complete。catalog_active 保持 Shop 本地展示意图，不自动改共享 availability。 |
| content / image | 延续已有材质化流程；保留 AI 原状态、Image UUID、原 OSS key、删除状态、用途和来源。新人工改动/排序不被重跑覆盖。 |
| upload / multipart | upload UUID、client_request_id、实际 staging key、到期、分片及 storage_upload_id 保留。活动 owner 必须来自显式 B3 subject binding。已确定 uploading 可继续；初始化/完成/取消的外呼结果不明则进入 recovery_required，不自动再调用云端。旧 lease/token 不作为新执行权。 |
| upload_intent / image_recovery | 保留原 image/etag/attempt/error。prepared 或 waiting_cloud 等不明结果必须核对最终对象和原幂等意图，再由明确恢复命令处理；不自动生成“已验图”结论。 |
| inbox | unassigned 资产、upload ID、group/order、claim/expiry、assignment_key、原文件来源、删除状态均保留。活动 owner/claimer 未映射阻断。assigning 需要核对 image 和 assignment 结果；不以清空认领掩盖未完成归档。 |
| inbox_job / agent_job / agent_run | 终态按真实状态留档，保留 attempt、原 actor、模型及成本；活动 lease 必须 drain 或由独立 fence 证据撤销。不得把未知旧授权版本改成当前版本并继续跑。新联网/重算任务由当前用户明确启动。 |
| manual_research / bundle | 历史请求、保留预算、次数、结果包引用和到期完整留存。终态成果按当前资源权限投影；新请求有独立预算和授权，不重演未知外呼。未结束任务列入恢复清单。 |
| audit / identity_event | 原事件 UUID、actor subject、快照与合并关系保留，不伪造 canonical actor/人工确认；未完成的身份变更阻断，不能直接重放事实归属变化。 |
| catalog_link / catalog_conflict | 通过根模块的绑定/裁决记录对接；原 archive mapping 不改。只有已核对来源 ID 才可绑定，不能按 PN 显示名近似合并。 |

## 回归与恢复检查

每类至少覆盖：同一来源重放、源摘要漂移、丢页、目标并发变化、未映射账号、保留旧 UUID、事务回滚、历史真来源及终态/活动状态区分。上传需在新 owner 会话下恢复原 upload_id 和分片；收件箱需通过新 API 列出、认领、归档，并保留分组排序；活动旧 worker 晚完成不得得到新执行权。迁移失败恢复只重跑未完成转换，不重发未知云端动作。

新 authority 已接收写入后不能恢复旧数据库抹去新事实；回退只能使用继续消费新 authority 的兼容代码，反向迁移必须另行冻结、演练和对账。当前所有验证仅使用隔离合成数据。
