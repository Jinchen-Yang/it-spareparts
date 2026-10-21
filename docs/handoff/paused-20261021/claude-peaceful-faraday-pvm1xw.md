# 交接：销售看板 D-44（itsp PR #442，分支 claude/peaceful-faraday-pvm1xw）

> 暂停说明：金臣（产品负责人）2026-10-21 18:45（北京时间）下令全部开发暂停、准备重构。本文件由暂停收尾执行者于 2026-10-21 2026-10-21 19:10（北京时间）写入，只记录现状，不补完、不修测试。收尾期间**未跑任何测试**，下文测试结果均摘自原执行日志，标明来源。
> 本 PR 带标签 `paused-refactor`，标题前缀「[暂停-待重构]」。不合并、不部署；未删除任何分支、worktree、容器、stash 或数据库。

## 目标与编号
- 销售看板 D-44（方案、决策、后端、前端、runbook）；分支内文档：docs/handoff/2026-10-21-sales-dashboard-d44.md、docs/releases/2026-10-21-sales-dashboard-d44.md、docs/decisions/*趋势与金额区间.md、docs/*前设计方案_2026-10-21.md。
- 附带：docs/BRANCH_POLICY.md 按金臣 2026-10-21 17:06 裁定取消 main 强制 APPROVED review（commit 5353cb3）。

## 已完成 / 未完成
- 已完成：D-44 后端（sales_dashboard.py、sales_analytics 扩展）、前端（趋势/分组/概览 Tab、金额区间筛选等）、测试、runbook、交接文档（c2b3c43 为止）；main(f95f933) 已合入分支。
- 未完成：Codex 双审结论「必须改」的全部问题（见下）均未修；产品未决项未裁定。
- **合并未完成的说明**：本地 wt-mergemain-442 在 18:43 做过一次 merge main（1300a2f）+ BRANCH_POLICY 文档提交（5353cb3），未推送；暂停期间远端又被推入 b51e902（merge main）、a3b612a（交接文档更新），提交者 "Claude"，时间 18:51、19:02（北京时间，**晚于暂停令**，非开发机进程推送，推测为金臣自己的 Claude Code 会话）。收尾执行者用普通 merge 把远端合入本地（848985d，无冲突，相对远端仅多 docs/BRANCH_POLICY.md 一个文件），再普通 push，未 force、未 rebase。

## 当前状态
- 暂停收尾时代码 head：`848985d`（父：5353cb3 + a3b612a）；本交接文档提交在其后，最终 head 见 PR。
- 测试（摘自 ~/workspaces/reviews/mergemain-442-progress.md 与 mergemain-442-logs/rc.txt，对象为本地 merge 后 5353cb3）：uv sync --locked rc=0；alembic heads rc=0；`pytest tests/test_sales_dashboard_d44.py tests/test_sales_analytics_api.py tests/test_sales_detail_hidden.py tests/test_sales_visibility.py` 被暂停 C-c 中断 rc=2（中断前 69 passed）；`pytest tests/test_agent_file_acl.py` 10 passed 但随后被 C-c，rc 记为 130。前端未跑。848985d 本身未跑任何测试。
- CI（gh 查询 ~19:00）：远端 a3b612a 的 CI 前后端均 IN_PROGRESS；848985d 推送后会触发新 run，结果未知。
- Codex 双审对象为 c2b3c434aa（未包含后续 merge）。

## 已知问题（Codex 双审 2026-10-21 18:06–18:38，结论均为「必须改」，以下照抄报告标题）
报告：
- ~/workspaces/reviews/acceptance-20261021/review-442-codex-astra.md（摘要 review-442-codex-astra.last.md；证据 442-astra-evidence/）
- ~/workspaces/reviews/acceptance-20261021/review-442-codex-sol.md（摘要 review-442-codex-sol.last.md；证据 evidence-442-sol/）

Astra（10 项必须改）：
- M01：详情公开行营收，可还原被遮蔽的成交单价（权限红线）
- M02：成本覆盖率和 revenue_costed 漏脱敏；revenue_costed 的结论为“必须改”
- M03：无金额权限仍能使用营收金额区间，违反 §7 明确的 403/null 契约
- M04：全部时间查询漏掉无日期订单，趋势不等于 KPI
- M05：行级名义未税在不同层级舍入，四处金额不等
- M06：金额直方图丢零值，非整千最大值落入错误区间
- M07：空规范名客户计入真实客户及新客
- M08：只实现部分列排序，未达到“任意列升序/降序”
- M09：默认开启对比时，成本覆盖率消失
- M10：点击趋势桶扩大日期范围，下钻金额跳变

Sol（12 项必须改）：
- H01 最高优先：新增行级输出可精确反推已遮蔽单价
- H02 同一行级筛选下金额不守恒
- H03 全部范围的无日期有效单被趋势漏算
- H04 空字符串规范名被当成真实客户
- H05 任意列排序没有完整实现
- H06 按分组名倒序，空组未恒排末尾
- H07 直方图最大值被放进不覆盖它的尾桶
- H08 日期边界产生 HTTP 500
- H09 默认上月对比隐藏成本覆盖率
- H10 多选后点击分组未收窄到该组
- H11 趋势下钻扩大原日期范围
- H12 新增长文字对比度低于审查标准

其他坑：
- BRANCH_POLICY.md 改了，但 AGENTS.md:12-14、.github/pull_request_template.md:14、.github/CODEOWNERS 注释及若干历史 handoff 仍引用“main 强制审批”，未同步（见 mergemain-442-progress.md）。
- 测试容器 mergemain-pg（127.0.0.1:33070）保留。

## 未决业务问题（摘自审查报告“需产品确认”）
- customer_type 为 NULL（显示“未填”）是否可筛选；金额排序回退字段（revenue vs 单数）与条文文字差异。详见两份报告第 4 节。

## 下一步怎么接
1. 先确认金臣自己的 Claude 会话是否仍在往该分支推送，避免两边并行；2. 按两份报告修必须改（H01/M01 权限红线优先）；3. 新 SHA 重新独立验收。

## 涉及文件与模块
- 后端：backend/app/api/sales_analytics.py、services/sales_analytics.py、services/sales_dashboard.py、config.py；tests/test_sales_dashboard_d44.py
- 前端：frontend/src/api/salesAnalytics.ts、components/charts/SalesDashboardCharts.tsx、pages/sales/analytics/*（AmountRangeFilter、FilterBar、GroupsTab、OverviewTab、TrendTab、DrillTable、DetailDrawer、SummaryView、urlState、useAsyncData）及测试
- 文档：CONTEXT.md、docs/BRANCH_POLICY.md、docs/handoff|releases|decisions 下 D-44 文档

## 代码作者（按模型）
- c2b3c43 及之前、b51e902、a3b612a：Claude Code（金臣自己的会话，提交者 "Claude"），具体模型未知。
- 1300a2f（merge main）、5353cb3（BRANCH_POLICY 文档）：mergemain-442 执行代理，模型未记录。
- 848985d（合并远端）：暂停收尾执行者手工 merge，无代码生成。
