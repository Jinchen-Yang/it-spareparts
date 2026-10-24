# S4 通用导入执行合同（只读设计）

本文件依据 S3 实施后的实际源码冻结通用导入接缝，尚未实施或验证 S4。S3 精确 CI 通过并收到 root 开工通知后才能改 app/tests。范围是 `api/imports.py` 的 9 个 HTTP 入口及其 worker；平台管理、机器接口归 root，Agent/聊天/文件归另一作者。已完成的 S1 控制面与 S2/S3 provenance、领域模块不重写。

## 1. 实际执行域与需要保留的行为

`etl/mapping.py`、`sheet_selection.select_workbook_sheets`、`pipeline.transform_workbook` 和 `loader.load` 共同确认只有五个实际执行来源：purchase、sales、inventory、maintenance、expense。INQUIRY 常量和旧 ORM 注释不是可执行支持。`workbook` 是选表包装：存在报销页时合并所有报销页，忽略其它识别页，最终实际执行 expense；没有报销页时只选第一个可识别页。文件名、客户端声明、`SysImportBatch.file_type` 单列均不足以决定权限。

| 来源 | 实际原语与副作用 | 授权不能误改的业务行为 |
| --- | --- | --- |
| purchase | `_load_orders`；型号/别名/供应商维度；金额异常检测；同事务维保成本重算 | 全库源单 skip/upsert；不是 assigned_customers/project 子集导入。不能先丢掉无权行再声称原件已导入 |
| sales | `_load_orders`；客户/型号维度；`sync_uploaded_sales_workbook` 建项目、合同与归属；同事务维保成本重算 | 销售源自动建项是既有受控副作用，不要求另一个人工 project.create，也不恢复被 D-05 禁止的手工建项 |
| inventory | `_load_inventory`；真实快照日/型号库存写入 | 仍按完整来源快照与既有冲突规则处理；不能用 read inventory 代替写源合同 |
| maintenance | `_load_maintenance`；`prelock_import_assignment_targets`、`auto_assign_imported_orders`、仓库/归属修复；同事务成本重算 | WBDD 真实源订单与所有旧/新项目、负责人用户集合；已有 S3 手动 source/warehouse 动作不能变成内部隐式附加门 |
| expense | `_load_expense`、`expense_void.classify/assert_fingerprint` | 单合同替换、D-09 抑制作废、作废预演 HMAC 与实时指纹、同批最多一个会作废的报销文件全部保留 |

每文件独立事务保留：先前文件成功不会因后续撤权而回滚。采购/销售/维保的成本重算仍和源事实同事务；利润、库存成本和主数据刷新仍是提交后的独立派生工作。不能把派生刷新失败包装成源事实未提交，也不能为内部刷新额外索取 `profit.recompute` Grant。原始归档原子性、hash 去重、失败批次留痕与修复模式并发保护继续复用。

## 2. 资源、动作、字段与迁移来源

root 已批准五个来源资源加独立作废预演合同的方向，并明确：旧报告未遮罩是要修补的权限缺口，不可因 `page_import` 自动给全部 C/S/K/F。以下六资源、六动作与 void.preview 名称、完整诊断四字段、混合投影及 canonical void fingerprint v2 已由 root 冻结；仍是设计，尚非实施通过。运行时权威将由唯一 executable definitions / registry 提供。精确 DTO 见 [wire 合同](s4-import-wire-contract.md)，前端不另造字段形状。

| resource | scope | 数据/写动作 | 字段合同 |
| --- | --- | --- | --- |
| `import_purchase` | `all` | `precheck`, `submit_skip`, `submit_upsert`, `read_status`, `read_report`, `export_errors` | 完整诊断合同见下文 |
| `import_sales` | `all` | 同上 | 同上 |
| `import_inventory` | `all` | 同上 | 同上 |
| `import_maintenance` | `all` | 同上 | 同上 |
| `import_expense` | `all` | 同上 | 同上 |
| `import_expense_void` | `all` | `preview` | 可选 `data_purchase_cost`；不是 `data_profit` |

`submit_*` 标 mutation，其余标 data；展示动作另由 frontend surface 合同登记，不从 read 自动授 show。五来源的 `precheck/submit_* /read_status` 不要求诊断字段，也不暗中授报告。`read_status` 同时用于批次基本状态、该来源的作业 item 和历史列表，避免为同一来源再造一个全域 job 管理资源。

完整诊断包含任意错误文本、历史报告、来源信息与派生结果，不能靠递归 key mask 证明无敏感信息。第一版采用保守完整字段合同：`data_customer`、`data_supplier`、`data_purchase_cost`、`data_profit` 四者均具备时，才允许 `read_report/export_errors`；这四字段仅在本来源资源内有效。后续若要缩小某一来源的字段集合，必须先将该来源所有错误/历史报告变成可证明的封闭 typed DTO，不能凭常见样本删依赖。字段子集可保存，但不自动补齐硬依赖。

作废预演必须独立：当前 `apply_field_visibility` 的通用 `amount` 键属于 K=`data_purchase_cost`。K=false 仍可获得获准的合同/行标识与作废流程状态，金额为 null；不得借来源诊断字段把金额放开。金额不允许时，不选择原始金额列到 Python 再遮罩：以 SQL 不透明摘要参与承诺指纹，只选择允许的展示列。现有 v1 指纹算法和令牌不能静默重解释：canonical 固定 fingerprint v2，以 SQL 不透明金额摘要承诺，preview 与 loader 校验同版；旧 never-cutover 分支继续 v1。v2 指纹与 HMAC 均不是授权，当前主体与动作仍必须复验，不新增 HTTP 路径。

**精确 seed 规则：**记 `P=runtime_safe(effective_for_user(old_user)).page_import is True`，`D` 为同一旧有效图，不是 token 任意布尔、更不是角色名直通。

- 仅 `authorization_legacy_seed_eligible=true`、非 deleted 的旧用户/旧模板参与；inactive 保留 entitlement，认证仍拒绝。canonical 创建的 none/空包/复制对象不种。已有 Grant、override、snapshot 键（包括空拒绝）不覆盖。
- 五来源 `precheck/submit_skip/submit_upsert/read_status` 在 P 时种；`read_report/export_errors` 仅在 P 且上述四个 D 字段全 true 时种。各字段本身按 D 的对应值复制，不因报告原来泄露就补 true。P=false 写显式空动作作已迁记录；源资格标记不消费，保留供其它阶段使用。
- `import_expense_void.preview` 在 P 时种，字段 K 按原 D.K；无额外 F 依赖。show 还需实际旧 ImportPage 的显示条件，不能用数据门单独推页面。
- 这是明确安全收紧：此前只有导入页权限但缺完整字段者仍可预检/上传/看安全状态，失去历史完整诊断和错误 CSV；成功 JSON、job note、duplicate 提示、worker 摘要均不得绕回未授权报告。

## 3. 九个真实入口的绑定

所有 canonical 入口使用真实 typed identity。授权视图仅服务器登记的当前 entry；不接受客户端 resource/action。输入按真实 bytes、文件顺序、mode、解析器/选表版本、作废令牌承诺构造 operation context。对象及历史读取按真实持久 manifest 解析，不能把 transient `operation_input` 保存成 worker 来源。

| method/path（均 `/api`） | 真实动作与准入 | 结果/SQL 合同 |
| --- | --- | --- |
| POST `/import/precheck` | 已识别文件全部实际 selected source 的 `precheck` AND；未知输入另有精确分支 | 授权通过后原内容预检 v2 保持真值，外层独立 envelope；重复 batch ID 仅在该真实批次 `read_status` 也获准时返回。未知来源诊断分支只需任一明确来源 precheck，零业务/duplicate SQL；不能给正式提交授权 |
| POST `/import/upload` | 实际 source 的 `submit_skip` 或 `submit_upsert` | 同一深模块同步执行一项，仅返回固定命令回执；报告通过已有 batch detail 的独立 read_report 获取，不在命令回执内夹带。去重冲突无 batch read 时只有固定冲突码，不含现有批次 ID |
| POST `/import/upload-batch` | 所有实际执行 item 的 submit 动作 AND；同一完整输入 plan | 全部授权通过才建 job/manifest/enqueue。源可确定但内容错误的文件可按原业务形成失败批次；完全不能识别来源的文件不能凭一个任意来源 Grant 排队，提交前结构化拒绝且零 job/事实写入，明确此失败时点收紧 |
| POST `/import/expense-void-preview` | `import_expense_void.preview`，实际上传解析 + 旧报销集合 | 当前真实合同/行完整集合，K 控制金额投影；保留全部业务 status、行数上限、HMAC 承诺；deny 不查询报销业务列 |
| GET `/import/jobs` | 至少一个实际 item 来源 `read_status` | 按持久 item source 权限先过滤、再 limit/count；不能先取最近 50 全域 job 再 Python 筛选 |
| GET `/import/jobs/{job_id}` | 当前至少一个实际 item 的 `read_status`；逐 item 投影 | 不从提交成功或 owner 推读许可；不读取未授权 item 的完整 note/report。混合投影规则如下 |
| GET `/import/batches` | 各实际来源 `read_status` | 明确 generic source protocol + 已验证来源/owner；SQL 筛选先于最新 100/paging。S3 类型不得混进通用读权限 |
| GET `/import/batches/{batch_id}` | `read_status` 或完整 `read_report` 任一独立可用 | status 与 report 为两个固定块；报告 deny 时不查 report_json/error_detail/error count。report-only 可以获得最小 batch locator + 完整获准报告，不强加 status 动作 |
| GET `/import/batches/{batch_id}/errors.csv` | 该实际 source 的完整 `export_errors` | 同 RR 内完整 spool，保留 CSV injection 防护、UTF-8 BOM、顺序和完整件；使用公共 publication guard，audit 后/headers 前/各 chunk 重验，撤权不发送剩余数据 |

所有涉及实际来源的 canonical resource 未 enabled 或 suspended 都失败关闭；缺动作是普通 deny，PDP/DB 故障为整请求 503，不能伪装成内容错误或 partial allow。legacy 仅在相应来源 never-cutover 时存在；marker 后配置撤回不恢复旧 `page_import` 通行。

## 4. 最小 wire 与混合作业投影

复用 S1 envelope、S3 `authorization_context` / `authorization_precondition`、typed detail code 与 no-store；不另造 epoch。新协议显式版本化，旧数组 shape 通过前端 adapter 明确转换，不能凭缺新字段自动回退。

- Precheck：正式成功返回共同 envelope、`content:<原v2响应>`、`inputs:[{file_index,input_sha256,source_domain}]` 及本 entry 的 `authorization_context`。已识别源有一项 deny 则整体 403，不把 deny 塞进内容 parser。完全未知输入的精确分支要求任一明确 source.precheck，且仅返回用户输入的格式诊断。`file_index` 保持本次输入位置；正式 upload 必须按自己的 entry 重新 view→execute，不复用 precheck context。
- 提交：保留 files/mode/preview_tokens；增加完整 input context 的 precondition。HMAC token 绑定业务作废承诺，precondition 绑定当前身份/Grant/完整命令，二者不能互相替代。成功 `{job_id,status:'processing',accepted_files,authorization_context}` 只证明接收；无 `read_status` 不自动 poll。
- 单批：固定 `block_access:{status:Decision,report:Decision}`；deny status 时 status/计数/上传者等为 null，保留 batch_id；deny report 时 report=null、errors=[]、issue_count=null。允许但确实无错误才是 errors=[]、issue_count=0。
- 混合 job：只返回获准 item，稳定随机 item_id；跨域部分可见时 input_index=null，不能用位置空洞泄露隐藏文件数量。顶层 `partial=true,restricted=true`、total_files/done_files/error_files/status/note=null，另给仅基于可见项的 visible_status/visible_*；既不拼接旧全 job.note，也不把隐藏项当 0。全部 item 可见时可保留原完整聚合。初始上传者提交回执已知文件数，不等于以后任意读者可见完整作业。
- 元数据成功不能包含 source raw、原错误字符串、金额、未获准报表/重算统计；固定错误码可提示“来源尚未核验/权限已失效/需重新预检”，无 debug SQL。所有 number ID/version 使用 safe integer，所有未知字段严格拒绝。

## 5. 深模块 Interface 与数据库接缝

HTTP 与 worker 共同调用 `services/source_import_access.py`，不互相 import。原 parser/transform/classify 和 loader 仍是内部计算/存储原语；不能在每个路由重复 role、source 推断、字段和锁序。

| Interface（拟定） | 输入与返回 | 不可分割的责任 |
| --- | --- | --- |
| `inspect_inputs(session_factory, identity, inputs, *, mode, authorization_view=False)` | `InputFile` → 版本化内容/授权 DTO | 限额、安全解包、真实选表、只读上下文、授权后 duplicate 可见性；不建 job/batch |
| `submit_inputs(db, identity, inputs, *, mode, preview_tokens, precondition, background)` | 同步 batch 或异步 job 回执 | 冻结完整输入 manifest、当前所有来源准入、stable owner/epoch、同事务 job item/sidecar；失败清理仅本次临时对象 |
| `execute_job_item(session_factory, job_id, item_id)` | 真实持久 ID → typed item outcome | 实际 manifest/hash/owner 复验、恢复任务身份、完整领域锁/命令、提交前重验与结果发布；不接 username/UserContext 当身份 |
| `read_import(session_factory, identity, selection, *, precondition=None)` | closed `History/Job/Batch` selector → DTO/spool | 同 RR 内 metadata 解析、当前来源权限、SQL 范围/块投影/完整输出；Blob 交公共 guard |
| `preview_expense_void(...)` | 实际文件/模式 → 原业务预演 + context | 全旧行闭包、不透明指纹与 K 投影，不复用完整报告字段 |

关键变量只有一套含义：`identity:VerifiedIdentity` 是验签或任务恢复结果；`InputFile` 是不可变 bytes/hash/filename/ordinal，非客户端路径；`manifest` 是服务器选表后的完整持久计划；`permit:SourceImportPermit` 是同事务封闭执行许可；`selection` 是闭集查询用途/实际对象主键；`precondition` 是用户上一观察，不能代替当前授权；`item_id` 是持久 item 主键，不是文件名。这些 invariant 由深模块执行，不要求路由自行按顺序拼 helper。

schema 最小建议由同一 S4 迁移统一落实：

1. `SysImportJob.owner_user_id` nullable RESTRICT FK、enqueued_epoch、manifest version/generation；旧 unknown 保持 null，不能按当前同名 username 认领。
2. 新 `SysImportJobItem`（或根批准的等价规范表）保存 job FK、稳定 ordinal/item ID、actual source/protocol、原件 SHA/size、parser/selection version、mode、完整计划摘要、状态、batch FK、结构化 error code。完整不可变输入和 mutable outcome 分开；至少唯一(job_id,ordinal)。历史/混合权限的 SQL 过滤需要规范 source 列，不能只在 Python 读 JSON 再筛。
3. 复用 S3 已有 `SysImportBatch.owner_user_id`，补实际 generic protocol/source 的可索引来源元数据；`workbook` 包装原列不覆写，以免改业务审计口径。复用 `SysAuthorizationArtifact`/schema2 provenance 保存 actual job/item/batch generation，不另造文件授权表。
4. 本作者统一 S4 单迁移 seed、来源回填与 downgrade 守卫：不删除已激活 source marker、不擦除 stable owner/manifest/canonical false 来源；空库与纯可逆旧状态才允许既定往返。新 protocol rows 和 sidecar 必须参与保护，不仅检查 Grant 行。

worker 身份复用 `principal.resume_task_identity(owner_user_id,enqueued_epoch)`；同 ID 改名可恢复，同名新 ID 不行。入队、开始、每文件提交前、结果发布和读回各自重验。撤权时保留之前已提交文件，本项未提交全回滚，后续不执行；不得把旧 job owner 当无限授权票据。授权拒绝本身不能被 parser 错误分支 `commit()` 误提交；应先区分 auth exceptions，再沿原允许的失败批次协议处理。

## 6. 领域锁和共享原语

事务锁序沿实际 pipeline：全局 DATA_CHANGE advisory → 源行/全部旧新关系/项目/contract/workbook states/warehouse/所有派生影响域 → 最终 actor/实际参与用户锁 → 当前 identity/Grant/输入与关系闭包重验 → 变更回执投影 → commit。不能先持 actor SHARE，再由 auto assignment 追锁/升级 SysUser；复用 S3 source auto 的“预收实际 final users + 一次稳定 ID 排序锁 + 映射重验”。新增项目是本事务对象，真实 ID 产生后在提交前再验，不造 fake project/空 ALL。

内部 `SourceImportPermit` 必须封闭构造、绑定 db transaction、owner/epoch、mode、原件/完整 plan、允许的实际 source IDs 和域目标。销售自动建项、WBDD 自动归属、仓库关联、成本重算、DQ 检测只能消费该许可的确切计划；不能将它变成外部 API 可传的 bypass bool，也不借已迁 S3 的任意 read Grant。源合同 `all` 仅指这一来源的全量源写入，不能因此允许其它来源、任意人工财务写入或回传全公司统计。

源导入和派生重算若实际需要全库锁/更新，应在真实计划内明确该服务器派生阶段，不虚称只影响本次上传项目。最小实现可保留既有全局 advisory 与计算，重点是先收齐实际用户/域锁、最终复验，避免凭权限改业务事务边界。

## 7. 历史归属与安全差分

- generic `/batches` 当前直接读整个 SysImportBatch，含 S3 的 maint_bulk/roundtrip/其它协议。S4 必须按真实 protocol 分开；推荐通用九入口只读已验证 generic source batches，S3 内容继续走其已有独立领域入口。若要提供跳转，只可返回当时获准的目标 locator，不借 generic read_report 返回 S3 report。这是关闭旁路的明确差分。
- `file_type=workbook` 只能由可验证归档与同版选表、或者有一致性证明的 immutable 原始来源证据回填 expense。未知 inquiry/类型/损坏文件/owner 不明确不猜权限；返回封闭 origin-unverified 状态，不暴露原件/报告。不能通过用户名匹配当前账号补稳定所有者。是否存在可证明历史 stable identity 的取证来源需迁移时逐类核对；无证据的旧行不自动认领。
- 当前 worker `except loader.ImportIntegrityError:` 后使用 `str(exc)`，但没有 `as exc`。这是实际异常分支错误，S4 同包须用真实失败文件复现并修正，保留原子回滚和后续进度，不把它当授权成功或吞掉最后状态。
- 系统账号控制面、机器接口、public/auth bootstrap 由 root 单独定义。S1 profile/template 原子命令、epoch/CAS 和 legacy-seed-origin 均复用；不因当前 catalog 旧 planned alias 或过时总数重新造授权真值。

## 8. 文件 ownership 与逐入口反例

本包已冻结独占：`api/imports.py`、新增 `services/source_import_access.py`、必要 `etl/pipeline.py/loader.py/expense_void.py` 受控 callback，以及 import tests。共享 models、单迁移、catalog/policies/registry/seed/runtime 由本作者唯一写入，消费 root 平台/机器声明及 Agent 作者声明；root 独占 boundaries、control_access、公共 domain_contexts 抽取与相关 API/principal/service 接缝，不并改。前端 owner负责 ImportPage/API adapter；Agent 不调用 imports API 实现，若未来工具支持导入，只消费同一个深模块。

| 测试组 | 必须实测的反例/不变量 |
| --- | --- |
| 分类/入口 4 POST | 同名文件不同 bytes/顺序；mixed workbook 实际expense而非purchase；无询价执行能力；一个来源获准不放整批；view零job/事实/文件持久写；precheck内容v2断言不改 |
| 上传与报告分离 | submit-only成功但零历史poll/报告SQL；report-only独立可读实际报告；legacy seed缺一字段不种整报告；返回report/recompute/worker note均不可偷带诊断 |
| 作废预演 | K=false金额列不物化/DTO null、SQL指纹仍随实际金额改变；HMAC正确但epoch/plan过期仍拒；同批两作废文件保持422；D-09/行数上限不降弱 |
| 历史 5 GET | direct ID越域拒；范围在limit/count前；mixed job隐藏域不返回filename/note/计数/下标；未知owner/type、S3batch不能借generic门；无report不查errors/error count；允许空结果与拒绝不同 |
| worker | enqueue后撤epoch、首文件后撤epoch、等域锁时撤权；前文件保留/当前及后续零写；改名与同名新ID；manifest/原件hash/plan/结果篡改；旧NULLowner；线程异常和IntegrityError真实分支 |
| 副作用 | sales自动项目/合同、WBDD自动新建+owner-only回填、仓库旧新归属、expense作废全目标；域锁后一次final users，无actor先锁；currentsource授权失败所有未提交副作用与sidecar回滚 |
| 发布 | 同RR完整CSV spool；审计后/首header前/下一chunk撤权拒；临时文件断连关闭；CSV公式保护；无partial byte被宣称成功 |
| 迁移 | true/false origin；inactive/deleted；显式空override/snapshot不覆写；workbook有效分类/unknown拒；marker/owner/manifest降级保护与真实旧schema往返 |

原测试至少保留：`test_import_precheck_v2`、`test_import_precheck_exact_duplicate`、`test_import_precheck_upsert_expense`、`test_expense_void_preview(_api)`、`test_expense_void_foundations`、`test_import_platform/robustness/file_count_limit/error_download`、`test_raw_archive_atomicity`、销售/维保地址/导入硬化与真实 loader 事务测试。新测试通过公开 HTTP、worker 深模块和真实 PostgreSQL/PDP，不用 mock allow 或只扫 route 数量代替语义验证。
