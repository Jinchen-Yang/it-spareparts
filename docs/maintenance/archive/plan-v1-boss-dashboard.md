# 维保备件展示板 v1.1 可执行计划（2026-08-16 修订）

> v1.0 经独立复审判定 No-Go，本版为修订稿：补 M0 决策、纠正 M1/M2/M4 数据契约、
> 重排里程碑为 M0→M1→M2→M4→M3→M5，并补最低通过标准。
> 本版只修改计划文档，不动代码。

---

## 0. 定位与铁律（不变）

后事实展示板：定期上传 → 归集 → 成本回填 → 展示。铁律：
1. 可信事实链：需求单（申请）→ 发货单（实发）→ 返库单/入库单（收回），按 WBDD 单号/XSDD/项目名勾稽；
2. 需求单流转状态列原样展示、不计算、不标注；
3. 成本复用现有五层取价瀑布，只接线不重写；
4. 权限 = 勾选名单制 + 专用动作键（见 §4）；
5. 每周/每月上传，当天最新即可。

**待 M0 拍板的四个关键口径**（未拍板前不得进入 M3 设计）：
A. 老板每周/月据此**具体做什么决定**（哪个项目超支？哪个货积压？哪个该返还没还？）——决定异常队列的内容；
B. 项目经理看**本人项目还是全部项目**——决定权限矩阵的第二维；
C. 「申请估算成本」与「项目已计成本」的正式名称与展示位置——决定字段命名与口径文案；
D. 事实粒度：v1 定为**项目＋PN 聚合**（不做需求明细行级分配，见 §5），除非业务坚持明细级。

---

## 1. 基线数字（生产 2026-08-14）

| 指标 | 值 |
|---|---|
| 需求单/明细行 | 19,046 单 / 38,483 行（2023-07-03 ~ 2026-08-14，历史快照母集） |
| 2026-01-01 至今需求单/明细 | 6,903 单 / 14,272 行（= 母集在 YTD 窗口的子集） |
| 2026-08-16 新导出文件 | 6,913 单头 / 6,633 明细行（上传快照，与生产差 8-15/8-16 两天增量） |
| 项目数 | 生产 maintenance_project 415 个（跨年度全量）；2026 新导出含 226 个项目名（YTD 有单项目） |
| XSDD 挂载/销售命中 | 100% 挂 / 99.3% 命中 |
| 成本覆盖 | 88.7%（五层来源分布：direct 11,600 / window 8,807 / pool_purchase 4,813 / none 3,461 / month_avg 3,448 / purchase_history 2,818 / sales_history 2,628 / 空 884 / pool_sales 24） |
| 通用池命中（YTD） | 行级 50.4%（7,187/14,272）；PN 级 21.5%（619/2,883）；active 池 98.6% |
| 发货↔需求单（真实样例） | 99.8%（5,701/5,712 维保供货头，按 WBDD 单号） |
| 返库单（真实样例） | 宽版 3,019 单头 / 7,102 明细行；窄版 27 行；测试结果 成品 6,703 / 坏品 399 |
| 预交付 | 需求单项目名带「预交付-」前缀（YTD 新导出 38 个此类项目名） |
| 领用/返还/坏件/回款 | 生产 0 行（v1 不依赖） |

---

## 2. M0 决策冻结（第 0 周，纯业务沟通）

1. 产出《口径确认单》：§0 的 A/B/C/D 四个问题逐条书面确认（业务签字）；
2. 好件/坏件正式数据源确认：**未用件=返库单 return_order（成品）、坏件=入库单
   rkd_inbound（坏品/废品）**；RKD 未上传时坏件显示 `not_imported`，不显示 0；
3. 展示口径确认：「已知申请估算成本（含税）」，拆 actual/estimated/missing/
   coverage/quality 五个子字段；缺价显示「不完整/已知下限」；
4. 上传节奏确认：快照式全量重传 vs 增量追加；作废单/草稿单的处理（逻辑删除规则）。

---

## 3. M1 WBDD 数据底座（第 1 周）

### 3.1 导入契约修正（复审 P0-1）

- **复用双表头契约**：`HeaderPair(position, internal_code, business_label)`
  （见 `maintenance_warehouse_adapters.py:52`），头段与明细段**分开解析**；
- **只对 WBDD adapter 放行重复业务名**（新 91 列布局「需求数量/备注/图片/附件」
  头、明细重名）：reader 的重复列校验只记录不抛错（`etl/reader.py:769` 已有此语义），
  transform 按位置段（头段/明细段）取列，不按全局名称；
- 布局探测：先找「需求明细.数据ID(不可修改)」列位置 D；D<44=新布局
  （头段 [0..6]∪[44..90]，明细段 [7..43]），D≥44=旧布局（头段 [0..52]，明细段 [53..89]）；
- **保留「有单头、无明细」订单**：现 transform 在无明细数据ID时整行跳过
  （`etl/transform.py:218` 附近），改为单头入库、明细 0 行，计入「无明细清单」；
- 快照/增量/逻辑删除：按 M0-4 结论实现（默认：全量快照 upsert + 同文件重传幂等；
  数据状态=已取消 保留历史事实，仅标记）。

### 3.2 补全字段（列名级清单，数目标注为 34/28）

**f_maintenance_order 新增（34 列，全 nullable）**：
制单人员 created_by_raw、采购员 purchaser_raw、项目经理人员 project_manager_staff_raw、
项目经理 project_manager_raw、合作伙伴人 partner_raw、维保负责人 maintainer_raw、
维保工单 work_order_no、协同销售人员 co_salesperson_raw、销售部门 sales_dept_raw、
采购人员 purchaser2_raw、仓管员 warehouse_keeper_raw、仓储中心 storage_center、
仓库 warehouse_raw、是否变仓库 change_warehouse_flag、变更仓库 change_warehouse、
变更仓承办人 change_warehouse_handler、仓库承办人 warehouse_handler、
供货期限 supply_deadline、选择收货地址 delivery_address_option、收货人 receiver、
收货人电话 receiver_phone、收货地址 receiver_address、快递单号 express_no、
快递单号# express_no2、图片 image_urls、附件 attachments、
整机需采备件校验 whole_machine_check、需求数量(头) head_demand_qty、
需采数量(头) head_purchase_qty、已发货数量 head_shipped_qty、
已返货数量 head_returned_qty、是否可以接受通用号 accept_generic_flag、
创建时间 created_at_raw、修改时间 modified_at_raw。

**f_maintenance_line 新增（28 列，全 nullable）**：
退返旧件 return_old_part、各仓库存 warehouse_stock_raw、个别调整发货仓
adjust_warehouse_flag、调整仓库 adjust_warehouse、发货仓库 ship_warehouse、
发货仓ObjectID ship_warehouse_object_id、调整仓储中心 adjust_storage_center、
调整库管员 adjust_keeper、发货库存 ship_stock、变更仓需采数量
change_warehouse_purchase_qty、需采数量 purchase_qty、整机需采备件
whole_machine_purchase_part、整机备件已采 whole_machine_part_purchased、
需采备件说明 purchase_note、备注 line_note、整机/备件 whole_or_part、
图片/附件 line_image_urls、已采数量 purchased_qty、待采数量 pending_purchase_qty、
直采直发数 direct_ship_qty、库房需发数 warehouse_need_qty、
库房发货数 warehouse_shipped_qty、已供数量 supplied_qty、待供数量
pending_supply_qty、已返数量 returned_qty、待返数量 pending_return_qty、
领用数量 consumed_qty、需求待返数 demand_pending_return_qty。

（已存在不重复建：qty=需求数量、return_qty=退货数量、serial_numbers=发货SN。）

### 3.3 专用上传端点与权限（复审 P0-6）

- 新端点 `POST /api/maintenance/wbdd-import`（maintenance-only），
  只接受 WBDD 文件类型，**不复用 `/api/imports/upload`**（那是 page_import 全家桶）；
- 新动作键 `action_maintenance_wbdd_import`（默认仅 admin/名单勾选人员）；
- 全项目范围 vs 本人项目范围**独立授权**：看板数据范围用独立开关
  （`page_maintenance_boss` 全范围 / `page_maintenance` 本人范围），
  `action_*` 只用于上传等写操作；
- 上传后自动触发 maintenance_cost.recompute（沿用现有后置刷新链，确认触发条件）。

### 3.4 Reconciliation（验收即对账，不设 ±2%）

- 冻结快照下：上传文件 单头数/明细行数/Σ需求数量/Σ需采数量 与解析结果精确相等；
- 与生产母集按 order_no 精确对平：覆盖数、新增数、行数差=0；
- 成本 recompute 后：有成本行数、五层来源计数与 §1 基线**精确一致**（确定性计算，
  不允许 ±2% 容差）。

### 3.5 测试清单

1. 91 列新布局：头 6,913/明细 6,633；重名「需求数量」头→head_demand_qty、明细→qty；
2. 90 列旧布局：14,988 头/29,140 行，与改前解析一致；
3. 有单头无明细 → 单头入库 + 无明细清单计数；
4. 空值/全角/千分位/超长截断/坏行 error 计数；
5. 幂等重传：行数不变、新列覆盖、成本列不覆盖（upsert 白名单）；
6. 权限：WBDD-only 账号上传 WBDD 成功；采购/销售/库存/报销文件上传均 403 零写入。

---

## 4. M2 稳定项目归属（第 1-2 周）

1. **复用现有归属模型**：`maintenance_source_order_assignment`
   （ADR `docs/adr/0002-manual-source-order-project-assignment.md`：
   名称只是线索、稳定 ID 才是身份）——WBDD→项目 归属走该表，不新建第二套关系；
2. 名称规则只产生**候选**：「预交付-」剥前缀 + 台账项目编码匹配 → 候选列表，
   人工确认后才写 assignment；多候选不自动；
3. **保留未归属桶**：无 assignment 的 WBDD 进「未归属」清单，不静默丢；
4. 项目主数据（改名/期限）走台账导入 + 现有 project 维护端点，不新增表；
5. 测试：候选生成率报告（对生产 415 项目只读跑一遍，出报告不写库）、
   确认/解除幂等与审计、未归属桶计数。

---

## 5. M4 正确事实接线（第 2 周，M3 之前）

1. 三个来源分别接入、**各自独立 readiness**：
   - 实发：CKD 发货单维保供货明细；
   - 未用件收回：返库单 return_order（成品）；
   - 坏件回收：入库单 rkd_inbound（坏品/废品，类别白名单=返件类，业务已确认）。
2. 每个来源在展示上返回：`as_of`（该源最新已导入批次业务日期）、`readiness`
   （not_imported / partial / ready）、`batch_id`；**未导入显示 not_imported，
   绝不显示 0**；
3. 粒度：**项目＋PN 聚合**（复审结论：CKD/RKD 无明细行级键，需求单重复 PN
   无法行级分配；v1 不做明细行分配，重复 PN 场景输出「歧义清单」人工看）；
4. 未关联处理策略（整批失败 vs 部分应用）在 M0 拍板；默认：关联失败行进
   「未关联清单」+ 批次标记 partial，不整批失败；
5. 自报列（head_shipped_qty 等）与事实表数字并排展示，差异只提示不拦截；
6. 测试：三源真实样例全量导入幂等重传；readiness 状态机；歧义清单计数；
   上传顺序无关性（先传 RKD 后传 WBDD 也能关联）。

---

## 6. M3 决策看板（第 3 周，最后建设）

### 6.1 首屏结构（复审 P0-7）

`来源健康 → 本期变化 → 需关注事项 → 全项目列表（分页表格） → 单据/PN 证据下钻`
1. **来源健康**：四源 readiness（WBDD/CKD/return_order/RKD）+ 各自截止日期；
2. **本期变化**：orders_ytd / lines_ytd / 已知申请估算成本（含税）环比；
3. **需关注事项（异常队列，≤10 条）**：内容由 M0-A 拍板（候选：本期超预算项目、
   池归档件仍在流转、待返件多、无参照价占比高、未归属单）；
4. **全项目列表**：卡片只显示前 6-12 个重点项目（按需关注排序），其余进
   服务端分页表格（page_size 默认 20）；
5. 证据下钻：项目 → 单据 → PN 行，逐层服务端分页。

### 6.2 API 契约（修正版）

- 时间窗不写死年份：`orders_ytd / lines_ytd`（窗口参数 from/to，默认本年）；
- 成本字段改名固定为「已知申请估算成本（含税）」并拆五子字段：
  `actual / estimated / missing / coverage_pct / quality`；缺价显示
  「不完整/已知下限」，**绝不按 0 计**；
- 六种空值状态：`restricted / not_imported / partial / ready / stale / error`
  （权限不可见 / 未导入 / 部分关联 / 就绪 / 数据过期 / 接口失败），
  前端逐状态区分展示，不共用 null；
- 项目集合口径写明：默认「全量项目（415）+ 未归属桶」，YTD 有单项目加标记；
- 全部列表接口：服务端分页/筛选/排序，p95 门槛（见 §8）。

### 6.3 权限与脱敏（复审 P0-6）

- 查看：`page_maintenance_boss`（全范围）/ `page_maintenance`（本人范围）；
- 金额/成本：data_profit / data_purchase_cost 分别控制；
- 无成本权限时：金额、来源、覆盖率、排序、排名**全部不可见**，不得形成侧信道；
- 三类账号 HTTP 矩阵：老板全范围 / 项目经理本人范围 / 无权限账号。

### 6.4 前端

- 页面：BossOverviewPage（首屏结构如上）、项目下钻页（单据/PN 证据，分页表格）；
- 表格列（PN 行）：PN | 描述 | 需求 | 需采 | 已采 | 待采 | 直采直发 | 库房需发 |
  库房发货 | 已供 | 待供 | 退货 | 已返 | 待返 | 领用 | 需求待返 | 已知申请估算成本(含税) |
  取价来源 | 通用池 | 池名 | 发货SN；
- 通用池列：active 池标记「通用」、archived 池黄色警示；
- 导航四个入口：老板看板 / 项目列表 / 上传（需求单+三单）/ 项目维护。

---

## 7. M5 灰度与生产闸门（第 3-4 周）

1. 冷备 + 从生产快照建灰度库；
2. **最小迁移链**：从分支 23 提交中只提取本计划所需迁移（M1 新列迁移为纯加法、
   向前兼容），其余冻结能力**不随本次发布**——增加独立后端 feature flag
   （如 `maintenance_boss_dashboard_enabled`），看板与上传端点受该 flag 控制；
3. 迁移演练：灰度升级 + 旧应用连新 schema 兼容性验证（新增列全 nullable）；
4. 真实账号权限矩阵演练（三类账号）；
5. **回滚 = 关闭 feature flag + 旧应用兼容新 schema**；不做 downgrade 回滚；
   灾难恢复走已演练的备份；
6. 发布须过：精确 SHA 独立复审、完整 CI、迁移演练、备份恢复演练、灰度观察期。

---

## 8. 最低通过标准（摘复审结论）

- 冻结快照下订单/明细/金额/成本来源**精确对平**（确定性计算不允许 ±2%）；
- 项目汇总 + 未归属桶 = 全局有效 WBDD 母集；
- WBDD-only 账号能上传 WBDD；采购/销售/库存/报销文件均 403 且零写入；
- 老板全范围/项目经理本人范围/无权限三类 HTTP 矩阵通过；
- 无成本权限时金额、来源、覆盖率、排序、排名无侧信道；
- 415 项目 overview/detail 服务端分页，p95 性能门槛达标（列表 p95 < 800ms，
  明细 p95 < 1.5s，以灰度实测为准）；
- 四源各自显示截止与 readiness，未导入不得伪装成 0。

---

## 9. 工期（修订）

| 里程碑 | 人日 |
|---|---|
| M0 口径冻结 | 1-2 |
| M1 数据底座 | 6-8 |
| M2 稳定归属 | 4-5 |
| M4 事实接线 | 5-6 |
| M3 看板 | 8-10 |
| M5 灰度闸门 | 4-6 |
| 合计 | **29-40**（两名工程师并行 + QA/业务验收同步，约 3-4 个自然周） |

---

## 10. 明确不做（冻结清单）

AI 兜底列映射、补库购物车 Beta、回款凭证上传、坏件变卖登记、项目工作簿 v3 导出、
前置库账本页、收回清单页、销售看板页、报销对账页——代码保留、导航隐藏、不上线。
