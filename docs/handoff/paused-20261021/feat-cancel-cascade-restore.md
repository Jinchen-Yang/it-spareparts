# 交接：误作废全量恢复 第 2 轮返工（itsp PR #440，分支 feat/cancel-cascade-restore）

> 暂停说明：金臣（产品负责人）2026-10-21 18:45（北京时间）下令全部开发暂停、准备重构。本文件由暂停收尾执行者于 2026-10-21 2026-10-21 19:10（北京时间）写入，只记录现状，不补完、不修测试。收尾期间**未跑任何测试**，下文测试结果均摘自原执行日志，标明来源。
> 本 PR 带标签 `paused-refactor`，标题前缀「[暂停-待重构]」。不合并、不部署；未删除任何分支、worktree、容器、stash 或数据库。

## 目标与编号
- 误取消/误作废确认的完整恢复与面板恢复入口（需求 R24；决策 D-0022）。
- 第 2 轮返工 5 条必须改（来源：review-440-codex-sol.md M1–M4、review-440-codex-astra.md B1–B4，对象 a762473）：
  1. 畸形快照全 schema 校验（清单/预览/执行统一先整体校验，坏单可读 4xx、清单标“快照损坏”，含缺 voided_at）
  2. before 非法状态值在 preview 阶段 4xx（不能 flush 报 500）
  3. 历史作废行冒充：须证明快照成员是本次确认作废的行，否则整单零写入拒绝
  4. 前端账号切换会话守卫（弹窗记会话标识，提交/重试/异步落地前比对）
  5. 状态标签对比度 ≥ 4.5:1，只用主题 token

## 已完成 / 未完成
- 第 1 轮返工已交付并推送：7e71f15（后端）、a762473（前端）；CI 39561127489 除依赖扫描外全绿（rework-440-progress.md 16:29）。
- 第 2 轮（GLM-5.3 会话 19f8a295，tmux rework-440-r2，日志 ~/workspaces/reviews/rework-440/cc-r2.log，提示 rework-440/prompt-r2.md）：
  - 步骤 a 完成：5 条要求的新测试（后端 test_maintenance_source_cancel_restore_integrity_r2.py；前端 SourceCancelRestorationSection.session.test.tsx、frontend/tests/sourceCancelRestorationContrast.test.tsx；两个既有前端测试 beforeEach 补 token）。
  - 步骤 b 完成：在 wt-restore-cancel-r2verify（a762473）上修前失败证明：before-fix-backend.log 退出 1（GLM 最后称修正测试后为 20 失败/9 通过），before-fix-frontend.log 退出 1（7 失败/1 通过）。日志在 rework-440/r2/。
  - 步骤 c 进行中被中断：GLM 自述“后端实现完成（integrity_r2 29 passed）”，新增 maintenance_source_cancel_restore_schema.py；前端正在加 sessionIsCurrent 会话守卫与 token 标签样式，**前端测试尚未跑完**即被 18:47 C-c 中断。以上均为 GLM 自述，收尾执行者未复核。
  - 未完成：前端第 4、5 条实现与验证；全量回归；反向测试；PR 正文更新；推送前未做任何独立复核。
- 已本次原样提交：a55c3c8「wip(#440): 第 2 轮返工半成品原样提交」（含 944c83c merge main 一并推送）。

## 当前状态
- 收尾时代码 head：`a55c3c8`（父 944c83c = a762473 合入 origin/main 0545c16，无冲突）；本交接文档提交在其后，最终 head 见 PR。
- 测试：a55c3c8 上**没跑**任何测试（收尾不跑）。GLM 会话中途的定向运行见上，未经复核。
- CI（gh 查询 ~19:00）：PR 旧 head a762473 的检查显示前后端 FAILURE（第 1 轮记录为仅依赖扫描红：certifi/pillow/follow-redirects，已由 #441 修）；新 head 推送后的 CI 结果未知，预计会红（含 scratch 探针、半成品）。

## 已知问题 / 坑
- `backend/tests/test_r2_scratch_probe.py` 是 GLM 的临时设计探针（文件头写“不提交”），按暂停规则原样提交，重构时删除。
- stash（全仓共享，**只记录，未 apply、未 drop**）：`stash@{1}: On feat/cancel-cascade-restore: flash-r1b-440` —— 第 1 轮 DeepSeek flash 的未提交改动（flash 不得写代码，已另存 rework-440/flash-uncommitted.patch、flash-snapshot/）。注意 stash 编号会随他人 push stash 变化，按消息名找；曾因误按 stash@{0} apply 引发冲突（rework-440-progress.md 14:12）。
- 孤儿进程：GLM 起的本地全量 `nohup pytest`（PID 1297418，基于旧代码）已于 17:59 被上一执行者停掉；18:47 收尾时该 PID 已不存在。
- 相关 worktree：wt-restore-cancel（本分支，已提交干净）；wt-restore-cancel-r1verify（detached da3dfca + 第 1 轮新测试，未跟踪，验证用）；wt-restore-cancel-r2verify（detached a762473 + 第 2 轮新测试未跟踪副本，验证用）；wt-acc-440-astra / wt-acc-440-sol（验收用，干净）。均未删除。
- 测试容器 itsp-laneB-cancelrestore-20261021（127.0.0.1:32941）保留。
- 前端 package.json 无 typecheck 脚本，按 CI 口径用 npx tsc。

## 未决业务问题
- 第 3 条“证明本次作废”的依据（作废事件/回执行 id 集合 + voided_at）是否需要产品确认，未裁定。
- 审查报告“建议改/需产品确认”节未处理。

## 下一步怎么接
1. 在 wt-restore-cancel 跑 r2 新测试确认 GLM 后端自述；2. 完成前端会话守卫与对比度（只用主题 token），跑 colorGuard；3. 全量回归 + 反向测试；4. 删除 scratch 探针；5. 更新 PR 正文后重新独立验收。

## 涉及文件与模块
- 后端：backend/app/services/maintenance_source_cancel_restore.py、maintenance_source_cancel_restore_schema.py（新）、services/maintenance_source_cancel_cascade.py、api/maintenance_source_cancel_cascade.py；测试 test_maintenance_source_cancel_restore{,_integrity,_integrity_r2}.py、test_r2_scratch_probe.py
- 前端：frontend/src/components/maintenance/SourceCancelRestorationSection.tsx、SourceCancelImpactPanel.tsx、api/maintenanceSourceCancelCascade.ts 及 __tests__、frontend/tests/sourceCancelRestorationContrast.test.tsx

## 代码作者（按模型）
- 初版（da3dfca 及之前）：未知（PR 正文/前序记录）。
- 第 1 轮 7e71f15/a762473：恢复服务与前端由 DeepSeek flash 编写、GLM-5.3 核验修订；测试主体 GLM-5.3（见 r440r2-*.md 与 rework-440-progress.md）。
- 944c83c：执行代理 merge main。
- 第 2 轮 a55c3c8 内全部改动：GLM-5.3（会话 19f8a295，cc-r2.log 请求模型仅 glm-5.3，flashwatch 未触发）。
