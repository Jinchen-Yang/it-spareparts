# 需求单返还汇总契约 v2.0-draft

- 版本：`v2.0-draft`。
- 日期：2026-10-16。
- 状态：**草稿，与 PR #370 候选实现对齐，尚未正式集成**。实现基准为 `wt-itsp-00004` 的 `7175e1c06ecfb50ba327b03c13c01be5145337bf`，核对 `maintenance_return_request_summary.py` 与 `maintenance_boss_board.py`；本文同步不代表测试、用户验收或发布已完成。
- 决策：D-31（含第 8 款 2026-10-15 修订）；免返提醒沿用 D-32。
- 提供方：P1／S-A；消费方：P1 需求单列表、详情及录入候选的数量展示；P4／S-C 保留只读数量快照适配边界，本轮不发布待返结论。

## 依据与命名

来源为本批《01-开工计划》§二、§五、《P1 返还台账边界》§4.1、《P4 分析页边界》§4.2、需求00004调研§3.2及金臣答复§17；业务口径以 [D-31 第 8 款方案 A](../decisions/0009-返还数量上限与需求单核对.md)为准。统一落点为 `backend/app/services/maintenance_return_request_summary.py`；P1 边界中的旧拟名 `maintenance_demand_return_summary.py` 不再使用。

本契约把 P1 的按单映射接口和规范化数量快照合为同一 Module 的两个入口：每单计算只存在一份；快照入口仅负责编排批次与独立未关联桶。快照函数沿用 `outstanding` 名称，不表示本轮已发布待返业务结论。这是工程统一方案，不将技术名称、批次大小或事务选型称为金臣原话。

## Interface 与权限

```python
# PR #370 候选入口；内部数量用 Decimal，JSON 示例为离线序列化形状。
def summarize_demand_returns(
    db: Session,
    *,
    source_order_ids: Collection[str],
    allowed_project_ids: Collection[str] | None,
    as_of: date | None = None,
) -> dict[str, DemandReturnSummary]: ...

def get_return_requests_outstanding_summary(
    db: Session,
    *,
    source_order_ids: Collection[str],
    allowed_project_ids: Collection[str] | None,
    project_ids: Collection[str] | None,
) -> ReturnRequestSummarySnapshot: ...
```

`source_order_ids` 是稳定 `raw_order_id` 的去重集合，由调用方在服务端完成授权与筛选后构造，展示单号不能替代它。`allowed_project_ids` 必须来自服务端身份范围；`None` 仅供已确认全域权限的内部调用者，空集合表示零数据。快照的 `project_ids` 是用户项目筛选，必须再与授权集合取交集；`None` 表示不额外缩小授权范围，空集合仍是零数据。不能把浏览器传来的项目集合当作授权集合。

按单入口每批最多 500 个去重 ID；超过明确拒绝，不静默截断。空 ID 集合或空授权集合直接返回 `{}`。无权或不存在的 ID 均不返回，不暴露存在性。有效范围内但不可核对的单据可返回原因，用于管理历史列表的只读富化，不能被当成新增候选。

快照入口允许完整筛选集，由提供方分块调用唯一按单入口；每个 ID 仅计算一次。即使需求单集合为空，仍可返回显式授权项目范围内的未关联桶，不能因空单集合扩大项目范围。它不自行发现其他需求单，不复制 N/R/P 算法，不把全集直接下发浏览器。

## 每单字段与公式

| 字段 | 约束 |
|---|---|
| `source_order_id`、`order_no`、`project_id` | 稳定原始身份、展示号和当前有效归属；展示号及项目可为 `null`，权限先于返回 |
| `effective` | 有效单头、未作废、状态显式为“已生效”，且有当前有效归属、项目有效；使用 P1 的 `active_beta_maintenance_orders` 口径，不改旧分析的 `active_orders` 或开启 cutover |
| `demand_qty`（N） | 该单全部有效明细 `qty` 合计（`qty` 为 NULL 的明细按 0 计）；不扣 `return_qty`，不取头自报量、净需求量或实际领用量 |
| `received_qty`（R） | 精确关联 `source_order_id` 的全部 active 收货数量，涵盖 manual／RKD、全部件况与全部 PN；作废退出，未关联收货不摊入 |
| `pending_qty`（P） | 纯数量兼容别名；仅 `basis_state=complete` 时为差额 `max(N-R,0)`，否则 `null`；不是已发布的待返结论或写授权 |
| `remaining_qty` | 与 P 完全相同的纯数量兼容别名；不表示当前可操作，不是录入授权或并发容量令牌 |
| `over_received_qty` | 仅 `basis_state=complete`（有效且基数完整、N>0、无归属冲突）时为 `max(R-N,0)`，否则 `null`；不得截断 R |
| `basis_state` | `complete / incomplete / inactive / ownership_conflict`，是读取结果的有效性与完整性，不是返还状态或持久业务状态 |
| `reason_codes` | 去重原因数组：`no_active_lines`、`negative_demand_qty`、`invalid_demand_qty`、`zero_demand_qty`、`inactive_order`、`tombstoned_order`、`unassigned_order`、`inactive_project`、`ownership_conflict`、`undated_receipts` |
| `undated_receipt_qty` | 当前累计中业务日期缺失的收货数量；计入 R 并提示，缺日期本身不使当前累计失真 |

不存在归属冲突且没有收货时 R=`0.000`。**需求数量 NULL 的明细按 0 计入 N**，不再因此置 N=`null`；任何有效明细有负量、合计为非有限数或无有效明细时 N=`null`，分别给出 `negative_demand_qty`、`invalid_demand_qty` 或 `no_active_lines`，不以头自报值或已知部分补齐。有有效明细且全部 NULL 时 N=`0.000`，原因是 `zero_demand_qty`。N=0 仍为不完整基数。数量为 `Decimal | None`，JSON 一律十进制三位字符串或 `null`，不输出浮点数；该接口不提供金额。

`basis_state` 判定顺序为：存在关联实收但需求单无当前有效项目归属，或实收项目与当前归属不一致时为 `ownership_conflict`；否则 `effective=false` 为 `inactive`；否则 N 未知或 N≤0 为 `incomplete`；其余为 `complete`。失效单仍可保留可读的 N/R，但不计算差额或超收；单头未生效、无当前有效归属、项目失效分别用 `inactive_order`、`unassigned_order`、`inactive_project` 提示。

**本轮方案 A：** 需求单只交付品类筛选、原始下单量 N、实收 R 与差额 `max(N-R,0)`；**免返件不从 N/R 剔除**（预购未用件必须返还并可追溯，按金臣原话），**本轮不发布任何返还状态**（不显示已返／未返、收齐／未收齐、无需核对或按免返调整的状态），也不发布依赖状态的 P4 待返结论。免返的完整优化（区分已用旧件与未用新件等）留待后续需求另定。免返文本和免返要求仅独立提醒，不抵扣 N/R/P。P 是“按下单数量核对的差额”，不称为免返后的催收义务。卡墙与 PN 分析返还率仍为同期实际返还÷同期实际领用，与本契约的 N/P 分开，不能用 N 替代返还率分母。

存在关联实收但需求单无当前有效项目归属，或跨项目历史收货与当前需求单归属不一致时，返回 `ownership_conflict`，`received_qty/pending_qty/remaining_qty/over_received_qty/undated_receipt_qty` 全部置 `null`，不得由衍生值泄露旧项目事实或形成确定结论。即使调用者可见当前项目，也不能把旧项目收货当成零量；无权旧项目的数量、名称不返回。容量写守卫仍须在完整稳定身份上检查异常，不能重新发满额度；本接口不自动迁移收货。

历史 R>N 保留原数并标超额。原件调小 N 不截断已收，不允许借只读结果继续增加；P1 的写守卫负责同归属不增占用编辑、减量、作废及新增容量校验。

## 筛选、时间、聚合与分页

需求日期只选择需求单集合；R 为截至本次读取的当前全部有效累计，包含需求日期起始日之前的收货。页面必须说明“所选需求单截至当前累计核对”。当前版本不开放历史截点：`as_of` 参数为未来兼容保留，v2.0-draft 仅接受 `None`，非空返回 `historical_as_of_not_supported`。快照的 `as_of` 是观察时间戳，不能与输入的历史日期混同，也不表示恢复当日审计版本。

本轮只交付品类筛选。品类等行条件只能由调用方用 EXISTS 等方式选出“包含匹配行的整张需求单”，随后 N/R/P 按整单全部行核对；页面说明“含所选备件的需求单，按整单核对”。不能先裁掉不匹配明细再算 N，也不能筛收货 PN 生成伪 PN 待返量。未来若适配 PN、描述、成本来源等条件，也须遵守整单选择与数据权限；这些不构成本轮新增筛选承诺。不支持的筛选参数应由适配层明确拒绝，不能静默忽略；两个汇总入口只接收已选中的稳定 ID，不自行解析这些筛选。

数量快照对完整筛选集先取得所有每单结果，再提供聚合。`summary.known_difference_qty` 为所有非 `null` 的逐单差额之和，不能用 `max(ΣN-ΣR,0)`；项目合计、全集合计和明细应来自同一快照。未知单不当零；合计只能称“已知数量差额”，不能称待返总量或催收义务。

| 快照 `summary` 字段 | 约束 |
|---|---|
| `known_difference_qty` | 已知逐单差额之和，三位十进制字符串；没有已知差额时为 `0.000`，须同时读取以下计数 |
| `unknown_order_count` | 仅统计 `basis_state` 为 `incomplete` 或 `ownership_conflict` 的单据，不包含 `inactive` |
| `inactive_order_count` | 单独统计 `basis_state=inactive` 的单据，其数量不参与差额合计 |
| `incomplete` | 仅为 `unknown_order_count>0`，表达未知基数／归属；不是返还进度，也不因仅有失效单而变为 `true` |

P4 本轮不接待返结论，不能据 `pending_qty/remaining_qty`、零差额或上述计数补推已返／未返。后续只读数量适配仍须先取完整筛选集再归组和分页：明细页大小上限 100，项目汇总也有界分页；不因翻页改变合计，不把未知或失效单悄悄过滤掉。这些是适配约束，不表示 #370 已交付 P4 分页 API。

`unassigned_by_project` 独立统计授权且选中项目范围内 `source_order_id=null` 的 active 收货。需求单属性筛选不能赋予这些收货关联，必须注明“当前项目范围全部未关联收货”，与需求单合计分区展示；不得按 PN、比例或项目总量分摊抵扣。不存在项目 scope 时也不返回越权桶。

## 查询、事务与职责

按单入口每批使用一条聚合 SELECT：有效明细、active 收货分别按单聚合，随后结合归属和授权，避免明细×收货 JOIN 放大或多语句读取两个版本。不得逐单查号或循环旧项目 `receipt_summary`。

快照入口在调用方建立的只读 `REPEATABLE READ` 事务内完成完整筛选集、全部 500 ID 批次和独立未关联桶读取；单批仍用单条聚合 SELECT。它先校验事务条件，不能在已有查询后悄悄切换隔离级别；不满足则拒绝。`as_of` 在该事务内取得一次并复用于整个响应。这样明确采用 P1 的分块方式满足 P4 的同一快照要求，不声称跨批只有一条 SQL。权限及详情能力查询可另行发生，不能据此宣称整个 HTTP 只有一条 SQL。

两个入口只读业务事实：无 FOR UPDATE、无容量锁、不写审计、不 commit/rollback、不推进 version/revision，并避免隐式 autoflush。调用方事务若有待写事实，应显式 flush 后再选择合适读取方式，不能靠汇总暗中写库。写侧必须先按 P1 容量协议加锁并复验，snapshot 不是并发容量保证。

可降级的页面富化由调用方在 Connection savepoint 中执行（不使用会无条件 flush 的
`Session.begin_nested()`）。成功释放 savepoint；查询失败只回滚该 savepoint，使主体后续查询
仍可执行，不回滚整个会话事务。此处仅多出事务控制语句，按单入口本身仍只有一条聚合 SELECT。
快照入口保持失败即拒绝，不回退拼接数量。

快照仅供内部数量消费。后续 P4 适配层须负责登录鉴权、客户/成本参数门禁、有界序列化、详情跳转能力与访问日志，本轮不据此发布待返结论。统计可见不授予项目详情访问权；台账跳转还需现行发布 flag 和详情权限。访问日志可沿用独立 best-effort 短事务，不属于本函数的业务写入或事实更正审计。

## 离线 JSON 示例

以下均为合成夹具，无需联网。A 超收不能抵 B 欠返，未关联 3 件也不能抵扣。

```json
{
  "contract_version": "v2.0-draft",
  "as_of": "2026-10-15T09:00:00Z",
  "scope": "current_cumulative_whole_orders",
  "orders": [
    {
      "source_order_id": "example-order-a",
      "order_no": "WBDD-20261001-0001",
      "project_id": "example-project",
      "effective": true,
      "demand_qty": "5.000",
      "received_qty": "7.000",
      "pending_qty": "0.000",
      "remaining_qty": "0.000",
      "over_received_qty": "2.000",
      "basis_state": "complete",
      "reason_codes": [],
      "undated_receipt_qty": "0.000"
    },
    {
      "source_order_id": "example-order-b",
      "order_no": "WBDD-20261001-0002",
      "project_id": "example-project",
      "effective": true,
      "demand_qty": "5.000",
      "received_qty": "1.000",
      "pending_qty": "4.000",
      "remaining_qty": "4.000",
      "over_received_qty": "0.000",
      "basis_state": "complete",
      "reason_codes": ["undated_receipts"],
      "undated_receipt_qty": "1.000"
    }
  ],
  "unassigned_by_project": [
    {"project_id": "example-project", "received_qty": "3.000"}
  ],
  "summary": {
    "known_difference_qty": "4.000",
    "unknown_order_count": 0,
    "inactive_order_count": 0,
    "incomplete": false
  }
}
```

`orders` 为内部数量快照形状，不是本轮已发布的 P4 HTTP 响应；后续对外适配须有界分页且不能补造返还状态。负量单的字段片段为 `{"demand_qty":null,"received_qty":"1.000","pending_qty":null,"remaining_qty":null,"over_received_qty":null,"basis_state":"incomplete","reason_codes":["negative_demand_qty"]}`，不能在客户端把 `null` 转成零。若快照另有两张 `basis_state=inactive` 的单据，上述已知差额仍为 `4.000`，`unknown_order_count=0`、`inactive_order_count=2`、`incomplete=false`；仅有失效单不等于存在未知基数。

## 错误与兼容

候选汇总服务实际抛出以下五个 `ReturnSummaryError.code`；其 `status_code` 为下表值，HTTP 由各 Adapter 映射。业务读取异常不得返回伪零。

| code | 建议 HTTP | 处理 |
|---|---|---|
| `invalid_summary_request` | 422 | 非法 ID、参数或类型；修正请求 |
| `summary_batch_too_large` | 422 | 去重后超过 500；调用方分块 |
| `historical_as_of_not_supported` | 422 | v2.0-draft 不接历史截点 |
| `summary_snapshot_required` | 503 | 完整快照未建立只读一致事务；服务端修正，不回退拼接结果 |
| `summary_unavailable` | 503 | 查询失败；用户可重试，不显示假空 |

单据不可核对属于成功结果的 `basis_state/reason_codes`，不以一个异常单阻断其他已授权单；无权 ID 的省略与空 scope 则不报存在性错误。

`unsupported_summary_filter`（建议 422）与 `summary_permission_denied`（建议 403）仅保留为后续 P4 适配层的筛选／权限错误约束，不是上述候选 Module 已实现的领域错误码。

**页面富化降级（`maintenance_boss_board.project_orders`、需求单搜索与详情）：** 在鉴权并查询当前页/单据后，按稳定 ID 批量富化 `return_summary`。捕获 `ReturnSummaryError` 时用 `logger.exception` 记录异常，各行返回 `return_summary=null`、`return_summary_error="summary_unavailable"`；详情在响应顶层返回同样字段。保留 HTTP 200、原列表、排序、分页、`total`、其他列与删除/明细 digest，不因摘要不可用丢行或返回假零。成功时不附 `return_summary_error`；单个 ID 未返回摘要时仅为 `return_summary=null`，不能等同于整批查询异常。消费者显示“摘要暂不可用”并隐藏数量，不能从自报值或旧待返算法补出结论；此处降级不表示快照入口可回退到多版本拼接，也不扩大为捕获全部异常。

有效墓碑使 `header_effective=false`，保留兼容原因 `inactive_order` 并增加
`tombstoned_order`（需求单已逻辑作废）。源单头单独未生效只有 `inactive_order`；
恢复墓碑后不再附墓碑原因，是否有效仍须检查源状态、当前归属及项目。

| 提供方／消费者 | 兼容行为 |
|---|---|
| 尚无 v2.0-draft 的主线／P4 | 数量契约不可用时明确提示；本轮待返结论保持不发布，禁止回退自报 pending、旧坏件或逐项目算法 |
| v2.0-draft 候选／旧列表 | 新增可选 `return_summary`，不改变原列表集合、排序、分页、删除 snapshot/version_digest 或明细 OCC digest；摘要异常按上述局部降级 |
| v2.0-draft 候选／P1 及后续 P4 数量适配 | 共用每单 DTO 与 fixture；不分别推导差额，不产生返还状态或 P4 待返结论 |
| v2.0-draft 候选／历史 as_of 调用 | 明确 422，后续另立版本定义历史日期与无日期事实解释 |
| 后续增加可选字段 | 消费者忽略未知字段；变更公式、空值、有效范围或删除字段必须升主版本并先联测 |

本契约不新增表、列、迁移或回填。撤下读适配可回退，保留真实收货、人工更正与审计；不能降级后仍声称具有新容量保护。

## 实现时必须验证的场景

| 目标／角色与起点 | 操作 | 可观察预期 |
|---|---|---|
| P1 与内部数量快照同口径；有权核对人；上述 A/B 及未关联夹具 | 查列表、详情与内部快照；后续数量适配再核对项目和全集合计及翻页 | A：N/R 齐全、超收 2、差额 0；B：差额 4；`known_difference_qty=4.000`；JSON 不含返还状态键、界面无已返／未返文案及 P4 待返结论；未关联独立 3；分页不改变合计 |
| 整单筛选；有权分析人员；同单有多个 PN、品类、退货与不同件况 | 只筛其中品类，再按需求日期窗口读取 | N 仍为整单 qty，R 含全部 PN/件况与窗口前收货，不扣退货或免返；后续 PN 等适配也须保持整单核对 |
| 基数和范围；仅新项目权限账号；NULL、负量、非有限数、零量、跨项目关联 | 读取完整筛选集及空 scope | NULL 明细按 0，负量／非法基数不补零；归属冲突屏蔽 R 及衍生量，旧项目数量/名字不泄露；空 scope 零数据 |
| 未知与失效分开；有效未知单与失效单并存 | 读取快照，再只保留失效单 | 未知仅计 `incomplete/ownership_conflict`；失效单独计数，差额和超收为 `null`；仅失效时 `incomplete=false` |
| 项目内列表降级；有权账号；摘要服务抛 `ReturnSummaryError` | 读取当前页 | 每行摘要 `null` 且错误为 `summary_unavailable`，有异常日志；列表、分页、`total` 与其他列保留，不回退旧算法 |
| 查询一致性；隔离真实 PostgreSQL 双连接；超过 500 单 | 一个连接提交完整改量/作废/补关联，另一个生成快照 | 总计、明细、未关联桶和各批来自同一版本；无 JOIN 放大和逐单查询 |
| 既有指标保护；有权账号 | 原件减量形成超收后查旧卡墙/PN率与新摘要 | 收货不截断，新摘要超额；旧率仍实际返还÷实际领用，digest 不含富化字段 |

这些是候选契约核对和用户点按验收输入；本次只交付文档，不代表已执行后端测试或业务验收。

## v1.x → v2.0 变更记录

| 历史版本 | 变化与兼容影响 |
|---|---|
| v1.0-draft | 最初拟定按整张需求单核对 N/R、逐单差额及独立未关联桶，原草稿包含 `return_status` 和 `summary.known_pending_qty` |
| v1.1-draft（2026-10-15） | NULL 需求数量按 0；方案 A 最终修订为免返不从 N/R 剔除、本轮不发布状态及 P4 待返结论，取代同日免返剔除／`not_required` 草案；当时头部与 JSON 仍残留旧版本及状态键，不再作为消费依据 |
| v2.0-draft（2026-10-16） | 对齐 #370 `7175e1c`：彻底删除 `return_status` 字段（不以 `null` 占位），`summary.known_pending_qty` 改为 `known_difference_qty`；新增 `inactive_order_count`，未知计数排除 `inactive`；仅完整有效单输出超收，补齐原因码与项目内列表降级约定 |

删除字段、快照字段改名及计数语义收窄属于不兼容变更，按本契约兼容条款提升主版本；旧消费者须联测后适配，不提供旧状态字段或旧汇总键作为兼容别名。`pending_qty/remaining_qty` 继续保留为纯数量差额别名。文件名保留 `-v1.md` 以维持已有决策与文档链接，当前唯一契约版本为 `v2.0-draft`。
