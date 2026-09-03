# 接手说明：写给后续接手的 Claude Code

- 接手日期：2026-09-03 及以后
- 上一会话：Claude（本机）负责架构审计 + 决策落库 + 本次部署交接；zcode 负责本次「全量编辑恢复」任务，任务结束后不再参与。
- 你是后续开发的主力。**你没有本会话的对话记忆，一切以仓库文件为准。** 这是刻意的：口径必须落在仓库里，不落在某个会话的脑子里。

## 第一步：先读这些，按顺序

1. `docs/decisions/0001-维保整改八条口径.md` —— 8 条已拍板口径，**唯一真值**。任何实现与此冲突，先核对是否已 supersede，否则以它为准。
2. `docs/handoff/2026-09-03-workbook-full-edit-handoff.md` —— 当前状态快照（PR #304/#305、生产基线、验收口径、风险）。
3. `docs/reference/sales-order-columns.md` —— 期限缺失根因与建项字段口径。
4. `docs/reports/2026-09/2026-09-03-重构决策审计报告.md` —— 为什么是"定向整改而非整体重构"的完整依据。

读完后先 `git fetch` 对齐远端，别在过期 ref 上开工。

## 第二步：记住四条纪律（都是事故换来的）

1. **不要从历史 PR/issue 反推口径。** 8 条口径在 `docs/decisions/`，用它，不用猜。如果它没有覆盖你要做的事，先写一条新决策编号再实现。
2. **合并走 main + 分支保护。** main 要求后端 pytest + 前端构建两个必需检查全绿 + 1 审批，`enforce_admins=true`。禁止从 fix/feature 分支直部署（09-02 事故）。
3. **同一模块多工具反复改，必带契约测试。** 尤其 `maintenance_project_master_workbook.py`、`etl/loader.py`、`maintenance_project_identity.py`，并发语义和 bump 调用点是高危区。
4. **部署不是自动的。** 写生产前先备份、前后端同 commit、`alembic upgrade head` 跑完再启镜像、打 `release-` 标签。用户没明确说"开始部署"，不要动生产。

## 第三步：你的工作范围（按阶段）

详见 `docs/reports/2026-09/2026-09-03-重构决策审计报告.md` 第四节，摘要如下：

- **阶段 0（流程护栏）**：大多已由本次会话落地（main 保护、发布校验缺口见交接说明）。检查 CI 是否已补 `alembic heads==1` 卡口与 PR 号豁免删除。
- **阶段 1（清残骸）**：前端 32 个死文件 + 11 个死测试删除；`services/legacy/` 冻结 migration 系列；`.deploy/` v120-v122 归档；删已合并分支；权限修 3 个 bug（`page_parts` 假权限、2 个死 action 键、`ACTION_DATA_DEPENDENCIES` 重复键）。
- **阶段 2（维保定向重建）**：拆 `maintenance_project_operations.py`（7199 行）为 6-8 模块、拆 `master_workbook.py` 为 v1/v2/common；补 `bulk_import`/`project_identity`/`migration_runs` 测试（当前 3126 行仅 12 测、2197 行仅 12 测）。
- **阶段 3（权限矩阵化）**：45 键守卫层不动，加「岗位×模块×看/改/钱」矩阵视图。前置：先修阶段 1 的 3 个权限 bug。**待用户确认「仓库」岗位是否存在。**

## 第四步：留给你确认/拆分的事项

- PR #302（`codex/sales-xsdd-auto-project`，233 文件/5.8 万行）要拆成独立小 PR 分批合，**不要整体合**。核心是销售订单导入建项（见 `docs/reference/sales-order-columns.md`）。
- PR #305（docs 分支）合并后，决策日志即进入 main，成为后续一切开发的基准。
- 权限矩阵的岗位清单、以及「销售 vs 维保负责人的成本/合同额可见边界」需用户最终确认（见决策 D-03/D-07）。
