# 主数据库只读 PN 快照契约 v1

- 日期：2026-10-16；协议版本保持 `schemaVersion: 1`。
- 决策：[D-36](../decisions/0014-主数据库与商城目录互通.md)，含 2026-10-15 18:16Z 金臣裁定 (b) 的销售聚合计数窄例外。
- 范围：既有 `/snapshots` 的兼容扩展；生产者为备件系统主数据库，消费者为商城目录同步服务。本文不表示商城存储、排序、用户验收或部署已经完成。

## 1. 路由、认证与请求

`POST /api/integrations/resale/parts/snapshots`，请求与响应均为 JSON，字段沿用 camelCase。该接口只读，不创建业务记录、库存流水或 AI 研究任务。

使用 `Authorization: Bearer <catalog-service-token>`，由独立配置 `resale_catalog_token_sha256` 校验凭据的 SHA-256。它与返件导出凭据独立，不接受用户登录凭据代替，也不向浏览器分发服务令牌。未配置时返回 HTTP 404 `{"detail":"PN 只读导出尚未启用"}`；缺失或无效凭据返回 HTTP 401 `{"detail":"PN 只读凭据无效"}`。成功响应带 `Cache-Control: no-store`。

```json
{"partIds":[101,101,999]}
```

- `partIds` 是长度 1..100 的整数列表；长度上限在去重前校验，所以 101 个重复 ID 仍超限。
- 每个 ID 必须为正数；空列表、超限或非正数返回 HTTP 422。错误沿用 v1 的 `detail` 形式，不改用 v2 错误信封。
- 按第一次出现的位置去重，响应每个去重后的请求 ID 返回一个 item，顺序保持一致。
- 沿用既有合并解析：active PN 返回自身；merged PN 沿 `merged_into_id` 查找最终节点，响应保留 `requestedPartId`，`partId` 为最终身份，`status` 为 `merged`。不存在、目标缺失、循环或超过 10 次重定向时按 `not_found` 返回，不猜身份。
- 这是按已知身份读取资料的接口，不是增量发布或 v2 固定水位快照。整目录列举见 [resale-catalog-v1-list](resale-catalog-v1-list.md)（`POST /list`，D-36 PR1）。

## 2. 响应字段

成功信封为 `{"schemaVersion":1,"items":[...]}`。既有字段语义保持，所有 item 新增下表两个可空计数字段。

| 字段 | 类型及语义 |
|---|---|
| `requestedPartId` | 请求的正整数 ID。 |
| `partId` | 找到的最终稳定 PN ID；`not_found` item 不含此字段。 |
| `status` | 找到时沿用 PN 状态；经合并重定向时为 `merged`；无法解析时为 `not_found`。 |
| `pnStd` | 最终 PN 的规范名；只用于展示，不用作计数关联键。 |
| `description`, `brand`, `categoryMajor`, `categoryMinor`, `unit` | 既有 PN 通用资料，字符串或 null。 |
| `needsReview`, `isExcluded` | 既有治理标记，布尔值；不决定计数是否纳入。 |
| `lockedFields` | 既有人工保护字段名数组，排序输出。 |
| `updatedAt` | 既有 PN 更新时间或 null；不代表销售计数的更新时间。 |
| `specs` | 既有参数数组；每项为 `key`, `value`, `unit`, `source`，沿用原值。 |
| `salesRecordCount` | 非负整数或 null；null 表示本次没有可信计数。 |
| `salesRecordCountAsOf` | ISO-8601 UTC 时间或 null，格式 `YYYY-MM-DDTHH:mm:ss[.ffffff]Z`；整数计数必须有此时间，本实现未知计数时也返回 null。 |

`not_found` item 仅含 `requestedPartId`、`status` 及两个为 null 的计数字段。成功示例采用合成资料：

```json
{
  "schemaVersion": 1,
  "items": [
    {
      "requestedPartId": 101,
      "partId": 101,
      "status": "active",
      "pnStd": "DEMO-PN-A",
      "description": "合成备件",
      "brand": "Demo",
      "categoryMajor": "硬盘",
      "categoryMinor": "SSD",
      "unit": "块",
      "needsReview": false,
      "isExcluded": false,
      "lockedFields": [],
      "updatedAt": "2026-10-15T08:00:00+00:00",
      "specs": [],
      "salesRecordCount": 2,
      "salesRecordCountAsOf": "2026-10-16T01:00:00Z"
    },
    {
      "requestedPartId": 999,
      "status": "not_found",
      "salesRecordCount": null,
      "salesRecordCountAsOf": null
    }
  ]
}
```

## 3. 销售记录数与失败语义

计数为最终响应 `partId` 对应的**全历史已生效销售订单上的销售明细行数**。按 `f_sales_line.part_id` 聚合，通过 `f_sales_line.order_id` 关联 `f_sales_order.id`，订单条件仅 `data_status='已生效'`。同单同 PN 两行计 2；包含销售换货、零价行、`counts_revenue=false` 及治理排除的 PN。不按数量求和、不去重订单、不限制日期、价格或业务类型，不以 PN 文本或别名猜归属。只统计事实当前绑定的稳定 `part_id`，读取不改写销售事实或身份。

按本批已解析的最终 ID 去重后执行**一条 GROUP BY 查询**；先完整读取结果，再形成所有计数。仅 PN 已存在且整批聚合完整成功时给整数，确无符合行时为可信 `0`。未知不是 `0`：不存在或无法解析的 PN 返回 null；聚合查询失败时，整批 item 的两个计数字段均返回 null，已经可读的 PN 资料仍正常返回。不得采用失败前读到的部分计数，也不向响应回显 SQL、异常内部信息或业务明细。其他资料读取错误仍沿用原接口的错误行为。

成功批次的 `salesRecordCountAsOf` 相同，取**执行聚合查询前记录的 UTC 开始时间**，表示本轮计数观察开始时点。它不是精确数据库事务水位、PN 的 `updatedAt`、订单最后变更时间或跨 PN 资料与销售事实的一致性快照；并发提交可能影响实际查询可见结果。返回后销售事实仍可变化，本接口不承诺跨请求翻页或跨系统实时一致。

## 4. 消费者兼容、刷新与数据边界

旧 v1 客户端可忽略这两个新增响应字段；URL、凭据、请求、旧字段及 `schemaVersion` 保持不变。新客户端必须兼容旧生产者缺失字段的响应：缺失、null、类型无效或身份不能可靠匹配时，均视为没有可信计数并保存 NULL，不能补成 0，也不能从有限的价格证据条数、销量或订单数推算。

商城只将计数作为来源快照的只读元数据，用于**分页前**的资料待办排序：同 PN 好坏件在库合计降序 → 销售记录数降序（NULL 最后）→ 商城 `PN.id` 稳定升序。库存属于商城事实，本接口不提供库存。未取得可信值时自然使用其余排序键，不让未知值排在可信 0 之前。

销售行或订单状态变化不一定修改 PN `updatedAt`；消费者需沿已批准的快照刷新机制重新读取计数，不能只依赖 PN 更新时间。刷新失败不得写入伪 0；如继续保留最后成功来源快照，须保留其原观察时间并标明待刷新，不能标成此次成功值。身份改绑后不能沿用旧身份计数。本次生产者扩展不代表商城已经完成接收、存储与排序。

D-36 的例外仅限上述 v1 `/snapshots` 的聚合计数及观察时点，用途仅为排序。目录负载仍不得带任何销售明细，包括价格、客户、单号、人员、销售行／订单标识，也不得扩大为采购、库存、项目或审批信息。既有独立参考价点击查询不因此成为目录批量取价；计数不进入人工资料命令白名单，不授予服务身份或 AI 写业务状态的权限。

本扩展不向 [catalog v2](catalog-v2.md) 的 node、hash 输入或人工命令添加字段，不改变其版本、发布捕获范围或水位规则。两仓需同步本契约与 v2 兼容说明，并分别验证生产者与消费者；一侧文档或代码更新不证明另一侧已经接通或发布。

## 5. 商城参考均价 GET（独立端点）

- 日期：2026-10-20；决策：[D-42](../decisions/0020-商城参考价销售均价.md)（30 天窗口 + 指数时间衰减，覆盖计划中的“12 个月”默认）及 [D-43](../decisions/0021-商城参考价采购均价.md)（金臣 2026-10-20 裁定继续提供采购价）。
- 范围：商城详情页按 PN 点击查询采购、销售参考均价，独立于 `/snapshots`、`/list` 与 catalog v2。只读，不写业务状态，不新增迁移。

### 5.1 请求与认证

`GET /api/integrations/resale/parts/price-reference?pn=<PN>`。

- `pn` 必填，原始长度 1..256；纯空白返回 422。响应 `pn` 原样回显 HTTP 解码后的请求值，不做 strip/upper；仅查找副本调用既有 PN 标准化与别名/合并规则。
- 认证与 `/snapshots` 相同：`Authorization: Bearer <catalog-service-token>`，由 `RESALE_CATALOG_TOKEN_SHA256` 校验；未配置 404，缺失/错误凭据 401，其他 integration 凭据不通用。
- 成功（含空样本）与手工 409/503 均返回 `Cache-Control: no-store`。
- PN 候选先分别沿合并链取最终身份，再按最终 id 去重：同一最终身份可返回；`pending` 别名、不同最终目标、循环、断链或超过 10 跳返回 409，不任选候选。原 `POST /resolve` 行为不变。

### 5.2 响应

固定 snake_case，无 `schemaVersion/items` 信封。非空示例与跨库夹具一致（合成数据；固定业务日 2026-10-19，半衰期 7 天）：销售两单分别为今天 qty=1 含税 113、7 天前 qty=1 含税 226，均标税率 13%；采购两单为 10-18 未税单的原价 100（qty=2）、200（qty=1）两行，税率 0，以及 10-11 含税单的原价 113（qty=1）一行，税率 NULL。

```json
{
  "pn": "DEMO-PN",
  "prices": [
    {"kind": "sale", "amount": "150.67", "currency": "CNY", "occurred_at": "2026-10-19T00:00:00+08:00", "source": "itsp销售均价",
     "tax_basis": "inc_tax", "sample_count": 2, "order_count": 2, "window": {"start": "2026-09-20", "end": "2026-10-19"},
     "latest_sale_date": "2026-10-19", "oldest_sale_date": "2026-10-12", "nominal_sample_count": 0},
    {"kind": "sale", "amount": "133.33", "currency": "CNY", "occurred_at": "2026-10-19T00:00:00+08:00", "source": "itsp销售均价",
     "tax_basis": "ex_tax", "sample_count": 2, "order_count": 2, "window": {"start": "2026-09-20", "end": "2026-10-19"},
     "latest_sale_date": "2026-10-19", "oldest_sale_date": "2026-10-12", "nominal_sample_count": 0},
    {"kind": "purchase", "amount": "145.29", "currency": "CNY", "occurred_at": "2026-10-18T00:00:00+08:00", "source": "itsp采购均价",
     "tax_basis": "inc_tax", "sample_count": 3, "order_count": 2, "window": {"start": "2026-09-20", "end": "2026-10-19"},
     "latest_sale_date": "2026-10-18", "oldest_sale_date": "2026-10-11", "nominal_sample_count": 2},
    {"kind": "purchase", "amount": "128.57", "currency": "CNY", "occurred_at": "2026-10-18T00:00:00+08:00", "source": "itsp采购均价",
     "tax_basis": "ex_tax", "sample_count": 3, "order_count": 2, "window": {"start": "2026-09-20", "end": "2026-10-19"},
     "latest_sale_date": "2026-10-18", "oldest_sale_date": "2026-10-11", "nominal_sample_count": 1}
  ],
  "reference_only": true,
  "calculation": {
    "method": "time_decay_qty_weighted", "decay": "exponential_half_life", "window_days": 30, "half_life_days": "7",
    "date_basis": "order_date", "timezone": "Asia/Shanghai",
    "window": {"start": "2026-09-20", "end": "2026-10-19"},
    "order_statuses": ["已生效"], "counts_revenue_only": true,
    "positive_qty_only": true, "include_zero_prices": false,
    "exclude_confirmed_source_error": true,
    "sales_unit_price_basis": "inc_tax",
    "tax_policy": "order_rate_or_13pct", "fallback_tax_rate": "0.13",
    "currency": "CNY", "sample_count": 2, "order_count": 2,
    "latest_sale_date": "2026-10-19", "oldest_sale_date": "2026-10-12"
  }
}
```

采购与销售分别判断空样本：某域无合格样本就不输出该域条目，只有采购时可以仅返回 purchase 两条。未知 PN 或两域均无合格样本时仍返回完整 `pn/reference_only/calculation`，`prices=[]`。顶层 `calculation` 始终只描述销售；无销售时其两个计数为 0、`latest_sale_date`/`oldest_sale_date` 为 null，即使存在采购条目也如此。口径与窗口照常声明，不返回空 body、伪零价格或 null 金额条目。

| 字段 | 类型及语义 |
|---|---|
| `pn` | 请求原文回显。 |
| `prices` | 有样本的域各输出两条：先 sale 的 `inc_tax`、`ex_tax`，再 purchase 的 `inc_tax`、`ex_tax`。原 sale 值及顺序不变；金额为 Decimal 字符串，HALF_UP 两位。 |
| `prices[].kind` / `source` | 销售为 `sale` / `itsp销售均价`；采购为 `purchase` / `itsp采购均价`。 |
| `prices[].occurred_at` | 本域最近合格订单日的 Asia/Shanghai 零点，兼容旧必填 datetime；不是抓取时点或真实成交时刻。 |
| `prices[].sample_count` / `order_count` | 本域参与均价的明细行数 / distinct 订单数；sale 按 `FSalesOrder.id`，purchase 按 `FPurchaseOrder.id`，不是销售/采购数量。 |
| `prices[].window` | 本次实际窗口闭区间。 |
| `prices[].latest_sale_date` / `oldest_sale_date` | 沿用旧字段名；sale 表示最新 / 最旧合格销售订单日期，purchase 表示最新 / 最旧合格采购订单日期。 |
| `prices[].nominal_sample_count` | 该税基为名义侧的参与行数，分侧统计，见 §5.3；不是数量或衰减权重。 |
| `reference_only` | 恒 `true`；参考价不等于定价。 |
| `calculation` | 保持既有字段集合，继续只描述销售口径与样本；计数/日期与 sale 条目一致。含实际 `method`（`time_decay_qty_weighted` / `time_decay_line_weighted`）、`decay`、`window_days`、`half_life_days`、日期基准、时区、窗口、状态列表、过滤开关、销售税基、税率策略、兜底税率、币种与销售计数/日期。采购口径由条目和本文表达，不新增元数据字段。 |

### 5.3 计算口径

- 两域共用 `end = business_today()`（Asia/Shanghai）与 `start = end - (window_days - 1)` 天，闭区间，默认含今天共 30 天。销售按 `FSalesOrder.order_date`、采购按 `FPurchaseOrder.order_date`。
- 两域共用每行衰减 `d=0.5^(age_days/half_life_days)`，其中 `age_days=(end-order_date).days`；`qty_weighted` 行权重 `w=qty*d`，`line_mean` 行权重 `w=d`。每个税基的均价为 `Σ(w×该税基单价)/Σw`，复用同一衰减及加权函数。
- 有效税率复用采购分析 `_rate_expr`：`r=票面非零税率，否则 13%`。销售原始 `unit_price` 恒含税，未税单价为 `unit_price/(1+r)`。采购按 `_line_amount_exprs` / `line_amounts_reference` 的原始侧语义：`is_tax_inclusive=True` 时原价含税、未税价为 `unit_price/(1+r)`；False/NULL 时原价未税、含税价为 `unit_price*(1+r)`。采购行不套用单头金额缺口回退，不使用旧型号分析固定 13% 的换算。
- SQL 按日期、有效税率与原始税基分组取精确聚合值，Decimal 完成税基换算与衰减加权，最终才 HALF_UP 两位。不能调用会逐行舍入的金额函数，否则会改变均价。
- 两域默认过滤：订单状态在 `RESALE_PRICE_REFERENCE_ORDER_STATUSES`（默认已生效），`qty>0`、`unit_price>0`、订单日期在窗口内。零价开关打开时取 `unit_price>=0`；负价/NULL 恒排除。源错误开关打开时分别以 `_dq_exists("sales", ...)` / `_dq_exists("purchase", ...)` 的 NOT EXISTS 排除已确认源数据错误；未确认数据疑点仍计入、不做离群裁剪。
- `counts_revenue_only` 仅适用销售，消费已落库的 `counts_revenue`。采购没有该收入属性，不关联销售订单判定收入。采购必须遵守 D-39 **单头批次行**：只计 `f_purchase_line.import_batch_id = f_purchase_order.import_batch_id` 的行；旧批次残留（包括新批次已删行号）只在读侧排除，不改事实数据。
- 名义样本分税基统计参与行数：销售含税侧恒为 0，未税侧计票面税率 NULL/0 的行。采购直接复用 `_nominal_side_expr` 的行级结构性规则；税率 NULL/0 时，含税单的 ex 侧为名义，False/NULL 标记单的 inc 侧为名义；已标税率两侧均非名义。
- NULL/未来订单日期自然不入池；窗口外行不影响均价与日期。任何域的合格样本中出现 `rate<0` 或 `rate>=1` 的坏税率，整次请求 503，不静默丢弃或发布部分价格。两域均沿用商城金额上限检查（整数位最多 14 位）。

### 5.4 默认配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `RESALE_PRICE_REFERENCE_WINDOW_DAYS` | `30` | 1..366，闭区间日历日。 |
| `RESALE_PRICE_REFERENCE_HALF_LIFE_DAYS` | `7` | Decimal，(0,3650]。 |
| `RESALE_PRICE_REFERENCE_ORDER_STATUSES` | `["已生效"]` | JSON 数组；非空去重，仅允许已生效/已取消/草稿/进行中。 |
| `RESALE_PRICE_REFERENCE_COUNTS_REVENUE_ONLY` | `true` | 仅销售消费已落库 `counts_revenue`；采购不适用。 |
| `RESALE_PRICE_REFERENCE_INCLUDE_ZERO_PRICES` | `false` | true 时纳入 0 价；负价/NULL 始终排除。 |
| `RESALE_PRICE_REFERENCE_WEIGHTING` | `qty_weighted` | `qty_weighted` / `line_mean`。 |
| `RESALE_PRICE_REFERENCE_EXCLUDE_CONFIRMED_SOURCE_ERROR` | `true` | 仅排除已确认源错误。 |

采购复用以上窗口、半衰期、状态、零价、加权与源错误配置，不新增配置项。非法配置在 Settings 构造时失败，不静默回退。响应 `calculation` 由实际生效 Settings 构建，不写死默认值；其中样本统计仍只取销售。

### 5.5 错误、隐私与兼容

- 未配置凭据 404；缺失/错误 Bearer 401；参数非法 422；身份歧义/坏合并链 409；数据库、聚合、坏税率或超商城金额上限 503（固定 `detail`，不含 SQL/参数）。未知 PN 或两域均无样本是 200 空 `prices`，不把故障伪装成无数据。
- 响应只白名单序列化；不返回客户、供应商、采购员、单号、人员、采购/销售行与订单 ID、原始证据、原始税率明细或内部错误。
- **已上线商城兼容约束**：商城 DTO 使用 `extra=forbid`，本次不新增 ReferencePrice、顶层或 `calculation` 字段，仅扩展提供方 `kind/source` Literal。ReferencePrice 固定字段为 `kind`、`amount`、`currency`、`occurred_at`、`source`、`tax_basis`、`sample_count`、`order_count`、`window`、`latest_sale_date`、`oldest_sale_date`、`nominal_sample_count`；顶层固定为 `pn`、`prices`、`reference_only`、`calculation`。`amount>=0`、`max_digits=18`、最多 4 位小数，`source` 最长 256 字符、`prices` 最多 100 条；本 GET 保持最终两位金额。销售样本计数/日期仍与 `calculation` 一致，采购不混入。独立冻结的商城等价 DTO 测试验证兼容，跨库夹具见 `backend/tests/fixtures/resale_price_reference_contract.json`。
- **本次部署顺序**：itsp 先于商城或同窗发布即可，商城继续按 `users.can_view_purchase` 过滤采购条目，不因上游返回采购价而放宽展示权限。本契约不授权部署。
- **历史兼容说明**：“旧 shop + 新 GET 不兼容”仅针对引入 D-42 元数据前的消费者，其 `extra=forbid` 会拒绝当时新增的样本字段与顶层 `calculation`，原切换流程见实施计划 §4。已支持 D-42 元数据及 purchase/sale 的上线版本可直接解析本次响应。停用 URL 仍只会回到“参考价不可用”，不影响库存/图库。
