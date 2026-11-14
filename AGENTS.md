# AGENTS.md — 本仓库对一切 AI 开发代理的工作协议

> 适用对象：Claude Code / Codex / zcode / Cursor / OpenCode 等所有在本仓库工作的 AI 代理。
> 目标：任何代理可以无缝接替，**口径落在仓库文件里，不落在某个会话的脑子里**。
> 本文件是唯一协议入口；`CLAUDE.md` 只是指向本文件的指针。

## 0. 分支、合并与生产基线（最高优先级）

**main 是集成主线，不等于生产；生产以 `release-` 标签为准。test 仅用于远期实验，不是近期功能的必经集成线。**
近期要进入正式系统的功能从最新 main 创建独立分支，PR 指向 main；
不要携入 test 上的其他实验功能。该分工以杨金臣 2026-10-10 的后续更正为准。
**main 合并不再需要 GitHub APPROVED review**（杨金臣 2026-10-21 17:06 裁定）；保留的保护：
两项必需 CI 全绿、分支与 main 同步（strict）、管理员同样受约束、禁止强推和删除 main、讨论须全部解决。
代理合并前完成跨模型独立复审并在 PR 中记录结论，CI 全绿后正常合并。
禁止管理员绕过、关闭或修改保护、强推 main、自动将 test 发布到生产。
**判断是否已上线看发布标签，不看 main**；热修从当前生产标签开 `release/*` 或 `hotfix/*` 分支，上线后合回 main。
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

**UI 改动的验收路径**：基于 main 的候选就绪后报告「待验收」，由用户安排点按验收；
验收环境与远期 test 分支是不同概念，不要求先合入 test。
代理驱动浏览器只用于开发期调试，不代替用户点按验收。何时本地测见 §2.1。

### 2.1 何时跑本地测试 / 何时等 CI（不要事事全量）

**禁止默认行为**：不要对每次改动、每次提交都跑全量 `pytest`、全量前端 `tsc && vitest && vite build`，
也不要为了「保险」空等 GitHub CI 再继续改代码。全量门禁留给 **合入 main 的 PR 必需检查**
（见 §0 / `docs/BRANCH_POLICY.md`），不是日常迭代税。

| 改动类型 | 代理应做 | 不必做 |
|---|---|---|
| 纯文档 / 决策 / PRD / 交接 / 注释 | 读相关口径、保证链接与编号正确 | 本地测、等 CI |
| 后端业务 / API / 迁移 / 权限 | 跑**相关** pytest（单文件或模块）；迁移改动顺带确认 alembic heads | 无关前端全量构建 |
| 前端页面 / 组件 | 对触及范围做 `tsc` 与相关 vitest；关键交互可按需冒烟 | 无关后端全量 pytest |
| 高危模块（§3.3 所列）或并发/权限契约 | 必带/必跑对应契约测试，失败先修 | 用「等 CI」代替本地契约 |
| 准备合入 main 的 PR | 推送后让两项必需 CI 自然跑；红了再针对性修 | 合入前反复手工复跑整仓当仪式 |

用户或任务**明确要求**全量回归、发布候选验收、热修上线前核对时，再跑 §4 全量命令。
CI 红了只修与本 PR 相关的失败；既有 flaky / 跨文件隔离问题记下来，不擅自扩大改动面去「顺手全绿」。

## 3. 四条纪律（都是事故换来的）

1. **不要从历史 PR/issue 反推口径。** 口径只在 `docs/decisions/`；PR 描述里必须写
   `决策: D-NNNN` 或 `决策: 无`（合入 CI 会校验该行存在，与「每次改动都跑 CI」无关）。
2. **近期正式功能以 main 为基线；合并 ≠ 上线。** 合入时两项必需 CI 全绿、分支与 main 同步即可，
   遵循 `docs/BRANCH_POLICY.md`；禁止从功能分支直接部署生产，生产只部署打了 `release-` 标签的精确提交。
3. **同一模块被多工具反复改，必带契约测试。** 高危区：`services/maintenance_project_master_workbook.py`、
   `etl/loader.py`、`services/maintenance_project_identity.py` —— 并发语义与 bump 调用点。
4. **部署不是自动的。** 写生产前先备份、前后端同 commit、`alembic upgrade head` 跑完再启镜像、
   打 `release-` 标签。用户没明确说"开始部署"，不要动生产。

## 4. 常用命令（按需，非每次必跑）

```bash
# 后端：优先单测/模块，不要默认全仓
cd backend && uv run --extra dev pytest tests/<相关文件> -q
# 前端：优先类型 + 相关测试；全量构建留给合入门禁或用户点名
cd frontend && npx tsc && npx vitest run <相关>
# 本地起完整栈 / API 冒烟 / UI 截图：用 .claude/skills/run-it-spareparts/ 技能
```

分支命名沿用现状：`chore/*`、`fix/*`、`feat/*`、`claude/*`、`codex/*` 均可；近期正式功能的 PR base 为 `main`，仅明确安排的远期实验指向 `test`；热修分支 `release/*`、`hotfix/*` 从当前生产标签创建。生产部署须管理员单独授权。

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
