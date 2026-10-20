# 领用事实编辑原语契约 v1

- 版本：`v1.1`（新增 HTTP 调用方，原语接口不变）。
- 日期：2026-10-20。
- 状态：#379 已实现公共原语并接入既有 V1／V2 工作簿、page_manual 完整命令；默认局部补丁协议现由 ITSP-2 的单行网页更正命令接入，履行本文的权限、CAS、幂等及事务职责。开发验证与用户验收状态见候选交接。
- 决策：D-31、D-32；提取工作簿共用写链遵循既有 D-01、D-02、D-26、D-28；V2 首次完整回执遵循 [D-38](../decisions/0016-工作簿首次应用回执.md)。
- 唯一提供方：P2／S-B，`backend/app/services/maintenance_site_issue_facts.py`。现有消费方：V1／V2 的 06 工作簿 Adapter、page_manual 完整命令；默认协议消费方：P1／S-A 单行网页更正命令。

## 来源与范围

依据《01-开工计划》§二、§五、《P1 返还台账边界》§2.2、§2.3、§3.6、§4.2及《P2 行级表工作台边界》§2、§4。#379 已从现有 06 事实编辑路径提取本原语并接入旧入口；后续 P1 页内领用编辑只编排命令，不另写 no_return、成本、版本或事件算法。

本原语只服务已经授权的人的编辑。运行时 AI 不得据此写采购、销售、库存、项目、领用或申请/审批业务状态。实际返还收货是另一独立事实链，不因领用修改而增减，不按领用行宣称已返；领用登记及更正本身无库存效应。

## Interface

```python
# 输入实体已按调用入口协议锁定，字段意图已完成鉴权/CAS验证。
def apply_locked_site_issue_edits(
    db: Session,
    *,
    project: MaintenanceProject,
    issue: MaintenanceSiteIssue,
    line_changes: Mapping[str, Mapping[str, object]],
    header_changes: Mapping[str, object] | None,
    operated_by: str,
    reason: str,
    legacy_context: LegacySiteIssueEditContext | None = None,
) -> SiteIssueEditResult: ...
```

| 输入／输出 | 约束 |
|---|---|
| `project`、`issue` | 同一 Session 中已锁定、锁后新鲜重读的实体，issue 属于 project；不得只凭展示号或 PN 定位 |
| `line_changes` | key 为稳定 `issue_line_id`；值为局部字段补丁，未出现行不动，未出现字段不动；空映射合法无操作 |
| `header_changes` | `None` 或空映射表示不改单头；只接受既有可编辑字段和归一化字段意图 |
| `operated_by`、`reason` | 真实实名操作者及非空原因；不接受前端冒认身份；同事务审计可追溯 |
| `legacy_context` | 默认 `None` 使用局部补丁协议；只有已接入的旧完整命令可传 `v1/v2/manual` 兼容上下文，不能由 HTTP 请求选择 |
| `SiteIssueEditResult.changed_line_ids` | 实际发生事实、成本证据或版本变化的行 ID，按稳定顺序，不包含原样回传行 |
| `issue_version`、`line_versions` | 本次完成后权威版本；line_versions 覆盖请求涉及及因单头日期重算实际变化的行；无变化维持原版本 |
| `before`、`after` | 本次实际更正的单头/明细及必要成本证据快照，供审计与结果核对；不是可直接下发的脱敏 DTO |

局部补丁不表达新建、删除、复活或整单替换，不接收“本次未出现的行即作废”。06 的 CREATE／缺行／VOID、签名基线和三路合并仍由工作簿 Adapter 在原整本事务中处理；不能把网页选中数组送到完整 lines 替换接口。

## 字段意图与来源

原语复验行属于本单、来源合法、当前有效、字段白名单及字段值；调用者已校验也不能省略归属和白名单防线。事实字段沿用现有 06 可编辑集合，典型包括 PN/part_id、quantity、SN、no_return、remark；不是开放任意 ORM 列。PN 文本与 part_id 必须按既有身份解析成一致的字段意图，不能直接绑定停用或歧义身份。主数据库 PN 描述、品牌、品类不由领用原语修改。

默认协议对显式提供的 `demand_order_no` 去除首尾空白，空字符串归一为 `None`，表示清空关联；未提供则保留原值。非空单号必须通过既有 `_resolve_demand_project`，以需求单的活跃项目归属验证属于当前项目，不按项目名称、PN 或单号相似度推断。不存在、未归属或跨项目统一返回 `site_issue_edit_demand_project_mismatch`，message 为“关联需求单不属于当前项目，请核对单号或留空”。全部行意图和单头意图在任何赋值前完成验证；即使同次请求还有合法修改，也不得写入事实、版本、审计或事件。旧完整命令继续由原入口完成其归一化与归属校验，不借兼容上下文扩大默认协议的输入范围。

`no_return=true/false/null` 分别为免返／明确应返／恢复按规则；省略表示不修改，null 不能等同省略。继续使用现行标准硬盘默认免返、逐行明确设置优先及已有冻结义务规则。项目免返文本只作提醒，不由本原语解析成事实或开启项目默认开关。

单头可编辑字段限现有 `issue_no/issue_date/site_location` 等已获入口批准字段。行 PATCH 必须令 `header_changes=None`，改单头走独立 issue CAS 命令；地址“未提供／设置／显式清空”意图沿用现有 D-26，包括其 2026-10-13 修订，不能把缺字段解释为清空。人工改 issue_no 仍走既有改号唯一性校验；`issue_id/issue_line_id/project_id/source/import_batch_id/display_no` 不可由此修改，展示号分配后不变。

page_manual 保持既有完整预览冻结协议；workbook／legacy 由 P1 的稳定受控 Adapter 接入，不能假设旧 page_manual 专属 PATCH 已覆盖它们。默认局部协议只允许 `page_manual/workbook/legacy` 且 confirmed/corrected 的有效行；`site_issue_v2` 和带 `delivery_line_id` 的行继续拒绝。原生发货来源、冻结义务及生产失败关闭守卫不变；抽取共用原语不授权打开 Beta、重启原生确认或伪造 delivery_line_id。

## 调用者与原语职责

| 责任 | P1 网页／P2 工作簿调用者 | P2 共用原语／P3 规则函数 |
|---|---|---|
| 登录、动作权、项目范围、字段权限、实名 | 入口校验并锁后复验；不把前端 scope 当授权 | 原语复验实体归属、来源和字段白名单，不建立第二权限系统 |
| 版本和冲突 | 网页校 expected issue/line version；工作簿校签名和三路基线；先验证后赋值 | 不替调用者接受过期 CAS；按最终实际变化推进必要版本 |
| 幂等 | 网页沿既有 command action=`correct`，全表唯一键与指纹覆盖 issue/line/意图/基线，持锁后确认重放；工作簿保留既有 replay | 不创建命令回执、不申请幂等事务锁、不在重放时再次执行事实编辑 |
| 字段与成本 | 提交归一化局部意图，不复制赋值、成本解析 | 原语做统一验证/赋值，复用现有成本证据失效与重算 |
| 返还要求 | 可只读调用 P3 `line_details`，不自行拼 outbox | 原语先 P3 `snapshot`，再赋最终事实，调用 P3 `record_corrections` |
| 审计 | 命令结果、入口访问日志与必要外层业务动作留痕 | P3 写返还要求 correction audit/outbox；原语补齐快照外事实审计，不重写同一更正事件 |
| workbook revision／提交 | 外层一次 bump（沿根事务按项目去重 helper）、写结果回执、一次 commit；异常 rollback | 原语不 bump、不 commit/rollback、不取调用者规定的上层锁 |

P3 `snapshot` 覆盖 `issue_line_id/no_return/quantity/part_id/is_active/version`；`record_corrections` 只对这些返还要求事实变化推进 line/issue 版本并写 correction event。它不替调用者建立上层锁前缀、校 CAS、赋 no_return、提交或推进工作簿 revision；历史义务兼容分支保留原下游锁，详见下节。

默认协议每次调用只处理一张领用单：有实际变化时恰好写一条 `site_issue_facts_edited`，包含本次单头与全部受影响行的前后快照；无变化写零条。多单据命令应分别调用，每个受影响单据恰好一条；同一事务内不同的实际编辑仍分别留痕，不按 Session 或事务吞掉后续编辑。P3 的 `return_obligation_corrected` 逐行审计和每个返还要求实际变化单据一次 correction outbox 是独立职责，不计为重复的 `site_issue_facts_edited`；只有单头或备注变化不产生 P3 事件。旧 `legacy_context` 路径保留原有外层审计，不额外写默认协议的事实审计；其收口阶段按单汇总原始 P3 快照，包含外层软作废行，每单调用一次 corrections。

SN-only、remark-only、仅成本证据和单头字段变化不在该 snapshot 中。原语必须补相应事实审计和 line/issue 版本，使旧编辑/补价 CAS 失效；P3 已经推进的版本不能由新命令再推进一次，无变化不制造版本。旧完整人工接口在 corrections 后可能还有既有 issue.version 推进，提取必须分别冻结旧接口兼容返回与新接口返回，不能为统一次数顺手破坏旧 CAS。

PN 变更使旧成本证据失效，quantity 变化按现有规则重新计算成本；仅改备注不强制重写未触碰的历史数量。单头 `issue_date` 更正时，调用者须先锁本单全部有效行，原语逐行复核日期影响的取价/证据，推进真正变化行的版本，并把这些行纳入结果。成本瀑布、税率与未知不等于零均沿现状，不发明另一套取价；地址变更不成为重新取价依据。

## 锁序与事务

新网页命令遵循已有领用链：命令幂等事务锁 → workbook state → project → issue → 按稳定顺序指定行 → 锁后新鲜重读与 CAS → 原语 → 外层 revision/回执 → commit。改单头日期时，行锁覆盖本单全部有效领用行，不能只锁用户请求行。批量仍是逐行事务；同单上一行成功后，下一行使用回执里的新 issue version，不能无条件复用旧版本。

06 原有全局数据锁、身份锁、state/project、需求及领用锁前缀和整本原子事务继续由 P2 Adapter 负责。原语不在拿到行锁后再补申请 state/project 等上层锁，不能自行开启另一个事务；多项目锁保持既有确定顺序。P2 接 06 时移除已由原语承担的重复 corrections 调用，保留签名、七字段身份、三路 merge、作废优先及根事务 revision 去重。

原语自身的实体读取、需求单归属校验和常规 P3 correction 路径不发出 `FOR UPDATE`，依赖调用者已持有的锁。若本单已有 `maintenance-return-obligation-interface-v1` 历史事件，P3 继续调用 `maintenance_bad_returns.consume_return_event`，其既有义务行等 `FOR UPDATE` 保持不变。本候选不改变这条历史锁协议；契约验证分别观察常规路径无新增行锁，以及历史分支原有下游行锁和事务回滚，不能把“原语不自取上层锁”解释成所有历史下游路径都无锁。

任一字段、成本、版本或审计失败，调用者回滚同一事务，事实、版本、审计、outbox、命令回执和 revision 不得半提交。先验证全部最终意图再赋 ORM；失败不能留下稍后被无关 commit 偷写的脏实体。不以 SQLite／PGLite 或顺序请求证明此锁序并发正确。

## 离线 JSON 示例

这些是内部原语输入／结果的合成序列化夹具，不是新增 HTTP 路由或真实业务数据；权限、expected versions 与幂等键由外层命令处理，不冒充原语参数。

```json
{
  "contract_version": "v1.0",
  "input": {
    "project_id": "example-project",
    "issue_id": "example-issue",
    "line_changes": {
      "example-line-a": {"no_return": null, "remark": "人工核对"}
    },
    "header_changes": null,
    "operated_by": "example-real-user",
    "reason": "恢复按现行规则判断并补备注"
  },
  "result": {
    "changed_line_ids": ["example-line-a"],
    "issue_version": 8,
    "line_versions": {"example-line-a": 4},
    "before": {
      "lines": {"example-line-a": {"no_return": true, "remark": "", "version": 3}}
    },
    "after": {
      "lines": {"example-line-a": {"no_return": null, "remark": "人工核对", "version": 4}}
    }
  }
}
```

未出现在 `line_changes` 的 example-line-b 保持有效且原值不变。上例前置 issue version 为 7，结果 8、line 3→4 仅用于说明一次实际更正；不能将这些固定数字当所有来源旧接口的统一推进次数。结果 before/after 含内部审计资料，HTTP Adapter 必须按字段权限投影，不能直接回传成本或来源私有字段。

## 错误码与恢复

`SiteIssueEditError(code, message)` 提供稳定的 `code`、面向人的 `message`、`status_code` 和 `detail={"code": code, "message": message}`；`str(exc)` 仍为 message。调用方按 code 分支，不解析中文文案。原语可抛出的错误如下：

| code | HTTP | 语义／恢复 |
|---|---|---|
| `site_issue_edit_invalid_fields` | 422 | 未知/只读字段、非法值或不合法字段意图；修正输入 |
| `site_issue_edit_identity_conflict` | 409 | 项目不匹配、行不存在或不属于锁定本单；重新读取，不猜匹配 |
| `site_issue_edit_inactive` | 409 | 项目停用、单头或行已作废；拒绝复活 |
| `site_issue_edit_source_blocked` | 409 | 来源/冻结义务/生产守卫不允许本命令；走批准的原入口 |
| `site_issue_edit_permission_denied` | 403 | 调用方无权执行该更正；不写事实 |
| `site_issue_edit_actor_required` | 422 | 缺实名操作者（空或超过 64 字符）或缺更正原因；修正输入后重试，不写事实 |
| `site_issue_edit_demand_project_mismatch` | 409 | 关联需求单不存在、没有本项目的活跃归属或跨项目；核对单号或显式留空 |

现有工作簿 apply 与 page_manual 写入包装器窄捕获 `SiteIssueEditError`，先 rollback，再按 `status_code` 返回既有结构化错误格式：

```json
{
  "detail": {
    "code": "site_issue_edit_demand_project_mismatch",
    "message": "关联需求单不属于当前项目，请核对单号或留空"
  }
}
```

工作簿 detail 另外保留 `issues: []`。其他既有校验、冲突及权限异常仍沿用原入口的状态码和响应，不在本次统一改形；旧完整命令可能在进入原语前已由原校验拒绝输入。新增单行 HTTP Adapter 使用相同的结构化错误形状，见下节。

以下错误属于外层命令职责；单行网页命令已实现 version/idempotency conflict，lock_context/audit_unavailable 仍为保留码。原语不检测调用者是否实际持锁、不执行 CAS 或幂等回执，也不把任意数据库异常转换成这些码：

| code | 建议 HTTP | 外层职责／恢复 |
|---|---|---|
| `site_issue_edit_lock_context_required` | 503 | 调用者未建立必要事务/锁上下文；服务端修正，不能降级无锁写 |
| `site_issue_edit_audit_unavailable` | 503 | 同事务审计无法保存；回滚，不先写事实再补审计 |
| `site_issue_edit_version_conflict` | 409 | 外层 CAS 失败；保留草稿，显式重读新基线后再确认 |
| `site_issue_edit_idempotency_conflict` | 409 | 外层同键不同载荷/基线；不能换键盲重试 |

原语本身不承诺幂等重放；外层先检查既有回执，不重复调用原语，所有业务审计和事件条数保持不变。手工整单命令持久化完整响应，同键重放返回原回执（仅重放标志变化）；V2 完整回执及历史兼容限制见下节。网络结果未知时沿同一请求/键核对或回读，不能擅自判定首次未写；成功但刷新失败应保留成功结果，不再次更正。

## 网页单行更正调用方（ITSP-2）

稳定路由：`PATCH /api/maintenance/site-issues/{issue_id}/lines/{issue_line_id}`，HTTP Adapter 位于 `api/maintenance_project_operations.py`，事务编排位于 `services/maintenance_site_issue_line_correction.py`。请求不接收操作人，实名来自当前登录账号。

```json
{
  "expected_issue_version": 7,
  "expected_line_version": 3,
  "changes": {"no_return": null},
  "reason": "按合同核对后恢复规则判断",
  "idempotency_key": "site-return-example-command-001"
}
```

本轮采用窄字段白名单，只允许 `no_return` 为严格布尔或 null；`changes={}` 表示不修改，未提供 `changes` 为非法请求。未知字段、改单头、整个 lines 数组均拒绝。`False/True/None` 分别为应返／免返／按规则判断，与 UI “是否应返还”的是／否方向相反。数量、PN、SN、备注及需求单等后续扩展须补相应 HTTP 契约测试，继续调用同一原语；本轮不提前开放 ITSP-8 编辑能力。

权限复用既有 `page_maintenance`、`action_maintenance_site_issue_manage`、`data_purchase_cost` 及项目范围，不新增角色。首次与锁后均检查当前实名账号和权限，幂等回放也不绕过当前授权。只使用默认局部协议，HTTP 不能选择 `legacy_context`。

命令使用全表唯一 `maintenance_site_issue_command.idempotency_key`、`action=correct`；指纹包含入口名 `site_issue_line_correction_v1`、project/issue/line、两个 expected versions、完整 changes、原因及实名操作者，区分省略和 null。首次执行先拿幂等事务锁，再 state→project→issue→目标行，锁后新鲜重读和授权，须同时通过单头与明细版本 CAS。已有同指纹回执在 issue 锁及新鲜授权之后、目标行锁/CAS 之前返回，因此成功后合法合单迁移明细也不丢失原回执。

成功响应示例：

```json
{
  "issue_id": "example-issue",
  "issue_line_id": "example-line-a",
  "issue_version": 8,
  "line_version": 4,
  "no_return": null,
  "changed": true,
  "idempotent_replay": false,
  "workbook_revision": 12
}
```

只投影身份、版本、三态、变化/回放标记及 revision，不下发原语完整 before/after。实际变化经原语记录每行返还要求审计和事实审计，并产生相应 correction outbox；外层沿根事务按项目去重 helper bump 一次 revision，再保存原响应并提交。无变化不推进版本/revision、不增加业务审计，但保存首次命令回执。重放只将 `idempotent_replay` 改为 true，其余返回首次保存值。

版本冲突为 HTTP 409：

```json
{
  "detail": {
    "code": "site_issue_edit_version_conflict",
    "message": "领用单或明细版本已变化，请刷新核对后重试",
    "current_issue_version": 8,
    "current_line_version": 4
  }
}
```

同键不同意图/原因/基线/入口为 `site_issue_edit_idempotency_conflict`（409）；数据库完整性冲突为 `site_issue_edit_write_conflict`（409），回滚后提示刷新核对；项目范围或动作权限失败为 403，字段输入非法为 422；原语来源/有效性/归属错误沿上表，不转成成功或复活行。任意失败回滚，审计/回执失败不得遗留事实或 revision。

页面批量是一组独立单行命令。同单下一行只能接续上次成功回执中的 `issue_version`，目标行仍使用读取时的 `line_version`；409 不能自动取错误详情中的版本覆盖重试。原始 no_return 已等于目标的行在 UI 报告“未变化”且不发请求；不能将规则派生的相同显示当作相同三态。结果未知只用原请求/原键核对，成功行不重发，结束刷新读模型。工作簿继续原有签名/三路合并冲突规则，revision 推进不意味着所有旧文件一律拒绝。

### V2 工作簿首次完整回执

[D-38](../decisions/0016-工作簿首次应用回执.md)固化本次用户“重放返回原回执”的验收要求。V2 apply 在现有 operation 之外，复用 `MaintenanceProjectOperationAudit` 保存首次完整应用响应；不新增迁移，不让事实原语拥有回执职责：

| 命令完成审计字段 | 约束 |
|---|---|
| `project_id`、`operated_by` | 与成功 operation 的项目和实名操作者一致 |
| `entity_type`、`action` | `master_workbook_operation`、`apply_completed` |
| `entity_id`、`operated_at` | `str(operation.id)`、`operation.created_at`；审计自身 id 独立生成 |
| `after_json` | 保存 `schema=master_v2_apply_receipt_v1`、`operation_key`、首次完整 `response` 与布尔元数据 `requires_contract_amount_manage`（首次 plan 是否含合同额修改） |

首次 result 的批次号、summary、changes/conflicts/overridden、warnings、voided_rows、接管与 revision 漂移标记等都属于快照。命令完成审计、operation、事实、版本和原有事实审计／事件同事务保存，快照失败则整个应用回滚；完成快照不是 `site_issue_facts_edited`，应独立计数。有快照时重放只把保存 response 的 `replayed` 改为 true，其余字段与首次完全一致；不新增任何业务审计、事件或 operation，不再次推进 revision。

真实 HTTP 同文件重上传先执行原有登录、项目范围与工作簿编辑鉴权，再按文件 SHA、项目和当前实名操作者匹配已成功的应用快照，在重新验证当前事实前返回。SHA 精确匹配首次已校验的文件字节，不解析或信任客户端提交的新计划，也不读取其他项目或操作者的回执。服务端首次 plan 生成的 `requires_contract_amount_manage=true` 时，重放仍需 `_require_contract_amount_manage` 的当前资格；其余原有依赖不放宽，不能从当前已无差异的计划推断无需金额资格。提前查询只读、不加新锁或提交；service 继续保留原有锁前缀、幂等事务锁和 operation 锁，处理同次首次应用与重试竞争。已有快照若 schema、身份或布尔权限标记无效，以 `idempotency_conflict` 拒绝，不能伪装成历史缺快照降级。

历史 operation 缺少完整快照时，HTTP 继续原完整 validate/apply，service 保留既有回执重建兼容路径及防重复执行。此路径无法准确恢复首次完整 response，不能承诺批次号与所有变化／警告相等；不以当前库态生成假历史快照，不做回填。有持久快照的成功应用才具备首次完整回执保证，旧 page_manual 的既有完整回执协议保持。

## 兼容、交付与验收

| 提供方／消费者 | 兼容要求 |
|---|---|
| #379／既有 06 与人工完整命令 | 已接入同一公共原语，通过 `LegacySiteIssueEditContext` 保留旧入口的版本、批量重价、审计及完整单语义 |
| ITSP-2／默认局部补丁协议 | 单行网页命令只开放 no_return 三态，保留原语审计、来源和稳定错误契约；不放开 Beta |
| P2 提取／既有 06 | 保留往返/冲突/作废/审计/版本/revision 契约；未选行、七字段基线和旧文件身份保持 |
| P2 v1／P1 新网页命令 | 同一事实原语，局部补丁；P1 负责权限、CAS、幂等、锁和提交 |
| P2 v1／旧 page_manual 完整命令 | 保留预览冻结、完整单语义和旧版本兼容，不以局部补丁替代整单协议 |
| V2 apply／原文件再上传 | 当前鉴权后读取首次持久快照，除 `replayed` 外返回原完整响应；历史无快照保留重建限制，不伪造 |
| 后续字段扩展 | 先补白名单、字段意图、成本/版本/审计契约及两消费者测试；删除字段或改变副作用须升主版本 |

默认无迁移、无历史业务回填。回退新增页内入口可保留原语及 06 兼容实现；已成功更正事实与审计保留，恢复走人的正常更正，不反向批量写 no_return。schema downgrade 不适用。

| 目标／角色与起点 | 操作步骤 | 可观察预期 |
|---|---|---|
| 局部编辑；有权实名人；同单 A/B 两行 | 页面只改 A 的 quantity/no_return/remark，读取并导出 06 | 只改 A，B 未失效；三态往返和 R/库存不变，成本按当前规则 |
| 版本覆盖；两名有权人；旧 Excel 和旧补价基线 | 仅改 SN／备注／成本证据，再提交旧编辑/补价/工作簿 | 必须按各入口既有冲突协议识别变更，不能因 P3 snapshot 未覆盖而漏 CAS |
| 单头日期；有权更正人；多行取价窗口不同 | 更正 issue_date 后回读全部行和成本 | 锁全部有效行，受影响成本重算、变化行版本推进，地址不触发重价 |
| 来源和作废；workbook/legacy/page_manual/site_issue_v2 夹具 | 各来源页内更正，再尝试已作废行或受禁来源 | 支持来源走正确 Adapter；作废不复活；原生失败关闭保持 |
| 原子性；真实 PostgreSQL 双连接＋故障注入 | 网页与 06/补价/作废并发，审计或重算中途失败 | 无死锁/丢改动/半提交；每项目每根事务 revision 至多一次，审计/outbox 不重复 |
| 幂等和批量；有权人；同单多行、响应丢失 | 相同键重试，第二行用前行回执版本；409后保留草稿 | 成功不重复执行，冲突不自动升版本覆盖，失败行明确；批量无跨行原子承诺 |
| 多单审计；默认协议及 V1／V2／人工完整命令合成夹具 | 一次命令改多单多行，另改单头，再重放原请求 | 默认每个变更单据一条 facts 审计；P3 每个返还要求变化单据一次事件；旧外层审计保持；手工命令与有快照的 V2 重放原完整回执，历史 V2 保留兼容 |
| 需求单归属；同项目、跨项目及未归属需求单夹具 | 显式关联、空白清空，混合合法行与跨项目行提交 | 同项目合法；空白归一为空；跨项目返回稳定 code，所有事实、版本、审计和事件零新增 |
| 错误和锁；默认协议、旧入口与历史 P3 事件夹具 | 覆盖六个原语错误码及 HTTP 映射，记录执行 SQL | 返回 code/message；失败零写；常规原语路径无 FOR UPDATE，历史下游锁按原协议保留 |
| V2 原回执；真实 validate/apply 与 HTTP 夹具 | 首次包含单头／多行／接管或作废提示，再原文件重上传；撤销权限或注入快照保存失败 | 成功重放除 replayed 外完整相等且业务审计不增；当前无权拒绝；快照失败业务整体回滚；历史缺快照保留原兼容分支 |

上述同时列出原语契约与后续用户点按场景；quantity/remark/单头编辑属于原语已有能力，不是 ITSP-2 的 HTTP/UI 范围，本轮网页只开放 no_return。`R/logs/S-B-379fu.report.md` 仅记录旧 #379 原语候选的测试，不能代替 ITSP-2 当前验证；本轮证据见 [候选验收与回退](../releases/2026-10-20-itsp2-batch-no-return.md)。自动化验证不代表用户点按、main 审批或部署已完成。
