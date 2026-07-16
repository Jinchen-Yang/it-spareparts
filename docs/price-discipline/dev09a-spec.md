# DEV-09A：池内 PN 股票式价格区间图

## 1. 目标

在现有全员互通池价格分析页中，用“最低—最高区间 + 中位 + 数量加权均价 + 最近价”替换过渡性的横向金额柱状排名，让采购、销售和管理层在同一时间窗口内比较池内全部 PN 的真实历史价格分布。

本切片只记录和展示已有采购/销售事实。它不提交价格、不审批、不拦截业务、不评价员工，也不读取或推导库存。

## 2. 范围

### 包含

- 新增 `GET /api/pool-analysis/pools/{group_id}/price-map`。
- 采购、销售双侧对称实现。
- 全部有效池成员都返回；无正式参考样本的成员保留，统计字段为 `null`。
- 统一未税口径：采购沿用 `purchase_ex_unit/purchase_ex_tax_expr`，销售沿用 `sale_ex_unit/revenue_amount`。
- 正式参考统计：最低、最高、中位、数量加权均价、最近价、数量、订单数、明细数。
- 当前人工约束价和当前约束差额；总图明确标注“当前约束”。
- 数据质量：`open/source_changed` 仍计入并标记；`confirmed_source_error` 不进正式参考统计，但其原始事实仍可作为最近原始点返回并标记。
- 排除计数：非生效订单、非正价格、非正数量、未来订单、销售非营收、数据疑点、已确认源数据错误。
- 价格治理权限关闭时结构化清空价格、统计、约束、差额、质量数量，并把价格排序降级为 PN 排序。
- ECharts `CustomChart` 股票式区间图；旁边/下方保留完整等价数据表。
- 点击图形或表格 PN 进入现有型号详情，保留池分析时间、方向和采购类型筛选。
- 移动端图形点击显示固定详情卡；核心信息不依赖 hover。

### 不包含

- DEV-09B 单 PN 逐笔时间线、分布直方图、历史约束版本匹配。
- 订单详情弹窗目标 `line_id` 高亮。
- 利润、成本引擎或库存逻辑修改。
- 报价、调价、询价、提交、审批、拦截、整改任务、员工评价或自动建议。

## 3. 统计规则

### 采购

- 全部真实采购类型均可见；`purchase_type` 仅作筛选，不复用利润成本池类型。
- 正式计价行：已生效、订单日期不在未来、单价 > 0、数量 > 0、未被确认源数据错误。
- 含税采购按现有统一换算；未税采购保持原值。

### 销售

- 正式计价行：已生效、计入营收、订单日期不在未来、单价 > 0、数量 > 0、未被确认源数据错误。
- 销售按现有未税口径，不另写税价公式。

### 数据质量

- 一条事实行命中任意 `confirmed_source_error` 疑点时，从正式参考统计剔除。
- `open`、`source_changed` 仍计入正式参考统计，并计入 `suspected_records`。
- `confirmed_valid` 正常计入。
- 已确认源错误的原始事实不删除；每个成员另返回 `latest_raw_record`。若它恰为最近原始事实，可与正式 `stats.latest` 不同。
- 多条疑点的展示优先级：`confirmed_source_error` > `open/source_changed` > `confirmed_valid` > `none`。

### 当前约束

- 总图使用 `valid_to IS NULL` 的当前采购上限或销售下限进行复盘。
- 采购加权均价 `>` 当前上限为 `above`；销售加权均价 `<` 当前下限为 `below`；等于不越线。
- 本切片不声称当前约束就是历史订单发生时约束。

## 4. API 契约

### 请求

```http
GET /api/pool-analysis/pools/{group_id}/price-map
  ?side=purchase|sales
  &range=30d|90d|365d|all|custom
  &date_from=YYYY-MM-DD
  &date_to=YYYY-MM-DD
  &purchase_type=销售订单
  &employee=张三
  &sort=pn|weighted_avg|constraint_delta|latest_date
  &order=asc|desc
```

规则：

- 默认 `side=purchase`、`range=90d`、`sort=pn`、`order=asc`。
- `employee` 在采购侧匹配采购员，在销售侧匹配销售员。
- `purchase_type` 只影响采购侧；销售侧保留该字段但不使用。
- 价格受限账号请求价格排序时，`effective_sort=pn`、`effective_order=asc`。

### 响应

```json
{
  "contract_version": 1,
  "side": "purchase",
  "basis": "ex_tax",
  "price_restricted": false,
  "pool": {"group_id": 5, "name": "4T 硬盘池", "member_count": 16},
  "window": {"range": "90d", "date_from": "2026-04-18", "date_to": "2026-07-16", "as_of": "2026-07-16"},
  "filters": {"purchase_type": null, "employee": null},
  "sort": "weighted_avg",
  "order": "desc",
  "effective_sort": "weighted_avg",
  "effective_order": "desc",
  "current_constraint": {"status": "set", "value": 725.66, "changed_at": "...", "input_basis": "ex_tax"},
  "pool_stats": {
    "weighted_avg": 698.20,
    "median": 682.30,
    "min": 420.00,
    "max": 920.00,
    "latest": 800.00,
    "total_qty": 930.0,
    "order_count": 42,
    "line_count": 51
  },
  "excluded": {
    "inactive_orders": 8,
    "nonpositive_price": 2,
    "nonpositive_qty": 1,
    "future_orders": 1,
    "non_revenue_sales": 0,
    "suspected_records": 1,
    "confirmed_source_error_excluded": 1
  },
  "members": [{
    "part_id": 101,
    "pn_std": "ST4000NM0035",
    "description": "4T SAS 硬盘",
    "brand": "Seagate",
    "stats": {
      "weighted_avg": 760.00,
      "median": 735.00,
      "min": 610.00,
      "max": 920.00,
      "latest": 800.00,
      "total_qty": 88.0,
      "order_count": 7,
      "line_count": 9,
      "latest_date": "2026-07-10"
    },
    "current_reference": {"relation": "above", "delta_amount": 34.34, "delta_pct": 0.0473},
    "latest_raw_record": {
      "order_id": 55,
      "line_id": 991,
      "order_no": "CGDD-20260710-0012",
      "order_date": "2026-07-10",
      "employee": "张三",
      "price_ex_tax": 920.00,
      "quality_status": "open"
    },
    "quality_counts": {"suspected": 1, "confirmed_source_error": 1}
  }]
}
```

## 5. 权限

- 接口必须通过 `page_pool_analysis`。
- `data_pool_price_governance=false` 时：
  - `price_restricted=true`；
  - `current_constraint` 为 restricted；
  - `pool_stats=null`；
  - 每个成员仍保留 `part_id/pn_std/description/brand`，但 `stats/current_reference/latest_raw_record/quality_counts` 均为 `null`；
  - 价格排序降级 PN，避免通过顺序反推；
  - 图表不渲染，等价表显示“无池价格权限”。
- 经办人和订单号在有价格治理权限时公开。
- `data_supplier/data_customer` 不影响本切片响应，因为 `latest_raw_record` 不返回对手方；以后逐笔接口再按字段权限返回。

## 6. TDD 公共测试缝

已确认只在以下公共缝测试：

1. HTTP：`GET /api/pool-analysis/pools/{group_id}/price-map` 的统计、筛选、排序、权限和错误响应。
2. 前端纯函数：`buildPoolPnPriceMapOption` 与 click resolver 的视觉编码和数据映射。
3. 页面行为：切换采购/销售、排序、点击图/表、权限受限、旧请求最后返回不覆盖新状态。
4. 独立 SQL：生产快照 staging 上按公式直接聚合，与 API 数字逐项核对。

不直接测试服务私有 helper，也不 mock 私有查询结构。

## 7. 合并门槛

- 后端全量测试、前端全量测试、类型检查、生产构建、`alembic check` 全绿。
- 采购/销售双侧公式与独立 SQL 一致。
- 疑点和已确认源错误语义有回归测试。
- 价格治理受限账号不能从响应、排序、颜色、tooltip 或 DOM 反推价格。
- 股票式区间图和等价表表达同一完整成员集合。
- 越线同时使用颜色、图形和文字；null 不渲染成 0。
- 1440px、768px、390px 真实浏览器通过；移动端不产生整页横向滚动。
- 单池 365 天最重池 `price-map` 在生产快照 staging p95 < 800ms。
- 双轴复审（Standards + Spec）无 Blocker/P1。

满足以上条件后可合并，但仍不等于可生产；生产发布另需按发布手册完成备份、staging、权限冒烟、性能复验和观察期。
