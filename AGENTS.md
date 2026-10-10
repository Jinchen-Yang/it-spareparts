# AGENTS.md — 本仓库对一切 AI 开发代理的工作协议

> 适用对象：Claude Code / Codex / zcode / Cursor / OpenCode 等所有在本仓库工作的 AI 代理。
> 目标：任何代理可以无缝接替，**口径落在仓库文件里，不落在某个会话的脑子里**。
> 本文件是唯一协议入口；`CLAUDE.md` 只是指向本文件的指针。

## 0. 分支与管理员审批（最高优先级）

**main 是生产发布线，test 是 Beta 测试线。日常功能 PR 指向 test。**
任何 main 合并必须先联系杨金臣（yangjinchen，GitHub **@Jinchen-Yang**），
取得其对当前候选 SHA 的人工 GitHub APPROVED review；CI 通过或他人批准不能替代。
禁止代理代批、管理员绕过、强推 main、关闭保护或自动将 test 发布到生产。
详见 [`docs/BRANCH_POLICY.md`](docs/BRANCH_POLICY.md)，该文件覆盖所有历史冲突指引。

## 1. 项目是什么

**IT 备件智能管理系统**：氚云导出数据（采购/销售/库存/维保）的汇入、治理与经营分析，
FastAPI（`backend/`，Python · uv · SQLAlchemy · Alembic · PostgreSQL）+ React/Vite/AntD（`frontend/`），
Docker Compose 交付。当前主线是维保业务闭环（见 issue #128）与只读自治智能体平台（issue #217）。

## 2. 开工前必读（按顺序，缺一不可）

1. `docs/decisions/` —— 已拍板口径，**唯一真值**。实现与它冲突时，先核对是否已 supersede；
   未覆盖你要做的事，先写新决策编号再动手。
2. `CONTEXT.md` —— 领域统一语言（数据疑点、墓碑、经营事实 vs 计算结果……）。用词必须与它一致。
3. `docs/handoff/` 最新交接文档 —— 当前状态快照与风险。
4. `docs/agents/` —— issue 约定（GitHub issues + `gh` CLI）、triage 标签
   （`needs-triage`/`needs-info`/`ready-for-agent`/`ready-for-human`/`wontfix`）、领域文档规则
   （单一 `CONTEXT.md` + `docs/adr/`）。
5. `git fetch` 对齐远端，**别在过期 ref 上开工**。

**UI 改动的验收路径**：本地回归（pytest / vitest / `tsc && vite build`）完成后报告"待验收"，
由用户把前端构建部署到 **https://test.yunci.ink** 点按验收；代理驱动浏览器只用于开发期调试，
不代替点按验收。

## 3. 四条纪律（都是事故换来的）

1. **不要从历史 PR/issue 反推口径。** 口径只在 `docs/decisions/`；PR 描述里必须写
   `决策: D-NNNN` 或 `决策: 无`（CI 校验该行存在）。
2. **日常开发合入 test；main 只收管理员批准的发布。** 两个必需 CI 检查全绿，
   main 还须 @Jinchen-Yang 的 Code Owner 批准，遵循 `docs/BRANCH_POLICY.md`；禁止从功能分支直接部署生产。
3. **同一模块被多工具反复改，必带契约测试。** 高危区：`services/maintenance_project_master_workbook.py`、
   `etl/loader.py`、`services/maintenance_project_identity.py` —— 并发语义与 bump 调用点。
4. **部署不是自动的。** 写生产前先备份、前后端同 commit、`alembic upgrade head` 跑完再启镜像、
   打 `release-` 标签。用户没明确说"开始部署"，不要动生产。

## 4. 常用命令

```bash
# 后端测试（需要本机 Postgres :5433）
cd backend && uv run --extra dev pytest
# 前端检查与测试
cd frontend && npm ci && npx tsc && npx vitest run && npx vite build
# 本地起完整栈 / API 冒烟 / UI 截图：用 .claude/skills/run-it-spareparts/ 技能
```

分支命名沿用现状：`chore/*`、`fix/*`、`feat/*`、`claude/*`、`codex/*` 均可，日常 PR base 一律 `test`；面向 `main` 的发布 PR 必须走管理员审批。

## 5. 提交与文档约定

- 中文 conventional commits（`feat:` / `fix:` / `chore:` / `docs:`），正文写清动机与验证方式。
- 大改动先开 issue 对齐（含编码前方案文档放 `docs/` 对应目录），**不塞巨型 PR**
  （前科：233 文件/5.8 万行的 PR 被要求拆分分批合）。
- 涉及口径的改动：先 `docs/decisions/` 编号，再写代码；`CONTEXT.md` 与实现同步改。
- 发布能力/验收/回滚写 runbook 进 `docs/releases/`，格式沿用 v1.36 起的版本。

## 6. 红线

- AI 永远不得写采购、销售、库存、项目、维保或申请/审批业务状态（智能体平台不变量，issue #217）。
- 口径未拍板的功能不开发：待确认项统一在 issue #136，宁可停等，不带猜测上线。
- 不做整体重写（语言/框架/前端）；重构一律 strangler 定向整改，每步有测试托底。
- 页面批量 = 逐行编排，不承诺跨行数据库事务（v1.36 契约）。

## 7. 历史文件说明

`.ai/AI_WORKFLOW.md` 是早期工作流草稿，其引用的 `.ai/*.md` 上下文文件多数不存在，
**以本文件为准**；`.ai/` 其余内容为各期实现方案归档，仅作历史参考。
