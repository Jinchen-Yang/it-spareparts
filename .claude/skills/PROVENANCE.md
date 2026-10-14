# Skill provenance & local adaptations

本目录（`.claude/skills/`）只放**项目自研技能**，Claude Code 与 OpenCode 共用这一棵树，
不要复制到 `.agents/skills/` 或 `.opencode/skills/`（OpenCode 会全部扫描且要求名称唯一）。

| 技能 | 来源 | 说明 |
|---|---|---|
| `run-it-spareparts` | 项目自研 | 本地起栈 / API 冒烟 / UI 截图；指向生产的驱动须用户明确批准 |
| `db-change-safety` | 项目自研 | PG / SQLAlchemy / Alembic 变更安全流程 |
| `regression-gate` | 项目自研 | 完成度声明与 PR diff 回归门禁 |
| `vertical-slice-contract` | 项目自研 | 跨库/后端/权限/前端垂直切片一致性 |

外部钉版技能（`vercel-react-best-practices`、`supabase-postgres-best-practices`、
`web-design-guidelines` 等）**不入本目录**：由各机用户级 skills 目录安装，固定来源与
SHA256 记录在 `docs/agents/skills-lock.json`（避免与用户级同名技能冲突）。历史上曾把
外部技能整套入库的方案见 `chore/agent-skills-and-permissions` 分支（已废弃该做法）。
