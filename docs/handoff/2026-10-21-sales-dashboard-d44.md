# 销售看板（D-44）开发交接

- **分支 / PR**：`claude/peaceful-faraday-pvm1xw` → `main`，[PR #442](https://github.com/Jinchen-Yang/it-spareparts/pull/442)（草稿）。基线 `origin/main` = `d41e3d6`，开发期间 main 未变化。
- **决策**：[D-44](../decisions/0022-销售看板分组趋势与金额区间.md)（杨金臣 2026-10-21 全部拍板，13 条）。前置口径 D-39 / D-40 / D-41 不变。
- **状态**：方案、决策、后端、前端、runbook 全部完成并推送，共 14 个提交。CI 前端已绿，后端结果见文末「当前状态」。**未做浏览器点按验收，未合并，未部署，未连接生产。**
- **规模**：31 个文件，+4852 / −618 行，其中测试约 1,100 行，依赖锁文件与 SBOM 约 550 行。按金臣指示，全部工作合在一个 PR 内，不拆分。

## 一、需求与口径（一页看懂）

需求原话：解决“今天、昨天和自定义时间内销售数据的可视化：按客户分类、按销售、按时间，能升序降序，能看价格区间……其余由开发探索设计”。

D-44 拍板要点（实现必须与之一致，**改口径先改决策**）：

| # | 口径 | 落点 |
|---|---|---|
| 1 | 扩展现有「销售分析」页（`/sales/analytics`），沿用 `page_sales_analysis`，仍只给 admin / boss | 不新增权限键 |
| 2 | 按客户、按客户类型都做，均可分组、可筛选 | `customer_name[]`、`customer_type[]` |
| 3 / 7 | 价格区间 = **订单金额区间**；双端拖动条，可输入，**1k 步长**，默认不限 | `amount_min/max` + `amount_basis` |
| 4 | 任意列升序 / 降序，空值恒在最后 | `order=asc\|desc` |
| 5 | 加毛利，口径与老板看板 KPI 一致 | 已配成本营收 − 成本；毛利率分母 = 已配成本营收 |
| 6 | 回款视角本轮不做（销售回款导入尚未适配） | — |
| 8 | 按商品大类 / 品牌筛选或分组时拆明细行，只计匹配行；不含税 = 含税 ÷ 1.13，标注「名义」 | 行级口径 |
| 9 | 对比期 = **上个月**（区间整体前移一个自然月），不做同比；区间长于一个月不提供 | `compare` |
| 10 | 负金额单、无金额单都计入合计，另加两个开关 | `include_negative` / `include_empty` |
| 11 | 无客户权限的账号不能按客户 / 客户类型 / 城市 / 来源筛选或分组 | 后端 403 |
| 12 | 客户按规范名（`dim_customer.name_normalized`）合并 | 同名多个 `customer_id` 算一个客户 |
| 13 | 新客 / 老客：首单（全历史已生效单最早日期）落在区间内为新客 | 不受其他筛选影响 |

## 二、后端

### 2.1 模块

| 文件 | 职责 |
|---|---|
| `app/services/sales_analytics.py` | **单一口径源**。`_order_query(**filters)` 生成“一单一行”的子查询（含营收、已配成本营收、成本、毛利、客户属性）；summary / orders / order_detail / filter_options 都基于它。行级口径也在这里切换。 |
| `app/services/sales_dashboard.py`（新） | 分组排行 `groups`、趋势 `trend`、新老客 `customer_lifecycle`、上月对比 `summary_compare` / `compare_window` / `shift_month`、直方图 `amount_histogram`、导出取数 `export_orders`。**全部在 `_order_query` 子查询外再聚合，不另起金额口径**。 |
| `app/api/sales_analytics.py` | 公共筛选依赖 `sales_filters`（`SalesFilters` dataclass）、`_guards` 前置硬界、排序防泄漏 `effective_sort`、占比口径强制公开 `_public_basis`。 |
| `app/config.py` | `FIELD_GROUPS` 新登记：`customer_type`、`customer_source`（customer_info 组），`amount_ex_tax_max`、`amount_inc_tax_max`（purchase_cost 组）。 |

**核心不变量**：任一筛选下，KPI 合计 = 分组各组合计（含“其他”）= 趋势各桶合计 = 订单列表合计（单头口径）。测试有锁。行级分组（大类 / 品牌 / PN）下，各组单数之和可以大于总单数（一张单可计入多组），接口带 `line_level: true` 标记。

### 2.2 接口（均为 GET，只读，前缀 `/api/sales/analytics`）

公共筛选参数（所有端点同一套）：`date_from, date_to, business_type[], customer_name[], customer_type[], salesperson_name[], category[], brand[], amount_basis, amount_min, amount_max, include_negative, include_empty`；另保留旧的模糊参数 `customer`、`salesperson`，用于兼容旧链接。

| 端点 | 新 / 改 | 要点 |
|---|---|---|
| `/options` | 改 | 增加 salespeople / customer_types / customers（`customer_q` 补全，最多 50 条）/ categories / brands / `amount_range`（当前其他筛选下三金额的最大值，忽略金额区间本身） |
| `/summary` | 改 | KPI 增加 customers、salespeople、revenue_costed、gross_profit、gross_margin、cost_coverage、new_customers、returning_customers；带 `compare=true` 时附上月同期 KPI；返回 `line_mode`、`compare_available` |
| `/orders` | 改 | sort 可取 `amount_ex_tax / amount_inc_tax / revenue / gross_profit / order_date / customer / salesperson`，加 `order`；行里加 customer_type、gross_profit、gross_margin |
| `/orders/{id}` | 改 | 加 `lines[]` 明细行（PN、大类、品牌、数量、含税单价、行金额、营收、毛利） |
| `/orders/export` | 新 | CSV，同筛选、同排序、同脱敏；被遮蔽的列整列不导出；上限 10,000 行，超出返回 413；复用维保导出的 `_csv_stream`（防公式注入、BOM、大小上限） |
| `/groups` | 新 | `group_by` ∈ customer / customer_type / customer_city / customer_source / salesperson / business_type / warehouse / category / brand / pn；`sort`、`order`、`top`（0 = 全部）、`compare`；返回 groups、other、concentration（前 1/5/10 名占比）、每组 share / cumulative_share |
| `/trend` | 新 | `granularity` = day / week（周一起）/ month；返回完整桶序列（无成交的桶为 0，有成交但金额全缺为 null）、上月同期序列、星期分布；桶数上限 1000，超出返回 422 |
| `/amount-histogram` | 新 | 订单数分布（20 段，段宽为 1k 的整数倍），负数单与无金额单单独计数 |

### 2.3 权限与脱敏规则（改动时最容易出错的地方）

1. 受限销售（`own_customers_only`）访问所有端点都返回 403（沿用 D-41）。
2. 客户类属的筛选或分组（`customer`、`customer_name`、`customer_type`，以及 4 个客户类分组维度），在客户字段被遮蔽时返回 403，防止拿筛选结果的数量探测客户名是否存在。
3. 金额区间、正负开关或空值开关所用的金额口径被遮蔽时返回 403；`revenue` 一直公开，所以按营收做区间不受影响。
4. **排序防泄漏**：按被遮蔽的字段排序时，即便值已置空，行的先后顺序本身也会泄露排名。所以：
   - 订单列表：金额 / 毛利排序退回 `revenue`，客户排序退回 `order_date`；
   - 分组排行：退回按单数排序。
   - 响应会回显实际生效的排序键，前端据此提示。
5. 占比、单均、集中度按 `amount_basis` 计算；该口径被遮蔽时，API 强制改用 `revenue`，所以这些派生键不用另外登记脱敏。
6. 新增的输出键都要逐字登记到 `FIELD_GROUPS`，否则会静默泄漏（仓库已有教训）。注意 `revenue_costed` **故意没有登记**：利润页和 part_ranking 里的同名键一直公开，登记会改变那些页面的行为。

## 三、前端

| 文件 | 职责 |
|---|---|
| `src/api/salesAnalytics.ts` | 类型与请求函数。多选参数用重复键（`k=v1&k=v2`），不用 axios 默认的 `k[]`。**汇总请求恒带 `compare=true`**，「对比上月」开关只控制显示，切换不重新请求。 |
| `pages/sales/analytics/urlState.ts` | 全部状态写进 URL：新增 `customer_name / customer_type / salesperson_name / category / brand / amount_basis / amin / amax / neg / empty / order / tab / group_by / gsort / gorder / gtop / gran / compare`；默认值不写进 URL，金额按 1k 取整。 |
| `SalesAnalyticsPage.tsx` | KPI 条常驻；四个页签「概览 / 分组排行 / 趋势 / 订单明细」，**默认订单明细**（兼容 D-41 与旧分享链接）；导出按钮；本地 `data_*` 权限快照做控件门控（数据层面的真值始终以后端为准）。 |
| `FilterBar.tsx` / `AmountRangeFilter.tsx` | 多选筛选；客户选择框按输入向后端补全（防抖 300ms）；拖动条在拖动过程中只改本地草稿，松手或输入框失焦才提交到 URL。 |
| `SummaryView.tsx` | KPI 卡与上月对比；增减带正负号和箭头，上月为空或为 0 时显示「—」。 |
| `GroupsTab.tsx` / `TrendTab.tsx` / `OverviewTab.tsx` | 三个新页签；点击下钻（`drillPatch` / `bucketRange`）会把条件写进筛选并切到订单明细。「未填」组和没有对应筛选参数的维度（城市、来源、仓库、PN）不可下钻。 |
| `components/charts/SalesDashboardCharts.tsx` | 横条图、趋势图、星期分布。遵循 `charts/README` 约定：option 用纯函数生成、null 不画成 0、颜色只取 `CHART_COLORS`、**单纵轴**（金额 / 单数 / 毛利切换，不做双轴）。 |
| `useAsyncData.ts` | 页签内取数的小封装，带序号，过期的响应会被丢弃。 |

## 四、测试与复跑

| 范围 | 命令 | 结果 |
|---|---|---|
| 后端 D-44 | `cd backend && uv run --extra dev pytest -q tests/test_sales_dashboard_d44.py` | 66 passed |
| 后端销售 / 脱敏 / 权限 | `cd backend/tests && uv run --extra dev pytest -q $(ls \| grep -iE "visib\|field\|mask\|permission\|sales\|customer\|dashboard")` | 521 passed |
| 后端全量（本地） | `cd backend && uv run --extra dev pytest -q` | 6342 passed；112 个失败全部是部署脚本类测试（本地容器以 root 运行、缺少工具），同样的文件在 CI 上通过 |
| 前端 | `cd frontend && npx tsc --noEmit -p . && npx vite build && npx vitest run` | tsc 通过；构建通过；152 个文件 / 1773 个用例全部通过 |
| 依赖审计 | `uv export --frozen --no-dev --no-hashes --no-emit-project \| uvx pip-audit -r /dev/stdin`；`npm run audit:prod` | 无漏洞 |

后端测试数据覆盖：同规范名多 customer_id、负金额单、无金额单、无明细单、未配成本行、已取消单（一切口径都排除），并和 `dashboard.kpi` 对拍毛利。中文排序依赖数据库排序规则，相关测试只断言升降序互逆，不断言具体顺序。

**本地环境注意事项**（云容器复现时会踩到）：
- 前端 `package-lock.json` 里有 29 个包的下载地址是 `registry.npmmirror.com`，云容器的网络策略不允许访问它。本地安装用 `npm ci --replace-registry-host=always`，**不要改锁文件**。
- 后端测试需要 :5433 端口上的 PostgreSQL。云容器会回收后台进程，数据库会被停掉，需要重新拉起；关掉 fsync 后全量测试大约 56 分钟。

## 五、顺带修复（与业务无关）

开发当天公布了新的依赖漏洞，导致 CI 的「生产依赖漏洞扫描」在只改文档的提交上也会失败，本 PR 内已修：

- `follow-redirects` 1.16.0 → 1.16.1（前端锁文件改 3 行）
- `certifi` 2026.5.20 → 2026.10.19，`pillow` 12.3.0 → **13.0.0**（大版本升级；用到 PIL / pdfplumber 的 63 个测试通过）
- 依赖升级后重新生成了 `backend/requirements.lock` 和两份 SBOM（`test_dependency_locks_and_cyclonedx_sboms_are_fresh` 会校验它们和锁文件一致）

**main 分支大概率会因为同样的漏洞变红**，可以单独挑出这三个提交（`1c02839`、`ac82dfd`、`edebc46`）先合入。

## 六、已知限制与建议跟进

| # | 项 | 影响 | 建议 |
|---|---|---|---|
| 1 | 没有做浏览器点按验收，图表布局也没有截图核对 | 视觉问题（标签重叠、窄屏换行）未知 | 按 runbook 的 9 条清单验收 |
| 2 | 没有在生产规模数据上做 `EXPLAIN ANALYZE` | “全部”区间、PN 分组（组数可能上千）的耗时未知；分组排序在 Python 里做 | 发布前在 staging 跑一次；超过 1 秒再考虑物化日汇总表（需另行决策） |
| 3 | `/amount-histogram` 后端已实现，前端没画 | 拖动条上方没有分布小图 | 可作为小迭代补上 |
| 4 | 分组键用「未填」表示空值 | 如果真有客户或销售叫“未填”，会和空值组合并 | 概率极低，必要时改用独立的空值标记 |
| 5 | 筛选候选（`/options`）在每次筛选变化时重新拉取 | 每次筛选多一次查询 | 量大时可以把 amount_range 拆成单独接口 |
| 6 | 销售人员是原文字符串，不做别名合并 | 写法不一致会被分成两组 | 需要的话另开主数据决策 |
| 7 | 回款视角（D-44 §6）没做 | — | 等销售回款导入适配后再决策 |
| 8 | 旧的 `customer` / `salesperson` 模糊参数仍然保留 | 只为兼容旧链接 | 稳定后可以下线 |

## 七、发布

见 [runbook](../releases/2026-10-21-sales-dashboard-d44.md)。要点：**无迁移、无回填、无写操作**；前后端必须同一个提交发布（新前端依赖新接口）；回滚即撤销本 PR 的合并提交并重新部署。合并 main 必须由杨金臣（@Jinchen-Yang）对候选 SHA 给出人工 APPROVED review（`docs/BRANCH_POLICY.md`）。

## 八、关键文档索引

- 决策：`docs/decisions/0022-销售看板分组趋势与金额区间.md`
- 方案（含 §九 实现差异）：`docs/销售看板_编码前设计方案_2026-10-21.md`
- runbook：`docs/releases/2026-10-21-sales-dashboard-d44.md`
- 术语：`CONTEXT.md`（订单金额区间、看板对比期、新客 / 老客、看板毛利）
- 前置决策：D-39（`0017`）、D-40（`0018`）、D-41（`0019`）

## 九、当前状态（2026-10-21 写作时）

- 最近一个前后端 CI **全部通过**的提交是 `ea96fdb`（后端第二批）。
- 之后的提交（行级口径、直方图与导出、前端、runbook）：前端检查在 `5e6bbe1` 上已通过；后端测试运行中，结果以 PR #442 的检查为准。本交接文档是纯文档提交，不影响测试结果。
- PR 没有评审意见；待办只剩 CI 跑完、点按验收和管理员批准。
