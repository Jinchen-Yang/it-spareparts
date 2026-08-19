# IT 备件智能管理系统 — Claude Code 兼容入口

> `AGENTS.md` 是本仓库对所有开发 Agent 的唯一通用规则源。Claude Code 开始任务时先完整读取根目录 `AGENTS.md`，不要在本文件维护第二份项目状态。

仓库是单体部署应用：FastAPI/PostgreSQL 后端位于 `backend/`，React/Vite/TypeScript 前端位于 `frontend/`，通过 Docker Compose 发布。

开始开发前：

1. 读取 `AGENTS.md`。
2. 确认父 Feature、自己的实现子 Issue、Workstream 和冻结的 Contract SHA，并读取 `docs/agents/issue-tracker.md`、`docs/agents/collaboration.md`。
3. 按任务读取 `CONTEXT.md`、相关 ADR、模块文档和 `.ai/contracts/`。
4. 只在该子 Issue 的独立 worktree/branch 与 Owned paths 中工作，不进入用户已有脏工作区。

需要在本地启动或驱动应用时，可使用 `.claude/skills/run-it-spareparts/`。Claude Code 专属命令只放在本文件；业务口径、任务进度和架构决策不得重复记录在这里。
