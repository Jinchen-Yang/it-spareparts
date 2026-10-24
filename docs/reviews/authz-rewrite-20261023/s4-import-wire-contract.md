# S4 通用导入 wire 合同 v1

状态：资源、动作、字段和受限投影已由 root 冻结；本文是实施前的唯一 DTO 设计，不是运行证据。S3 精确 CI 全绿并收到开工通知前不改 app/tests。业务与 seed 合同见 [执行合同](s4-import-execution-contract.md)，样例见 [golden DTO](s4-import-wire-golden.json)。样例中的 revision/token/hash 是形状示例，不是可复算数据库或 HMAC 测试向量。

## 1. 共用类型与请求约束

复用现有 `AuthorizationContext`、`Decision` 和单 carrier `authorization_precondition`；不增加第二份 epoch、allowed、角色或授权缓存。所有正式 JSON 成功响应固定带以下四项；错误只有 `detail`，授权视图只有下述两项，不能机械套成功 envelope。

```ts
type ImportEnvelope = {
  schema_version: 1;
  authorization_mode: "unified_modules";
  catalog_version: string;
  authorization_context: AuthorizationContext;
};
type Source = "purchase" | "sales" | "inventory" | "maintenance" | "expense";
type Mode = "skip" | "upsert";
type AuthorizationResolution = {
  locator: { collection_ref: string } | { object_type: string; object_id: string };
  authorization_context: AuthorizationContext;
};
```

这里 `AuthorizationContext` 就是当前 `ContextAccessSet.wire()`：schema_version、catalog_version、authorization_epoch、context、resources。`resources` 按本 entry 的真实分支给出完整 resource/action 决定、grant_version 和本资源 field_permissions；不是省略 deny 的 allow 列表。context 的内部 selector 不传客户端，规范参数和实际文件已经绑定到 authorization_revision。前端严格匹配当前生成的 entry/branch 合同，不能用另一个 entry 的 context 提交。

六资源都登记 `context_scopes=["all"]`、`query_attributes=[]`；all 表示完整来源范围，不豁免实际 input/collection/job/batch 上下文。模块快照的数据动作仍 requires_context，display 动作沿既定模块 allow；只有当前实际 entry 的正式决定可放行数据请求。

| 字段 | 类型与含义 | 约束 |
| --- | --- | --- |
| 数值主键 | 正 safe integer | 不接受字符串、bool、浮点或超出 JS 安全范围的数字；内部 ObjectRef 的相同 ID 是十进制字符串 |
| version、epoch、数量、下标 | 非负 safe integer | 不舍入；隐藏数量为 null，真实空才是 0 |
| source_domain | Source | 来自实际服务器选表，不接受客户端赋权声明；历史未知不猜 |
| input_sha256 | 64 位小写 hex | 服务端由真实 bytes 计算；可回显用户自己的输入承诺，不能代替权限 |
| item_id | 32 位小写 hex | 持久随机主键；不编码 ordinal、文件数、source 或用户 |
| mode | Mode | canonical 未知值 422，不像旧路由静默改成 skip |
| partial / restricted | boolean | partial 表示该 job 有未返回项；restricted 明确表示受限投影，不能当作尚无结果 |

所有成功、错误和授权视图均 `Cache-Control: no-store`。未知 query/body/form 字段、重复冲突 precondition、非法 mode/ID、坏 JSON 均用统一 detail 错误，不吞字段继续执行。JSON body 放 `authorization_precondition`；multipart 放同名 Form 字符串；GET/Blob 放同名 query JSON。相同入口只允许一种位置，query+form 同时提供即 422。

## 2. 九个入口的固定传输

全部路径以 `/api` 开头；保持已有方法，不新增通用 raw download、job cancel 或报告别名。

| entry | 原业务输入 | canonical 正式结果 |
| --- | --- | --- |
| POST `/import/precheck` | multipart `files`，query `mode=skip` | PrecheckResponse |
| POST `/import/upload` | multipart `file`、可选 `preview_token`，query mode | UploadReceipt |
| POST `/import/upload-batch` | multipart `files`、重复 `preview_tokens`，query mode | JobReceipt |
| POST `/import/expense-void-preview` | multipart `files`，query mode | VoidPreviewResponse |
| GET `/import/jobs` | 无业务 query | JobListResponse；最多 50 个获准 job |
| GET `/import/jobs/{job_id}` | 实际正整数 ID | JobResponse |
| GET `/import/batches` | 无业务 query | BatchListResponse；最多 100 个获准 batch |
| GET `/import/batches/{batch_id}` | 实际正整数 ID | BatchResponse |
| GET `/import/batches/{batch_id}/errors.csv` | 实际正整数 ID | 原 UTF-8 BOM CSV Blob |

九条同路径支持 `view=authorization`：只解析必要实际输入/身份/关系并返回标准 AuthorizationResolution，零 job、batch、归档、事实写入和任务启动。GET 不存在对象不能返回 allow。POST 上传按 bytes、顺序、规范 filename/MIME、mode、选表/解析版本及实际 preview-token 承诺绑定完整 command；正式请求重传同输入并重验。

精确载体：precheck/upload/upload-batch/void-preview 用已注册 `operation_input`，purpose 分别为 `import.precheck`、`import.upload`、`import.upload_batch`、`import.expense_void_preview`；它们不能持久作 worker 来源。jobs/batches 列表分别用 collection `generic_import_jobs` / `generic_import_batches`，selection={}。持久 job/batch 使用 object_type `generic_import_job` / `generic_import_batch`，实际 ID 为 SysImportJob / SysImportBatch 主键。worker 内部 item 可用 `generic_import_item`，不因此增加 HTTP 路径。

canonical 的列表是显式对象 envelope，不探测“新对象或旧数组”自动兼容。只有服务器明确 legacy runtime 才走旧 adapter。列表 `total:null` 表示该协议未请求全量计数；`returned_count` 仅为返回 items 长度，`limit` 是 50 或 100，不能代表总量。先按已验证 generic protocol、source 权限及 origin 筛 SQL，再排序和 limit。

## 3. 内容预检与提交分开

```ts
type InputBinding = {
  file_index: number;
  input_sha256: string;
  source_domain: Source | null;
};
type PrecheckResponse = ImportEnvelope & {
  content: ExistingImportPrecheckV2;
  inputs: InputBinding[];
};
type UploadReceipt = ImportEnvelope & {
  batch_id: number;
  source_domain: Source;
  status: "success" | "partial" | "failed";
  committed: true;
  derived_refresh: "complete" | "failed" | "not_needed";
};
type JobReceipt = ImportEnvelope & {
  job_id: number;
  status: "processing";
  accepted_files: number;
  committed: true;
};
```

`ExistingImportPrecheckV2` 是现有 `import_precheck.response` 的内容合同，前端把 `content` 单独交 `normalizeImportPrecheck`；不改 severity、can_import、can_import_all 或 sheet 决定来表达权限。`inputs` 与用户 files 和 content.files 一一同顺序，不能凭同名文件合并。输入诊断只来自用户提供的内容；数据库 duplicate 信息是额外授权读取。

已识别文件逐 source 要求全部 precheck；缺任一为整体 403，没有 partial content。完全不能识别的输入使用精确 `unrecognized_input` 分支，要求至少一个明确 source.precheck，且只给用户输入格式诊断；不查询业务事实、duplicate、不排队、不归档。混合已识别与未知输入仍须全部已识别来源 precheck，未知部分不制造额外来源许可。未知源正式 submit 固定 422，零 job/事实写。

`exact_success_match.batch_id` 仅在对应真实 batch.read_status 获准时可出现；无该权时 exact_success_match 和 blocked_reason 为 null，内容决策按未获可见重复证据的输入计算。null 不保证数据库没有相同文件。正式 duplicate 仍按原幂等规则拒绝；无历史读权限只返固定错误，不带旧 batch ID/上传人。存在 read_status 时允许的重复信息仍遵守原 skip/upsert 内容规则。

UploadReceipt 是本次命令回执，不是历史读权：status 只说明本次已完成源命令。它不返回 report/recompute/错误明细/全库派生统计；报告通过已有 batch detail 独立 read_report。derived_refresh=failed 说明源已提交而服务器后置派生失败，不能自动重传源文件。JobReceipt 的 accepted_files 仅是调用者此次提交的已知文件数，不授后续 job 读取。

前端 submit 面的 display 依赖是 `precheck AND (submit_skip OR submit_upsert)`，各 mode 分支分别成立；这不变成后端 submit 动作的 precheck 硬依赖。正式提交依自身 entry view→execute，不能用 precheck 的 context/precondition。expense 文件只有实际业务要求作废 HMAC 时才执行独立 void.preview；不把此动作加到所有 upsert。

## 4. 批次与混合作业

```ts
type BatchMetadata = {
  filename: string;
  file_type: string;                 // 原业务审计包装，可为 workbook
  status: "processing" | "success" | "partial" | "failed" | "superseded";
  uploaded_at: string;
  uploaded_by: string | null;         // 仅显示，不作 canonical owner key
  rows_total: number;
  rows_inserted: number;
  rows_skipped: number;
  rows_error: number;
  rows_inactive: number;
};
type BatchStatusItem = {
  batch_id: number;
  source_domain: Source;
  metadata: BatchMetadata;
};
type BatchResponse = ImportEnvelope & {
  batch_id: number;
  source_domain: Source;
  metadata: BatchMetadata | null;
  report: Record<string, JsonValue> | null;
  errors: {row_no:number|null; nature:"提示"|"错误"; error_type:string|null; detail:string|null}[];
  issue_count: number | null;
  block_access: {status:Decision; report:Decision};
};
type BatchListResponse = ImportEnvelope & {
  items: BatchStatusItem[];
  returned_count: number;
  total: null;
  limit: 100;
  restricted: boolean;
};
```

batch detail 的结构合同是同 source 的 read_status/read_report 两项，准入为二者 OR。status deny 时 metadata=null；report deny 时 report=null/errors=[]/issue_count=null，且不执行 report_json/error_detail/error count 查询。report allow 且无错误才给 issue_count=0。完整报告/错误受本 source 的 C/S/K/F 四字段 AND 约束，不能借 void.K 或其它 source 字段。最多 500 条错误明细的原业务限制保留；issue_count 是获准报告的真实总数。`raw_row` 不因拥有报告权自动成为新输出列。

```ts
type JobStatus = "processing" | "done" | "partial" | "failed";
type JobItemCode =
  | "IMPORT_CONTENT_INVALID" | "IMPORT_DUPLICATE" | "IMPORT_CONFLICT"
  | "IMPORT_VOID_PREVIEW_REQUIRED" | "IMPORT_VOID_PREVIEW_STALE"
  | "IMPORT_STORAGE_UNAVAILABLE" | "IMPORT_EXECUTION_FAILED"
  | "AUTHN_SESSION_INVALID" | "AUTHZ_FORBIDDEN" | "AUTHZ_CONTEXT_STALE"
  | "AUTHZ_ARTIFACT_STALE" | "AUTHZ_RESOURCE_UNAVAILABLE" | "AUTHZ_ENGINE_UNAVAILABLE";
type JobItem = {
  item_id: string;
  input_index: number | null;
  source_domain: Source;
  filename: string;
  state: "queued" | "processing" | "success" | "partial" | "failed" | "authorization_failed";
  batch_id: number | null;
  code: JobItemCode | null;
};
type JobProjection = {
  job_id: number;
  created_at: string;
  finished_at: string | null;
  mode: Mode;
  partial: boolean;
  restricted: boolean;
  status: JobStatus | null;
  total_files: number | null;
  done_files: number | null;
  error_files: number | null;
  note: string | null;
  visible_status: JobStatus;
  visible_total_files: number;
  visible_done_files: number;
  visible_error_files: number;
  items: JobItem[];
};
type JobResponse = ImportEnvelope & {job:JobProjection};
type JobListResponse = ImportEnvelope & {
  items: JobProjection[];
  returned_count: number;
  total: null;
  limit: 50;
  restricted: boolean;
};
```

每个 job 至少一个真实 item.read_status 获准才可返回。partial=true 必须 restricted=true，全部 item.input_index=null，finished_at/status/total_files/done_files/error_files/note 全 null；items 只含获准 source，按稳定随机 item_id 排序，不保留隐藏 ordinal 空洞。visible_* 仅由这些可见项计算，不扫描未授权结果 JSON。visible_status 是可见进度：尚有 queued/processing→processing；全部已终结且全成功→done；全部失败→failed；其余→partial。授权失败属于可见失败项，不能含具体隐藏目标/错误正文。

全部项可见时 partial=false/restricted=false，input_index 为实际输入顺序，顶层完整聚合可返回。note 即使此时也仅封闭固定状态说明，不能重放旧 SysImportJob.note 中任意文件/异常文本；诊断只走独立报告。列表 restricted 表示读者并非所有 generic source 的 read_status 全允许，不通过扫描隐藏 rows 证明其存在。允许空列表正常返回 items=[]/returned_count=0；缺全部来源 read_status 为 403。

JobItemCode 是共享生成器的闭集，不是旧异常 code 的自由字符串：queued/processing/success/partial 的 code=null；failed 只用 IMPORT_* 七项；authorization_failed 只用 AUTHN_*/AUTHZ_* 六项。reader/transform/完整性错误归 CONTENT_INVALID，唯一性冲突归 CONFLICT，原文件重复归 DUPLICATE，归档故障归 STORAGE_UNAVAILABLE，其余未分类内部异常归 EXECUTION_FAILED。完整原异常仅服务器日志；旧 worker note 不参与 code 推断或透传。

已知 batch/job 的 deep link 只需其实际读取动作。error-export-only 可直接对已知 batch ID 请求 errors.csv 的授权视图，不要求 list/read_status/read_report；无 ID 只显示输入壳。未知 protocol/type/owner 不猜来源，直接定位返回固定 `IMPORT_ORIGIN_UNVERIFIED`，无 filename/report；列表排除这类对象。具有 scope=all 的读者仍可读其它已验证所有者的合法 generic 历史，绝不默认改为 owner-only。

## 5. 作废预演、错误与发布

```ts
type VoidPreviewResponse = ImportEnvelope & {
  fingerprint_version: 2;
  previews: ExistingExpenseVoidPreview[];
};
```

`ExistingExpenseVoidPreview` 保留既有 ready/suppressed/will_be_rejected/not_applicable/unreadable/too_large、contract/contracts、行数、void/void_rows、row_cap、preview_token/error。ready 仍要求非空 token 与真实 contract；K=false 时 void.amount 和每行 amount=null，SQL 只返回显示允许列及数据库内金额摘要。source 输入错误由实际输入生成；内部数据库/驱动错误只能固定安全文案。canonical token 明确 v2 指纹；旧 v1 仅 never-cutover 分支接受，不解释成 v2。

v2 金额承诺在 SQL 内完成，输入规范使用完整 raw ID/状态/合同/金额摘要与现行 suppressed 条件；preview 与锁后 loader 同算法。不得将原金额、未加保护的金额明文派生值或库异常塞入 token payload。token 的外部形状仍不透明字符串；它只证明业务预演承诺，不授 void/read/submit 权。正式输入/模式/token/权限任一变化都 409 或相应拒绝，前端不自动重放。

所有错误使用 `detail.code` 与 `detail.message`。授权错误沿现有 authz_error；不存在对象 404，上传输入的未知来源/输入格式 422，历史 origin/owner 无法核验为 409 IMPORT_ORIGIN_UNVERIFIED，版本/预演/重复冲突 409，授权基础设施故障 503。业务旧失败批次留痕必须诚实：如已经记录 failed batch，错误额外给 `commit_state:"receipt_only"`，不要说整笔数据库零写；尚未写入为 `commit_state:"none"`。此字段不代替既有 HTTP 状态，失败时不自动上传重试。仅新实际回执可回本次 batch_id；旧 duplicate ID 必须单独 read_status。后置派生失败仍返回已提交 UploadReceipt，不伪造未提交错误。

CSV 保留原列、顺序、BOM、公式注入防护和 MIME；同 RR 内完整物化后通过公共 publication guard。audit 后、首 headers 前、每个 chunk 均复验当前动作与持久 batch 来源，授权失效不继续发剩余 bytes。不因身份是生成者或管理员跳过 export_errors/来源检查。

## 6. 接口冻结与验证边界

前端只消费本文件形状和从运行时 registry 生成的词表；golden 为 DTO 对拍辅助，不替代真实 PG/PDP、worker 撤权、字段 SQL 和字节发布测试。底层来源解析、完整内容摘要、origin 迁移、同事务锁序见执行合同。S4 shared catalog/registry/models/seed/单迁移由 backend owner 单写，root 提供平台边界并抽公共 domain_contexts；这里不引入第二 registry。
