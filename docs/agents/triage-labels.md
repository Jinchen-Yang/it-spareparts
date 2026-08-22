# Triage、Workstream 与执行标签

Issue 正文和父子链接是完整事实源；标签只用于快速筛选。父 Feature 不挂执行器标签，Workstream、Agent 和日常执行状态主要放在实现子 Issue。

## 1. Canonical triage 标签

| 标签 | 含义 |
| --- | --- |
| `needs-triage` | 尚未完成范围与优先级判断 |
| `needs-info` | 等待需求方补充关键信息 |
| `ready-for-agent` | 验收、契约和边界足够明确，可由 Agent 认领 |
| `ready-for-human` | 需要人工业务判断或人工实施 |
| `wontfix` | 明确不处理，并在 Issue 留下理由 |

## 2. Workstream 标签

以下标签只挂在实现子 Issue：

| 标签 | 归属 |
| --- | --- |
| `area:contract` | 跨层契约冻结与共享边界 |
| `area:database` | schema、model、constraint、migration |
| `area:backend` | service、API、Pydantic/OpenAPI |
| `area:frontend` | TypeScript API contract、页面与交互 |
| `area:integration` | 跨层测试或必要的集成修复 |

每个实现子 Issue 通常只有一个主要 `area:*` 标签。必须跨两个边界时，优先拆 Issue；确实不可拆时由 Integration Owner 记录路径所有权。

## 3. 执行状态标签

| 标签 | 使用位置 | 含义 |
| --- | --- | --- |
| `status:in-progress` | 子 Issue | 已有 Write Owner 正在实施 |
| `status:blocked` | 子 Issue | 已停止推进，Issue 中有精确 blocker |
| `status:review` | 子 Issue | 分层实现已形成，等待独立 Review |
| `status:integration` | 父 Feature | 分层 PR 已就绪，正在做最终跨层集成 |

父 Issue 在普通分层开发阶段通过 Workstreams 清单聚合状态，不同时挂三个 `agent:*` 或三个 `area:*`。

## 4. Agent 标签

以下标签只挂在实现子 Issue：

| 标签 | 执行器 |
| --- | --- |
| `agent:kimi-code` | Kimi Code |
| `agent:zcode` | ZCode |
| `agent:opencode` | OpenCode |
| `agent:codex` | Codex |

需要其他执行器时，按同一 `agent:<name>` 规则新增；不要用模型版本代替执行器名称。

## 5. 状态流转

实现子 Issue：

```text
needs-triage
  -> needs-info -> needs-triage
  -> ready-for-human
  -> ready-for-agent + area:<workstream>
       -> status:in-progress + agent:<executor>
       -> status:blocked
       -> status:review
       -> merged / closed
  -> wontfix
```

父 Feature：

```text
workstreams active
  -> status:integration
  -> integration passed
  -> deployed（若范围要求）
  -> accepted（若范围要求）
```

- Claim 时，子 Issue 从 `ready-for-agent` 转为 `status:in-progress` 并添加一个执行器标签。
- 阻塞时使用 `status:blocked`，同时写清解除条件和被阻塞的下游 Issue。
- 发起 Review 时使用 `status:review`；Reviewer 不替换 Write Owner 的 Agent 标签。
- 移交时先写 checkpoint，再替换执行器标签。
- 子 PR 合并后清理临时执行状态；父 Feature 只有进入跨层验证时才使用 `status:integration`。

先用 `gh label list` 核对仓库当前标签。缺失标签不能被假定存在，也不得在未授权时静默创建；标签暂未配置时，父子链接、Claim 和 checkpoint 仍是权威记录。
