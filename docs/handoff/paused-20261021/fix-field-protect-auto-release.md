# 交接：字段保护自动解除（itsp PR #430，分支 fix/field-protect-auto-release）

> 暂停说明：金臣（产品负责人）2026-10-21 18:45（北京时间）下令全部开发暂停、准备重构。本文件由暂停收尾执行者于 2026-10-21 2026-10-21 19:10（北京时间）写入，只记录现状，不补完、不修测试。收尾期间**未跑任何测试**，下文测试结果均摘自原执行日志，标明来源。
> 本 PR 带标签 `paused-refactor`，标题前缀「[暂停-待重构]」。不合并、不部署；未删除任何分支、worktree、容器、stash 或数据库。

## 目标与编号
- ITSP-6：改回本轮首次快照自动解除字段保护，并阻断旧簿基准漂移（决策 D-34 Q1=A / Q3=A）；计划 docs/plans/itsp6-field-protect-auto-release.md。

## 已完成 / 未完成
- 已完成：第 1、2 轮返工（9392930、58aff31、6b99480），远端 = PR head = 6b99480。
- 未完成：第 2 轮 Codex 双审（2026-10-21 18:06–18:18）结论「必须改」的各项均未修（见下）；未 merge main（PR mergeState=BEHIND）。

## 当前状态
- 暂停时代码 head：`6b99480483010d67bd12e6b5365f46d17b357629`；本交接文档提交在其后，最终 head 见 PR。
- 测试（摘自 ~/workspaces/reviews/rework-430-progress.md，15:11 接手执行者亲跑，对象为 6b99480 + 未提交 merge origin/main d41e3d6 的临时 worktree wt-430-verify-merge-1505）：colorGuard 3 passed exit0；前端全量 vitest 150 文件/1750 passed exit0；npx tsc exit0；对比度用例 3 passed exit0；后端 PR 改动的 11 个测试文件 254 passed exit0。分支本身：WorkbookRoundTrip vitest 34 passed、tsc exit0（14:13）。
- CI：run 39554142291（6b99480）后端 alembic/单头/pytest success、前端构建+vitest success；仅两个依赖扫描步骤 failure（certifi、pillow、follow-redirects，已由 #441 在 main 修复，本分支未合入）。gh 现状显示两 job 均 FAILURE（即上述扫描）。

## 已知问题（第 2 轮 Codex 双审，结论均为「必须改」，以下照抄报告）
报告：
- ~/workspaces/reviews/acceptance-20261021/review-430-codex-astra.md（18:06 完成；摘要 review-430-codex-astra.last.md）
- ~/workspaces/reviews/acceptance-20261021/review-430-codex-sol.md（18:18 完成；摘要 review-430-codex-sol.last.md；证据 review-430-codex-sol-evidence/）

Astra（4 项必须改）：
- M1 — 清空隐藏“实体ID”后，旧簿可以覆盖已进入新保护周期的字段
- M2 — 接近 Excel 上限、含换行或 JSON 转义字符的合法文本仍无法下载并回传
- M3 — 弹窗仍有文字对比度不足，现有测试只覆盖说明段
- M4 — PR 正文写有 X / 遮罩关闭，但实际只支持 ESC

Sol（4 项必须改）：
- M1. 弹窗仍有文字低于 4.5:1
- M2. 当前长文本仍撑爆基线，合法字段值使整本无法下载
- M3. 两个核心测试不能抓住它们承诺覆盖的回退
- M4. PR 正文的关闭行为与实际不符

其他坑：
- stash（全仓共享，**只记录，未 apply、未 drop**）：`stash@{0}: On (no branch): rework2e GLM-5.3 colorGuard 注释修改（被改指令中止，保留）` —— GLM-5.3 对 WorkbookRoundTrip.tsx 注释去两个十六进制色值的改动（blob b53f836，与 a338ae4 内容一致）。按消息名找，编号会变。
- 本地分支 `ref/flash-a338ae4-430` → a338ae4（DeepSeek flash 原提交，6b99480 是它的 cherry-pick，patch-id 一致），未推送，保留。
- 临时 worktree（均保留未动）：
  - wt-430-verify-merge-1505、wt-430-colorguard-merge-tmp：detached 6b99480 + `git merge --no-commit --no-ff origin/main(d41e3d6)`，**无冲突**、处于未提交的合并状态，仅为验证用（MERGE_HEAD 在）；未提交、未 abort。
  - wt-430-impl：detached 6b99480，干净，无残留。
  - wt-laneB-protrelease：本地分支停在旧的 5fa7f4e（6b99480 的祖先），干净。
  - wt-acc-430-astra / wt-acc-430-sol：验收用，干净。
- 测试容器 wt-430-pg（:32990）等保留。

## 未决业务问题
- 审查报告“需产品确认”：NULL 数量、PN 别名与旧簿恢复流程、超过 30 条问题时的定位清单、是否提供 X/遮罩关闭（见 review-430-codex-sol.md §4、§7）。

## 下一步怎么接
1. merge main（含 #441 依赖修复）；2. 按两份报告修 M 项（长文本导出、对比度、实体 ID 绕过、测试变异存活、PR 正文）；3. 重新独立验收。

## 涉及文件与模块
- 后端：backend/app/services/maintenance_field_protection.py、maintenance_project_master_workbook.py、maintenance_demand_manual.py、etl/loader.py、models/maintenance.py、api/maintenance_project_master_workbook.py；测试 test_maintenance_field_protection_*.py 等 11 个
- 前端：frontend/src/components/maintenance/WorkbookRoundTrip.tsx、api/maintenanceWorkbooks.ts 及测试
- 文档：CONTEXT.md、docs/plans/itsp6-field-protect-auto-release.md、docs/decisions/*保护与终态确认.md

## 代码作者（按模型）
- 9392930、58aff31：GLM-5.3。
- 6b99480（= a338ae4 cherry-pick）：DeepSeek flash（金臣 14:11 裁定“flash 写的也能用”）。
- 以上据 PR #430 正文“代码作者（按模型）”表与 rework-430-progress.md。
