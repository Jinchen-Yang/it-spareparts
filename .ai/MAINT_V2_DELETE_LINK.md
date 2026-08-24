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

## 八、客户反馈六条实施记录（2026-08-21，分支 feat/maintenance-feedback-v124 @ 基线 577f7b4）

微信客户（亚博威）六条反馈分四批落地，全部按第七节纪律做了真实登录（admin_t1@8000 + spareparts_dev@5433）+ psql 对账：

### 批次一（展示修正）
- 卡片显销售：`GET boss-board/projects` 返回 salesperson=李呈辉 ↔ psql 台账 salesperson 空 + 需求单销售众数 5 票李呈辉，兜底路径对账一致；未归属桶 salesperson=None。
- 明细倒序：`master-workbook/rows?sheet=03_备件订单` 前三行 2026-08-19/08-18-0004/08-18-0009 ↔ psql 同 ORDER BY 完全一致。
- 发货数文案（3,446 个）为前端渲染，留页面验收。

### 批次二（维保负责人自动回填）
- auto-assign 实跑：sales_filled 417 / manager_filled 414 / assignments_created 18 ↔ psql 417/419 项目有销售、20 活跃指派（18 新 + 2 旧）。
- 采样：中国广电BOSS数据库项目 salesperson=刘青青、assignment→sales_t1（salesperson_name 匹配）三字段一致。
- 幂等重跑全零、指派数不变。
- 下拉数据源修复中发现并修正：assignments 路由整体挂 Beta 总闸，search 端点挪 stable_router（page_maintenance 门），维保负责人账号实测 200。

### 批次三（验收清单导入）
- 模板下载 200（真 xlsx、示例行防呆跳过）→ preview 4 行（2 完成 2 待验收）→ apply → psql 批次表 applied/4/0 + 行表逐行一致。
- 替换语义：第二版 will_replace_rows=4 提示、replaced_batch_id 串链、history=2；问题行（"大概"）409 整批拒绝且当前清单未被污染；同 key 同文件幂等重放返回原批次。

### 批次四（维保负责人角色 + 行级隔离）
- 建号 maint_test_lch（role=maintenance_manager、salesperson_name=李呈辉）：template page_maintenance=T、行键=T、data 组全 F。
- boss-board total=79 ↔ psql salesperson='李呈辉' 项目数 79；页面只见李呈辉项目、无未归属桶；admin 全量 419 不受影响。
- PN 排名收敛（1492 PN）+ 成本位 restricted（无 data_purchase_cost）。
- 越权：访问他人项目 403、自己项目 200、验收清单读 200。

### 遗留
- **WSL pytest 未跑**（wsl.cloudlay.cn:2222 持续 Connection Refused，机器 ssh 未起）：三个新测试文件（owner_backfill / acceptance_checklist / role_scope）+ 受影响回归只做了语法级验证 + 本地端到端；WSL 恢复后必须补跑。
- mac Node≥22 的 localStorage 垫片使 `spyOn(Storage.prototype)` 落空（LoginPage 既有失败），已改 spy 实例；前端 631/631 绿。

### 测试收口（2026-08-21 晚，cloudlay-ts = Tailscale 100.95.182.73）
- WSL（wsl.cloudlay.cn:2222，3080 宿主）ssh 全程 Refused；改走 Tailscale cloudlay-ts（原自建 CI runner）：bundle 传输 + docker PG15@5435 + uv（清华镜像 TLS 瞬态，换 aliyun）。
- **迁移链**：alembic upgrade head → alembic check 双过（ALEMBIC_OK）。
- **全量首轮**：3637 过 / 21 失败（74:56）→ 12 个真问题全部归因修复（commit b4b8fab）：目录搜索迁 maintenance_manager_directory 稳定版模块（beta 模块看守不变量）、workspace collection_total 漏改的真 bug（rows=4/total=3 分页错位）、六处测试期望随行为更新（未来月份行集/迁移哨兵/权限接线清单/目录开放）。
- **全量复跑**：3648 过 / 6 跳过 / 10 失败（77:04）——9 个 https_rollback 为已知环境类（纯基线同败、CI main 绿），1 个 run_isolation 为同集群连跑残留（单独复跑即过，CI 新库不命中）。
- 前端：631/631 + tsc 绿（Mac 本机）。
- GitHub：三分支已推，堆叠 PR #273（workbook-ux→main）→ #274（analytics）→ #275（feedback-v124）；CI 计费仍堵（job 未启动，Billing 报错），生产前绿章以 cloudlay-ts 全量为准。

## 九、2026-08-24 验收开放 + 免审批（客户微信拍板）

> 客户原话：「验收这个我觉得可以开放给销售 项目经理和维保负责的人」「不需要审批」。

### 实现口径
- **开放**：sales 与 maintenance_manager（=项目经理/维保负责人账号角色）模板默认带 `action_maintenance_acceptance_submit`；sales 另开 `page_maintenance` + `own_maintenance_projects_only`（行键收敛「自己销售∪自己负责」，与 2026-08-21 反馈同口径）。验收两键移出 `ACTION_ADDITIONAL_PAGE_DEPENDENCIES`（Beta 附加依赖），前端验收面板从 canUseBeta 门改随维保页面渲染。
- **免审批**：提交即生效——`submit_acceptance` 直接落 `approved`（approved_by=提交人，满足字段一致性 CHECK）；生效后仍可补附件/重新提交（版本乐观锁+幂等键不变）；`review_acceptance` 服务与 `/review` 端点删除；工作簿 v3 审批文案改为「提交即生效」。`action_maintenance_acceptance_review` 键留在注册表兼容历史快照（同 checklist 先例），标已废弃。
- **迁移 a9e2f7c4d1b8**：删 `ck_maintenance_acceptance_no_self_approval`（模型同步移除）；存量 submitted+not_reviewed 整体转 approved（approved_by=提交人）；模板+账号快照（template_perms/legacy permissions）同步合并，覆盖层让位（管理员可再按账号收紧）。降级：自审行退回 not_reviewed、模板/快照回退、恢复 CHECK（有自审行则显式失败）。
- 逾期口径（未提交且未通过才算逾期）、pending_review 系统任务/提醒筛选随「提交即 approved」自然失效，不需要改代码；工作簿 v3 表头列保留（行签名兼容），只改规则文案。

### 测试（Mac Docker python:3.13-slim + 容器内 PG15，绕开 macOS conftest 拒跑）
- 后端定向回归：acceptance_api 17 过、role_scope、beta_gate、workbook_v3、v3_migration 9 过、tracking_board、pools_api（账号 _meta）、operations_api+assignments 67 过——合计 149 过 0 失败。
- 迁移链：upgrade head → check 双过；downgrade -1 → upgrade head 闭环过。
- 前端：tsc + vite build 绿；vitest 632/632 绿。
- 全量 pytest 未跑（沿用本仓纪律：发布前以 CI/全量为准）。

### CI 契约修正（2026-08-24 深夜，rm8 补丁）
- 全量 CI 抓到 8 个契约测试失败：权限中心禁止代码模板漂移历史冻结值（test_frozen_templates / migration_reconciliation）——修正实现：**sales 的验收开放改为只走 DB**（迁移 a9e2f7c4d1b8 改模板+账号快照），代码兜底模板保持历史冻结口径（旧 token/共享口令回退不开放，重新登录后生效）；maintenance_manager 为后期新角色不受冻结约束，代码模板保留 acceptance_submit=True。
- 其余：废弃键 typical 补非空文案；迁移链头钉死测试推进到 a9e2f7c4d1b8。

## 十、2026-08-24 领用取价层修复（博瑞兴云 LEGACY-0104 对账结论）

> 根因：3252463 引入的需求单价格层两处缺陷 + 一处导入数据错误。对账事实见当日会话记录（需求 54 行↔领用 54 行逐行相等、三口径成本 53548/40860/61303 打架）。

### 修复（分支 fix/maint-demand-price，堆叠在 #278 之上）
- **净数量分母**：单价 = cost_amount_ex_tax ÷ (qty−return_qty)（分子本就是退货冲抵后金额）；整行退净（净≤0）不入选。旧公式 600÷10=60（退 4/10、真实单价 100）这类压价消除。
- **匹配优先级**：source_line_id 精确行 → 同单同 PN（领用单号=WBDD 单号）→ 同项目同 PN 最新一单（仅兜底）。旧版一律取最新，同 PN 多单价不同时取错。
- **单行路径补层**：resolve_line（fill_manual_cost/重算入口）补上需求单层，与批量确认路径同口径；抽 _demand_unit_for_line 便于单测打桩。
- ALGORITHM_VERSION → site-issue-cost-v2.1（仅溯源戳）。
- **迁移 b3f8e1d6c4a2**：WBDD-20251204-0011 订单日期 2025-10-27 → 2025-12-04（单号日期段+领用日期双证据，带旧值守卫）。

### 部署后动作
- 对 LEGACY-0104 调 `POST /maintenance/projects/stable/502a02af-8988-41c6-9bc9-d1043314460e/site-issue-costs/backfill?force=true`（admin）force 重算领用成本；其他有部分退货需求行的项目同法。

### 待业务确认（未擅动）
- 11 条 workbook_manual 需求行与"需求=领用镜像"结构：需按纸面 WBDD 重录（工作簿 VOID/CREATE）。
- WBDD-20260224-0019 退 2/2 与"领用 2 正确"矛盾：需业务核对真实退货数。
- 三口径成本展示（03 表申请口径 vs 看板 YTD vs 工作台领用口径）是否统一标注。

### 测试
- 新增 5 回归（部分退货不压价/整行退净回退/同单优先/精确行优先/单行路径同口径）+ 既有 3 过；受影响套件 78 过 0 失败；迁移 upgrade→check→downgrade -2→upgrade 闭环过。

### 悬案收口（2026-08-24 下午，客户微信确认）
- **WBDD-20260224-0019 退 2/2 矛盾：客户确认"是换下来的坏件退回来了，就按照领用走"**——即氚云行"已返数量=2"是旧件返还，导入被当"退货数量"冲抵需求净量/成本。修复走迁移 c4d9a2e7f1b0（清退货数量，行成本按单价重算：未税 1592.92×2=3185.84、含税 1800×2=3600；旧值守卫，downgrade 可逆）。部署后 force 重算时，该单两张领用会按本单 1800/块 精确计价（新算法同单优先）。
- **WBDD-20260727-0018 挂老合同：原始单证据反转**——关联销售订单 XSDD-20241129-0055（老合同）、表单维保起止 2024-08-20~2025-11-03、事由"拿给北京库许昭一维修博瑞设备816485420"（送修单，非现场消耗），挂 LEGACY-0102 与单据一致，**倾向不改**；待客户确认算老合同售后还是现合同服务。
- 数据通道确认：手工填的需求行（workbook_manual）就是权威修正通道，8-23 导入时已落主表；此后业务修正一律走总表工作簿 03 表 CREATE/UPDATE/VOID（数量/退货/单价可改，全程审计）。

### 无价领用行排查收口（2026-08-24 晚）
- **生产已回填**：绕过 500 端点、容器内直调 `backfill_site_issue_costs(force=false)`：50/50 行取价、0 残留、5 项目（首发高速威石 33/鼎甲 6/黄山九章云智 5/至纯 3/太阳神XtremIO 3）。补回成本：黄山 299,789、威石 45,255.56、鼎甲 10,454.06（与扫描预演一致）、至纯 8,996.30、太阳神 7,073.80。
- **端点双 NameError**：`operations_service` 未定义 + `force=_force` 未定义（两个回填端点同病，从未成功过）——本 PR 修复并加端点级冒烟测试钉住。
- **根因修复**：总表工作簿 apply 建/改领用行后立即批量取价（此前不调取价，等全局回填才恢复；8-24 两项目新增 38 行无价即此因）。新增测试：手工建行走需求单价格层。
- 需求行待补价存量（联通仁科/移动咪咕/广州盈融等，500+351+308+250+190 行）为设计内人工回填工作流，待业务排计划。
