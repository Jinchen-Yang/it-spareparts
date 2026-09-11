# it-spareparts — agent guide

Single deployable app: **FastAPI backend** (`backend/`, Python ≥3.11 · uv · Alembic ·
SQLAlchemy 2 · PostgreSQL 15) + **React 18/Vite/AntD frontend** (`frontend/`), Docker
Compose 交付。Issues/PRD 在 GitHub issues（`Jinchen-Yang/it-spareparts`）。共享工作流见
`.ai/AI_WORKFLOW.md`（所有 Agent 必读）。

## 授权边界（对所有 Agent 生效）

**无需询问即可执行**：读写仓库文件、安装依赖、跑测试/类型检查/构建、起本地开发服务
与测试库（Postgres :5433）、加载已审阅的 Skills、Git 建分支/提交/推送功能分支。

**必须先问用户**：合并分支或 PR、部署/发布/生产迁移/回滚、任何生产访问（含只读查询）、
敏感数据导出、破坏性或批量数据操作；以及任何改变业务口径、架构或破坏接口的决策。
上一阶段的授权不推导下一阶段——测试通过不等于可以合并，合并完成不等于可以部署。

## 常用命令

```bash
# 后端测试（本机 Linux 原生 pytest；需 :5433 Postgres，conftest 自建测试库）
cd backend && uv run --extra dev pytest -q

# 迁移链验证（CI 同款：升级 + 零漂移 + 单头）
cd backend && uv run --extra dev alembic upgrade head && uv run --extra dev alembic check && uv run --extra dev alembic heads

# 前端（build = tsc && vite build；test = vitest run；无独立 lint/typegen 脚本）
cd frontend && npm run test && npm run build

# 本地全套起栈（Postgres + 后端 :8000 + 前端 :5176，幂等）
.claude/skills/run-it-spareparts/dev-up.sh
```

## 导航

1. **`.ai/AI_WORKFLOW.md`** — 共享开发工作流（Phase/验收/留痕，所有 Agent 遵守）。
2. **`CONTEXT.md`** — 领域单一上下文与数据可信语言；**`docs/decisions/0001-维保整改八条口径.md`**
   — 业务拍板 D-01…D-16（后者覆盖前者）；`docs/adr/`、`docs/maintenance/` — 架构与需求。
3. **本机专属（不入 Git）**：`HANDOFF.md`（交接总文档）、`memory/`（生产拓扑、部署通道
   与陷阱、历史修复链，入口 `memory/MEMORY.md`）。查背景先翻这里，别重新考古。

## 红线（违者即生产事故）

- **生产库只读**：查询 = `ssh ybwznt → docker exec -i db-1 psql`（SQL 走 stdin heredoc）；
  写数据一律走带审计的应用 API，绝不直接 UPDATE 生产表。
- 部署 ff 前 **must fetch**（防假 fast-forward 盖掉别人的提交）；版本核对看 docker 镜像标签。
- yabowei.xyz §13 已取消，勿执行。
- 升版三件套 / 日历雷 / 哨兵假阳性——细节见 `memory/glm-backend-dual-baseline-topology.md`。

## Skills（`.claude/skills/`，Claude Code 与 OpenCode 共用，来源见 PROVENANCE.md）

`run-it-spareparts`（本地起栈/冒烟/截图）、`db-change-safety`（数据库变更安全）、
`vertical-slice-contract`（跨层一致性）、`regression-gate`(回归验收)、
`supabase-postgres-best-practices`（仅取通用 PG 建议）、`vercel-react-best-practices`
（限 React 18 + Vite）、`web-design-guidelines`（固定版本规则，AntD 风格优先）。
