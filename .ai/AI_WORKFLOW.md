# AI Development Workflow

> 所有 AI Coding Agent（Claude Code、OpenCode、Cursor 等）在本仓库开发时必须遵守本协议。
> 目标是让不同 Agent 可以无缝接替，不依赖聊天历史。

---

## Phase 0: Before Any Code Change

任何 AI Agent 开始工作前，**必须按顺序执行**：

### Step 1 — 读取项目上下文

```
Read: .ai/AI_WORKFLOW.md          → 本协议
Read: CONTEXT.md                  → 领域语言与数据可信边界
Read: docs/decisions/0001-维保整改八条口径.md → 业务拍板 D-01…D-16（后拍板覆盖先拍板）
按需: docs/maintenance/ARCHITECTURE.md / REQUIREMENTS.md、docs/adr/
本机若有(不入 Git): HANDOFF.md、memory/MEMORY.md（生产拓扑、部署陷阱、历史修复链）
```

### Step 2 — 分析当前状态

```
- 检查当前 git 分支和状态
- 读取要修改的文件（Read，不要假设内容）
- 理解上下游依赖（谁调这个模块，这个模块调谁）
- 检查是否有相关的架构/业务决策（`docs/adr/`、`docs/decisions/`）
```

### Step 3 — 确认任务边界

```
- 这次要解决什么问题？
- 影响范围是哪些文件/模块？
- 哪些文件绝对不能动？
- 完成标准是什么？（测试通过？构建通过？UI 截图？）
```

### Step 4 — 输出实施计划（Plan-First 协议）

> **触发条件**：涉及 >1 文件、新增功能/API/页面、Bug 修复、重构、数据模型变更。
> **例外**：单行 typo、格式化、用户明确说"直接做"。

以 **Markdown 格式** 输出以下内容。计划必须包含 **5 个必需章节**：

```markdown
## 计划：<一句话标题>

### 1. 要解决的问题 (Problem)
当前状态 + 为什么必须改（2-3 句话）

### 2. 达成的目的 (Goal)
改完后能做什么（业务语言）+ 本次不改什么

### 3. 实现的路径 (Implementation Plan)
- [ ] Step N: <文件路径> → <改动内容> → <原因>

### 4. 验收标准 (Acceptance Criteria)
- [ ] 测试：<哪些测试必须通过>
- [ ] 构建：tsc + vite build 无错误
- [ ] 行为：<用户可感知的验收点>
- [ ] 留痕：.ai/CHANGELOG.md 已追加记录（含 commit SHA）

### 5. 影响面与风险 (Impact & Risk)
- 改动文件数 / 是否架构变动 / 是否破坏接口 / 是否需要迁移 / 已知风险
```

**计划确认规则（按风险分级）**：输出计划后，常规实现直接继续执行，不必等待用户逐字批准。
只有当计划涉及以下内容时，先停下等用户决策：业务口径变化、架构变更、破坏性接口改动、
破坏性/批量数据操作、生产访问。实现细节的最小安全选择由 Agent 自行决定。

---

## Phase 1: During Development

### 原则

1. **小步修改** — 每次只改一个问题，不要顺手重构无关模块
2. **单一职责** — 一个 commit 只做一件事
3. **不破坏已有接口** — 修改 API 参数/响应结构必须先讨论
4. **不主动改变架构** — 除非任务明确要求架构变更
5. **保持代码风格一致** — 模仿现有代码的命名、缩进、注释风格
6. **不修改无关文件** — 不要"顺便优化"旁边的文件

### 技术规范

- **Backend:** type hints, Pydantic 校验, pytest 测试, ruff 格式
- **Frontend:** TypeScript strict, hook rules, vitest 测试
- **Database:** 所有 schema 变更必须有 Alembic migration
- **Security:** 不硬编码密钥，不过滤掉权限检查，不跳过 RBAC

---

## Phase 2: After Completion

### 必须执行

1. **运行测试**
   ```bash
   cd backend && uv run --extra dev pytest -q  # 后端全量
   cd frontend && npm run test                   # 前端全量
   ```

2. **检查构建**
   ```bash
   cd frontend && npm run build   # tsc + vite build
   ```

3. **检查 lint**（如配置了的话）

4. **更新文档（留痕协议，硬性要求）**
   - 修改了业务口径 → 确认与 `docs/decisions/0001` 的 D-xx 一致；拍板变更需用户确认后记录
   - 修改了架构 → 写 ADR（`docs/adr/`，含"原有→新→原因→影响"）
   - **任何变更** → 追加 `.ai/CHANGELOG.md`，**必须含**：before（改动前状态）/after（改动内容）/原因（Issue 编号）/验证结果/**commit SHA**
   - 收尾自答 0.3 检查清单（原有状态？变成什么？为什么？是否架构变动？影响面？）

5. **Git 提交与推送（已授权自动执行）**
   ```bash
   git add <changed files>
   git commit -m "type(scope): 中文描述 (#issue)"
   git push -u origin <branch>
   ```
   验证通过后在功能分支上直接提交并推送，commit message 必须独立说明"改了什么、为什么"；
   提交后把 **commit SHA 回填到 CHANGELOG 记录**（同一次会话内完成，不留空）。
   **边界**：合并分支/PR、部署、生产操作不属于本授权——必须用户单独批准。

---

## Phase 3: Review Protocol

代码提交前，AI 必须自审：

### AI_REVIEW_CHECKLIST（自审清单）

```
□ 是否破坏架构分层
□ 是否引入重复代码
□ 是否有安全漏洞（SQL 注入、硬编码密钥、权限绕过）
□ 是否有测试
□ 是否影响已有接口
□ 是否符合业务规则
□ 是否更新了文档
```

---

## Special Rules for This Project

### Beta 功能开发

- 新 Beta 功能必须加总闸（`maintenance_beta_enabled` 等）
- 路由必须加白名单守卫
- 测试必须覆盖"总闸关闭时返回 404/403"的场景

### 维保模块开发

- 成本相关逻辑必须在 `services/maintenance_cost*.py` 中
- 迁移脚本必须可逆
- 涉及取价链/证据冻结的逻辑，测试必须覆盖 append-only 审计

### AI Agent 模块开发

- 新 Agent 工具定义在 `agent/tools.py`
- 新 Skill Playbook 定义在 `agent/skills.py`
- System prompt 变更在 `agent/prompts.py`
- 不修改 agent runtime loop 除非任务明确要求

---

## Agent Handoff Protocol

当一个 AI Agent 接手另一个 Agent 的工作时：

1. 读取 `.ai/CHANGELOG.md` 了解最近的变更
2. 读取 `docs/decisions/0001-维保整改八条口径.md` 与 `docs/adr/` 了解最近的决策
3. 读取 `CONTEXT.md` 了解领域语言
4. 运行 `git log --oneline -20` 查看最近提交
5. **不要基于"我认为"做假设**——代码和文档是唯一真相
6. 如果某个变更**找不到** CHANGELOG 记录或 ADR，视为留痕缺失，先补齐再继续，不得跳过
