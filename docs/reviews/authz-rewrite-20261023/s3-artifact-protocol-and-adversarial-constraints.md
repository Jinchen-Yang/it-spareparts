# S3 文件协议与对抗约束（设计，未实施）

本补充承接 `s3-artifact-task-execution-seams.md` 与 `s3-execution-contract-delta.json`。仅审查和约束，不表示 S3 已获实施许可。共享 authz/principal/service/provenance 与单一 Alembic 归 root；文件、工作簿、来源任务适配由领域包承接。

## 1. 先冻结最小共享接口

```python
# principal.py；只能由服务器从持久任务行调用，不能接受 HTTP 自报身份
resume_task_identity(db, *, owner_user_id: int, enqueued_epoch: int) -> VerifiedIdentity

# provenance.py；现有调用签名不改
validate_provenance(db, identity: VerifiedIdentity, value: dict) -> None
AccessCollector.record(access: ResourceAccess) -> None
AccessCollector.inherit(value: dict) -> None
AccessCollector.snapshot() -> dict

# 领域 resolver registry；只能登记服务器函数，不能解释 JSON 为代码/SQL
revalidate_project_dependency(db, identity, dependency) -> ResourceAccess

# 共享文件模块，具体模块名由领域 owner 落地
build_artifact_manifest(*, document_access, format_id, content_sha256) -> dict
validate_artifact(db, identity, *, manifest, artifact_ref, purpose) -> None
```

`resume_task_identity` 必须按稳定 ID 查有效且未删除账号、精确比较 enqueue epoch；返回身份应保留 `expected_user_id`，后续 `load_principal` 每次再次比较。只把 ID 翻译为当前 username 再丢掉 ID 不足：恢复后改名/删除与同名建号可能重新绑定另一个主体。不能把当前 epoch 赋给旧任务。HTTP token 可继续原验签模型，此字段为服务器任务适配器的额外约束，不是客户端权限。

schema 2 保留根键 `schema_version/principal_id/authorization_epoch/catalog_version/resources`。每个依赖项仅允许：

| 字段 | 允许值和用途 |
| --- | --- |
| `resource_id` | 已登记资源 ID |
| `actions` | 非空、无重复的已登记动作；每项都重新授权 |
| `grant_version` | 正整数，精确比较当前 Grant |
| `object_ref` | 全局依赖为 null；对象为 `{kind,id}`；集合为 `{kind,id,selector}`，kind/id 必须在封闭 registry 中；selector 仅该集合定义的规范化参数 |
| `authorization_revision` | 全局为 null；对象/集合为非空 opaque 字符串，重新解析和计算后比较 |

不保存 SQL、attributes、predicate、角色或可调用 resolver 名称。去重键为 `(resource_id, canonical(object_ref))`；同资源不同项目不得折叠，revision 冲突不得取最后一次。schema 1 仅继续支持原全局依赖；绝不能把有 object 的 schema 2 降为 schema 1。当前 `ResourceAccess` 缺完整 object_ref，需要 root 增加不可变可信引用并由 domain adapter 在 `_record` 前填入；不能在 collector 根据 object_id 猜对象类型。

`selector` 的项目集合、月份、filter、用途都参与指纹；pagination 不改变已选全集。客户端请求只能给筛选输入，服务端根据 registry 再规范化并重查全集。对象 authorization_context 与 provenance 必须来自同一次主体/关系/Grant 判定；UI token owner + epoch + catalog + query identity 共同隔离响应，不能只比较 project.version。

## 2. Manifest 必须证明输出列集合

建议 manifest 闭集键：`schema_version=1, format_id, projection_digest, content_sha256, producer, content_dependencies, complete`。`producer={user_id,authorization_epoch}` 为审计/任务身份；`complete` 必须 true。NULL、缺键、未知格式、未知列、无法解析完整原件不能降为可信空依赖。private Agent 内容仍严格 owner+epoch；共享项目文件的 reader 是当前请求主体，由 content_dependencies 重新选择完整资源合同，不能假冒 producer，也不能因为 producer 离职而默认把 reader 授权省略。

`projection_digest` 应由服务器格式 registry 生成：格式协议版本、所有 sheet/table（含隐藏页）、稳定列 ID、列类型、导出顺序、派生字段的原始数据依赖、mask/null 规则、该内容采用的资源合同。按确定的 JSON canonicalization 哈希；值和行数据由 `content_sha256` 负责，不混入列摘要。排序/格式变化是否触发失效由显式格式版本决定，不能靠随意删掉 digest 项放行。导出代码新增敏感列却未登记应在生成器/契约测试失败，而不是让未登记列跟随原授权输出。

项目 A 的 managed 合同与项目 B 的 general 合同分别保存。每个项目必须由一个完整候选合同提供所需 scope 和 fields；不同资源的字段 union、A 的关系+B 的字段不能组成一个完整文档允许。若某张表确实混合不同资源内容，逐块列明 AND 依赖；这不同于把互斥候选的字段拼接。

读取固定旧格式文件时可保留注册的旧格式定义，但完整内容所需字段必须仍可重验；不能拿当前 renderer 少了某列为由降低旧 bytes 的资格。原始上传包含未解析额外页时，只有覆盖该完整内容域的明确原件资格可以下载，否则保守拒绝并允许重新生成受控版本。

## 3. 任务、关系、发布的一致性

1. 任务行保存稳定 owner ID、enqueue epoch、generation、来源 manifest/provenance。retry 是当前用户明确重新授权的新 generation，不是自动替旧任务续签。cancel 可只返回无敏感内容的状态，但必须验证自己的 task 动作和 owner；不能为取消失权任务先返回旧 preview。
2. 工作线程必须自己创建 Session、安装 recorder。start、首次领域读取、外发 LLM 前、LLM 返回后、preview token/错误文件/结果发布、幂等回执、每次 bytes 发布均重新检查。没有 callback 只说明没记录依赖，不能证明原件安全。
3. 关系指纹依赖完整真实 parent、有效 links、assignment/actor 映射及集合 selector。多次 READ COMMITTED 查询不能拼成授权快照；要用已定义短一致性读取/领域锁，使授权和投影看到同一关系。读者不依赖 username 字符串推断 owner。跨父对象移动要检查旧、新双方。
4. 发布前短事务检查 generation CAS 和当前授权，再保存 manifest/ready 状态；未发布临时 bytes 不得被历史路径、异常返回或并发下载获取。事务失败只清理/保留隔离临时文件，不返回 token/path/成功回执。
5. 独立 artifact-protocol 标记事务先于任何领域/actor 锁；不持锁跨 LLM 或下载。逐 chunk 重验的保证是“下一次发布检查观察到撤权后不继续产出”，不是撤回已发送 bytes，也不是在没有锁的网络 send 间隙宣称全局原子撤权。
6. opaque 指纹+每次实际重验不证明任意 SQL A→B→A。授权判断使用当前关系；若要求曾变即永久使旧文件失效，需额外版本化关系写路径，当前设计不能宣称已实现该保证。

## 4. S3 → S2 → S1 计数器接缝尚需 root 冻结

已核对 S2 `c9d4e8f2a6b1` downgrade：在表锁下逐实体比较 `current == receipt.after_state`，其中含 token_version、Grant.version、template.version；安全恢复 before 时又单调增加这些计数。因此 S3 即使逻辑授权无变化，安全回退后的计数也可能让随后 S2 回退误拒。倒退计数会复活旧 token/preview；无条件改写 S2 receipt 则会掩掉真正的管理员改权。

建议最小算法（待 root 明确采用，不能当既成实现）：

1. S3 upgrade 记录自己的完整 before/after、实体集合和前驱 S2 receipt 摘要；必须记录实际读到的计数，不只保存配置 hash。
2. S3 downgrade 先按统一顺序锁 receipt/cutover/授权表/涉及业务任务与文件元数据表；任何 S3 cutover、artifact-used、新协议非 SQL NULL 数据拒绝。逐项验证 S3 after 精确未变、实体集合未变，校验来源 true/false、空 override/tombstone/固定 snapshot 都未损坏。
3. 计算回退配置；user epoch、每个保留 Grant.version、template.version 均严格递增。越界或不合法计数时整事务拒绝，不能部分 DDL。
4. 只有 S3 **before 全态** 与当时保存的 S2 after 相等（或已有可验证的前驱 counter forwarding 链），且当前前驱 receipt 摘要仍等于 S3 记录值，才允许转发前驱 **after 中的 counter 字段** 到恢复后的实际值。非 counter 字段、before_state、来源资格、snapshot、实体边界绝不改写。转发与恢复在同一事务。
5. 若管理员在 S2 与 S3 之间改过授权，即使又改回同样配置，其 counter 也不会匹配；S3 可按自身 receipt 回到它的 before，但不得为其重写 S2 after，后续 S2→S1 继续拒绝。没有可证迁移链时保守拒绝深层回退。
6. S3 不删除 S2 artifact 哨兵；新 S3 artifact 哨兵应独立可识别，并让 canonical 权威下限持续有效。回退过程中任一并发 canonical 创建、关系协议发布或标记事务必须被同一锁协议串行化；guard 到 DDL 不能有未保护插入窗口。

## 5. 独立失败用例计划

| 向量 | 预期与证据 |
| --- | --- |
| 恢复 task 身份后旧账号改名，另一个 ID 复用用户名/epoch | 后续 task/PDP/发布拒绝；确认没有开始敏感 SQL/LLM |
| A 的 manager 完整字段，B 仅销售且 F=false，混合工作簿 | A 可单独生成；A+B 整份拒绝，不拼字段 |
| 同资源 A/B 两对象；B 撤关系或 link 改挂 | schema 2 保留两项；回放/下载/错误回执拒绝，不能只验 A |
| 在 parent 读取与 assignment/Grant 读取之间提交关系移动 | 只接受一个一致快照；不能授权旧 A 而读取新 B |
| renderer 新增隐藏财务列/错误表加原始单元格 | 未登记 projection digest 失败；旧 manifest 不能放行新 bytes |
| 原件 queued/failed 无 plan rows、额外 sheet/未解析行 | 不能变空依赖下载；隔离留存可保留 |
| enqueue 后撤权；LLM 等待时撤权；生成后发布前撤权 | 无 ready/token/文件链接；retry 不替旧 generation 换 epoch |
| 第一 chunk 之后撤权，第二连接提交后放行下一检查 | 后续 chunk 停止；已发 bytes 不宣称收回 |
| 幂等 apply 返回 accepted_batch/result_json，但当前 reader 已失权 | 无写也重验，不返回旧回执/敏感统计 |
| S2 baseline→S3 up/down→S2 down/up，未 cutover/无人工写 | 若采纳 forwarding，允许并保持各 counter 单调；旧 token/preview 始终失败 |
| S2 人工改权后→S3 up/down；或人工改回原配置 | S3 可安全回 before；S2 深层回退拒绝，不能擦掉 counter 证据 |
| source=false 空拒绝、模板空 snapshot、来源 true 现有 override | 多阶段往返保留精确来源语义，不能重新 seed |
| 前驱 receipt 丢失/被改、实体增删重建、计数溢出 | 原子拒绝，schema/source 不丢失 |
| 第二连接未提交 artifact sentinel/canonical row；downgrade 等锁 | writer 提交后 guard 看见并拒绝；rollback 不擦来源 |

最终实现前仍须闭合：schema 2 的具体不可变 object_ref 类型；共享文件 reader 的 content-dependency 重授权返回类型；各 renderer 完整投影 registry；S3 counter forwarding 是否采用及其保存位置。上述任一项未落，不应把“有 provenance JSON”当作文件全链已受保护。

## 6. 入口上下文与读写一致性的可实现性复核

独立复核发现两项原合同阻断，根代理已采纳修正方向，尚不视为运行验证：第一，71 个相同 collection selector 占位不能证明具体动作可接受何种对象，尤其不能让集合资格满足 proposal.accept、batch.apply 或 raw_download；第二，多项目关系布尔值不能先 any/all 聚合再送 PDP。`A.manager=true,B.viewer=true` 应分别满足每个项目的 `(manager OR viewer)`；`A.manager=true,B.none=true` 必须整件拒绝。owner 是另一独立 AND，不能归入项目布尔聚合。

本包给后端合并的 `/tmp/s3-task-flow-action-contexts.json` 精确覆盖 **23 资源、56 个入口**，保留各入口原 resource/action bindings，补逐入口 kind、固定 purpose、封闭 selector 类型、真实 parent 解析和完整对象量词；不是重新定义权限种子。纯列表的 SQL 条件必须作用于统计、排序、分页之前。批量写的 discovery 只确定候选，必须随后对实际对象逐项完整授权；不允许 discovery-only 结果冒充允许证明。项目 ID 使用 project 类型，不冒充 project_assignment 子对象 ID。

读的一致性由核心拥有的 context manager 保证：专用不可伪造 `ReadSnapshotProof` 绑定同一 Session、同一活动 `REPEATABLE READ READ ONLY` 事务。仅该 proof 可使 `_read_principal` 不取默认 FOR SHARE；结束事务后、换 Session 后复用都失败。关系、主体、Grant、事实、字段投影以及 DTO 物化必须全部留在该事务内，不允许把已授权 predicate/ORM 留给另一个连接执行。每次 Agent/LLM/文件 chunk 边界重新取得短快照；不持该事务跨网络等待。纯读使用此路径，不会产生“第一个对象已拿 actor SHARE、第二对象再等领域锁”的反序。

写的一致性不复用上述读快照：先按完整业务 topology 收齐所有旧/新 parent 和 child 锁，再按稳定 ID 与最终需要的强度一次锁所有参与用户，最后执行授权 callback 与投影。callback 不补领域锁、不把 SHARE 升级 UPDATE、不换连接。解析后发现新 parent/新候选必须回滚重试。项目 SHARE 不能替代 child 锁：receipt 移动也可能先取得项目 SHARE 再修改 child，所以只有逐 child 完整锁集或一致读快照才成立。

仓库 preview→apply 的实际生命周期已经核对：preview 是纯解析并返回计划、内容导出的 import_id 与 HMAC，不保存 bytes；apply 重传完整 multipart 文件并重新解析。最小新增采用服务器可信 `operation_input`：真实 actor、固定入口 purpose、规范参数和实际输入 SHA256；它只被具体注册的 preview/create 动作接受，不造不存在的仓库导入实体。首次 apply 重新验证实际 bytes、导入 ID 和 HMAC，并在 worker 当前身份/本事务中授权；已有 import 的 replay 则解析真实对象。HMAC 认证输入计划，不认证当前权限。若日后增加跨请求获取预览/原件，必须在可取回前登记 sidecar 与完整 manifest；现阶段无需为纯预览强建业务实体。

必须执行的新增反例：

- 将合法 list collection selector 用于 proposal.accept、return raw_download、另一个 resource/action：在业务 SQL 前拒绝。
- A 管理员关系、B 查看关系分别可读的完整文件允许；B 无权或字段缺失时整个文件拒绝；owner 不匹配时即使全部项目允许仍拒绝。
- 同快照读到旧 parent/Grant/事实，另一连接移动 parent 或撤权；旧快照内不得混入新事实，下一次快照必须拒绝。
- 将 proof 交给第二 Session、结束后复用、普通 dict 模仿 proof：拒绝且默认主体读取锁未被全局移除。
- 两对象读取不持 actor 后追领域锁；写 callback 所需对象集变化只冲突，不补锁或部分提交。
- 50/51 个 PDP 项、同资源 51 个不同对象、第二批超时/缺项/重复/非法字段：所有批次严格验证完成前不返回任何部分允许；各批共享同一主体、Grant 与数据库观察。
- warehouse apply 仅给 UUID/HMAC、不重新提供匹配 bytes，或 bytes 相同但 actor 已撤权：拒绝；真实已有 import replay 不能跳过当前授权。
- warehouse 歧义候选六类 target 均按真实对象解析；dim_part 不编造项目，warehouse_document 则继续解析其实际项目 links；客户端换 target_type/target_id 不能绕过 stored candidate 与 link-kind 矩阵。

本段确认方案可实现的必要约束，不声称 S3 已通过测试。合并后的逐动作注册表仍须机械校验与实际 resolver/SQL/并发反例验证；资源数、入口数相等本身不是权限语义覆盖证明。

`operation_input` 还有持久化边界：它证明本次服务器实际解析的输入，不是以后凭 SHA 即可恢复的数据库对象。纯预览不保存历史可随请求结束；需要发布可回放结果时，先把输入与真实 batch/sidecar/generation 和完整 manifest 绑定，后续经持久域对象重新授权。collector 不得静默删除 transient operation 依赖再声称“可信空来源”；未完成绑定的 transient ref 不允许作为可持久回放 provenance 发布。

根代理进一步明确：只对逐项注册的 operation_input 入口提供纯授权解析 view（不落 staging、不调用 LLM、不返回领域 payload）；正式命令必须与实际 bytes、规范参数和完整目标集合摘要一致。具名全局 bulk 动作也可使用命令计划 operation_input，但该 proof 仅准入编排：候选发现 collection 不授权，所有实际 child/project 仍逐对象完整 AND；领域锁后集合/关系变化则冲突回滚。不能把统一 operation_input 类型解释为所有动作都能接受任意命令 hash。

空集合同样不能省略准入：bulk 目标数为 0 时 `ALL([])` 为真，不代表任何账号都有该动作。0/1/51 个目标都先验证具名 operation/action 的当前 Grant、字段、epoch，再检查实际 targets；无 Grant 的空集合请求也必须拒绝，不能返回成功或全局候选计数。

## 7. 合并合同的最终独立复核结论

复核 `s3-execution-spec.json` SHA256 `7081c058de7ebebd845c171a6f35578ea86d43c80d5f531316e9fdb9087cf3c6`：原两项阻断在**设计层**闭合。通用 collection 占位已改为精确入口/动作合同；global workbook apply、business-type-backfill apply 的 collection 明确 non-authorizing，另有具名 operation admission 和完整实际项目 policy subjects。master workbook 重挂增加 source-order 实际集合与全部 old/new parents；collection-plan apply 保留稳定 owner AND 整个 contract plan，未借普通 project.read 代替原高风险全计划资格。空集合仍先校验操作 Grant，不能利用全称空真绕过。

本次作者交叉复核重点是另一作者的 172 个入口定义及共同核心；我写的 56 个任务入口由另一作者合并复核，不把自写内容计为独立审查。源码对拍确认：旧 V2 工作簿 K+C+F、V3 的 C+F、月度各端点的 F+当前非空 primary-manager 集合仍独立；manager 与 viewer 关系没有混为一个布尔量；collection-plan 的 all_imports 读取和 own_imports 应用仍分资源。所抽查的 whole-artifact/任务/锁序/主体接缝未发现新的未决设计阻断。

独立原样运行设计校验器通过：73 资源、232 动作（128 data/104 mutation）、228 human、11 retired 保留、38 collection refs/79 exact purposes、33 child types、13 base+23 input resolver，612 真实持久 legacy 输入×73资源共 44,676 个种子评估；日志 `/tmp/itsp-s3-spec-independent-final.log`。这是静态设计与种子规则证据，**不证明** SQL/PDP adapter、PG 迁移、锁并发、完整入口等价或产物生命周期已经通过。

新增身份扫描结论由 `s3-artifact-sidecar-schema.md` 第 5/6 节承载：sidecar+6 owner 列、canonical 稳定 ID 幂等 namespace、迁移三阶段职责隔离、delete intent 原操作者、签名工作簿稳定 owner、S3 复用 bulk 的受控 callback，以及 S4 退役审批的后续清单。它们是实施必须交付的安全约束；缺任一可达旁路时不能按本设计结论验收实现。仍待主代理通知 S2 精确提交 CI 全绿后才开始 S3 应用/测试实施。

## 8. 现有 child 的授权视图补充

对更新的 spec `23e5d4d144b8ed567e17e0e76dca1bb583bcff047019646db38c22e0bb7d1497` 复核，基础接口统一为 root ContextBatch/ReadSnapshotProof，RR READ ONLY 内解析和 DTO 物化，与本报告锁序约束一致；原入口和 seed 合同未变。

根代理采纳两项有限补充，不扩张通用授权接口：

1. 现有 child GET 的 `view=authorization` 只对该 GET 自身动作和完全依赖同一已存 topology 的精确登记动作给出决定。低权 read 的 allow 不能复制给 write，也不能把旧 parent 的许可当作新目标许可。具体反例是 `PATCH /api/maintenance/return-receipts/{receipt_id}` 的 `ReceiptUpdate.project_id`：只授权旧项目 A、正文改为 B 时，GET 视图不得产生可用于 A→B 的 allow。依赖新 parent/selection 的 POST/PATCH 必须用同命令路径、完整实际 body 的零业务写视图解析全部目标；正式写仍持完整锁后重新授权。无完整命令上下文时返回 requires_context，不猜请求目标。
2. ResolvedContext 显式绑定精确 `entry_id` 与已解析 branch，禁止只根据 kind 的并集匹配。空实际集合只在已登记 operation_input、服务器证明完整 verified-empty 且 live explicit Grant/action/fields/epoch 通过时允许无操作；普通 project/child 或未解析/分页集合不能凭 `ALL([])` 放行。

实施必测：GET read-only grant 不得到同对象 write allow；旧 A 的 context 带到移动 B 命令拒绝；合法 child context 换 entry/action/branch 拒绝；同路径 authorization view 无业务/staging/task/文件发布/LLM 副作用；完整 body 视图允许后改任一输入/目标/epoch，正式命令冲突或拒绝且不自动重放；普通 child 空 subjects 拒绝，具名 bulk verified-empty 在无 Grant 时也拒绝。只读视图不是保存的能力凭据，正式执行从不省略当前授权。

## 实施审查修正：共享 reader 与内容拓扑版本分离

根独审发现真实阻断：`ProjectEvidence.authorization_revision` 含当前 actor ID/epoch/sales 映射，不能拿 producer 的该值作为共享文件当前 reader 必须相等的内容指纹。否则不同但已完整获权的 reader 永远不能读取共享文件，静态 revision 的合成测试无法发现。

实施改为两个明确值：`authorization_revision` 保留当前授权观察；`content_revision` 仅绑定真实 carrier、完整 parent/link 拓扑、输入、generation 和格式内容选择，不含 reader。项目关系基础提供包含完整 assignment 集合的 `ProjectEvidence.content_revision`，文档 adapter 进一步组合实际 carrier/file 绑定。manifest 固定后者；每次读取仍针对当前 reader 执行完整 DocumentAccess/PDP，不用 producer 身份、不去掉关系变化对拍。实际 bytes 的完整列集合另由格式 inspector 核对 projection digest，内容 hash 独立核验。

新增真实 PostgreSQL/Cerbos 测试以一个实际 MaintenanceProject、实际 viewer assignment、两个不同 user ID 和两份不同 scope/version Grant 读取同一共享文件：授权观察不同、内容拓扑相同仍允许。追加一个新真实 manager assignment 后，reader 本身仍是 viewer、当前 action 仍有资格，但旧文件因完整拓扑变化拒绝；reader 自己 Grant 撤销也拒绝。运行证据随实现测试报告记录，不把静态 test document 当作该边界证明。

## 实现阶段交叉核验补充

本包作者不能将自己的 artifact/workbook 测试作为独立评审。Backend 角度指出月度工作簿先 probe 后等待 project 锁时 ORM 可能仍保留旧 version；本包新增真实两个连接，并由 `pg_stat_activity.wait_event_type='Lock'` 确认等待。旧实现错误提交（`/tmp/itsp-s3-monthly-lock-red.log`），锁后 `populate_existing` 修复原样用例拒绝旧批次，新月度 8 + 原适配 10 共 18 项通过（green 同前缀）。Backend 已复读修复位置，未削弱业务 CAS。

Root 编写的 migration-run 包由本角度只读交叉审查并独立复跑 `test_authz_s3_migration_runs.py`：9 项通过，`/tmp/itsp-s3-migration-cross-review.log`。核查了实际 run/event sidecar 绑定、稳定创建/对账/审批 ID、未知历史拒绝、每次 replay 当前 grant、完整事件链摘要、RR 内 manifest 物化和领域锁后的最终 actor/PDP。此次范围未发现新增阻断；结论仅覆盖这一包，不等同 S3 全入口、最终 CI 或用户验收。

共享错误文件新增 producer/consumer 独立合同之后，当前完整 producer 缺 action 或缺 customer 字段均不得发布；真实外项目许可也不能代替 actual parent。上述为作者回归证据，由 Root 复审共享接口；不得称作者自签通过。

### 行工作台跨作者实现复核与下载前置条件

独立读取 root 的 `maintenance_row_workbench_access.py`、API、query/field/export 分支：canonical 金额只取当前完整 Grant 的 `data_purchase_cost`，legacy 的 `data_profit` 解释器留在旧分支；查询范围在 SQL 中先于 count/page/两种导出，未观察到跨 alternative 借字段。新增 `test_authz_s3_independent.py` 两个真实 PostgreSQL/Cerbos 用例，直接调用受保护导出后再逐块消费 ASGI response，分别确认 CSV/XLSX 返回前连接已归还，慢端发送期间没有 SQL，并且最终关闭 spool。与原作者 6 项组合 **8 passed**，日志 `/tmp/itsp-s3-row-independent.log`。本结论只覆盖该包，不替代 S3 阶段验收。

本作者产物 helper 追加同 RR 文档 precondition：所有实际 access 的主体、epoch、catalog 必须一致，完整文档授权后使用 core strict comparator 比较实际 carrier/revision，再读取 bytes。真实错误工作簿 HTTP context 正例、旧 revision 409、view-only 重取及拒绝混 catalog 共随 artifact/v2 组合 **21 passed**；已有工作簿正式 GET 接线组合 **35 passed**，原 master DTO/行表/验证错误回归 **151 passed**。日志分别为 `/tmp/itsp-s3-artifact-precondition.log`、`/tmp/itsp-s3-workbook-get-preconditions.log`、`/tmp/itsp-s3-master-dto-regression.log`。本段的 helper 实现是作者验证，仍需其他角度复审。


Root 编写的验收附件包交叉复核：实际 BusinessFile→active Link→Deliverable→Project 绑定、完整字节在 RR READ ONLY 内取回并校验 SHA、离开快照后独立写审计；命令先收完整 project/deliverable/link/file 锁，再取 actor SHARE。独立新增两个真实 PG/PDP 故障向量，在完整 publish 成功后及最终 commit 前抛错，均确认已经写出的实际文件被删除，File/Link/Operation/Sidecar 全回滚，独立 S3 protocol-used 哨兵保留。加上作者 10 例与既有 row 独审 2 例，共 14 passed（`/tmp/itsp-s3-acceptance-independent.log`）。未发现该包新的阻断；这不是 S3 全阶段验收。

Master V1/V2 实现组合 204 passed（`/tmp/itsp-s3-master-combined.log`），另严格 view/当前授权前置条件 1 passed。包含实际改价/项目备注/源单跨项目重分配、最终 actor 锁前第二事务撤 epoch 导致全部业务回滚、稳定 owner 改名后的原回执、原始父集合外迁后拒绝。Backend 独审指出闭包内 source assignment 新代数必须改变 authorization_revision，现补实际 assignment ID/version/project；immutable receipt content_revision 保持不受后来项目备注影响，当前权限独立每次重验。此段为实现证据，独立对抗复核由另一作者新增文件承担。

## 补库独立交叉审查

实际版本生产者 `Version.owner_user_id` 与申请 owner 分离；当前 reader 以自己的完整 Grant 读取，版本内容和稳定生产者由真实行与 sidecar 对拍。写路径按领域锁→actor→完整当前授权→业务与 sidecar 同事务；读取和 XLSX 在 RR 内完成。未发现当前包阻断。新增真实 PG/PDP 两例通过：真实 binding 注册后抛错，申请/版本/明细/审计/sidecar 全部回滚；数据库拒绝 submitted 明细修改，合法追加实际版本审计后原 whole-record binding 使 detail/export 同时拒绝。证据：`test_authz_s3_independent.py`，`/tmp/itsp-s3-replenishment-independent-final.log`，2 passed。这是本地独审，不替代 S3 全阶段 CI 与统一验收。

## Warehouse / AI 交叉证据与导入事务收口

Warehouse 静态核对实际递归parent closure、查询谓词、零写preview、domain→actor及actualowner receipt后，独立真实manifest发布完成后注入失败，Batch/Document/Link/Audit/sidecar全部回滚至原计数（1passed，`/tmp/itsp-s3-warehouse-independent.log`）。当前content_revision包含项目内容版本，纯备注修改也可能导致历史回执保守失效，这不是身份撤权。

AI新增跨操作者真实反例：A生产proposal后停用并撤epoch，B仅有accept可按storedtype解析并生成B-owned实际batch/acceptance binding，原proposal owner仍A；当前B accept撤除后回放403（1passed，`/tmp/itsp-s3-ai-independent.log`）。当前未发现这两包阻断；author tests不冒充独审。

Doc/CKD canonical错误统一外层rollback，保留原raw pending与错误响应，不在撤权或领域锁释放后另起事务写failed；legacy原失败状态行为保持。no_targets只有完整实际解析确无项目时可走，仍要求batch.apply AND targets.apply+cost，实际batch empty-plan subject关系全false，仅明确all_projects作用域可通过，不能ALL(empty)默认允许。新10真实PG/PDP已过，等待其他作者独审。

## 来源取消与 WBDD 接线证据

来源取消四入口采用 actual source + batch + project，来源写完整域锁后才取 actor 最终锁；稳定 ID 的回执 namespace 与整份实际 report_json 摘要并存。新 3 例与旧 45 例合计 48 passed，涵盖改名重放、独立 confirmation 权限、最后撤 epoch 后墓碑/明细/回执/sidecar 全回滚，以及历史 JSON 被改后拒绝。WBDD 上传与核对四入口的新 7 例通过；同一 reconcile 动作的 discovery SQL 只用于完整候选范围，随后仍须 actual batch 与每个实际父项目/无父来源全称授权。原无父来源需 all_projects 显式动作，不能利用空集合授权。核对回执保留每个源的原父集合并与当前父集合共同重验。新包尚待其他作者独立审查。

返还二十入口独立静态审查未发现阻断。新增真实 PostgreSQL/PDP 反例在 publish_artifact 已写完整 manifest 后抛错，确认 BadReturn、Command、Audit、Artifact 全回滚：1 passed，日志 /tmp/itsp-s3-return-independent.log。作者 19 新 + 110 旧例为补充证据，不计作独立作者的新测试。

## 尾部共享服务独立复核补充

- Collection plan 来源下载的 reference callback 原先要求不存在的参数，两个真实下载正例得到500；作者改为兼容核心签名后，独立原样复跑全部9例通过：`/tmp/itsp-s3-collection-cross-review-fixed.log`。
- Global workbook 六入口独立复跑作者8例通过；静态核对同RR完整导出、SQL字段投影、完整recompute closure与最终actor检查，未发现新增阻断。
- Demand 13入口与 master剩余3入口独立检查实际source/line/intent、完整上传未改行、历史/当前父集合、producer/consumer不同动作和事务收口；原样复跑20+6例通过（与返还导入9例组合35通过，`/tmp/itsp-s3-demand-master-return-cross-review.log`）。
- 独立新增 `test_authz_s3_tail_independent.py`：restore持真实domain锁后另一连接仍可修改actor epoch，随后401且无恢复；真实demand已执行回执读取确实经过artifact/READ ONLY REPEATABLE READ且无DML或行锁。连同manager目录HTTP的准确precondition/no-store测试共3通过，`/tmp/itsp-s3-tail-independent.log`。
- 覆盖账重算为 workflow134 live+11 retired，与另一账94 live交集为空，live并集228，逐entry等于执行规格。此前文首135与陈旧counts已修正。retired入口保持410，不计canonical业务已实现。

## 返还回执异步导入 6 入口本地实现

`maintenance_return_import_authorization.py` 接入 submit/job_read/raw_download/retry/cancel/apply。实际上传字节先纯解析，授权上下文只查询项目/来源/既有返件的身份、父关系和版本；完整 business plan 在正式授权后构建。不能因类别在业务导入时被排除而从原件范围检查中删除；未解析目标以及额外未解释的工作表作为不具备项目关系的实际输入主体，只能由明确全范围 Grant 准入。

任务使用实际 batch 稳定 owner、代次、原件 hash/归档行与持久 sidecar。submit 的 transient input 不持久化为许可；排队后注册同 submit 动作的实际 batch 分支。retry 用当前 retry 动作建立新代次及 worker provenance，不借用 submit；cancel 清理旧结果并使旧代次失效。worker 在任何 raw/plan 发布前恢复稳定 ID+epoch，完整当前重授权，撤权时回滚而不另写 failed 回执。预览 plan 的完整 raw/normalized 数据和输出摘要单独绑定，防止只匹配原文件 hash 而接受被替换的结果。

正式 apply 完整锁闭包内捕获实际输入和父集合，全部变更后最终 actor/PDP 复验，再同事务提交。原件先在同一 RR 内校验并物化，再经公共 guarded stream 在 headers/chunk 发布前重验当前读者 provenance 与实际 sidecar；不持有 DB 事务跨下载。

作者9个真实PG/PDP生命周期反例及36个旧业务用例组合45通过、1原有跳过：`/tmp/itsp-s3-return-import-final-combined.log`。独立审查确实复现过原件发布窗口、multipart前置条件和授权前完整记录查询三类问题；对应修正不以作者测试作为独立通过结论，最终独立结果以 `test_authz_s3_return_import_independent.py` 的复跑为准。

返还导入独立四例最终闭合：发布前撤epoch、删除实际binding、FormData旧precondition+新字节、拒绝路径不SELECT完整Project/Receipt业务列均由另一作者原样验证。SQL条件中读取存储的identity head-id用于解析实际父关系是metadata谓词，不是加载完整source_payload；独立断言已精确到SELECT投影。独立4+作者9+全局10共23通过：`/tmp/itsp-s3-return-global-independent-final.log`。

## 授权 bootstrap 与 multipart 收口

独立审查新增的五个红例已经原样通过：doc/CKD/WBDD 的 FormData `authorization_precondition` 与实际上传字节绑定；query 与 form 同时提供但内容冲突时拒绝；source cancellation 与 WBDD 授权视图只查询实际关系标识/版本，不能提前 SELECT 客户、联系方式或成本列。独立5、作者27、返还独立4组合36通过：`/tmp/itsp-s3-workflow-independent-fixed.log`。

随后作者主动补查 WBDD 已执行回执的授权视图，真实 SQL 返回列仍包含整份 `sys_import_batch_report_json`，新增测试先失败（`/tmp/itsp-s3-wbdd-report-red.log`）。修复将 snapshot/完整回执的 SHA256 在 PostgreSQL 内计算，仅返回 opaque digest、稳定 owner 与历史父关系标识；sidecar 校验仍覆盖完整回执，不能只校验关系子集。新增测试还修改回执 result 的任意值，确认授权视图拒绝，证明未以“少读字段”削弱完整性。新8+独立5+旧核对17合计30通过：`/tmp/itsp-s3-wbdd-report-final.log`。该额外修订已交另一作者独立审查；全应用挂载与阶段 CI 仍待统一完成。

## Batch transfer 最后交叉检查：两项待修阻断

在作者106项组合通过之后，另加两个真实独立反例于 `test_authz_s3_tail_independent.py`：

- `test_selected_transfer_authview_never_loads_unselected_full_plan`：同一 preview 含项目A/B，随后账号仅获A作用域并只选A。授权视图200，但真实SQL仍返回整份 `report_json`，将未授权B的计划一起物化。该路径应保留数据库内完整摘要验证，并仅取选中行实际计划/映射/结果，不能把部分提交权限解释成整批内容权限。原红日志：`/tmp/itsp-s3-transfer-selected-sql-red.log`。
- `test_transfer_preview_waits_for_project_before_holding_contract_lock`：连接一先锁实际项目UPDATE；真实HTTP preview先锁合同SHARE再等项目SHARE；连接一继续合同UPDATE NOWAIT得到 `LockNotAvailable`。这是与已有project→contract领域写链的反序，不能因actor锁在最后而认为锁序闭合。应先完整排序项目锁，再合同锁，并拒绝锁等待后的父集合漂移。原红日志：`/tmp/itsp-s3-transfer-lock-order-red.log`。

两项已交root作者修复，当前transfer5仍不计独立闭合。其他静态检查覆盖实际输入发现、F-only销售SQL投影、实际receipt摘要、独立apply生产者与失败文件补偿；这些通过项不能抵消上述阻断。

Batch transfer 两项阻断现已闭合：作者 SQL 按实际所选 row_map/public key 投影对应 operations/contract_states，完整冻结 input 仅在数据库内做摘要/局部更新；bootstrap、正式 APPLY 与 replay 均不物化未选计划。领域锁改为 projects→contracts，锁后若实际父集合超出已锁闭包立即409，不持子锁追新父锁。原两红不改预期转绿，并将SQL断言延伸到正式APPLY及回放。独立复跑作者12+另一作者3+本独审4共19通过（`/tmp/itsp-s3-transfer-final-independent.log`）；最后冻结含otherbatch重放新RR admission版本的tail4再次通过（`/tmp/itsp-s3-transfer-frozen-tail.log`）。当前transfer5本地跨作者闭合；实际全应用外层挂载仍另行验证，不以该结果代替阶段CI。

## 实际 main 挂载的 branch 边界

原 outer Beta guard 把 AI document/ledger 两个独立分支的全部资源合并为AND，导致仅切换document的合法 readonly Grant 回到旧page 403。真实main独立反例已红（`/tmp/itsp-s3-mount-branch-red.log`）。修复只把完整已切换branch视作退出旧发布白名单的条件，仍要求全部正式branch union包含在router声明内；实际业务branch只由typed facade解析。独立 `test_authz_s3_mount_independent.py` 验证document-only200、未启ledger正式命令503且零LLM/Proposal、doc/CKD复合AND只切一半仍旧白名单403。连同原协议与unknown/声明检查共44通过：`/tmp/itsp-s3-mount-final-independent.log`。该测试文件与结论已冻结。

## AI accept bootstrap 新发现（待修）

沿同样元数据边界新增真实拒绝SQL反例：已创建Proposal，但当前用户没有accept。正式请求正确403之前，API `db.get` 与 facade `_resolve/_state` 已物化 `header_snapshot/sample_rows/proposal`。测试 `test_authz_s3_independent.py::test_denied_ai_accept_reads_only_proposal_identity_and_opaque_digest` 先红，日志 `/tmp/itsp-s3-ai-bootstrap-sql-red.log`。应仅取实际身份/type/hash/status/接受回执链接与SQL opaque完整摘要，当前action通过后才读内容。已交另一作者修复，独立断言不变；共享AI包三入口暂重开独审阻断，不以此前26/41等通过掩盖此新证据。

## 尾部 bootstrap 真实 SQL 独审收口

AI accept 原拒绝 SQL 反例在 backend 作者修改后由独立审查者原样复跑：14 新 + 12 旧 + 1 独立 = 27 passed（`/tmp/itsp-s3-ai-metadata-independent-final.log`）。授权前仅返回实际提案身份、状态与完整 immutable JSON 的 SQL SHA；全部未知 JSON key 仍参与绑定，完整 proposal 仅在当前 accept 合同通过后物化。AI 三入口重新本地闭合。

Root migration/attachment/roundtrip 三个真实 bootstrap 反例，连同三个作者文件、roundtrip 独审与已有跨模块独审共 43 passed（`/tmp/itsp-s3-root-bootstrap-independent-final.log`）。静态核对：迁移运行仅项目标识及事件完整 SQL 摘要先行；附件 deliverable 仅实际 ID/parent；roundtrip 的唯一全局 all_contracts.apply 在解析历史业务内容前用同一已登记 context/PDP 判定，正式完整输入仍再次授权。没有引入角色旁路或省略最终内容校验。旧取消清理测试的 `SimpleNamespace` → 真实 `UserContext` 适配保留四个原值和全部取消、清理、锁断言，独立单例通过（`/tmp/itsp-s3-cleanup-fixture-independent.log`）。

本审查者自有工作簿包另复现四个拒绝请求提前读取业务内容：checklist report、monthly plan/issues/result、project validation error bytes/plan、master 完整 audit result。持久测试 `test_authz_s3_workbook_bootstrap.py` 原四例红；修订改为 SQL 元数据及完整 opaque digest，完整回执 schema/result 校验保留在授权后。四例及相关作者/既有 master 独审组合 45 passed（`/tmp/itsp-s3-workbook-bootstrap-final.log`）；该修复由 backend 独立复审中，未作为作者自签。另 checklist 输入行未绑定的三项独立 tamper 反例正在收口，覆盖账保持 pending。

旧 roundtrip busy/cancel 回归的独立复核：原测试在创建 busy 请求后固定 50ms 释放 worker，无法证明第二请求已到互斥锁；root 保留 100ms 人为请求延迟作为复现向量，改成等待实际 busy 请求完成后在 finally 释放 worker。原 worker 2s 上限、429、零 body 消费、Retry-After、取消异常、临时文件清理与锁可重新获取断言全部保留。连同真实 UserContext 适配两条原测试独立 **2 passed**（`/tmp/itsp-s3-roundtrip-fixtures-independent-final.log`），接受这两项仅测试修订。

工作簿最终闭合：backend 独立审查者原样执行 bootstrap4 + 自增 checklist immutable-row tamper3 + master/master 独审/monthly/project/checklist 组合 **48 passed**（`/tmp/itsp-s3-workbook-metadata-independent-final.log`），并静态确认完整 SQL 摘要与 producer 一致、master 授权后完整 schema/result/manifest 校验未削弱。清单摘要纳入实际 batch 稳定身份/父项目、原件信息和所有 item 原始/归一化列，排除合法状态流转字段。上述四入口覆盖账恢复 verified；这只证明本地及跨模型复核完成，最终提交 CI 和统一验收仍独立。

## S3 CI 四项旧 schema 断言适配的独立复核

`test_maintenance_collection_reminder_schemas.py` 的三个精确请求字段断言补入可选 `authorization_precondition=None`，原业务字段、响应字段和拒绝额外字段断言保持。另六例分别在提供/省略 precondition 时调用实际 API adapter，核对受保护 facade 收到的 params/command 完全相同而 precondition 单独传递；这是 adapter 参数分离证据，不是 PDP/真实 HTTP 全链测试。静态跟踪三个 canonical 入口均先 `model_dump(exclude={'authorization_precondition'})`，旧分支继续逐业务参数调用；未发现授权观察进入业务查询或幂等摘要。

`test_maintenance_wbdd_display_columns_migration.py` 按当前 S3 schema 精确增加 nullable Integer `owner_user_id`、指向 `sys_user.id` 的 RESTRICT 外键，并把旧用户名唯一约束改为精确验证 stable-owner/legacy-only 两个 partial unique index 的列与谓词。仍要求 batch 唯一及原非空业务列，未降低幂等或显示列保证。两文件没有对应生产修改；作者原四失败之后，独立完整复跑 **29 passed**（`/tmp/itsp-s3-reminder-wbdd-schema-independent.log`）。接受最小测试修订，S3 exact CI 仍须重新通过。

后续 CI 三类旧测试适配也已独立闭合，无生产文件修改：

- `test_contract_amount_remediation.py` 将辅助源码顺序检查指向真正事务实现 `_apply_batch`；公开 `apply_batch` 仅新增委派与异常回滚。实读锁链仍为 batch → XSDD/contract advisory → 全部 workbook states → projects，原 PostgreSQL 双连接交错、等待事件、超时、state 锁可取得和最终金额/version 断言 AST 保持一致。原单例独立 **1 passed**（`/tmp/itsp-s3-ledger-lock-independent.log`）；源码辅助不被当成并发行为证明。
- `test_maintenance_project_management_api.py` 两个精确字典增加 `manager_assignment=None`，符合真实 primary_manager 关联查询；旧 project_manager_id 字符串不代表账号关系。原所有业务字段、OCC 与 audit 断言未变。完整文件独立 **12 passed**（`/tmp/itsp-s3-project-management-independent.log`）。
- `test_beta_account_whitelist.py` 不再要求所有路由直接挂同一个旧 Depends 函数，改为真实 main 的全部 23 个补库 method/path 在 RBAC 开/关两种情况下均返回原白名单 403 原因，包含旧退役路径。同时用真实 PostgreSQL/PDP 验证仅 catalog 已迁移且旧 bool 为 false 的 readonly 用户可按 Grant 访问；未迁 project 仍拒绝、撤 Grant 返回 403、发布开关关闭返回 404、已切换后撤配置返回 503。没有按闭包名字猜测依赖有效性，也未放宽旧 deny 或 canonical fault 断言。白名单文件与补库 Beta 回归组合独立 **22 passed**（`/tmp/itsp-s3-beta-composed-gate-independent.log`）。

上述五个旧测试文件及本报告可组成最小 CI 修订；它们的本地通过不代替新精确提交的完整 CI，S4 仍保持设计阶段。
