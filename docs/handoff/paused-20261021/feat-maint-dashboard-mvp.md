# 交接：维保数据看板 MVP（itsp PR #443，分支 feat/maint-dashboard-mvp）

> 暂停说明：金臣（产品负责人）2026-10-21 18:45（北京时间）下令全部开发暂停、准备重构。本文件由暂停收尾执行者于 2026-10-21 2026-10-21 19:10（北京时间）写入，只记录现状，不补完、不修测试。收尾期间**未跑任何测试**，下文测试结果均摘自原执行日志，标明来源。
> 本 PR 带标签 `paused-refactor`，标题前缀「[暂停-待重构]」。不合并、不部署；未删除任何分支、worktree、容器、stash 或数据库。

## 目标与编号
- 维保数据看板 MVP：T1–T4 数量与估值（需求 R33；决策 D-0022，以及分支内决策文档 `docs/decisions/*数据看板MVP口径.md`）。
- 设计依据：`docs/maintenance-dashboard-design.md`（~/workspaces/reviews/design-maint-dashboard-progress.md 记录设计过程）、`docs/product-memory.md` R33。

## 已完成 / 未完成
- 已完成：后端服务+接口（9171723）、前端页面与 4 个 Tab（e49b5e7）、决策文档（8d48234）、老板角色金额可见测试（ce64cf3）、访问日志 action 登记与守卫测试修复（4311684）、merge origin/main（含 #441，b7f0d71，无冲突）。Draft PR 已开。
- 未完成：本地后端全量 pytest 未跑完；CI 后端 job 暂停时仍在跑（结果未知）；**无独立验收审查**（acceptance-20261021 下没有 #443 报告）；未经金臣用户点按验收。

## 当前状态
- 暂停时代码 head：`b7f0d714a026a6bb4dc5fa968c2180cb8901c86d`（= 远端 = PR head）；本交接文档提交在其后，最终 head 见 PR。
- 工作区：~/workspaces/wt-maint-dashboard 干净（egg-info 3 个构建产物文件被设了 skip-worktree，不应提交）。
- 测试（摘自 ~/workspaces/reviews/maint-dashboard-progress.md，执行者亲跑）：
  - head 8d48234：alembic heads 单头 d8f6a2c4b9e7 exit0；新库 alembic upgrade+check exit0；tsc --noEmit exit0；colorGuard exit0；全量 vitest 151 文件/1753 passed exit0；npm run build exit0。
  - head 8d48234 后端全量 pytest 中止（负载高）：截至中止 1 failed/315 passed，失败 test_access_log_action_width.py::test_dynamic_sites_registered_with_exact_counts → 4311684 已修。
  - head b7f0d71：alembic heads/tsc/colorGuard exit0；全量 vitest 151/1753 passed；build exit0；新库 alembic upgrade+check exit0；后端全量 6 分片在 17:59 按指示中断：合计 852 passed、1 skipped、0 failed（未跑完）。
- CI（gh 查询，2026-10-21 ~19:00 北京时间）：run 39583572833 前端 SUCCESS；后端 IN_PROGRESS（暂停时盯 CI 的 gh run watch 已 C-c 停止，日志 ~/workspaces/reviews/maint-dashboard/ciwatch-39583572833.log）。PR mergeState=BEHIND。

## 已知问题 / 坑
- 本地全量 pytest 机器负载高时极慢；分片跑时注意：tmux new-window 不继承调用方 export 的 DATABASE_URL（会回落 :5433）、多片同时启动 conftest CREATE DATABASE 会竞态。
- uv sync 会改写已跟踪的 egg-info，已 skip-worktree。
- Claude 曾把 PR 草案写到 ~/reviews/maint-dashboard/pr-body-draft.md（路径偏差），已复制到 ~/workspaces/reviews/maint-dashboard/。
- 测试容器 maint-dash-pg（127.0.0.1:33060）保留未删。

## 未决业务问题
- 估值口径、数量核对口径以分支内决策文档与设计方案为准，金臣尚未做用户点按验收；无独立审查结论。

## 下一步怎么接
1. 看 CI 39583572833 最终结果；2. 派独立验收（参考 acceptance-20261021 的审查模板）；3. 重构方案确定后决定保留/重写哪些模块。

## 涉及文件与模块
- 后端：backend/app/api/maintenance_dashboard.py、backend/app/services/maintenance_dashboard.py、maintenance_dashboard_valuation.py、maintenance_dashboard_valuation_policy.py、backend/app/main.py；测试 test_maintenance_dashboard_{api,qty,valuation}.py
- 前端：frontend/src/api/maintenanceDashboard.ts、pages/maintenance/MaintenanceDashboardPage.tsx、pages/maintenance/dashboard/*、nav.tsx 及测试
- 文档：CONTEXT.md、docs/decisions/*数据看板MVP口径.md

## 代码作者（按模型）
- 9171723、e49b5e7、8d48234、ce64cf3、4311684：GLM-5.3（Claude Code，会话 699d0f7a，modelwatch 记录全程仅 glm-5.3）。
- b7f0d71（merge main）：执行代理手工合并，无代码生成。
