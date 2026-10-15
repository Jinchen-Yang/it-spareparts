# 需求单返还汇总契约 v1

- 版本：`v1.0-draft`。
- 日期：2026-10-15。
- 状态：**拟定，尚未实现**。Python 名称、DTO 和错误码是后续实现约束，不代表当前已有接口或已通过测试。
- 决策：D-31（含第 8 款 2026-10-15 修订）；免返提醒沿用 D-32。
- 版本：v1.1-draft（2026-10-15：NULL 需求数量按 0；全部为免返硬盘的单为 not_required 无需核对；免返硬盘从 N/R 剔除；同日补充行级显式免返 `line_no_return` 亦剔除）。
- 提供方：P1／S-A；消费方：P1 需求单列表、详情、录入候选和 P4／S-C 待返分析适配层。

## 依据与命名

来源为本批《01-开工计划》§二、§五、《P1 返还台账边界》§4.1、《P4 分析页边界》§4.2、需求00004调研§3.2及金臣答复§17。计划统一落点为 `backend/app/services/maintenance_return_request_summary.py`；P1 边界中的旧拟名 `maintenance_demand_return_summary.py` 不再使用。

本契约把 P1 的按单映射接口和 P4 的规范化快照建议合为同一 Module 的两个入口：每单计算只存在一份；快照入口仅负责编排批次与独立未关联桶。这是工程统一方案，不将技术名称、批次大小或事务选型称为金臣原话。

## Interface 与权限

```python
# 均拟新增；内部数量用 Decimal，JSON 示例为离线序列化形状。
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
| `source_order_id`、`order_no`、`project_id` | 稳定原始身份、展示号和当前有效归属；权限先于返回 |
| `effective` | 有效单头、未作废、有效归属且状态显式为“已生效”；使用 P1 的 `active_beta_maintenance_orders` 口径，不改旧分析的 `active_orders` 或开启 cutover |
| `demand_qty`（N） | 该单全部有效明细 `qty` 合计（`qty` 为 NULL 的明细按 0 计，v1.1）；不扣 `return_qty`，不取头自报量、净需求量或实际领用量 |
| `received_qty`（R） | 精确关联 `source_order_id` 的全部 active 收货数量，涵盖 manual／RKD、全部件况与全部 PN；作废退出，未关联收货不摊入 |
| `pending_qty`（P） | 仅 `effective=true`、基数完整且 N>0 时为 `max(N-R,0)`，否则 `null` |
| `remaining_qty` | 当前可操作且基数完整时与 P 相同；仅为容量展示，不能作为写授权令牌 |
| `over_received_qty` | N/R 完整时为 `max(R-N,0)`，否则 `null`；不得截断 R |
| `return_status` | 满足可核对条件时，R≥N 为 `returned`（已返），R<N 为 `not_returned`（未返）；部分仍未返；其余 `null` |
| `basis_state` | `complete / not_required / incomplete / inactive / ownership_conflict`（`not_required`=无需核对，v1.1），是读取结果的完整性，不是持久业务状态 |
| `reason_codes` | 原因数组，如 `all_exempt_disk`、`negative_demand_qty`、`zero_demand_qty`、`no_active_lines`、`inactive_order`、`ownership_conflict`、`undated_receipts` |
| `undated_receipt_qty` | 当前累计中业务日期缺失的收货数量；计入 R 并提示，缺日期本身不使当前累计失真 |

没有收货且可完整核对时 R=`0.000`。**v1.1（金臣 2026-10-15）：需求数量 NULL 的明细按 0 计入 N**（与 D-35 PN 分析、旧 pn_ranking 及库存口径一致），不再因此置 N=`null`；非法负量或无有效明细时 N=`null`，不以头自报值或已知部分补齐。**无需核对（`not_required`，v1.1）**：有效单在剔除免返硬盘前 N>0、剔除后 N=0（即全部需求为免返硬盘）时，`basis_state=not_required`、`reason_codes` 含 `all_exempt_disk`，P/`return_status`/`over_received_qty` 为 `null`；不计入 `unknown_order_count`，不使 P4 快照标为不完整，界面显示“无需核对”。其他 N=0（非免返剔除所致）仍标不完整，不能自动已返。数量为 `Decimal | None`，JSON 一律十进制三位字符串或 `null`，不输出浮点数；该接口不提供金额。

**v1.1 修订（2026-10-15）：**免返硬盘不计入返还核对（金臣 2026-10-15 裁定）：领用单、返还单和手工录入备件中，按现行免返判定（与 `maintenance_bad_returns` 的 `exemption_source` 一致：行级显式免返 `line_no_return`、项目默认免返 `project_default_no_return` 或硬盘品类默认免返 `category_disk`，三种来源均剔除；行级显式“必须返”优先，仍计入）判为免返的硬盘，从需求单核对的下单量 N 与已收量 R 中同时剔除，不参与已返／未返判断与差额。项目免返提醒文本仍只作提醒，不驱动剔除。实现须在汇总中分别给出剔除的免返硬盘需求数量与收货数量，便于核对。免返**文本**仍仅独立提醒。P 是“按下单数量核对的差额”，不称为免返后的催收义务。卡墙与 PN 分析返还率仍为同期实际返还÷同期实际领用，与本契约的 N/P 分开，不能用 N 替代返还率分母。

跨项目历史收货与当前需求单归属不一致时，返回 `ownership_conflict`，`received_qty/pending_qty/remaining_qty/over_received_qty/undated_receipt_qty/return_status` 全部置 `null`，不得由衍生值泄露旧项目事实或形成确定结论。即使调用者可见当前项目，也不能把旧项目收货当成零量；无权旧项目的数量、名称不返回。容量写守卫仍须在完整稳定身份上检查异常，不能重新发满额度；本接口不自动迁移收货。

历史 R>N 保留原数并标超额。原件调小 N 不截断已收，不允许借只读结果继续增加；P1 的写守卫负责同归属不增占用编辑、减量、作废及新增容量校验。

## 筛选、时间、聚合与分页

需求日期只选择需求单集合；R 为截至本次读取的当前全部有效累计，包含需求日期起始日之前的收货。页面必须说明“所选需求单截至当前累计核对”。首版不开放历史截点：`as_of` 参数为未来兼容保留，v1 仅接受 `None`，非空返回 `historical_as_of_not_supported`。快照的 `as_of` 是观察时间戳，不能与输入的历史日期混同，也不表示恢复当日审计版本。

PN、描述、品类、成本来源等行条件只能用 EXISTS 等方式选出“包含匹配行的整张需求单”，随后 N/R/P 按整单全部行核对；页面说明“含所选备件的需求单，按整单核对”。不能先裁掉不匹配明细再算 N，也不能筛收货 PN 生成伪 PN 待返量。不支持的筛选参数明确拒绝，不能静默忽略；成本／客户筛选继续沿用对应数据权限。

P4 对完整筛选集先取得所有每单结果，之后归组和分页。总计为逐单 `pending_qty` 之和，不能用 `max(ΣN-ΣR,0)`；项目总计、全集总计和明细来自同一快照。未知单不当零；单独报告 `unknown_order_count`、`incomplete=true`，已知 P 合计须称“已知待返差额”，不能称全部待返总量。明细页大小上限 100，项目汇总也必须有界分页；不因明细翻页改变总计，不把未知单悄悄过滤掉。

`unassigned_by_project` 独立统计授权且选中项目范围内 `source_order_id=null` 的 active 收货。需求单属性筛选不能赋予这些收货关联，必须注明“当前项目范围全部未关联收货”，与需求单合计分区展示；不得按 PN、比例或项目总量分摊抵扣。不存在项目 scope 时也不返回越权桶。

## 查询、事务与职责

按单入口每批使用一条聚合 SELECT：有效明细、active 收货分别按单聚合，随后结合归属和授权，避免明细×收货 JOIN 放大或多语句读取两个版本。不得逐单查号或循环旧项目 `receipt_summary`。

快照入口在调用方建立的只读 `REPEATABLE READ` 事务内完成完整筛选集、全部 500 ID 批次和独立未关联桶读取；单批仍用单条聚合 SELECT。它先校验事务条件，不能在已有查询后悄悄切换隔离级别；不满足则拒绝。`as_of` 在该事务内取得一次并复用于整个响应。这样明确采用 P1 的分块方式满足 P4 的同一快照要求，不声称跨批只有一条 SQL。权限及详情能力查询可另行发生，不能据此宣称整个 HTTP 只有一条 SQL。

两个入口只读业务事实：无 FOR UPDATE、无容量锁、不写审计、不 commit/rollback、不推进 version/revision，并避免隐式 autoflush。调用方事务若有待写事实，应显式 flush 后再选择合适读取方式，不能靠汇总暗中写库。写侧必须先按 P1 容量协议加锁并复验，snapshot 不是并发容量保证。

快照仅供内部消费。P4 API 负责登录鉴权、客户/成本参数门禁、有界序列化、详情跳转能力与访问日志。统计可见不授予项目详情访问权；台账跳转还需现行发布 flag 和详情权限。访问日志可沿用独立 best-effort 短事务，不属于本函数的业务写入或事实更正审计。

## 离线 JSON 示例

以下均为合成夹具，无需联网。A 超收不能抵 B 欠返，未关联 3 件也不能抵扣。

```json
{
  "contract_version": "v1.0-draft",
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
      "return_status": "returned",
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
      "return_status": "not_returned",
      "basis_state": "complete",
      "reason_codes": ["undated_receipts"],
      "undated_receipt_qty": "1.000"
    }
  ],
  "unassigned_by_project": [
    {"project_id": "example-project", "received_qty": "3.000"}
  ],
  "summary": {
    "known_pending_qty": "4.000",
    "unknown_order_count": 0,
    "incomplete": false
  }
}
```

`orders` 为内部快照形状；实际 HTTP 由 P4 分页输出。负量示例为 `{"demand_qty":null,"received_qty":"1.000","pending_qty":null,"remaining_qty":null,"over_received_qty":null,"return_status":null,"basis_state":"incomplete","reason_codes":["negative_demand_qty"]}`，不能在客户端把 `null` 转成零。

## 错误与兼容

以下是拟定领域错误；HTTP 由各 Adapter 映射，业务读取异常不得返回伪零。

| code | 建议 HTTP | 处理 |
|---|---|---|
| `invalid_summary_request` | 422 | 非法 ID、参数或类型；修正请求 |
| `summary_batch_too_large` | 422 | 去重后超过 500；调用方分块 |
| `historical_as_of_not_supported` | 422 | v1 不接历史截点 |
| `unsupported_summary_filter` | 422 | P4 不支持的筛选参数；明确提示 |
| `summary_permission_denied` | 403 | 参数要求未获数据权限；不返回无权值 |
| `summary_snapshot_required` | 503 | 完整快照未建立只读一致事务；服务端修正，不回退拼接结果 |
| `summary_unavailable` | 503 | 查询失败；用户可重试，不显示假空 |

单据不可核对属于成功结果的 `basis_state/reason_codes`，不以一个异常单阻断其他已授权单；无权 ID 的省略与空 scope 则不报存在性错误。

| 提供方／消费者 | 兼容行为 |
|---|---|
| 现 main／新 P4 | 新契约不可用；待返入口保持不可用，禁止回退自报 pending、旧坏件或逐项目算法 |
| v1 实现／旧列表 | 新增可选 `return_summary`，不改变原列表集合、排序、分页、删除 snapshot/version_digest 或明细 OCC digest |
| v1 实现／新 P1、P4 | 共用每单 DTO 与 fixture；不分别推导状态和差额 |
| v1 实现／历史 as_of 调用 | 明确 422，后续另立版本定义历史日期与无日期事实解释 |
| 后续增加可选字段 | 消费者忽略未知字段；变更公式、空值、有效范围或删除字段必须升主版本并先联测 |

本契约不新增表、列、迁移或回填。撤下读适配可回退，保留真实收货、人工更正与审计；不能降级后仍声称具有新容量保护。

## 实现时必须验证的场景

| 目标／角色与起点 | 操作 | 可观察预期 |
|---|---|---|
| P1/P4 同口径；有权核对人；上述 A/B 及未关联夹具 | 查列表、详情、项目和全集总计并翻页 | A 已返且超额 2，B 未返且差额 4，总差额 4；未关联独立 3；分页不改变合计 |
| 整单筛选；有权分析人员；同单有多个 PN、退货与不同件况 | 只筛其中 PN／品类，再按需求日期窗口读取 | N 仍为整单 qty，R 含全部 PN/件况与窗口前收货，不扣退货；免返硬盘按 v1.1 从 N、R 中剔除 |
| 未知和范围；仅新项目权限账号；缺量、零量、跨项目关联 | 读取完整筛选集及空 scope | 未知不补零，统计不完整；旧项目数量/名字不泄露；空 scope 零数据 |
| 查询一致性；隔离真实 PostgreSQL 双连接；超过 500 单 | 一个连接提交完整改量/作废/补关联，另一个生成快照 | 总计、明细、未关联桶和各批来自同一版本；无 JOIN 放大和逐单查询 |
| 既有指标保护；有权账号 | 原件减量形成超收后查旧卡墙/PN率与新摘要 | 收货不截断，新摘要超额；旧率仍实际返还÷实际领用，digest 不含富化字段 |

这些是后续契约测试和用户点按验收输入，本批只交付文档，不代表测试或业务验收已完成。
