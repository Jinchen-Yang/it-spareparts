# IT 备件智能管理系统 — repo guide for agents

Single deployable app: **FastAPI backend** (`backend/`, Python · `uv` · Alembic · SQLAlchemy ·
PostgreSQL) + **React/Vite/AntD frontend** (`frontend/`), shipped via Docker Compose. Backend
tests run with `pytest` against a Postgres on `:5433`; CI runs backend pytest + frontend
`tsc && vite build`. Work on a branch → PR → squash-merge → deploy (no auto-migration; run
`alembic upgrade head` on deploy).

To **run / drive the app locally** (launch the stack, smoke the API, screenshot the UI), use the
`run-it-spareparts` skill (`.claude/skills/run-it-spareparts/`).

## Agent skills

The engineering skills (`to-issues`, `triage`, `to-prd`, `diagnose`, `improve-codebase-architecture`,
`tdd`, …) read their per-repo configuration from `docs/agents/`:

### Issue tracker

Issues and PRDs live as **GitHub issues** (`Jinchen-Yang/it-spareparts`), driven by the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

**Canonical** label vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`); `wontfix` already exists, the rest are created on first use. See `docs/agents/triage-labels.md`.

### Domain docs

**Single-context** — one `CONTEXT.md` + `docs/adr/` at the repo root (created lazily by `/grill-with-docs`). See `docs/agents/domain.md`.

---

## AI Coding Rules

> 所有 AI Agent（Claude Code、OpenCode、Cursor 等）在本仓库开发时必须遵守。

### Before Coding

1. **Read `.ai/` documents** — `PROJECT_CONTEXT.md` → `ARCHITECTURE.md` → `TECH_STACK.md` → `DEVELOPMENT_RULES.md` → `BUSINESS_RULES.md` → `CURRENT_TASK.md`
2. **Understand architecture** — 分层规则：API → Service → Model，禁止跨层调用
3. **Check current state** — `git status`, `git log --oneline -10`, `.ai/CURRENT_TASK.md`
4. **Make a plan** — 用 TodoWrite 列出步骤，确认用户意图后再动手

### During Coding

1. **Follow existing architecture** — 不在 Router 写 SQL，不在 Service 操作 HTTP 对象
2. **Avoid unnecessary refactoring** — 不顺手改无关文件，不主动改变架构
3. **Small incremental changes** — 每次只解决一个问题
4. **Write maintainable code** — type hints, Pydantic models, 清晰命名
5. **No hardcoded secrets** — 所有密钥走 `.env` 或环境变量
6. **Beta features behind gates** — 新 Beta 功能必须有总闸 + 白名单守卫
7. **Migration scripts required** — 任何 schema 变更必须有 Alembic migration

### Traceability (hard requirement)

> 每次开发必须留痕且可回溯，能讲清"改动前的原有状态"与"架构变动"。详见 `.ai/DEVELOPMENT_RULES.md` 第 0 章。

1. **Trail before** — 开工前在 `.ai/CURRENT_TASK.md` 登记：改动前状态 / 预期改动 / 原因 / 是否架构变动
2. **Trail after** — 完成后在 `.ai/CHANGELOG.md` 追加：before / after / 原因(Issue#) / 验证结果 / **commit SHA**
3. **Architecture** — 架构变动必须写 ADR（`.ai/DECISIONS.md`：原有→新→原因→影响）+ 同步 `.ai/ARCHITECTURE.md`，未写 ADR 的架构变更视为未完成
4. **Traceable commits** — commit message 必须独立说明"改了什么、为什么"，一个 commit 一件事；提交后回填 SHA 到 CHANGELOG
5. **No ghost changes** — 无 CHANGELOG/ADR 记录的变更不得提交；Handoff 时发现留痕缺失先补齐再继续

### After Coding

1. **Run tests** — `cd backend && uv run --extra dev pytest -q`, `cd frontend && npm run test`
2. **Check build** — `cd frontend && npm run build`（tsc + vite）
3. **Self-review** — 逐项检查 `.ai/AI_REVIEW_CHECKLIST.md`
4. **Update documents** — API 变更 → `.ai/API_DESIGN.md`, 架构变更 → `.ai/DECISIONS.md` (ADR), 业务规则 → `.ai/BUSINESS_RULES.md`, 所有变更 → `.ai/CHANGELOG.md`
5. **Update task status** — `.ai/CURRENT_TASK.md`
6. **Commit** — `git add <changed files> && git commit -m "type: 中文描述"`

### Project-Specific Rules

- **Beta 功能：** 默认关闭（`maintenance_beta_enabled=false`, `replenishment_beta_enabled=false`, `enable_agent=false`）
- **数据库：** 单 head Alembic 线性历史，禁止从同一 revision 分叉
- **安全：** 生产模式 `ENVIRONMENT=prod` 下默认弱口令拒绝启动
- **权限：** 所有 API 端点必须鉴权，Beta 路由加白名单守卫
- **成本取价链：** append-only 审计，冻结后不可修改
- **AI 助手：** 仅辅助建议，不自动审批/报价/执行操作
