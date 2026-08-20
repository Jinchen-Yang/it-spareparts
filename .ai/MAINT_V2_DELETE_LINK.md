# 维保 V2 删除/作废 端到端链路与决策记录

> 创建：2026-08-19。父 Issue：#264（[Feature][父项] 维保部分可编辑删改实现与软作废归档实现）。
> 本文件是本轮重新分配子任务（#265–#268）的**决策基准**，后续会话先读这里再动手。

## 一、端到端打通链路（已确认的目标态）

```text
氚云侧删除需求单
  → 用户重传「维保需求单全量快照」(POST /api/maintenance/wbdd-imports)
  → 服务端快照差异报告：missing_orders = 库里有、本文件没出现的活跃单
  → 【新】前端渲染差异清单 + 「按氚云现状批量作废」按钮
  → 走既有墓碑 API（create → arm → execute，批量）写 tombstone
  → 【补】作废时同步停用挂靠关系 (MaintenanceSourceOrderAssignment.is_active=False)
  → 【补】读侧全部生效：
      成本重算 (maintenance_cost.py:373 已有 active_demand_condition ✅)
      需求单搜索/往返工作簿 (已有 ✅)
      项目总表 03/02 概览/全局行级表 (_assigned_lines ❌ 待补墓碑过滤)
  → 项目总表、概览数字、看板全部干净，行可恢复 (restore API 已有)

报销行删除（用户在下载的 Excel 里删行 → 回传 → 行消失）
  → 下载总表时每行带隐藏行 token
  → 回传 validate：比对「导出过但没传回来」的行 → 默认判作废，列出将作废清单
  → apply 生效：data_status='已作废'（复用既有语义）
  → 【补】读侧 _expenses (maintenance_expense_collection_workbook.py:133) 过滤/降权作废行

03 备件明细行级删除/新增/改
  → 复用 checkpoint eabd7af (feat/maintenance-v2-editable) 的操作列协议：
     CREATE / UPDATE / VOID (is_active=false + 级联作废 06 + 氚云重传不复活)
  → 需把该分支工作重搬到当前 main（main 重写了同一文件 ~644 行，不可直接 rebase，按功能重放）
```

## 二、已确认决策（用户 2026-08-19 拍板）

1. **撤回**上一轮「人工优先、来源只填空」规则——不在本轮范围。
2. WBDD 删除：复用既有墓碑机制，**补前端入口**（当前 API 和前端 client 都在，但无任何页面使用）。
3. 重传差异清单从「静默报告」（missing_orders 只在 POST 响应里，前端零消费）升级为**可操作清单**。
4. 报销行：**缺行 = 作废**（reconcile 语义）+ 04 sheet 补显式 VOID 操作列；validate 列出将作废清单再 apply。
5. 防呆仅一条：上传行数 < 导出行数一半 → validate 拒绝（防筛选/复制粘贴事故）。不做两阶段复核（甲方不在乎审核强度）。
6. 所有删除均为软删，可恢复；恢复 UI 低优先级。
7. 作废的报销行**彻底不导出**：所有导出/行数据/金额计算读侧直接过滤 `data_status='已作废'`（用户 2026-08-19 拍板，取代此前「保留展示不计金额」的默认建议）。
8. **一键作废走后端新端点** `POST /api/maintenance/demands/void-fast`（批量 source_order_ids + reason；内部一次事务完成快照校验+墓碑+挂靠停用，跳过 7 秒 arm 窗口；保留版本 digest 冲突检测与整批零删除语义）。既有两阶段 delete-intents API 保留不动。差异清单页与需求单列表作废按钮统一走该端点。

## 三、必修缺陷（随 #267 一起）

| # | 缺陷 | 位置 |
|---|---|---|
| 1 | `_assigned_lines` 无墓碑过滤：作废单仍进项目总表 03/02 概览/全局表 | maintenance_project_master_workbook.py:239-264 |
| 2 | 墓碑 execute 不停用挂靠关系 | maintenance_demands.py:675+ (execute 流程) |
| 3 | 报销读侧不过滤 data_status（作废行照常导出计入） | maintenance_expense_collection_workbook.py:133-140 |
| 4 | 上传入口 `max_part_size=1024` 字节，Excel 无法上传（链路入口级阻断） | api/maintenance.py:1110 |
| 5 | `latest_health` 不含 missing_orders 明细（做差异清单页需要新查询端点） | services/maintenance_wbdd_import.py:211 |

## 四、接口清单（现有 + 提案，详单见 #265 契约）

现有关键端点分组：源头上传（wbdd-imports / import/upload / warehouse-imports / recompute）、需求单（demands/search + delete-intents 生命周期 + restore）、项目总表（master-workbook .xlsx/rows/validate/apply + spare-part-lines 全局表）、稳定项目 CRUD/归档、项目操作（contracts/collections/site-issues/cost-gaps/expenses/workspace）、挂靠（project-assignments/orders）、工作簿 v2/v3。

新接口（已与用户确认，2026-08-19）：
1. `GET /api/maintenance/wbdd-imports/latest/missing` —— 差异清单明细（batch_id、uploaded_at、missing_orders[]：source_order_id/order_no/order_date/line_count/assigned_project_id）。导入时把差异快照持久化到 receipt（JSON 列或小表，#266 裁决），GET 读快照。
2. `POST /api/maintenance/demands/void-fast` —— 一键批量作废（见决策 8）。请求 `{source_order_ids: [], reason}`；响应 `{voided: n, conflicted: m, results: [{source_order_id, order_no, status: voided|conflict, cause?}]}`；任一单版本变化 → 整批零删除。
3. 04 VOID / 缺行作废 / 03 操作列：**不加新端点**，扩展既有 validate/apply 协议与响应（will_void_rows 清单）。

## 五、基线事实

- 当前 main：`27c95fa`（v1.23 收尾）；半成品 checkpoint：`eabd7af`（feat/maintenance-v2-editable，基于 2ce0380，22 文件 +1114/−74，10 个测试）。
- main 与 checkpoint 分叉后 main 独立演进 16 commit 且重写 master_workbook 同文件，**合并必须按功能重放**。
- `manual_order_no` 列：计划有、实现无；实现用「CREATE 行必须挂本项目已有需求单」替代——倾向判定为删除旧计划，#265/#266 最终裁决。

## 六、实现状态（2026-08-19 收尾）

> ⚠️ **血统事故与修正（重要，后续会话必读）**：
> 仓库存在两条并行血统——(a) origin/main squash 线（`27c95fa` = GitHub squash 合并）；(b) 本地链（`bd867a7`→…16 提交→`99b836c`，含购物车暂存/八Sheet 重写/补库原子提交/打回重编辑等未发布工作，用户后期把本地 main 重置回 `27c95fa`）。
> 我最初误从 `99b836c` 建分支（当时本地 main 指向它），后来**从 `27c95fa` 重建分支并 force push 替换远端同名分支**；本地链的 16 个提交仍在本地（无远端备份），归用户自行处置。
> 两血统的同名文件是**两套实现**（如 master workbook：27c95fa 版含 XSDD 合同回退/04 手工新增/FSalesOrder 自动建合同；99b836c 版是八Sheet 重写）。跨血统移植必须语义合流，不能文本 rebase。

- **后端+数据库完成**：分支 `feat/maintenance-delete-void`（@49081b5，基线 27c95fa），PR **#272** → main。契约文档 `docs/maintenance/contracts/project-master-delete-void.md`。
- 27c95fa 合流保留清单：04 空白实体手工新增（V2.1 与操作列双轨）、`project_sales_order_nos` 归集、`_v2_build_overview` XSDD 回退、`_xsdd_contract_for_project`、v1 apply 备注独立、`_v2_date` 带时间格式、`sheets=included` 语义。
- 前端 PR **#270**（三入口+两页重设计，基于 27c95fa）：组合验证 vitest 623/623 + 构建通过；前端期望的三处后端契约缺口（void-fast results[] / search include_voided / validate will_void_rows）已在 49081b5 补齐。
- WSL 全量：仅 https_rollback 9 失败（纯 27c95fa 基线同样失败，环境类；CI runner 上 main 全绿）。
- 测试环境：WSL `wsl.cloudlay.cn:2222`（密钥 `~/.ssh/cloud_claude_wsl`），克隆 `~/Workspaces/it-spareparts-verify`；本机 macOS conftest 拒跑。**本机到 github.com 443 常被重置——推送走 WSL git bundle 中转**。
- 剩余：PR #272 CI → 合并 → #270 合并；#268 两大场景 tracer bullet。

## 七、工作纪律（用户 2026-08-21 指定）

**每次修复必须端到端验证**：前端实际调用路径 → 后端路由真实命中（带鉴权的真实请求，不能用 401/单测 mock 代替）→ 数据库实际变更（psql 对账）。三段全通才算修完。教训案例：collection-plan 端点连续两次假修复（路径少段 404、Decimal 导入空转 500）都因只验证了片段。
