# S3 产物 sidecar 模型与接入合同（模型已实现，领域接入进行中）

root 已采纳 **一张受控 sidecar + 六张旧表各一个稳定 owner 列**，DDL/ORM 与迁移拒绝回退测试已实现；各领域的完成状态以实现覆盖账和真实运行测试为准。provenance、任务 epoch、manifest 不必向约二十张业务表复制；但现有基于 username 的唯一键不能被新 sidecar 自动修正。本方案供 root 单一迁移接入；`services/authorization_artifacts.py` 由 workbooks/source/flow 包负责，`authz/provenance.py`、principal 和迁移仍由 root 负责。

## 1. Sidecar 字段

建议模型名 `SysAuthorizationArtifact`，表名 `sys_authorization_artifact`。一个 sidecar 对应一个封闭 registry 中的领域实体/不可变业务版本；不是任意用户可写 KV，也不是允许凭据。

| 字段 | 类型/约束 | 用途 |
| --- | --- | --- |
| `domain_kind` | VARCHAR(64)，复合 PK | 已登记领域类型；例如 doc_batch、ckd_batch、ledger_batch、project_validation、manager_batch、return_receipt_task、business_file、replenishment_application_version。类型由服务端入口选定，不能由请求指定解析器。 |
| `domain_id` | VARCHAR(128)，复合 PK | 实际领域主键的规范字符串；registry 规定类型/编码，不使用显示名或 username。 |
| `owner_user_id` | INTEGER NOT NULL FK sys_user.id，RESTRICT | 稳定主体。私有批次/任务为 owner；共享文件为原始 producer 审计身份，不是 reader 必须相等的 ACL。 |
| `generation` | BIGINT NOT NULL，1..MAX_SAFE_INTEGER | 当前任务执行代；与现有领域 generation 在同一事务 CAS。没有重试的不可变实体固定 1，不拿 project.version 代替关系版本。 |
| `enqueued_epoch` | BIGINT NOT NULL，0..MAX_SAFE_INTEGER | 创建该执行/产物绑定时已验证的 producer epoch；同步生产也捕获当前 epoch。任务恢复精确匹配，不自动刷新；manifest/provenance 内仍用公共协议的 authorization_epoch 字段名。 |
| `input_digest` | CHAR(64) NOT NULL，SHA256 形状 | 本次不可变输入：协议/格式版本、规范请求、上传 bytes hash、业务 selector。重复请求同键异内容拒绝。关系权限 revision 仍单独重验。 |
| `command_namespace` | VARCHAR(64) NULL | 具有幂等语义的服务器命令类别；无幂等命令时与 idempotency_key 一起 NULL。 |
| `idempotency_key` | VARCHAR(128) NULL | 客户端原始业务请求键，绑定稳定 owner + command namespace；不与 username 组成新身份。 |
| `authorization_provenance` | JSONB(none_as_null=True) NULL | root schema 2。尚未完整解析的 task 可以 NULL，但 NULL 从不能支持内容发布/回放/下载。 |
| `artifact_manifests` | JSONB(none_as_null=True) NULL | 本代产物 purpose→完整 manifest 的封闭映射；原件、预览、错误工作簿、结果回执分别登记，不互相借资格。 |
| `created_at/updated_at` | 现有 TZDateTime，NOT NULL | 审计与清理，不参与授权版本替代。 |

索引/约束：PK(domain_kind,domain_id)；UNIQUE(command_namespace,owner_user_id,idempotency_key) WHERE idempotency_key IS NOT NULL；INDEX(owner_user_id,updated_at)。command_namespace 与 idempotency_key 必须同空/同非空、非空字符串。`input_digest` 对所有行必填，不另增加 command hash 列。JSON schema 由严格服务器 validator 验证；没有实际值时写 SQL NULL，JSON null 是无效输入。不建“默认空对象代表已确认”的 server default。

每个领域 registry 必须证明 sidecar.domain_id 解析到实际实体，并比较现有稳定 owner（若有）、实际文件 hash、业务版本/generation。多态 domain_id 无法靠一个外键约束所有表，不能把“sidecar 存在”当作证明领域实体存在；领域实体删除后 sidecar 不可转移给同名对象，正常 hard-delete 应由同事务受控清理或保留失效记录。所有登记函数拒绝覆盖已有 owner、input_digest 或 domain_kind。

## 2. 六个必要的 owner 增量

| 表 | 新列/现有唯一键调整 |
| --- | --- |
| maintenance_doc_import_batch | 加 nullable owner_user_id INTEGER FK；canonical 唯一(owner_user_id,idempotency_key) WHERE owner_user_id IS NOT NULL；旧唯一(uploaded_by,idempotency_key) 改为 WHERE owner_user_id IS NULL。 |
| maintenance_ckd_import_batch | 同上。 |
| maintenance_ledger_import_batch | 同上。 |
| replenishment_application | 加 nullable owner_user_id INTEGER FK；canonical 唯一(owner_user_id,client_request_id) WHERE owner_user_id IS NOT NULL AND client_request_id IS NOT NULL；旧 owner_username/request 唯一仅保留 owner_user_id IS NULL 分支。 |
| maintenance_acceptance_checklist_batch | 加 nullable owner_user_id INTEGER FK；canonical 唯一(owner_user_id,idempotency_key) WHERE owner_user_id IS NOT NULL；旧唯一(uploaded_by,idempotency_key) 只保留 owner_user_id IS NULL。 |
| maintenance_wbdd_import_receipt | 同 checklist；保留独立 batch_id UNIQUE，不混淆一份业务批次对应一份回执的语义。 |

新增 owner 列无 default，不按当前 username 回填。canonical 创建必须同时写非 NULL owner 和 sidecar，并逐次核对两者一致；原 uploaded_by/owner_username 保留审计及旧数据兼容，但 canonical 查找、重放、所有权不得再以字符串作为授权来源。旧行 owner=NULL 的权限内容在新权威保守拒绝，允许重新上传/生成。六表新增的 owner 索引只按实际 owner 查询需要添加，不盲目复制原全部索引。

已核对模型与创建迁移，root 的 DDL 名称如下；doc/ckd/ledger/checklist/WBDD 五项现为 UNIQUE **constraint**，replenishment 为 partial unique **index**，不能用同一种 drop 操作：

| 表 | 幂等字段 | 当前需替换名称 | 新 canonical unique index | 新 legacy unique index |
| --- | --- | --- | --- | --- |
| maintenance_doc_import_batch | idempotency_key | uq_maintenance_doc_import_idempotency | ux_maintenance_doc_import_owner_request | ux_maintenance_doc_import_legacy_request |
| maintenance_ckd_import_batch | idempotency_key | uq_maintenance_ckd_import_idempotency | ux_maintenance_ckd_import_owner_request | ux_maintenance_ckd_import_legacy_request |
| maintenance_ledger_import_batch | idempotency_key | uq_maintenance_ledger_import_idempotency | ux_maintenance_ledger_import_owner_request | ux_maintenance_ledger_import_legacy_request |
| maintenance_acceptance_checklist_batch | idempotency_key | uq_acceptance_checklist_batch_idempotency | ux_acceptance_checklist_batch_owner_request | ux_acceptance_checklist_batch_legacy_request |
| maintenance_wbdd_import_receipt | idempotency_key | uq_maintenance_wbdd_import_idempotency | ux_maintenance_wbdd_import_owner_request | ux_maintenance_wbdd_import_legacy_request |
| replenishment_application | client_request_id | ux_replenishment_application_owner_request | ux_replenishment_application_owner_request | ux_replenishment_application_legacy_request |

doc/ckd/ledger/checklist/WBDD 五个 legacy index 是 `(uploaded_by,idempotency_key) WHERE owner_user_id IS NULL`；replenishment 是 `(owner_username,client_request_id) WHERE owner_user_id IS NULL AND client_request_id IS NOT NULL`。canonical indexes 使用上表的稳定 owner+对应幂等字段。replenishment 可以复用旧 index 名，但先 drop 旧 index 再重建定义；其原 `owner_username` FK 本轮保留，不把它当权限依据，也不借机更改账号改名业务。

`maintenance_manager_upload_batch`、`maintenance_collection_plan_import_batch`、`replenishment_cart_draft` 已有稳定 owner ID，直接与 sidecar 对拍，不再加列。`maintenance_project_workbook_validation`、AI proposal 等无需为了授权再加 JSON/owner 列：sidecar 持久 owner，registry 绑定实际实体主键/hash。现有 result_json/report_json/plan_json 继续保存业务数据；只能引用 sidecar 或承载已有业务 generation，不能另存一份可独立变成 allow 的权限真值。

纯一表、零旧表列也可通过“sidecar 稳定身份幂等键→另生成内部旧 key”避开旧 username 唯一约束，但会增加双 key、历史碰撞、审计及回放协议。为省四列引入这层不是本建议。只加 sidecar 而仍用旧 username 唯一索引，会使同名新账号错误冲突；继续先按旧 username 查回执则可能泄漏旧账号内容。

## 2.1 迁移审批的稳定三人分工

`maintenance_migration_run` 增加三个 nullable FK：`created_by_user_id`、`reconciled_by_user_id`、`approved_by_user_id`。旧行不回填；canonical 行按状态要求稳定身份完整，创建、对账、审批三人 ID 两两不同。审计姓名允许改名或与同名新账号相同，但不参与 canonical 权限及职责分离判断。数据库约束保留旧记录的姓名分离规则，同时对新记录实施 ID 分离。

幂等键按稳定账号 ID 与具名动作哈希；重放仍检查当前完整动作/字段权限及实际运行内容。`migration_event` 是追加型审计实体：首次 INSERT 前生成实际运行、候选和差异记录的摘要，再同事务登记事件 sidecar；不通过 UPDATE 补写审计记录。每次读取验证当前事件、完整版本链和内容摘要。旧运行没有稳定身份/事件凭据时拒绝，不以同名当前账号认领。manifest 是已签名 JSON，复核当前来源事实及包含稳定 ID 的审批链；读取使用封闭的一致读证明，不能创建 workbook state 或取得领域写锁。此模块不激活生产。

新增稳定 owner、任意审批 actor、sidecar 或产物协议使用痕迹均阻止 S3 downgrade；撤销权限不删除历史事实。

## 3. 登记与发布事务

```python
# 以下为签名合同，不是新增应用代码。值对象均 frozen，映射深度不可变。
ArtifactTarget(kind: str, id: str, generation: int)
ArtifactSubject(owner_user_id: int, enqueued_epoch: int)
IdempotentOperation(namespace: str, key: str)
# AuthorizationProvenance = root schema 1/2 严格解析后的类型
# ArtifactManifest = 文件格式/完整投影/bytes/content dependencies，不能当 provenance 传入
# ArtifactBinding = target + subject + input_digest + operation + provenance + manifests

# 返回受控绑定对象；subject 从当前验证的 identity 产生，不让调用者自报
# 调用者控制事务，内部不 commit；locked_user 为已有最终锁集中的 actor
register_artifact_binding(db, identity, *, target: ArtifactTarget,
    input_digest: str, operation: IdempotentOperation | None = None,
    provenance: AuthorizationProvenance | None = None,
    locked_user=None) -> ArtifactBinding

# 仅可信编排器使用；加载不等于允许，不可直接把返回值序列化给 HTTP
load_artifact_binding(db, *, target: ArtifactTarget, lock=False) -> ArtifactBinding

publish_artifact(db, identity, *, binding, purpose, manifest,
    expected_generation) -> None

restart_artifact_task(db, identity, *, binding, expected_generation,
    provenance: AuthorizationProvenance | None) -> ArtifactBinding

# HTTP/任务/工具的实际读取门；验证 registry 领域绑定、完整内容、当前 reader
require_artifact(db, identity, *, target: ArtifactTarget,
    purpose: str) -> ArtifactManifest
```

- producer 创建领域元数据、sidecar、owner 与对应幂等记录在同一事务提交。既有内部 commit 原语必须提供 commit=False 或在提交前执行受控 callback；不能“原语先 commit，wrapper 再补 sidecar”。
- 幂等串行化复用既有领域命令/advisory 锁，以稳定 owner+命令+请求键为键；唯一约束作为并发兜底。不能在未知结果的 unique 冲突后直接返回已有 row；仍要验证 owner、input_digest、当前授权与完整回执资格。
- sidecar 的 owner FK 插入也可能取得用户 KEY SHARE；把它算进既有域锁→最终 actor SHARE/完整用户锁集的顺序。不要为了提前登记而在领域锁前插入带 owner FK 的行。外部文件 protocol-used 哨兵仍是独立短事务，先于任何 domain/actor 锁；它不是 sidecar producer 提交事务的替代。
- task generation 和 sidecar generation 同事务增加，旧 worker 捕获 expected generation，发布必须双重 CAS。retry 使用当前显式请求 identity 和新 epoch，不改变 owner/input_digest，不复活旧 generation 的 token/path。cancel 同样使旧 worker 无法继续发布。
- 对同一代已发布 purpose，hash/manifest 不允许静默覆写。要保留历史可下载版本时，为每个不可变版本登记独立 domain key，例如 application_version 的实际版本行 ID。一个可重试 task 当前 generation 不承担无限文件历史；重试清除当前未稳定发布项，旧代 URL 失败关闭，不能返回新一代 bytes 冒充旧 URL。
- 共享项目文件不要求 reader==producer；读取由当前 link/project/完整 content_dependencies 重验。private batch/Agent 文件保留自己的稳定 owner 约束。`validate_provenance` 仍用于本人的历史执行；共享 reader 使用域绑定重授权，不能把 producer schema 2 的 principal_id 换成 reader 伪装原证明。
- 所有者、执行者与当前 reader 不混为一人。管理者按显式 all_imports 操作别人的 batch 时，不能覆盖 batch 原 owner 或用原 owner's epoch 为管理者续签；若操作产生异步任务/新产物，创建独立 operation/domain-version 绑定，由实际执行者持有。AI 接受别人的 proposal 后产生的新 batch 就是此例。任务 resume 使用该执行绑定的 subject，领域 owner 约束仍由实际目标资源单独检查。
- 原始 bytes 先存隔离临时区，manifest/ready 原子发布后才产生可下载引用。失败/取消不发布；存储孤儿清理按 sidecar/domain 当前状态，不以文件存在作允许。

## 4. 迁移及独立反例

S3 downgrade 在与 producer 一致的锁协议下，**sidecar 任意行存在即拒绝移除本协议**；不只检查 manifest 非 NULL，因为 queued task 也依赖 stable owner/epoch。新 owner 列任意非 NULL 同样阻止擦除身份；artifact-protocol 哨兵覆盖 DB 外 bytes 已发布的情况。空库/未使用的新 schema 才进入 receipt 可核验恢复路径。保守 guard 不要求把全部旧业务表里的历史 JSON 都当成新协议使用痕迹。

必要独立反例：同用户名不同 stable ID 的同请求键互不读回执；同 owner 改名后同键仍命中同一个绑定；相同键异 bytes/协议拒绝；领域原语 commit 前故障使 sidecar+业务一同回滚；两个真实连接同键创建只产生一个 canonical entity；retry 后旧 worker CAS 失败且不发布；sidecar owner 与现有 owner 列不一致拒绝；伪造 domain_id/错误类型/hash 不得绑定另一文件；sidecar 有行但 task provenance NULL 时 downgrade 拒绝、原件下载也拒绝；旧 SQL NULL owner 绝不按 username 自动继承。

sidecar+六列方案已获 root 采纳；本文给出模型/表名、精确索引名和 typed 参数作为实施输入。这里不承诺已实施或已跑上述 PG 反例。

## 5. 稳定主体与幂等查询的追加审计

根代理发现 checklist/WBDD 两个遗漏后，独立扫描全部 `backend/app/models/*.py` 的 UniqueConstraint、unique Index（含表达式）与 mapped column unique，并追踪相关服务。与 username+请求键直接绑定的物理约束/索引共六处，已列入上表；未发现第七张同型物理约束。`SysUser.username` 自身唯一和用户目录索引不属于请求幂等键。以下是实际命中调用，不是只凭列名推断：

| 入口/模型证据 | 现有实际调用 | canonical 必须改为 |
| --- | --- | --- |
| models/maintenance_acceptance_checklist.py:77 | services/maintenance_acceptance_checklist.py:151 按 uploaded_by+key 查旧批次，:214 apply 比较 uploaded_by | owner_user_id+key，并独立核当前项目和整个回执；不能仅新增列而保留旧字符串 SELECT |
| models/maintenance_wbdd_import.py:35 | services/maintenance_wbdd_import.py:93 find_receipt；:35 _import_lock_identity 也用 operator+key | stable owner ID 查询、advisory namespace；原 batch_id 唯一保持 |
| models/maintenance_doc_import.py:81 | services/maintenance_doc_import.py:329；return_receipt_import.py:204–206 复用同表及 doc-key | owner ID+key；异步代/输入摘要另核，原始审计 uploaded_by 保留 |
| models/maintenance_ckd_import.py:73 | services/maintenance_ckd_import.py:221 | 同上 |
| models/maintenance_ledger.py:88 | services/maintenance_ledger.py:391 | 同上 |
| models/replenishment.py:101 | services/replenishment.py:1274–1280，replenishment_cart.py:219 ACK-loss replay | stable owner ID+client_request_id；cart 原有 stable owner 约束继续复用 |

还有派生 hash/global unique key 或 JSON 回执，它们未在物理键上直接写 username，仍有同类身份碰撞风险，不能漏迁。根代理已批准 canonical 改为带协议版本和 stable actor ID 的命令 namespace/hash，并用 sidecar 校验实际 owner/input/result；旧审计用户名只作为展示：

- `maintenance_acceptance.py:126–157`：`_operation_key(operator, operation_type, deliverable_id, client_key)` 生成 `MaintenanceAcceptanceOperation.operation_key`，`_existing_operation` 可直接读旧 result_json。新 hash 不能继续把 operator 名字当身份；返回前仍重验目标与整个结果。
- `maintenance_demand_manual.py:485`：用户名派生 SHA1 生成 `FMaintenanceOrder.raw_order_id=page-manual-...`。canonical namespace 应含 stable ID，已有事实/来源对象保持；不能删除旧业务行或用新名字猜旧 owner。
- `maintenance_source_cancel_cascade.py:207,240`：确认回执 JSON key 为 `_digest([operated_by,idempotency_key])`。稳定 ID 命名空间与当前真实 project/batch/source 组合共同绑定，不能因 JSON 无独立 owner 列漏掉重放授权。
- `maintenance_project_master_workbook.py:6236,6330–6347,5868`：V2 apply key、提前 ACK-loss 查找和备注重放分支使用 operated_by。canonical 都必须转稳定主体与实际 operation sidecar；保留原混合文件回执/金额资格，不能在新授权前走旧快捷返回。
- `maintenance_bulk_import.py:290,3594–3604,3679,3700,4586,4647`：operation_hash 和 SysImportBatch 查询/owner 判断使用 operated_by，涉及错误文件及 apply。通用 bulk 归 S4，但 S3 项目工作簿复用该服务时必须先接受控 callback、stable identity 和 sidecar；不能等 S4 才补 S3 已可到达的旁路。其成功文件 hash/selection_hash 业务唯一约束不机械改为 owner 唯一。
- `maintenance_migration_runs.py:589,1107,1356`：event replay 身份、创建者不能自行对账以及创建者/对账者不能最终审批的分工门当前比较 operated_by/created_by 字符串。canonical 必须比较 run/operation sidecar 稳定 ID；同一账号改名仍是同一创建者、同名新 ID 不是原人。没有可信历史 sidecar 就拒绝新权威复核，不能反查当前用户名回填。

新增必要反例：两不同 ID 先后同名且同请求键不拿到旧回执；原 ID 改名后同键仍命中自己且不重复业务；改名不能通过四眼复核；同字段/hash但 sidecar 主体不同拒绝；legacy 查询分支在持久 cutover 后不可触达。静态扫描证明上述位置存在，不冒称这些 S3 修订已经实施或通过 PG 测试。

## 6. S3 必须修复的主体比较，不限于幂等

继续对 services/api 的 Compare AST 做扫描，并读取各命中分支；以下是授权/业务主体关系，必须按 stable ID 实现。仅给新 sidecar 加 owner 列不足以闭合：

| 实际位置 | 业务不变量 | 必须保留的稳定身份语义 |
| --- | --- | --- |
| maintenance_migration_runs.py:1107 | reconcile 操作人不能是 dry-run 创建人 | 比较当前 actor ID 与绑定真实 run 行的 creator sidecar；改名仍拒绝自己对账 |
| maintenance_migration_runs.py:1356 | approve 操作人不能是创建人或已对账人 | 分别绑定 run creator 与实际 reconcile event actor 的 stable ID，不从请求、当前用户名反查或报告文本获取 |
| maintenance_migration_runs.py:589 | event replay 只能由对应原操作者继续 | operation/event sidecar 与实际 run/event 行一致，当前动作和完整回执重验；未知历史主体拒绝 |
| maintenance_demands.py:604,660；调用 :705,842,971 | delete intent 的详情、arm、execute、cancel 仅原操作者继续 | intent sidecar owner AND 当前目标全集授权；不能凭现在相同用户名认领旧 intent |
| maintenance_ai_fallback.py:364 | 非 all_proposals 只看自己的提案 | 用 proposal sidecar owner ID 过滤；不得把这个 list owner 门误加到原本无 owner 限制的 accept |
| maintenance_bulk_import.py:3679,3700,4586,4647 | 工作簿/原件/错误文件和应用的 owner 分支 | 当前资源 all-imports 或 stable owner 决策，不再用用户名或旧 admin boolean；S3 复用调用先迁，不推迟到 S4 |
| maintenance_manager_workbook_v3.py:924 | 签名工作簿属于当前经理 | 既有 owner_user_id 是权威；签名内 owner_username 可保留为生成时审计值，不能把当前改名当换了主体。签名完整性仍验证，不能删签名字段凑匹配 |
| replenishment.py:259,582,860；cart.py:219 | 申请列表、读取/修订与提交后 ACK-loss replay 的 owner | 使用新增 application.owner_user_id，与已有 cart.owner_user_id 一致；当前 action/scope/字段仍须验证 |

`maintenance_project_master_workbook.py:6316,6322` 中“保存审计名与保存回执名一致”可以继续作为历史内容一致性检查，但不能据此判断当前调用者身份；`:6567` 与查询 `:6344` 的 caller 比较必须迁为 stable ID。`maintenance_dashboard.py:1944` 的 created_by 是用户明确选择的历史展示筛选，并非 owner allow；保留审计字符串过滤，但必须在已授权 SQL 范围内执行，不能借筛选扩大范围。

S4 关闭清单也记录 `replenishment.py:1636`：旧 phase4 审批拒绝 `version.submitted_by == reviewer`，未来若恢复可执行业务必须比较提交版本 sidecar 的 stable ID。当前对应旧流程已退役的入口继续 410，不为迁移身份而提前复活审批；非 HTTP 直接调用不能被误当已完成新授权。

独立失败用例必须覆盖：同 stable ID 改名后 reconcile/approve 仍自审拒绝；创建者与对账者任意一人改名均不能最终审批；同名不同 ID 不继承旧 run/event/intent/proposal 身份；unknown 历史 producer 保守拒绝；请求填入 operator ID、sidecar 指向另一个 run/event、创建者与对账者 sidecar 互换都拒绝。所有 sidecar 由真实 producer 在领域事务内登记，不能由用户填写身份字段。

## 7. 写命令和共享文件稳定 owner 追加实现

实际master apply及验收接线确认还需为 maintenance_project_workbook_operation、business_file、maintenance_acceptance_operation 加 nullable owner_user_id FK RESTRICT。它们保留原审计用户名，canonical新记录同步写实际actor ID与sidecar；历史NULL不按同名账号回填。共享验收文件owner用于可信生产者绑定，当前读者通过实际文件link/project和独立下载动作授权，不要求等于上传者。三表任一非NULL owner阻止downgrade；新增真实guard测试3项，S3 migration全16项通过。

后续补库/仓库接线再加 replenishment_application_version.owner_user_id（该不可变版本的实际执行者，可能不同于application owner）与 maintenance_warehouse_import_batch.owner_user_id。均nullable FK RESTRICT，历史不回填。STABLE_OWNER_TABLES共5表，downgrade先ACCESS EXCLUSIVE锁这5表再检查，防止检查后出现新owner。补库版本sidecar以实际version行ID为domain_id，完整内容摘要包括原项目ID、申请稳定owner、版本稳定owner、实际不可变版本/审核/审计；不包含当前项目备注或当前读者身份。
