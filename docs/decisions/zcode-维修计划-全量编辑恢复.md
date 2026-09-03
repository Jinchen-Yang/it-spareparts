# 维保项目总表「全量编辑能力恢复」维修计划

目标 = 差异报告 8 项全绿 + 客户可感知验收（改错能覆盖重传、不同行互不阻塞、同行冲突可见可接管、回执逐字段、审计含失败）。已拍板：项目负责人/销售对本人项目工作簿**全部字段可见可改**（含成本与合同额，2026-09-02 业务决策，写入审计与 changelog 说明）。

## 核心设计：行级三路合并取代整本作废

现状根因：v2 apply 只做项目级 CAS（revision 不符→整本 409），且 03/04/05/06 行不携带导出基线，parser 与"服务端现值"diff——无法区分"用户改的"与"文件里带出来的旧值"。修法：

1. **导出侧**（`maintenance_project_master_workbook.py` build_v2 各 sheet）：03/04/05/06 每行新增隐藏「基线令牌」列 = 该行全部可编辑字段导出值的 JSON 快照 + HMAC 签名（复用 `_global_cost_base_token`（svc:684-719）的域分隔模式，新域 `ITDATA_MAINT_MASTER_ROW_BASE_V1`）。02 已有隐藏「基础版本」列、合同额已有 `contract_edit_version`，保留并入同一体系。模板版本 2.6.0→2.7.0（98_字段说明/00_使用说明同步）。
2. **上传侧**（validate/apply 各 parser）：逐行解基线做三路合并——
   - 用户值==基线 → 未触碰，整行跳过（服务端已变也自动 rebase，**导入连锁过期消失**）；
   - 用户改、服务端未变 → 正常应用（逐字段进 `changes[]`）；
   - 双方都改且不同 → 该字段进 `conflicts[]`；删行=作废但服务端该行已变 → 行级冲突。
   - 项目级 `stale_workbook` 硬 409 退役（svc:4529-4549）：元数据 HMAC/令牌完整性仍强校验；revision 继续记录用于审计。
3. **强制接管**：apply 增加 `force_takeover` 标志；true 时冲突按用户值覆盖，逐项记 `overridden[]`（谁何时覆盖了谁的什么）；false 时 409 返回结构化 `conflicts`（sheet/行标签/字段/导出值/服务端现值/用户值）。幂等重放 receipt 机制原样保留。
4. **回执**：`_v2_apply_result` 增加 `changes[]/conflicts[]/overridden[]` 字段级明细。

## 权限：负责人/销售获得本人项目全量编辑权

- 新增 `is_project_workbook_editor(db, project_id, ctx)`：FULL_SCOPE（admin/boss）走现有 `action_maintenance_expense_collection_upload` 键；否则= 本项目 `primary_manager` 挂靠（maintenance_project_user_assignment）或 项目 `salesperson==ctx.salesperson_name`。采用 replenishment 234f98b 的双查模式（`replenishment.py:504-566`：锁前 can_access → 行锁后重查权限，防吊销 TOCTOU）。
- API（`maintenance_project_master_workbook.py` download/validate/apply）：`require_action(_ACTION_KEY, require_data="data_profit")` 替换为「editor-or-action」依赖，fail-closed；`_require_contract_amount_manage` 特权门取消（拍板放开），保留合同 `base_version` CAS 与共享合同 warning。
- 03 表跨项目 assignment 更正维持 FULL_SCOPE（管理动作，不在本次放开范围）。
- 01_项目概览从只读改为全量编辑：项目主档（显示名/期限/负责人/销售/备注，走 `catalog.update_project` + project.version 乐观锁）+ 全部合同行合同额（去掉 svc:2118-2155「唯一/当前/未共享」三条件硬限制，保留 base_version CAS + 共享影响提示）。

## 审计：全操作留痕（含失败）

- validate/apply 的成功、409 冲突、422 拒绝、强制接管全部 `record_access_log`（action: workbook_validate/workbook_apply/workbook_apply_conflict/workbook_apply_takeover，detail 带 carried revision、冲突计数）+ `sys_audit_log`（接管逐项 before/after）。下载补记 revision。修复现状"18 次 409 零留痕"。

## 前端（版本 1.27.0）

- `WorkbookRoundTrip.tsx`：409 结构化冲突表（字段/导出值/当前值/你的值）+「强制接管」二次确认重传（带 force_takeover）；回执渲染 `changes[]/overridden[]` 字段级明细。
- 刷新屏障已有（panelUtils/MaintenanceProjectPanelPage refreshProject），补字段→展示映射断言。
- `version.ts` 升 1.27.0 + CHANGELOG + `version.test.ts` 同步（发版规范）。

## 实施：4 个 PR，每步 CI 绿（后端 ~35min/轮），分支保护走 main

- **PR-1 并发核心**（最大件）：基线令牌列 + 三路合并 + stale 退役 + force_takeover + 回执 changes/conflicts/overridden + 服务层测试矩阵（未触碰 rebase/正常/字段冲突/删行冲突/接管/幂等重放/令牌防篡改）。
- **PR-2 权限与 01 全量**：editor 判定 + API 依赖替换 + 01 主档/合同额全量编辑 + 审计补全 + 权限测试（负责人✓销售✓无关账号✗并发吊销✗）+ 失败审计断言。
- **PR-3 前端 + 联动验收**：冲突/接管 UI、回执明细、1.27.0 升版、vitest（含两账号不同行并发互不阻塞的回归用例）、展示联动断言。
- **PR-4 生产对齐与上线**：把当前在跑的 fix/contract-total-inc-tax（9b30126+a8e4 迁移）合入 main 对齐 alembic 指针 → 核实/打开 `MAINTENANCE_PROJECT_MASTER_V2_ENABLED` → main 部署（release-<commit> 标签、前后端同 commit、alembic upgrade head、迁移前备份）→ 观察 409 率与审计流水两个 cron 周期。

## 验收检查（严格口径）

1. 负责人/销售账号：下载→任意表改值/删行/加行→上传→回执逐字段列出→卡片墙/面板对应数字即时变化。
2. 两账号同时改同一项目不同行→互不阻塞各自成功（现 409 场景回归 200）。
3. 改同一行→后传者看到冲突明细（三值对照）→可强制接管→接管明细入回执与审计。
4. 导入发生后再上传未触碰行→不再 409（rebase 生效）。
5. 每次下载/上传/校验/拒绝/接管在 sys_audit_log 可查（含失败）。
6. CI 全绿（后端 pytest 全量 + 前端 build+vitest），无迁移链分叉。

## 风险与对策

- 5500 行服务文件改造：PR-1 先行服务层测试矩阵锁行为，再动 parser；模板 2.6.0→2.7.0 强制重下载（旧文件因缺基线列被令牌校验拒绝，提示重新下载——一次性迁移成本，已知会客户）。
- 本机 macOS 跑不了后端测试（Linux 隔离）：以 CI 为门；前端 vitest 本机可跑。
- 三路合并是语义改造：PR-1 里保留旧整本 409 行为的测试改为新语义断言，逐条列在 PR 描述供审。