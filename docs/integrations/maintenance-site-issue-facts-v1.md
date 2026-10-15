# 领用事实编辑原语契约 v1

- 版本：`v1.0-draft`。
- 日期：2026-10-15。
- 状态：**拟定，尚未实现**。本文定义后续公共原语及消费者职责，不代表当前函数已经存在。
- 决策：D-31、D-32；提取工作簿共用写链遵循既有 D-01、D-02、D-26、D-28。
- 唯一提供方：P2／S-B，`backend/app/services/maintenance_site_issue_facts.py`（拟新增）。消费方：P2 的 06 工作簿 Adapter 与 P1／S-A 的网页更正命令。

## 来源与范围

依据《01-开工计划》§二、§五、《P1 返还台账边界》§2.2、§2.3、§3.6、§4.2及《P2 行级表工作台边界》§2、§4。P2 从现有 06 事实编辑路径提取，并在同一候选把 06 接到本原语；P1 的页内领用编辑必须等该提取先落地，只编排命令，不另写 no_return、成本、版本或事件算法。

本原语只服务已经授权的人的编辑。运行时 AI 不得据此写采购、销售、库存、项目、领用或申请/审批业务状态。实际返还收货是另一独立事实链，不因领用修改而增减，不按领用行宣称已返；领用登记及更正本身无库存效应。

## Interface

```python
# 拟新增；输入实体已按调用入口协议锁定，字段意图已完成鉴权/CAS验证。
def apply_locked_site_issue_edits(
    db: Session,
    *,
    project: MaintenanceProject,
    issue: MaintenanceSiteIssue,
    line_changes: Mapping[str, Mapping[str, object]],
    header_changes: Mapping[str, object] | None,
    operated_by: str,
    reason: str,
) -> SiteIssueEditResult: ...
```

| 输入／输出 | 约束 |
|---|---|
| `project`、`issue` | 同一 Session 中已锁定、锁后新鲜重读的实体，issue 属于 project；不得只凭展示号或 PN 定位 |
| `line_changes` | key 为稳定 `issue_line_id`；值为局部字段补丁，未出现行不动，未出现字段不动；空映射合法无操作 |
| `header_changes` | `None` 或空映射表示不改单头；只接受既有可编辑字段和归一化字段意图 |
| `operated_by`、`reason` | 真实实名操作者及非空原因；不接受前端冒认身份；同事务审计可追溯 |
| `SiteIssueEditResult.changed_line_ids` | 实际发生事实、成本证据或版本变化的行 ID，按稳定顺序，不包含原样回传行 |
| `issue_version`、`line_versions` | 本次完成后权威版本；line_versions 覆盖请求涉及及因单头日期重算实际变化的行；无变化维持原版本 |
| `before`、`after` | 本次实际更正的单头/明细及必要成本证据快照，供审计与结果核对；不是可直接下发的脱敏 DTO |

局部补丁不表达新建、删除、复活或整单替换，不接收“本次未出现的行即作废”。06 的 CREATE／缺行／VOID、签名基线和三路合并仍由工作簿 Adapter 在原整本事务中处理；不能把网页选中数组送到完整 lines 替换接口。

## 字段意图与来源

原语复验行属于本单、来源合法、当前有效、字段白名单及字段值；调用者已校验也不能省略归属和白名单防线。事实字段沿用现有 06 可编辑集合，典型包括 PN/part_id、quantity、SN、no_return、remark；不是开放任意 ORM 列。PN 文本与 part_id 必须按既有身份解析成一致的字段意图，不能直接绑定停用或歧义身份。主数据库 PN 描述、品牌、品类不由领用原语修改。

`no_return=true/false/null` 分别为免返／明确应返／恢复按规则；省略表示不修改，null 不能等同省略。继续使用现行标准硬盘默认免返、逐行明确设置优先及已有冻结义务规则。项目免返文本只作提醒，不由本原语解析成事实或开启项目默认开关。

单头可编辑字段限现有 `issue_no/issue_date/site_location` 等已获入口批准字段。行 PATCH 必须令 `header_changes=None`，改单头走独立 issue CAS 命令；地址“未提供／设置／显式清空”意图沿用现有 D-26，包括其 2026-10-13 修订，不能把缺字段解释为清空。人工改 issue_no 仍走既有改号唯一性校验；`issue_id/issue_line_id/project_id/source/import_batch_id/display_no` 不可由此修改，展示号分配后不变。

page_manual 保持既有完整预览冻结协议；workbook／legacy 由 P1 的稳定受控 Adapter 接入，不能假设旧 page_manual 专属 PATCH 已覆盖它们。`site_issue_v2` 仍保留发货来源、冻结义务及生产失败关闭守卫；抽取共用原语不授权打开 Beta、重启原生确认或伪造 delivery_line_id。

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

P3 `snapshot` 覆盖 `issue_line_id/no_return/quantity/part_id/is_active/version`；`record_corrections` 只对这些返还要求事实变化推进 line/issue 版本并写 correction event。它不替调用者加锁、校 CAS、赋 no_return、提交或推进工作簿 revision。

SN-only、remark-only、仅成本证据和单头字段变化不在该 snapshot 中。原语必须补相应事实审计和 line/issue 版本，使旧编辑/补价 CAS 失效；P3 已经推进的版本不能由新命令再推进一次，无变化不制造版本。旧完整人工接口在 corrections 后可能还有既有 issue.version 推进，提取必须分别冻结旧接口兼容返回与新接口返回，不能为统一次数顺手破坏旧 CAS。

PN 变更使旧成本证据失效，quantity 变化按现有规则重新计算成本；仅改备注不强制重写未触碰的历史数量。单头 `issue_date` 更正时，调用者须先锁本单全部有效行，原语逐行复核日期影响的取价/证据，推进真正变化行的版本，并把这些行纳入结果。成本瀑布、税率与未知不等于零均沿现状，不发明另一套取价；地址变更不成为重新取价依据。

## 锁序与事务

新网页命令遵循已有领用链：命令幂等事务锁 → workbook state → project → issue → 按稳定顺序指定行 → 锁后新鲜重读与 CAS → 原语 → 外层 revision/回执 → commit。改单头日期时，行锁覆盖本单全部有效领用行，不能只锁用户请求行。批量仍是逐行事务；同单上一行成功后，下一行使用回执里的新 issue version，不能无条件复用旧版本。

06 原有全局数据锁、身份锁、state/project、需求及领用锁前缀和整本原子事务继续由 P2 Adapter 负责。原语不在拿到行锁后再补申请 state/project 等上层锁，不能自行开启另一个事务；多项目锁保持既有确定顺序。P2 接 06 时移除已由原语承担的重复 corrections 调用，保留签名、七字段身份、三路 merge、作废优先及根事务 revision 去重。

任一字段、成本、版本或审计失败，调用者回滚同一事务，事实、版本、审计、outbox、命令回执和 revision 不得半提交。先验证全部最终意图再赋 ORM；失败不能留下稍后被无关 commit 偷写的脏实体。不以 SQLite／PGLite 或顺序请求证明此锁序并发正确。

## 离线 JSON 示例

这些是内部原语输入／结果的合成序列化夹具，不是新增 HTTP 路由或真实业务数据；权限、expected versions 与幂等键由外层命令处理，不冒充原语参数。

```json
{
  "contract_version": "v1.0-draft",
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

下表为拟定领域错误，HTTP 映射由调用 Adapter 承担，不改变旧接口已冻结的错误形状。

| code | 建议 HTTP | 语义／恢复 |
|---|---|---|
| `site_issue_edit_invalid_fields` | 422 | 未知/只读字段、非法值或不合法字段意图；修正输入 |
| `site_issue_edit_identity_conflict` | 409 | 行不属于锁定本单、身份/来源漂移；重新读取，不猜匹配 |
| `site_issue_edit_inactive` | 409 | 单头或行已作废；拒绝复活 |
| `site_issue_edit_source_blocked` | 409 | 来源/冻结义务/生产守卫不允许本命令；走批准的原入口 |
| `site_issue_edit_lock_context_required` | 503 | 调用者未建立必要事务/锁上下文；服务端修正，不能降级无锁写 |
| `site_issue_edit_audit_unavailable` | 503 | 同事务审计无法保存；回滚，不先写事实再补审计 |
| `site_issue_edit_permission_denied` | 403 | 外层权限或实名校验不满足；不写事实 |
| `site_issue_edit_version_conflict` | 409 | 外层 CAS 失败；保留草稿，显式重读新基线后再确认 |
| `site_issue_edit_idempotency_conflict` | 409 | 外层同键不同载荷/基线；不能换键盲重试 |

原语本身不承诺幂等重放；外层成功结果按既有回执返回，不重复调用原语。网络结果未知时沿同一请求/键核对或回读，不能擅自判定首次未写；成功但刷新失败应保留成功结果，不再次更正。

## 兼容、交付与验收

| 提供方／消费者 | 兼容要求 |
|---|---|
| 当前 main／新 P1 行编辑 | 原语尚未实现；P1-3 不启用，禁止复制另一套事实算法 |
| P2 提取／既有 06 | 同一 PR 接入并携带往返/冲突/作废/审计/版本/revision 契约测试；未选行、七字段基线和旧文件身份保持 |
| P2 v1／P1 新网页命令 | 同一事实原语，局部补丁；P1 负责权限、CAS、幂等、锁和提交 |
| P2 v1／旧 page_manual 完整命令 | 保留预览冻结、完整单语义和旧版本兼容，不以局部补丁替代整单协议 |
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

以上均为未来实现验证和用户点按场景。本批只写文档，不宣称原语、测试或业务验收已经完成。
