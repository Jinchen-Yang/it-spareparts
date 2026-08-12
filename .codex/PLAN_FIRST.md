# Plan-First Development Protocol — Codex Edition

本文件是 IT 备件智能管理系统的 Codex 开发协议。
Codex 默认不强制计划，但本项目配置为 **plan-first 模式**：
所有非 trivial 变更必须先写计划，开发者批准后再实施。

## 适用范围

`/home/cloudlay-3080/Workspaces/it-data-pm/it-spareparts/`

## 触发条件

满足以下**任一**条件时，在编写任何代码之前生成计划：

- 涉及 **>1 个文件**的修改
- 新增功能、模块、API 端点、前端页面
- Bug 修复（非单行拼写/格式错误）
- 重构、架构变更、数据模型变更
- 用户说"我要做 X"但未给出具体步骤

**例外（不触发）**：单文件改一个字面值、修复明显 typo、格式化、用户明确说"直接做"/"不用计划"。

## 协议步骤

### Step 0: Read Context

```
Read: .ai/PROJECT_CONTEXT.md, .ai/ARCHITECTURE.md, .ai/BUSINESS_RULES.md,
      .ai/DEVELOPMENT_RULES.md, .ai/CURRENT_TASK.md
```

### Step 1: Write Plan（Markdown 格式）

```markdown
## 计划：<一句话标题>

### 问题
当前状态 + 为什么必须改

### 目标
改完后能做什么 + 本次不改什么

### 路径
- [ ] Step N: <文件路径> → <改动内容> → <原因>

### 验收
- [ ] 后端测试通过
- [ ] 前端构建通过
- [ ] 行为验收点
- [ ] 留痕完成

### 风险
文件数 / 是否架构变动 / 是否破坏接口 / 是否需要迁移 / 已知风险
```

### Step 2: Gate（停止并等待）

输出计划后**停止**。在开发者回复"批准"/"开始"/"go"之前**不得修改任何文件**。

### Step 3: Execute

逐步执行计划。偏离计划时暂停，更新计划并重新请求批准。

### Step 4: Verify & Document

```bash
cd backend && uv run --extra dev pytest -q
cd frontend && npm run test && npm run build
```

必须更新：`.ai/CURRENT_TASK.md`、`.ai/CHANGELOG.md`（含 commit SHA）。
架构变更：追加 `.ai/DECISIONS.md`。

## 快速记忆

```
读 .ai/ → 写计划 → 等批准 → 逐步实施 → 测试构建 → 留痕提交
```

## 绝对禁止

- ❌ 不读文件就写计划
- ❌ 不等批准就写代码
- ❌ "顺便"改计划外文件
- ❌ 提交不留痕（无 CHANGELOG / 无 commit SHA）
