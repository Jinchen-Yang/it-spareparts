# 智能体工具手册

主仓库 `backend/app/agent/tools.py` 暴露给模型的 9 个工具。所有工具：

- 结果只来自库内真实数据；异常以 `{"error": ...}` 返回，模型应自恢复（换词重搜/向用户澄清），不要让对话中断。
- 每次调用都写审计日志（谁问了什么、查了哪个型号）。
- 输出经过 RBAC 字段可见性过滤——越权数据**根本不会出现在工具结果里**（见 [security-compliance](../skills/security-compliance/SKILL.md)）。

## 查询类

### search_parts — 型号近似搜索

按型号/品牌/描述近似搜索（pg_trgm），返回按匹配度排序的候选。
容错：连字符差异（4089RT vs 4089-RT）、大小写、多余后缀、中英品牌混写（super/超微）、历史别名。

- 入参：`query`（用户原话里的型号或描述）、`limit`（默认 10，最大 20）。
- 每条带 `score`(0~1) 与 `match_reason`。
- 危险状态：`low_confidence=true`（没有可靠匹配，候选列给用户确认，不要擅自选）；
  `ambiguous`（系列名命中多个具体规格，按 [part-identify](../skills/part-identify/SKILL.md) 消歧）。

### get_part_overview — 型号全景

某型号的完整全景：基本信息（描述/品牌/品类）、近 20 单采购（供应商/单价）、近 20 单销售（客户/单价）、
分仓库存、替代料、两种成本法（移动加权/FIFO）平均成本与毛利率、历史询价区间、近 90 天销售速率
（sales_velocity）、近期成交参考价（ref_sale_price）。

- 入参：`pn_std` 必须是 search_parts 返回的精确值，自己拼的会 404。
- 已合并型号自动沿 merged_into 链重定向，返回 `redirected_from`——要把"你查的 X 已合并到 Y"告诉用户。
- 报价、压价、解释型号都以它为依据。

### lookup_prices_bulk — 批量查价

询价单/整机拆解场景的核心工具。对每个型号文本做近似解析并返回：
最近采购价/日期、近 N 天（默认 15）采购价窗口（均/低/高/笔数）、近期加权成交参考价（ref_sale_price）、
近 90 天均售价、库存合计。

- 入参：`queries` 字符串数组（可以是客户原话写法）。**一次最多 60 个，超过分批**。
- 每项带 `status`：
  - `ok` — 唯一命中，已附价格；
  - `ambiguous` — 多规格变体，带候选列表，按批量消歧规则在备注标注；
  - `not_found` — 没找到。
- 同样带 `redirected_from`（PN 合并重定向标注）。

### get_profit_ranking — 利润聚合排名

维度三选一：`part`（按型号）/ `salesperson`（按销售员）/ `customer`（按客户）。
含营收、两种成本法的毛利与毛利率，按营收降序，最多前 50 行。可选 `date_from`/`date_to`（YYYY-MM-DD）。

- 🔴 销售角色调用 salesperson/customer 维度会被服务端拒绝（防恶性竞争），不要替用户绕。

## 文件类

### inspect_file — 看 xlsx 结构

返回 sheet 列表 + 每个 sheet 前几行原样数据（1-based 行号）。
客户文件格式千变万化——由模型自己判断表头在第几行、哪列是型号、哪列是数量，**不要假设固定格式**。

### read_file_rows — 分页读 xlsx

`file_id` + 可选 `sheet`（省略=第一个）+ `start_row`（默认 1）+ `max_rows`（默认 50，最大 200）。
先 inspect 再读；行多就分页读完，不要只读一页就开始干活。

### read_document — 读任意上传文件全文

Word/PDF/txt/Excel/图片均可；图片和扫描件 PDF 自动走视觉识别（Qwen-VL，未配置 key 时返回降级提示，
此时让用户提供文字版）。整机拆解场景用它拿全文。
Excel 要按行列精确定位用 inspect_file/read_file_rows，要整体内容用本工具。

## 产出类

### write_excel — 写 Excel（回填/自建）

绝不改写原上传件，总是产出新 file_id + `download_url`。

- 回填客户模板：传 `base_file_id`，在副本右侧空列追加（原格式保留）；
- 新建：不传 base_file_id，自己规划表头和数据行。
- `cells`：`[{row, col, value}]`，1-based 行号 + 列字母或数字，**最多 3000 个**。
- 完成后必须把 `download_url` 告诉用户。

### write_report — 美化 Excel 报表

表头配色、边框、自适应列宽、金额千分位格式、冻结表头、斑马纹；
备注含"需确认"/"未找到"的行自动标橙/红。整机拆解报价单、批量查价结果优先用它（比 write_excel 好看）。

- `headers` 列名数组 + `rows` 对齐的二维数组 + `money_cols`（金额列 0 基下标）+ 可选 `title`/`output_name`。
- 完成后必须把 `download_url` 告诉用户。

## 工具选择速查

| 需求 | 工具 |
|---|---|
| 用户提到一个型号 | search_parts →（确认后）get_part_overview |
| 一串型号 / 询价单 | lookup_prices_bulk（>60 分批） |
| 上传了 Excel 要回填 | inspect_file → read_file_rows → lookup_prices_bulk → write_excel(base_file_id) |
| 上传了 Word/PDF/图片配置单 | read_document → 自行拆件 → lookup_prices_bulk → write_report |
| 生成对外好看的报价单 | write_report |
| 谁卖得多/哪个客户毛利高 | get_profit_ranking |
