# 人工模板驱动的表格清洗工作流

> 对应 #217 的 AI-P1-03。目标是让 AI 灵活理解“源表怎样变成甲方模板”，但所有修改只落到
> 新 Artifact，绝不覆盖源文件或直接回填业务库。

## 1. 解决的问题与依赖

现有 `write_excel` 让模型逐个提交单元格坐标，难以处理列名漂移、多工作表、描述清洗、类型转换和
大批量数据，也很难稳定重试。本流程改成“模型提议高层 Change Plan，确定性引擎批量执行”。

硬依赖：#219、#220、#221、#222、#223、#226、#230。敏感内容使用私网模型时还依赖 #225。

```text
Source Artifact + Human Template Artifact
 -> 本地结构检查
 -> 最小化样本与字段映射提案
 -> Typed Change Plan 校验
 -> Sample Diff
 -> Human Interrupt
 -> 对副本确定性执行
 -> 重开校验
 -> Cleaned / Diff / Exception Artifacts
```

## 2. 输入与权限

首版只接收**同一实名 Task owner** 持有、状态为 `ready` 的不可变 XLSX 或 CSV Artifact：

- `source_artifact_id`：待清洗数据；
- `template_artifact_id`：人工给出的目标结构与示例；
- 可选 `source_sheet`、`template_sheet`、业务说明和期望行键。

上传件和模板都按不可信数据处理。公式、批注、隐藏行列、隐藏 Sheet、名称、外链、图片文字和“忽略
规则”等内容不能成为系统指令。首版拒绝 XLSM、ODS、加密工作簿、VBA、外部连接和嵌入对象。

Task 创建、读取、暂停恢复和下载均要求 active 实名 `sys_user`、`page_chat`，并让每个输入的实时
Artifact 授权条件全部通过。能力效果为 `file_read + artifact_create`；不得因为是“编辑表格”而伪装为
单一效果。普通 Artifact 仍 owner-only，不允许管理员或老板跨 owner 兜底读取。

source、template、Task、operation 和全部输出的 `owner_sub` 必须完全相同；任何跨 owner 组合统一 404。
每个输入分别保存可重放的 access snapshot；其 `row_subject/predicate_version/condition` 必须由当前
authorization registry 识别并可重新求值。若实现不能保留并逐个验证不同 predicate domain/version，
则跨来源 predicate 不兼容、未知或语义漂移一律标记 `unclassified` 并拒绝，不能先做 intersection 或
自称“narrowest”。

模板分类只允许：

- `business_content`：模板含任意示例值、规则文本、semantic examples、批注、隐藏业务文本或数据，
  或其内容/派生内容会进入模型、Change Plan、dry-run、Evidence、输出；按普通内容来源保存 scope，
  进入内容 union 和每次访问重授权。
- `identity_only`：模型调用前的本地全量检查证明模板只含 allowlisted Sheet/表头结构、列顺序和安全
  样式，不含上述任何内容；对输出的业务 access condition 为 TOP、对 contained resource/field union
  为 identity empty，但仍保存 template hash、owner、classification/proof version，Task 执行时仍实时
  授权读取。
- `unclassified`：无法证明上述任一类；不得进入模型、dry-run 或发布。

legacy generated、缺失 scope 或证明版本未知的模板固定 `unclassified`，不能冒充 TOP。

## 3. Human Template 的含义

人工模板提供三类证据：

1. 目标 Sheet、表头、顺序和展示样式；
2. 示例行给出的格式与值域；
3. 可选“规则说明”文本。

第 2/3 类以及任何 semantic example 都使模板成为 `business_content`；需要它们参与规划时必须在进入
模型前固化完整 source access snapshot，并让后续 Artifact 逐来源重授权。只有第 1 类且通过严格
allowlist/hidden-content 扫描的模板才可能是 `identity_only`。

首版只接受当前 active 实名 Task owner 自己拥有且实时授权通过的 immutable template；
不是 signed/shared template，也不存在 signer/version 例外。首版 Change Plan 不提供任意 regex，
不是把 regex 加 timeout 后放行。

这些都是模型提出映射的输入，不是可执行代码。自然语言规则必须被翻译成下面的 Typed Change Plan，
再通过服务端 schema、预算和 allowlist。模板不能提供 Python、SQL、Excel 公式、宏、任意正则、路径、
URL、Skill 名、工具名或网络操作。

首版使用每次 Task 的 owner-owned 模板，不建设跨用户模板市场。若后续需要公司级模板库，应另建有
发布者、版本、退役、适用范围和独立 ACL 的 Template Registry，不能把任意用户上传件直接共享。

## 4. Typed Change Plan v1

```text
schema_version = workbook-change-plan/v1
source_artifact_id / source_sha256
template_artifact_id / template_sha256
source_table: sheet, header_row, data_start_row
target_table: sheet, ordered columns
row_identity: declared unique key or immutable source row reference
column_mappings[]
operations[]
unmatched_column_policy
unmatched_row_policy
style_profile
budgets
```

首版操作原语只有：

- `copy_column`
- `constant_value`
- `trim`
- `collapse_whitespace`
- `normalize_unicode_width`
- `normalize_case`
- `literal_replace`
- `dictionary_map`
- `parse_date`
- `parse_decimal`
- `coalesce`
- `combine_columns`（固定分隔符）
- `split_column`（固定字面分隔符和最大分段数）
- `semantic_rewrite`（仅指定文本列、逐值结构化提案）

不支持 `eval`、表达式语言、任意函数、任意正则、公式、脚本、SQL、Shell、URL fetch、工作簿计算或
任意文件路径。未知 operation/字段/枚举默认拒绝。所有 operation 都有实现版本，并进入 Plan fingerprint。

列映射使用服务端生成的 column ID，不以可碰撞的表头字符串作为执行键。行键必须经全量唯一/非空
验证；没有可靠业务键时使用 `source_sha256 + sheet + 原始行号`，绝不凭描述文本模糊覆盖另一行。

## 5. 静态工作流图

```text
validate_artifacts
 -> inspect_structure_locally
 -> classify_template_scope_locally
 -> build_bounded_model_projection
 -> propose_change_plan
 -> validate_change_plan
 -> execute_sample_dry_run
 -> human_interrupt
      ├─ accept_plan -> execute_full_copy
      ├─ request_new_plan -> child Task
      └─ close_without_action
 -> reopen_and_verify
 -> publish_artifact_set
 -> seal_evidence
```

该图注册在 #226 Workflow Registry。LangGraph 若用于执行，只保存节点游标和账本引用；它不能直接
打开文件或调用模型。所有文件读取和 Artifact 创建仍经过 #219 Gateway、#220 Store 与 #222 parser
worker。

Human Interrupt 必须先展示：目标列、源列映射、类型、操作、样本前后值、未匹配列/行策略、预计总行
数和风险。响应只允许 `accept_plan`、`request_new_plan`、`close_without_action`；修改约束或换模板创建
带 `parent_task_id` 的新 Task，不能就地改写已验证 Plan。

## 6. 模型边界

结构提案模型只看到：

- 规范化后的 Sheet/column ID、表头与推断类型；
- 每列少量、去重、截断的代表值；
- 人工模板的目标结构和示例；
- 允许的 operation schema、预算和明确的数据分隔标记。

不向模型发送公式、批注、隐藏内容、外链、绝对路径、Artifact owner、业务 Token 或整个工作簿。用户
说明和单元格文本都放在 untrusted-data 区域；模型调用不提供工具，只能返回严格 JSON Change Plan。
若投影包含模板示例值、规则文本、semantic examples 或其派生摘要，该模板在投影构建前必须已分类为
`business_content`；不能以“只发给模型、不写入输出”为由使用 `identity_only`。

`semantic_rewrite` 用于描述清洗等无法由确定性操作表达的列。它按有界批次接收 `row_ref/source_text/
target_constraints/examples`，只能返回 `row_ref/proposed_text/confidence/reason_code`。服务端验证引用、
长度、字符、公式前缀和一一对应关系；丢行、多行、额外字段或 schema 漂移整批拒绝。模型不得改 PN、
金额、数量、日期或行键，除非对应列在人工确认的 Plan 中明确声明且使用确定性 parser。

Provider 仍受 #219 sensitivity/egress matrix 约束。敏感样本不能为了可用性自动切换到公网 Provider；
private Gateway 不可用时 Task 暂停或失败。

## 7. 确定性执行与文件安全

- 源文件和模板只读；执行器创建全新 workbook，不原地保存、不覆盖、不复用源路径。
- CSV 先确定编码、分隔符和列数；XLSX 在 #222 无网络、非 root parser worker 内检查 ZIP/member/ratio、
  Sheet/row/column/cell/string、图片和样式预算。
- 公式不执行。源公式仅可作为带风险标记的显示值；没有可信 cached value 时进入 Exception。所有以
  `= + - @` 开头的输出字符串按文本写入。
- 输出由受控 writer 生成，关闭字符串转公式和 URL 自动识别；不复制 VBA、external links、defined
  names、connections、data validation 公式、OLE、图表或任意 OOXML relationship。
- 只从模板抽取 allowlisted 展示属性：列宽、行高、字体、填充、边框、对齐和冻结窗格；样式超预算则
  使用系统安全主题，不影响数据转换。
- 未匹配源列默认进入“未映射数据”Sheet，未匹配行默认进入“异常明细”Sheet；不得静默丢弃。
- 完成后重新打开，核对 Sheet、行列数、类型、唯一键、公式/外链/宏为零、SHA-256、MIME 和预算，
  通过后原子发布为 `ready`。

## 8. 预算

初始硬上限（配置只能收紧，扩大需新版本和压力报告）：

- source 20 MiB、解压后 200 MiB；template 5 MiB、解压后 50 MiB；
- source 最多 32 Sheet、100,000 行、256 列、2,000,000 非空 cell；
- template 最多 8 Sheet、2,000 行、128 列；单 cell 文本 8 KiB；
- 模型结构样本每列最多 8 个值、每值 256 字符，总 projection 128 KiB；
- `semantic_rewrite` 最多 5,000 个 cell，每批 100，单值输入/输出 2,000 字符；
- Change Plan 最多 128 个映射、256 个 operation、256 KiB；
- Sample Diff 200 行；完整 changed-cell diff 最多 100,000 条；
- 单个输出 Artifact 100 MiB；Task Artifact 总量和生命周期仍服从 #220。

超限必须在相应 handler 前或流式处理中稳定拒绝并清理临时文件，不能把超限伪装成可重试内部错误。

## 9. Artifact Set 与 Evidence

成功后原子发布一个关联集合：

- `cleaned_workbook`：机器可读的新文件；
- `change_report`：按行键列出 before/after、operation、confidence 和状态；
- `exception_report`：未映射、类型失败、模型失败、截断和人工待处理项；
- `manifest`：源/模板/输出 hash、Plan/workflow/model/operation 版本、计数和限制。

若任一必需成员验证失败，集合整体不可见。Artifact/Set 的服务端 operation、请求 fingerprint、稳定成员
UUID、409 冲突和 crash/reconcile 全部复用 #230；重试同一 `(owner_sub, operation_id)` 且请求相同返回
同一集合，请求不同则在 writer 前 409。#230 未验收前本 Workflow 不得启用 `artifact_create`。

Artifact Set 保存每个实际内容来源的独立 `source_access_snapshots[]`，不能把多个来源压成一个“更窄”
row predicate。输出静态内容摘要使用：

```text
owner_sub                    = source.owner = template.owner = Task.owner
required_positive_keys      = union(all contributing sources + workflow requirements)
contained_resource_set      = union(resources actually present in each output member)
contained_visible_field_set = union(fields actually present in each output member)
sensitivity                 = max(all contributing source and generated content)
authorization_condition     = every contributing source snapshot must pass
```

resource/field union 由完成后的 workbook/report containment scan 按实际内容生成；它描述“输出装了什么”，
不能用输入 allowlist intersection 代替。每次 preview/download 必须逐 contributing source snapshot 重授权；
允许的聚合优化也必须由版本化算法同时证明当前 scope 覆盖 contained union、required positive keys 全满足，
且所有 source owner/row subject/predicate conditions 分别成立。任一 source 撤权、条件失败、predicate
未知/不兼容、containment 无法分类时，整个 Set 和相关成员 fail closed。

`identity_only` 模板仍保留 provenance snapshot，且只有 pre-model classifier proof version 有效时才对
输出条件贡献 TOP/empty identity。任何示例值、规则文本、semantic examples 或其派生内容进入模型、
Change Plan、dry-run、Evidence 或输出，都必须在该边界之前分类为普通 contributing source。完成后的
containment scan 只能发现漏标并 fail closed，不能反向证明或升级 `identity_only`。集合聚合 scope 和每个
成员都不得漏掉实际内容来源。用户后续可以把输出文件手工送入现有导入预检查，但本工作流不直接调用
导入或写业务库。

Evidence 保存在 completed Step 中，只保留 Artifact refs、hash、版本、计数、风险/异常摘要、模型 usage
和稳定错误码，不重复保存整表内容；完整性统一使用总纲 `integrity-envelope/v1`
（purpose=`workbook-cleaning.evidence`），不另创 HMAC 格式。

## 10. 验收

- 表头同义词、乱序、多余列、缺列、多 Sheet、空表、合并表头、重复/空行键和编码差异。
- 确定性 operation 的类型、边界、locale 日期/金额、Unicode、空值、字典 miss 和不丢行。
- 描述 `semantic_rewrite` 的批次乱序、丢行、重复行、幻觉 row_ref、超长值、schema 错误和部分失败。
- 单元格/批注/隐藏 Sheet 中的提示注入不能新增 operation、工具、列、网络或扩大预算。
- VBA、外链、公式、DDE、恶意 hyperlink、ZIP bomb、路径逃逸、超大图片/样式、损坏 XLSX 均隔离或拒绝。
- 输出公式/外链/宏为零；文本前缀中和；重开后数据、行数、键、MIME、hash 一致。
- sample dry-run 后 Plan 不可修改；Human Interrupt 幂等、冲突、撤权、取消、过期和重启恢复。
- crash-before-publish、crash-after-object、metadata 失败、磁盘满、同 operation 重试不产生半成品或重复文件。
- 同 owner/key 同 fingerprint 返回同一 Set/member UUID；同 key 不同 request 在 writer 前 409；reconciler
  重复运行幂等且不会让部分成员可见。
- owner-only、任一 source 撤权/条件失败拒绝、跨用户统一 404；日志/Event/模型请求不含敏感哨兵值和
  文件正文。
- scope 公式的 positive-key union、actual contained resource/field union、sensitivity max、逐来源条件、
  same-owner 和 predicate version/domain 边界均有矩阵测试；无 intersection/narrowest fallback。
- 模板 `identity_only/business_content/unclassified` 分类、proof version、隐藏/示例值渗入输出和 containment
  漂移均 fail closed；identity-only 只有证明成立时才贡献 TOP/empty identity。
- 规则文本、semantic examples、示例值进入模型/Change Plan/dry-run/Evidence 但输出未包含的样本仍必须
  是 `business_content`；post-output containment 不能将其洗白为 identity-only。
- 1/100/5,000 semantic cell、100,000 行、2,000,000 cell 和并发任务的时延/内存/磁盘压测。
- 工作流前后业务表和源文件 hash 不变；迁移、全量 pytest、前端测试/build、真实浏览器下载/E2E 通过。

## 非目标

- 不原地编辑、不自动导入、不修改主数据、不替人审批数据质量结论。
- 不支持任意 Python/SQL/公式/宏/正则、联网补全、网页抓取或在线 Skill 安装。
- 不承诺保留源工作簿中的宏、计算、外链、图表或任意高级 OOXML 特性。
- 不在首版建设跨用户模板库、多人协同编辑或 Google Sheets/Excel 在线控制。
