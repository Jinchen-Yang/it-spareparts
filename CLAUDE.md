# CLAUDE.md

**本文件只是指针。对一切 AI 开发代理（包括 Claude Code）的工作协议，唯一入口是 [`AGENTS.md`](AGENTS.md)。**

开工前按 `AGENTS.md` §2 的顺序阅读：`docs/decisions/`（唯一真值口径）→ `CONTEXT.md`（领域统一语言）→
`docs/handoff/` 最新交接 → `docs/agents/`（issue/标签约定），然后 `git fetch` 对齐远端再开工。

本地跑栈 / API 冒烟 / UI 截图：用 `run-it-spareparts` 技能（`.claude/skills/run-it-spareparts/`）。
工程类技能（`to-issues`、`triage`、`to-prd`、`diagnose`、`tdd` 等）的仓库配置在 `docs/agents/`。
