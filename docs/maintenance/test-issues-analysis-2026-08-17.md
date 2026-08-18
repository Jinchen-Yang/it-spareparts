# 本地实测问题清单 · 详细分析（2026-08-17）

> **背景**：生产数据库完整备份（483MB，2026-08-17）已恢复至本地 `spareparts-dev-db`（127.0.0.1:5433/spareparts_dev），迁移升级至 `a4c6e8f1b2d3`；前端 vite :5176 + 后端 uvicorn :8000，admin/admin888。在**真实生产数据**上逐页实测发现 9 个问题。
> **文档定位**：每条 = 背景 → 现状证据（代码/数据）→ 思维链 → 方案建议 → 确认点。

---

## #1 维保主页项目卡排序：按成本率降序

### 背景
项目卡墙（维保主页）当前排序方式无法直接看出"哪个项目最亏钱/最需要关注"。

### 现状证据
- `backend/app/api/maintenance_boss_board.py:70`：`sort` 枚举仅支持 `attention|orders|name|known_cost`，**没有成本率排序**。
- `backend/app/services/maintenance_boss_board.py:536`：排序实现在服务层，`known_cost` 按已知成本降序（只看成本不看合同额）。
- 看板返回行含 `cost_ratio_pct`（成本÷合同额，`ready/not_imported/restricted` 三态），前端已有三色进度条（`STATUS_COLOR: normal/warning/alert`，#35 口径）。

### 思维链
成本率（成本÷合同额）是业务最关心的经营信号：>100% 亏钱、80-100% 低盈利。当前有字段但无排序 → 加一个 `sort=cost_ratio` 即可，排序键 = `cost_ratio_pct` 降序；`not_imported`（合同额缺失）排最后且不按 0 算（铁律 5：不知道≠0）。前端加排序下拉，与现有 attention/成本 排序并列。

### 方案
- 后端：`_SORTS` 增加 `cost_ratio`（`cost_ratio_pct.value` 降序，NULL/not_imported 沉底，合同额缺失不参与）。
- 前端：项目墙加「按成本率」排序选项。

### 确认点
① 成本率高的排前（降序），确认？② 已结束（ended）项目也参与该排序，还是只看进行中？

---

## #2 总表 V2 模板：逐一核对表头 ↔ 数据库字段

### 背景
项目总表（表 6）要从当前实现切到 V2 模板（`docs/maintenance/templates/维保项目工作簿模板_v2.xlsx`）。V2 是甲方/业务新定版式，需逐列核对映射后才能改导出/导入。

### 现状证据（V2 模板实测结构）
| Sheet | 列数 | 表头 |
|---|---|---|
| 00_项目总览 | 6 | 维保项目总览（合并单元格版式） |
| 01_基础信息 | 5 | 字段/值/来源/最后更新/备注 |
| 03_备件明细 | **26** | 维保单号(WBDD)/制单日期/销售订单(XSDD)/项目名称/需求类型/出库仓库/销售人员/业务类型/序号/产品编号(PN)/产品描述/需求数量/已知成本参考(含税)/单价(未税)/合计/发货SN/行成本单价/行成本金额/成本来源/**置信度** |
| 04_报销 | 11 | 同现有（未税金额（回填）/含税金额（系统）/备注（回填）） |
| 05_回款 | 7 | 同现有（操作/合同编号/报告月份/累计回款金额（含税）/回款凭证号/状态（系统）/备注（回填）） |
| 06_领用与返还 | 10 | 现场领用单号/领用日期/PN/备件SN/领用数量/是否应返还（回填）/**应返数量（系统）/返还状态（系统）/返还单号（系统）**/备注（回填） |
| 98_字典 / 99_元数据 | 2 | 字段/允许值；key/value |

对照现状实现（`maintenance_project_master_workbook.py`）：当前 03 为 14 列（`_PARTS_HEADERS`），V2 扩到 26 列，新增：需求类型、销售人员、业务类型、序号、已知成本参考(含税)、单价(未税)、合计、发货SN、行成本单价、行成本金额、置信度。

### 思维链
V2 的 26 列与现有字段关系：
- 维保单号→`FMaintenanceOrder.order_no`、制单日期→`order_date`、销售订单→`linked_sales_order_no`、项目名称→`project_raw` ✅ 已有
- 需求类型/销售人员/业务类型→需确认来源（订单头或台账，`FMaintenanceOrder` 是否有对应列待查）
- 序号→导出时生成（行号）
- 已知成本参考(含税)→`known_apply_cost_inc_tax`（boss-board 同口径）
- 单价(未税)/行成本单价/行成本金额→`unit_cost_ex_tax` / `unit_cost_inc_tax` 及其 override（`MaintenanceManualCostOverride`）
- 成本来源→`cost_source`（枚举见 #3）✅
- **置信度→由 cost_source 映射**（正好承载 #3 颜色标签）
- 06 的应返数量/返还状态/返还单号→`MaintenanceSiteIssueLine` 与返还单关联（需确认当前 06 sheet 未含，V2 新增）

### 方案
1. 按 V2 重写 `build_project_master` / `_sheet_parts` / `_sheet_site` 导出。
2. 逐列写映射契约文档（表头→表→列→口径），review 通过后实现。
3. 导入/apply 侧：只放开 V2 标注「（回填）」的列（未税金额、备注、是否应返还等），系统列（含税金额（系统）、状态（系统））保持只读。

### 确认点
① 26 列里哪些是**回填可编辑**列（我建议只有标注「（回填）」的列）？② 需求类型/销售人员/业务类型三列的数据源？（订单头？台账？）

---

## #3 备件成本 tab：显示列 + 成本来源颜色标签 + 无成本排查

### 背景
备件成本 tab 需要按 V2 口径展示行级数据，并用颜色表达成本来源可信度；同时存在"无成本"行需要定性。

### 现状证据
- 成本来源枚举（`maintenance_cost.py` 实测）：`direct / window / pool_purchase / purchase_history / sales_history / pool_sales / month_avg / none`，另有 NULL（起算日前不计价）。
- **生产数据分布（2026 YTD，1.4 万行）**：
  ```
  window 3705 | direct 3608 | pool_purchase 1779 | month_avg 1284
  none 1231 | purchase_history 997 | sales_history 853 | (NULL) 813 | pool_sales 2
  ```
  → **无成本 2044 行（none 1231 + NULL 813），约占 14%**，不是 bug 是全量数据常态。
- 成本瀑布（`maintenance_cost.py`）：采购价→7 天窗口→历史采购→池采购→销售回退→月均→留空。**无成本 = 瀑布全miss + 无人工回填**。

### 思维链
- "无成本" = 系统按瀑布取不到价且无人工回填（`MaintenanceManualCostOverride` 无记录）→ **不是 bug，是数据缺口**，解法 = ①池均价/缺失成本参考兜底（已有 `pool_purchase` 等）②人工回填入口（已有 03 sheet 黄底补价）③补池。
- 颜色标签按**证据强弱**排序：direct > window > purchase_history/pool_purchase > sales_history/pool_sales > month_avg > none/NULL。
- 显示列按用户要求：维保单号、制单日期、PN、产品描述、需求数量、出库仓库、成本来源、未税单价、含税单价（与 V2 03 列的子集一致，避免两处口径打架）。

### 方案
1. `PartsTab` 列收敛为上述 9 列。
2. 新增 `CostSourceTag` 组件 + 颜色规范写入 `docs/maintenance/frontend-design-standards.md`（绿→橙→红→灰）。
3. 无成本：区分 `none`（算了没算出来）与 NULL（起算日前）展示文案；给出补价引导。

### 确认点
① 颜色分级认可？② 无成本行要不要显示"建议补价"快捷入口？

---

## #4 购物车自动审核规则

### 背景
补库申请目前三查（`replenishment_screening.screen`）只记录不裁决，需要改为**系统自动审核**：池内放行、非池正常审、2 项不过整单打回、打回可重选、不合格标红并推荐相似 PN。

### 现状证据
- `replenishment_screening.py:43`：`CHECK_KEYS = ("pool_membership", "recent_activity", "niche_pn")`，`CheckResult.all_passed` 已有判定能力。
- `submit_application_atomic`（Issue #260）：提交时已冻结 `screening_json`（含 checks 与 anomaly_count）。
- 现状 `can_review=False`、`workflow_mode="system_screening"`——已经是"系统三查"语义，但没有"自动打回/推荐"逻辑。
- **相似 PN 推荐已有成品**：`part_resolver.py`（智能体核心回路第一环）——`_PART_SQL` 四路召回（token 相似 / word_similarity / 双向包含 / search_doc 检索）+ `_doc_recall`（变体词组命中词数降序，≤3 词全中、≥4 词允许错 1）+ 别名召回折叠 pn_std + `similar_items` 降级区。**直接复用其 resolve 结果即可**（需 #5 词边界修正保证召回可靠）。

### 思维链
自动审核规则可直接建立在冻结的 `screening_json.checks` 上，无需重新计算：
1. `pool_membership.passed`（在互通池）→ 该行**直接通过**（池内已有人工治理证据）。
2. 非池行走 `recent_activity` + `niche_pn` 判定：样本足够+价格合理 → 通过；样本不足/异常 → 不过。
3. 单内 **≥2 行不过 → 整单 status=needs_revision 自动打回**（不经过人工）。
4. 打回行标红 + **相似 PN 推荐**：调 `part_resolver` 候选（描述/规格模糊，`word_similarity` 精排），**优先过滤出互通池内成员**作为推荐（池内 = 已有人工治理，推荐可信）。
5. 打回后前端允许"处理打回条目"：移除不合格行、选推荐 PN、重新提交（现有 revision 流程已有雏形，需接通自动打回状态）。

### 确认点
① 打回后：保留合格行 + 只重选不合格行（推荐做法），还是整单重来？② 推荐相似 PN 的数量上限（3 个？）③ "2 项不过"是"≥2 行"还是"≥2 个不同 PN"？
**✅ 已确认（2026-08-17）**：相似 PN 推荐复用 `part_resolver` 候选逻辑。

---

## #5 搜索词边界："8t" 不应召回 "1.8t"

### 背景
补库选购 PN 的搜索是模糊匹配，词内子串命中导致噪声。

### 现状证据
- `replenishment.py:340-342`：`DimPart.pn_std.ilike(pattern)` / `description.ilike` / `brand.ilike`——`%q%` 全子串匹配，**无词边界**。
- 用户实测："8t" → 召回 "1.8t"（子串命中）。

### 成品搜索方案（已存在，仓库其他模块同源）
`backend/app/services/query_filters.py` 是**成品词元搜索**，注释原文即针对本问题：

| 函数 | 能力 | 证据 |
|---|---|---|
| `keyword_term_groups` | 分词 + 变体展开 | 丢弃单拉丁字母/数字噪声，保留单 CJK 字（"三"=三星）；`8TB/8T` 两种写法都收、`7.2K→7200`、`7200rpm→7.2K`、`GB/s→Gbps`、英寸中英文 |
| `keyword_groups_or_substr` | 兜底 | 查询全被丢弃时整串一个词组，避免零过滤全表返回（审计 P1） |
| `col_matches_any` | **左词界正则** | `(^|[^0-9.]){词}` 大小写不敏感 `~*`——注释原文："防子串误命中：'6TB' 不再命中 '16TB'/'1.6TB'、'6Gb' 不再命中 '16Gb'" |

配套基础设施：
- `DimPart.search_doc`（`models/dimensions.py:56`）：**STORED 生成列** `pn_std + 紧凑PN + brand + category_major/minor + description`，PN 改名/主数据编辑后库自动重算；带 **GIN trgm 索引**（`ix_part_search_doc_trgm`）。
- `part_resolver.py`：`_doc_recall` 在 search_doc 上按变体词组 `col_matches_any` 命中、命中词数降序（≤3 词全中、≥4 词允许错 1）；`_PART_SQL` 四路 pg_trgm 召回（GIN 毫秒级）。

使用方（同源）矩阵：

| 模块 | 成品搜索 |
|---|---|
| 型号查询 `part_overview.search_parts` | ✅ |
| 采购查询 `purchase_query.py` | ✅ |
| 库存查询 `inventory.py` | ✅ |
| 成本取价 `maintenance_cost.py` | ✅ |
| PN 解析/池身份 `part_resolver.py` | ✅（+similarity 精排） |
| **补库 `replenishment.catalog_search`** | ❌ **唯一未接入，仍在裸 ilike** |

### 思维链
`%8t%` 命中任何含 "8t" 的位置——但**解决方案不需要新发明**：
1. 补库是**唯一**没接成品词元搜索的模块 → 最小改动 = `catalog_search` 的 q 过滤替换为 `keyword_groups_or_substr(q)` 分词 + `col_matches_any(DimPart.search_doc, g)` 左词界匹配，与型号查询/库存/采购同源。
2. 一个列（search_doc）覆盖 PN/品牌/品类/描述，GIN 索引保证毫秒级，无需逐字段 ilike。
3. 兜底语义保留：搜单个字符/数字（如 "8"）退化为整串词组，左词界仍防止 "18"/"1.8" 误命中。
4. 排序沿用现状（pn_std 字典序 + 池/价格事实），召回质量提升后补库页即受益；#4 推荐复用 `part_resolver` 时也自动获得相同词边界。

### 确认点
**✅ 已确认（2026-08-17）**：#5 用 `query_filters` 成品方案（`keyword_groups_or_substr` + `col_matches_any(search_doc)`）替换补库 ilike；#4 相似推荐复用 `part_resolver` 候选逻辑。

---

## #6 购物车云端暂存（刷新不丢）

### 背景
当前购物车行是前端内存状态（`draftLines` useState），刷新即丢。需要云端暂存。

### 现状证据
- `ReplenishmentBetaPage.tsx`：`draftLines` 为组件 state，提交前不落库。
- 后端 `submit_application_atomic` 是一次性原子提交（Issue #260），**没有中间草稿状态**。

### 思维链
两种做法：
- **A. 新表暂存**（推荐）：`replenishment_cart_draft`（owner, project_id, lines JSONB, updated_at），每次增删改行自动 PUT；进入页面 GET 恢复；提交成功清空。刷新/换设备不丢。
- B. 复用 application draft 状态：与 #260 的"无草稿"设计冲突，不推荐。

### 确认点
① 暂存粒度：每人一份全局草稿，还是按项目多份草稿？② 暂存是否也存 client_request_id（防重复提交）？

---

## #7 购物车卡片改窄横条

### 背景
加入购物车后的行卡片信息过重（含半年采购/销售），挤占页面。

### 现状证据
`ReplenishmentBetaPage.tsx` 的 `draftLines` 渲染为完整卡片（PriceFacts 两行 + 数量/备注/移除）。

### 思维链
选购区已经展示完整价格事实；购物车行只需：PN（强）、数量（可调）、备注（可调）、移除。改为窄横条 = 单行布局，减少视觉噪音，信息密度与"待提交清单"语义一致。

### 方案
`DraftLineRow`：PN + 描述截断 + 数量 InputNumber + 备注 Input + 移除按钮，一行排布。

### 确认点
无，直接做。

---

## #8 回款计划 sheet → 分期提醒

### 背景
项目工作簿需加「回款计划」sheet，预填**回款日期 + 对应金额**；系统读取该表，在到期前提醒对应人员（负责人，见 #9）回款。

### 现状证据
- 台账已有 `02_回款计划` sheet（`maintenance_ledger.py:148`：台账回款计划行，旧 24 组横向对展开 / 新版 02_回款计划 sheet）→ **计划数据已存在**。
- `maintenance_collection_reminders.py` 已有完整提醒状态机：`needs_review > handled > incomplete > overdue > due_this_month > upcoming`、`derive_reminder_state`、下一条节点优先级（`overdue > due_this_month > incomplete > upcoming`）。
- 项目工作簿 V2 模板**没有回款计划 sheet**（00/01/03/04/05/06/98/99）。
- `project_manager_id` 当前为自由文本（#9 要改成账号）。

### 思维链
1. **数据源**：回款计划唯一事实源 = 台账 `02_回款计划`（REQUIREMENTS #31 已定，不重复录入）→ 项目工作簿只读展示计划行（回款日期、金额、合同）。
2. **提醒**：复用 `maintenance_collection_reminders` 状态机，绑定负责人账号（#9 修好后）→ 到 `due_this_month`/`overdue` 时给负责人站内提醒。
3. 前端展示：项目面板回款 tab 加"回款计划"子区（只读，日期+金额+状态徽标）。

### 确认点
① 提醒载体：站内待办/提醒页（现有）够不够，还是要邮件/企微推送？② 提前几天触发"即将到期"提醒？

---

## #9 项目负责人改为账号选择

### 背景
编辑项目基本信息时负责人是手敲文本，无法关联系统账号 → 无法做定向提醒（#8 前置依赖）。

### 现状证据
- `maintenance_project.py:34`：`project_manager_id: String(64)` 自由文本。
- `EditBasicsButton`（项目面板）用 `<Input>` 手敲。
- `SysUser` 91 个账号（生产数据）。

### 思维链
改为下拉选择 `SysUser`（active），保存 `project_manager_id = user.username`；后端校验存在性。旧数据（手敲值）保持原样可读，展示时若匹配不到账号显示原文+「未关联账号」提示。

### 确认点
无，直接做（下拉源 = active 用户列表，含搜索）。

---

## #10 上传解析入口：维保 roundtrip 的 multipart part size 限制与文件体积上限不一致（阻断正常文件）

### 背景
维保回填导入上传是通过 `MultiPartParser` 解析 `.xlsx`，但当前把 `max_part_size` 设成 1024 字节。

### 现状证据
- `backend/app/api/maintenance.py:1110-1114`：`max_files=1, max_fields=0, max_part_size=1024`。
- `_save_roundtrip_upload` 使用 `config.MAX_UPLOAD_MB` 作为文件上限，并逐块读取完整文件保存到临时文件。

### 思维链
`max_part_size` 限制的是单个 multipart part 大小。Excel 文件 part 常见远大于 1KB。这里即使总文件上限是几 MB，也会在表单解析阶段被拒绝，导致入口不可用（与后续写库无关）。

### 结论
这是高优先级阻断缺陷：上传约束与业务上限冲突。

### 建议
将 file part 的 `max_part_size` 调整为与 `config.MAX_UPLOAD_MB` 一致或与仓库导入共享统一上传预检策略。

### 确认点
① 是否允许把 text 字段与文件字段区分开分别设置上限？② 该入口是否保留 1MB 级更细粒度的防过载保护。

## #11 仓库导入/预检入口：`max_part_size=16KB` 同样过小，上传可复现失败

### 背景
仓库单据预览/应用路径的 `_multipart` 也对 file part 使用了固定 16KB 上限，远小于常规 `.xlsx` 文件体积。

### 现状证据
- `backend/app/api/maintenance_warehouse.py:130-134`：`max_files=1, max_fields=2/0, max_part_size=16 * 1024`。
- 文件上限使用 `config.MAX_UPLOAD_MB`，明显与单 part 上限不一致。

### 思维链
该配置会导致真实导入文件在解析阶段失败（413 或格式错误），前端/后端均难以复现“偶发”，而是与文件实际体积相关的稳定失败。

### 结论
这是与 #10 同类的高可见度配置缺陷，应与业务上限统一。

### 建议
将 `max_part_size` 调整为可落地的实际上限，并复用同一套 upload 上限常量，避免同类缺陷再发。

### 确认点
① 预检与应用共用同配置可接受吗？② 是否需新增单测覆盖 `.xlsx` 大于 16KB 的解析场景。

## 实施顺序建议

| 波次 | 项 | 理由 | 方案状态 |
|---|---|---|---|
| 第一波（小/独立/快） | #1 排序、#7 窄横条、#9 负责人选择 | 各自独立，单文件改动 | 待确认 |
| 第二波（中） | #3 成本 tab+标签+无成本、#5 搜索边界、#6 云端暂存 | 需要新组件/新表，但范围可控 | **#5 ✅ 已确认**（复用 query_filters 成品） |
| 第三波（大） | #2 V2 模板、#4 自动审核、#8 回款提醒 | 跨前后端+契约+测试，且 #8 依赖 #9，#4 依赖 #5 | **#4 推荐部分 ✅ 已确认**（复用 part_resolver） |

> 依赖关系：**#8 ← #9**（提醒需要真实账号）；**#4 ← #5**（相似 PN 推荐依赖可靠搜索，已确认 #5 复用 `query_filters`、#4 复用 `part_resolver`）；**#3 ← #2**（列口径与 V2 一致，避免两处打架）。
