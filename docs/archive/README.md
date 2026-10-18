# docs/archive：已归档文档

本目录存放已完成使命或已被取代的文档，**不再更新，只作历史与证据**。归档按原路径保留子目录（`docs/X/Y.md` → `docs/archive/X/Y.md`），每个文件首行写明归档日期、原因和替代文档。口径以 `docs/decisions/` 和 `CONTEXT.md` 为准，不要让归档文档参与新决策。

维保早期文档的归档另见 [`docs/maintenance/archive/`](../maintenance/archive/README.md)（2026-08-16 整理，保留原位）。

| 文件 | 归档日期 | 原因 | 替代文档 |
| --- | --- | --- | --- |
| [handoff/2026-08-10-development-handoff.md](handoff/2026-08-10-development-handoff.md) | 2026-10-18 | Issue #240 已于 2026-09-06 关闭（交接使命完成）；侧分支基线已被 2026-10-10 分支规则取代 | [AGENTS.md](../../AGENTS.md)、[BRANCH_POLICY.md](../BRANCH_POLICY.md) |
| [handoff/2026-09-03-workbook-full-edit-handoff.md](handoff/2026-09-03-workbook-full-edit-handoff.md) | 2026-10-18 | 所交接分支已经 PR #304 于 2026-09-03 合入 main | [AGENTS.md](../../AGENTS.md) |
| [handoff/ONBOARDING-云上ClaudeCode接手.md](handoff/ONBOARDING-云上ClaudeCode接手.md) | 2026-10-18 | 阅读顺序与纪律已并入 AGENTS.md（唯一协议入口） | [AGENTS.md](../../AGENTS.md) |
| [handoff/prompt-frontend-redesign.md](handoff/prompt-frontend-redesign.md) | 2026-10-18 | 一次性执行 prompt，任务已完成 | [handoff/nav-redesign-issue.md](../handoff/nav-redesign-issue.md) |
| [handoff/prompt-verify-patches.md](handoff/prompt-verify-patches.md) | 2026-10-18 | 一次性执行 prompt，任务已完成（迁移 `d6e1f4a8c3b5` 已在 main） | [maintenance/REQUIREMENTS.md](../maintenance/REQUIREMENTS.md) #47/#48 |
| [handoff/2026-09-12-return-ledger-progress.md](handoff/2026-09-12-return-ledger-progress.md) | 2026-10-18 | PR #323 已于 2026-09-14 合入；卡墙分母已被 D-31 取代 | [D-31](../decisions/0009-返还数量上限与需求单核对.md)、[CONTEXT.md](../../CONTEXT.md) |
