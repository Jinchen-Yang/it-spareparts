---
name: plan-first
description: >
  REQUIRED before EVERY non-trivial code change. Triggers when user asks to implement,
  build, fix, add, modify, or refactor anything that touches more than one file. Forces
  a structured plan → developer review → approval gate → implementation → verification
  cycle. Blocks all code edits until the plan is approved. Not for trivial one-liners
  (typo fix, format-only, user explicitly says "直接做").
---

# Plan-First Development Protocol

> 所有非 trivial 代码变更的强制门禁。不写计划不改代码，计划不批准不提 PR。

## 触发条件（满足任一即触发）

- 涉及 **>1 个文件** 的修改
- 新增功能、模块、API、页面
- Bug 修复（非明显的单行拼写/格式错误）
- 重构或架构变更
- 数据模型 / 迁移脚本变更
- 用户说"我要做 X"但没给具体步骤

**不触发**：单文件改一个字符串、修复一个明显的 typo、用户明确说"直接做"/"不用写计划"。

---

## Phase 0: Context Loading（先读懂）

按顺序读取以下文件，**在你的回复中列出你读了哪几份**：

```
1. .ai/PROJECT_CONTEXT.md     → 产品目标、用户角色、当前阶段
2. .ai/ARCHITECTURE.md         → 模块职责、分层规则、数据流
3. .ai/BUSINESS_RULES.md       → 业务规则、计算公式、约束条件
4. .ai/DEVELOPMENT_RULES.md    → 编码规范、留痕协议
5. .ai/CURRENT_TASK.md         → 当前正在进行的任务（避免冲突）
6. .ai/DECISIONS.md            → 相关 ADR（如果任务涉及架构决策）
```

然后做现状分析：

- 读取所有将被修改的文件（**禁止凭记忆/猜测内容**）
- 找出调用方和被调用方（谁依赖这个模块，这个模块依赖谁）
- 检查是否有未提交的变更或冲突分支

---

## Phase 1: Plan Output（写给开发者看的计划书）

以 **Markdown 格式** 输出以下内容。计划必须包含 **5 个必需章节**：

### 1. 要解决的问题 (Problem)

```
用 2-3 句话描述：
- 当前状态是什么（具体到文件/行为）
- 用户/系统遇到了什么问题
- 为什么现在必须解决
```

### 2. 达成的目的 (Goal)

```
- 改完后用户/系统能做什么（用业务语言，不是技术语言）
- 不改的范围是什么（明确本次不做什么）
```

### 3. 实现的路径 (Implementation Plan)

```
每个步骤一行，格式：
- [ ] Step N: <文件路径> → <改动内容> → <原因>
```

### 4. 验收标准 (Acceptance Criteria)

```
- [ ] 测试：<哪些测试必须通过>
- [ ] 构建：tsc + vite build 无错误
- [ ] 行为：<用户可感知的验收点>
- [ ] 留痕：.ai/CHANGELOG.md 已追加记录（含 commit SHA）
```

### 5. 影响面与风险 (Impact & Risk)

```
- 改动文件数：X 个
- 是否架构变动：是 / 否（是则必须含 ADR）
- 是否破坏已有接口：是 / 否
- 是否需要数据迁移：是 / 否
- 是否需要 Beta 门控：是 / 否
- 已知风险：<如果有>
```

---

## Phase 2: Gate — WAIT（等开发者批准，不等不写）

输出计划后，**停止一切代码编辑**。用以下话术等待：

```
以上是实施计划，请逐项确认后再开始写代码。
确认方式：
  - "批准" / "开始" / "go" → 开始实施
  - "第 N 步改一下" → 修改计划后重新呈现
  - "不做第 N 步" → 从计划中移除后重新呈现
  - "直接做第 N 步" → 仅执行该步骤
```

开发者批准后，**才进入 Phase 3**。

---

## Phase 3: Execute（按计划逐步实施）

1. 按 Phase 1 的计划**逐步骤执行**，不跳步，不加戏
2. 每完成一个 Step，更新计划中对应条目的勾选状态
3. 遇到计划未预见的障碍 → 停下来，更新计划，重新请求批准
4. **禁止顺手改无关文件** — 如果改造过程中发现另一个问题，记录下来，单独开计划

---

## Phase 4: Verify & Document（验收并留痕）

### 4.1 自动化验收

```bash
cd backend && uv run --extra dev pytest -q    # 后端全量测试
cd frontend && npm run test                    # 前端全量测试
cd frontend && npm run build                   # tsc + vite 构建
cd backend && uv run ruff check .              # Lint（如果配置了）
```

### 4.2 业务验收

对照 Phase 1 的验收标准逐项勾选确认。

### 4.3 留痕（硬性要求，任一不满足即视为未完成）

| 必须产出 | 写入位置 | 内容要求 |
|---------|---------|---------|
| 任务登记 | `.ai/CURRENT_TASK.md` | 更新状态，勾选已完成步骤 |
| 变更记录 | `.ai/CHANGELOG.md` | before / after / 原因(Issue#) / 验证结果 / **commit SHA** |
| 架构决策 | `.ai/DECISIONS.md` | 仅架构变动时；原有设计→新设计→原因→影响 |
| 架构同步 | `.ai/ARCHITECTURE.md` | 新增模块/路由/服务时必须更新计数和结构图 |
| 业务规则 | `.ai/BUSINESS_RULES.md` | 业务逻辑变更时必须同步 |

### 4.4 提交

```bash
git add <仅相关文件>
git commit -m "type(scope): 中文描述 (#issue)"
# 提交后立即把 commit SHA 回填到 .ai/CHANGELOG.md 对应记录中
```

---

## Quick Reference: 一句话版

```
读完 .ai/ → 写计划（问题/目标/路径/验收/风险）→ 等批准 → 逐步实施 → 测试+构建 → 留痕+提交
```

## Anti-Patterns（绝对不要做的事）

- ❌ 用户说"做个 X"就直接改代码，不写计划
- ❌ 计划里写"改几个文件"但不列具体路径
- ❌ 等批准期间自己先开始写代码
- ❌ 验收时说"应该没问题"但不跑测试
- ❌ 提交后 commit SHA 留空，说"回头补"
- ❌ 顺手改计划外的文件，说"顺便优化了一下"
- ❌ 凭记忆写计划不读实际文件
