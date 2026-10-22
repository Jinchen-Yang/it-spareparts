# ITSP-4b 免返历史批准清单更正工具

决策: D-32

本候选实现 [D-32 第 3 款（Q23=B）](../decisions/0010-免返标注与免返标准.md)：小郭先按合同核实项目和明细，有权实名操作者再逐项执行批准清单。覆盖度统计只是核对线索，不能自动生成批准结论。本文同时记录实现边界、清单格式、操作和回退方式，不是生产操作授权。

叠加在 #423 之上，审查请看 d9e26d7 之后的提交。沿用其 [单行更正契约](../integrations/maintenance-site-issue-facts-v1.md#网页单行更正调用方itsp-2)，唯一事实写入路径为 `maintenance_site_issue_line_correction.correct_site_issue_line`，对应既有 `PATCH /api/maintenance/site-issues/{issue_id}/lines/{issue_line_id}`。本工具通过新适配模块调用该服务，不修改该服务的公开签名，不另建 no_return 赋值算法。

## 范围与实现边界

| 文件 | 职责 |
|---|---|
| `backend/scripts/audit_no_return_coverage.py` | 输出只读覆盖报告，供人工核对合同和标准品类 |
| `backend/app/services/maintenance_no_return_coverage.py` | 使用统一实时／冻结读模型形成覆盖报告 |
| `backend/scripts/correct_no_return_from_approved_manifest.py` | 默认干跑；显式参数齐全时只执行一个清单项 |
| `backend/app/services/maintenance_no_return_approval.py` | 校验批准文件、稳定身份、前像、权限和因果回执，调用既有单行命令 |

只允许修改批准明细的 `no_return`：`true` 为免返，`false` 为必须返，`null` 为恢复继承规则。中文“是否应返还”的是／否与 no_return 布尔方向相反。原始 `null` 不表示错误或缺漏；继承标准硬盘规则后显示免返，也不等于原始值已是 `true`。

不修改项目 `no_return_default`、免返标注、数量、PN、成本或实际收货；不全量改硬盘、不固化所有 NULL、不提供批量 apply 开关；不重建旧发货义务，不新增返还状态。保持 [D-31](../decisions/0009-返还数量上限与需求单核对.md) 的实际收货及 N/R 口径。本 PR 不新增网页入口。

## 运行环境与身份

命令从本工作树 `backend/` 运行，使用现有应用配置中的 `DATABASE_URL`。环境连接由受控运维环境配置，本文不提供生产连接或访问步骤；示例中的 ID、账号和文件名均须替换为本次候选环境真实值。覆盖报告可使用数据库只读账号。

`--operator` 接受有效实名系统账号的精确 `sys_user.username`，不接受展示名、任意签名或共享身份。工具按当前账号重新检查 `page_maintenance`、`action_maintenance_site_issue_manage`、`data_purchase_cost` 和项目范围；锁后以及重放仍检查。批准人和执行人分别保留，批准凭据不代替系统编辑权限。

CLI 是受控运维入口，依赖主机和数据库访问授权；`--operator` 字符串本身不完成密码认证。拥有运行权限的操作者负责如实选用实际执行人的账号，不把本工具开放为未经认证的公共服务。

## 第一步：生成只读覆盖报告

```bash
cd /home/cloudlay/workspaces/wt-laneA-nrapprove/backend
uv run python -m scripts.audit_no_return_coverage \
  --environment-label candidate \
  --format json > no-return-coverage.json
uv run python -m scripts.audit_no_return_coverage \
  --environment-label candidate \
  --project-id project-example \
  --format csv > no-return-coverage.csv
```

`--project-id` 可重复指定稳定项目 ID；不指定则报告当前连接内的全部项目。报告在 PostgreSQL `REPEATABLE READ READ ONLY` 一致性事务中读取，不创建工作簿 state，不写命令、审计、版本或访问日志。

JSON 包含 `metadata`、`summary`、`projects`、`rows`；CSV 的 `record_type` 分别为 `metadata`、`summary`、`project`、`line`，嵌套聚合保存在 JSON 单元格内。元数据记录人工环境标签、快照时间、代码 SHA 和实际过滤口径，不输出连接串。有效明细为已确认／已更正、未作废单头下的 active 行；停用项目保留在报告中并标明状态，供历史核对，不因此获得更正资格。

CSV 对可能被表格软件解释为公式的文本前缀加单引号；JSON 保留精确文本。覆盖报告不能直接作为可执行 manifest，须逐项核对并填写明确批准目标和证据。

报告分开提供：

- 原始 `no_return` 的 true／false／null 数量；有效判定 `effective_no_return` 的 true／false／null 分别对应免返／应返／品类待判。
- 稳定项目、单据、明细 ID、双版本、来源、PN；标准分类 ID 和品类，与主数据文字品类的差异。
- 各项目硬盘／标准品类覆盖情况、应返／免返／待判数量及判定依据。已有活动旧义务沿统一读模型保留冻结分类和数量。

文字“硬盘”与标准分类不能混用。2026-10-15 调研中的“28 个项目”只是一份历史核对线索；本工具不把它当作当前库事实或自动更正条件。

## 第二步：形成并批准清单

支持 UTF-8 JSON 根数组或 CSV。SHA256 对文件**原始字节**计算，包括 BOM、换行和格式；调整空格、换行或 CSV 排版也会改变摘要。SHA 不放进文件自身，由批准记录绑定清单文件及外部摘要。文件上限 10 MB、1 至 10000 项；批准后保留原文件，不另存为或改编码后继续使用旧摘要。

| 字段 | 格式和用途 |
|---|---|
| `entry_id` | 本文件内唯一；1–64 位字母、数字、下划线、点或短横线，首位为字母或数字 |
| `project_id`、`issue_id`、`issue_line_id` | 稳定身份，不能用项目名、展示单号或 Excel 行号替代 |
| `source` | 核对时实际来源；可执行来源仅 `page_manual`、`workbook`、`legacy` |
| `expected_issue_version`、`expected_line_version` | 批准时单头／明细版本，正整数 |
| `expected_current_no_return` | 批准前像；JSON 严格 true／false／null，CSV 小写 `true`／`false`／`null` |
| `target_no_return` | 该项明确批准的目标三态，同上 |
| `reason` | 逐项更正原因，不以“统一所有硬盘”代替合同核对 |
| `approver` | 批准人，非空文本，最多 64 字符 |
| `approval_date` | 真实日期，`YYYY-MM-DD` |
| `evidence_reference` | 合同／凭据定位引用，非空，最多 500 字符 |
| `predecessor_entry_id` | 可选；同清单中该单的紧邻前项 ID，声明同单逐项执行的因果链 |

除可选前序外全部字段必填。CSV 空单元格不能代替布尔 `null`；可选前序为空表示无前序。JSON 不接受字符串 `"true"`、数字 0/1 或字符串版本号。拒绝未知字段、重复 JSON 字段、重复 CSV 表头、列数不一致、重复 entry_id 或重复目标明细。文本拒绝首尾空白和控制字符；需要在同一行追加反向更正时须另建清单。原因、SHA、entry、批准人、日期和凭据合成后的审计原因不得超过 1000 字符，超长会拒绝，不截断证据。

JSON 模板（保存为 `approved-no-return.json`；示例身份与版本须人工替换）：

```json
[
  {
    "entry_id": "E001",
    "project_id": "project-example",
    "issue_id": "issue-example",
    "issue_line_id": "line-example-a",
    "source": "workbook",
    "expected_issue_version": 7,
    "expected_line_version": 3,
    "expected_current_no_return": false,
    "target_no_return": true,
    "reason": "核对合同附件第3项，该明细约定免返",
    "approver": "批准人实名",
    "approval_date": "2026-10-20",
    "evidence_reference": "合同HT-示例/附件2/第3项/批准记录AP-示例"
  },
  {
    "entry_id": "E002",
    "project_id": "project-example",
    "issue_id": "issue-example",
    "issue_line_id": "line-example-b",
    "source": "workbook",
    "expected_issue_version": 7,
    "expected_line_version": 2,
    "expected_current_no_return": false,
    "target_no_return": null,
    "reason": "核对合同附件第4项，无逐行例外，恢复按规则判断",
    "approver": "批准人实名",
    "approval_date": "2026-10-20",
    "evidence_reference": "合同HT-示例/附件2/第4项/批准记录AP-示例",
    "predecessor_entry_id": "E001"
  }
]
```

同一项的 CSV 模板：

```csv
entry_id,project_id,issue_id,issue_line_id,source,expected_issue_version,expected_line_version,expected_current_no_return,target_no_return,reason,approver,approval_date,evidence_reference,predecessor_entry_id
E001,project-example,issue-example,line-example-a,workbook,7,3,false,true,核对合同附件第3项约定免返,批准人实名,2026-10-20,合同HT-示例/附件2/第3项/批准记录AP-示例,
```

同单多项保留同一个批准时的 issue 版本，后项显式引用本清单中该单的紧邻前项；两项必须属于同项目、不同目标明细。不要预填猜测的后续版本，不借其他清单或其他单据的成功回执接续。

## 第三步：默认干跑并复核

```bash
uv run python -m scripts.correct_no_return_from_approved_manifest \
  --manifest approved-no-return.json > no-return-dry-run.json
```

不带 `--apply-one` 时始终只读。可以额外提供 `--operator actual.sys.username`，在只读快照内一起检查该账号当前权限；不提供账号时只做清单和数据核对，不表示执行权限已经通过。

干跑输出文件摘要及每项稳定身份、辅助展示名／单号／PN、旧值、目标值、合同凭据、原因、批准版本和当前版本、可执行或冲突／拒绝原因。确认原始三态，不能仅按界面派生的“应返／免返”判断没有变化。干跑不创建命令回执、审计或 workbook state，不推进版本，不能锁定未来状态；执行时仍重新检查。

两项模板首次整体干跑时，E001 尚未执行，E002 会报告 `conflict / causal_receipt_required`，这是缺少前序成功回执的保守预期，不是要求刷新批准版本。E001 获批执行成功后，再用原文件干跑，E001 显示原回执 `replay`，E002 才按该回执版本核对。前序 no-op 也有成功回执，可以接续，但不推进单版本。正常退出码为 0；含 conflict／rejected 时为 2。意外故障或输出丢失不能证明提交未发生，按下文原请求重试核对。

来源、归属、前像或版本已变化时重新核对并形成新的批准清单；不把输出的当前版本拷回旧批准文件后沿用旧摘要。缺少证据的项保持不执行。

## 第四步：只执行一个批准项

批准人应明确确认精确文件及其 SHA256。下面命令中的摘要必须来自该批准记录；不能只对任意新文件临时算一个摘要，就宣称它已经获批。

```bash
sha256sum approved-no-return.json
uv run python -m scripts.correct_no_return_from_approved_manifest \
  --manifest approved-no-return.json \
  --apply-one E001 \
  --manifest-sha256 APPROVED_SHA256_HEX \
  --operator actual.sys.username > no-return-E001-receipt.json
```

三个执行参数必须齐全。每次只执行一个 entry、一个事务；没有 apply-all。当前权限、稳定身份、来源、有效性、批准前像和双版本均通过后，经既有命令的锁、CAS、审计及 revision 链提交。`site_issue_v2`、delivery-backed、非有效单据、停用项目或失效明细拒绝，不能借批准清单恢复它们。

幂等键由清单 SHA 和 entry_id 确定。事实变化、行／单版本、返还要求审计、事实审计、事件、工作簿 revision 和既有 command 回执同事务保存。审计原因包含 SHA、entry_id、原因、批准人、日期和证据引用。

既有 command 回执保持追加后不可修改。工具在现有 `maintenance_project_operation_audit` 追加批准执行审计：`entity_type=no_return_approved_entry`、`entity_id=command.command_id`、`action=approved_manifest_applied`；`after_json` 保存文件 SHA、批准项全内容、执行账号及首次实际 `effective_expected_issue_version`。该审计与事实、command 同事务提交；首次和重放的外层回执附带同一份 `approved_manifest`，不回写原 command JSON。无变化的首次执行也追加这条批准执行审计，业务变化审计和事件仍不增加。CLI 输出文件是操作凭据副本，不能替代数据库审计。

| 结果 | 含义及下一步 |
|---|---|
| `success` | 该项已改变事实；保存回执，按批准顺序继续下一项 |
| `replay` | 同一请求已成功，返回首次回执；不新增业务审计、事件或版本 |
| `no-op` | 前像和版本匹配，原始值已经等于本项目标；不推进事实版本／revision，保留命令回执及批准执行审计 |
| `conflict` | 归属、来源、版本或前像漂移，或幂等意图冲突；停止该项，重新核对，不自动刷新 |
| `rejected` | 参数、摘要、证据、权限、有效性或来源不满足；无业务写入，按具体原因处理 |

当前值碰巧已是目标，但没有原命令回执且批准前像不同，仍为冲突，不能声称是本工具先前成功。任何失败只回滚当前项，之前已经成功的其他项保留。

## 同单因果版本与中断重试

示例 E001 成功后，若回执单版本为 8，E002 仅可依据该成功回执以 8 执行；其明细版本和原始 no_return 仍须匹配批准前像。批准文件内的 7 及文件摘要保持不变。没有前序成功回执时拒绝接续；前序失败不能通过手工填“当前版本”跳过。

```bash
uv run python -m scripts.correct_no_return_from_approved_manifest \
  --manifest approved-no-return.json \
  --apply-one E002 \
  --manifest-sha256 APPROVED_SHA256_HEX \
  --operator actual.sys.username > no-return-E002-receipt.json
```

E001 成功后，如果第三人修改同单任意明细，使当前 issue 版本不再等于前项回执，E002 必须冲突。即使目标 E002 行自身没改，也不静默跨过这次外部变更。未声明前序的同单后项仍按其批准 issue 版本检查，通常会因前项推进版本而冲突。

执行超时、终端断开或输出文件丢失时，用**原文件、原 SHA、原 entry 和原操作者**重试；不要改文件、换 entry_id、另造键或查询最新版本来重构请求。已有回执固定首次执行期版本，后续行执行后重放较早项仍返回其原结果。重放仍受当前权限约束，已撤权不能凭历史成功绕过。

## 撤销与代码回退

业务撤销是新的反向批准清单，不删除原命令、审计或事件。复制原稳定身份，用原成功回执中的 `issue_version`、`line_version` 和 after `no_return` 作新批准前像，以原 before 值为目标；填写新的 entry_id、原因、批准人、日期和证据，并取得新文件摘要的批准，再执行相同的干跑和单项流程。

例如只执行了 E001 且回执为单版本 8／行版本 4／`no_return=true`，撤销项须以 8／4／true 为前像、false 为目标。原值如果是 null，撤销目标应为 null，不能替换成看起来相同的显式布尔。

若之后同单任何事实已经更正，撤销的原 after 基线会冲突，包括本清单另一项随后成功的情形。保留历史，请人重新核对是否仍应反向更正并另行批准；不得自动升级撤销版本或覆盖后来修改。

本候选没有 schema 迁移或历史回填，不需要数据 downgrade。代码回退只停用本工具入口，保留所有已提交事实与追加历史；代码回退不会自动撤销业务更正。候选不通过时停止继续执行，保留清单、摘要、结果与审计供核对。

## 定向验证场景

以下是验收合同；实际执行数量和命令记录在本候选 PR 草稿，不以场景列表冒充测试已通过。仅使用专用真实 PostgreSQL 和合成数据。每次测试前执行用户指定 `gate.sh`，返回 75 时等待 60 秒重试；同时最多一个测试进程，只运行定向文件，完整套件交 CI。

| 起点／操作 | 必须观察到的结果 |
|---|---|
| 批准文件默认干跑 | 业务行、双版本、state、命令、审计、事件均无变化 |
| 单项成功、同请求重放 | 首次审计／事件／revision／回执完整；重放原回执，业务计数不再增加 |
| 内容变动后沿用旧 SHA | rejected，零写入 |
| 批准后目标行变化；当前碰巧等于目标 | conflict，不能当作 replay 或刷新后覆盖 |
| 无动作／数据权限、范围外、停用账号 | rejected；撤权后历史重放同样拒绝 |
| delivery-backed、site_issue_v2、失效行／单 | rejected，不恢复失效事实 |
| 同单两项，后项指向前项 | 使用前项回执因果版本；无回执或外部插入变更时停止 |
| 中断后重试早期项 | 使用首次固定请求，后续成功不会改变早期回执 |
| 新反向清单撤销 | 无后续变化时成功并保留原历史；后续变化时 conflict |
| 只读账号生成覆盖报告 | JSON／CSV可核对，全部行与版本不变，标准／文字分类差异可见 |
| 事务中审计／事件／回执失败 | 当前项整体回滚，不留下半条事实或 revision |

## 待金臣确认

- **页面入口**：本 PR 仅后端及运维工具；是否增加网页批准清单入口及具体流程，另行确认。
- **批准材料流程**：批准人资格、批准记录存档位置、合同／凭据真伪和审批有效性的核验责任。本轮只强制非空合法字段、稳定身份／版本和精确 SHA 绑定，不自动认证文件所称批准是否真实，也不建立电子签名或新的审批角色。

开发测试通过不代表用户业务验收、main 审批或部署已完成。本任务只准备本地提交与 PR 草稿，不推送、合并、部署或访问生产。
