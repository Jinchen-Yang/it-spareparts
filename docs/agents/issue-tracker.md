# Issue Tracker：GitHub

GitHub Issues 是动态任务状态的权威来源；Draft PR 是分层实现与交接证据的权威来源。Agent session、聊天摘要和本地 `TODO` 不能替代它们。

仓库：`Jinchen-Yang/it-spareparts`。在 clone 内运行 `gh` 时优先由 Git remote 自动解析仓库。

## 1. 父 Feature 与实现子 Issue

| 对象 | 解决什么问题 | 写代码 | 主要 Owner |
| --- | --- | --- | --- |
| 父 Feature Issue | 聚合业务验收、契约、依赖 DAG、集成环境和最终状态 | 否 | Task/Integration Owner |
| Contract 子 Issue | 冻结跨层字段、语义、示例和 Contract SHA | 可以 | Contract Owner |
| Database 子 Issue | 实现 schema、model、constraint、migration | 是 | Database Write Owner |
| Backend 子 Issue | 实现 service、API、Pydantic/OpenAPI contract | 是 | Backend Write Owner |
| Frontend 子 Issue | 实现 TS contract、API 调用、页面与交互 | 是 | Frontend Write Owner |
| Integration 子 Issue | 必要的跨层测试或集成修复 | 必要时 | Integration Write Owner |
| Draft PR | 暴露准确 diff、SHA、验证和下游解锁条件 | 对应子 Issue | 子 Issue Write Owner |

一个 Feature 可以并行存在多个实现子 Issue。“同一时刻一个 Write Owner”只约束单个实现子 Issue；父 Issue 不挂 `agent:*` 标签，不持有实现 branch。

每个子 Issue 必须在正文链接父 Issue，父 Issue 的 Workstreams 清单也必须反向链接子 Issue。仓库支持原生 Sub-issues 时可以同时使用，但双向 Issue 链接仍要保留在正文中。

## 2. 父 Issue 维护内容

父 Issue 至少维护：

```markdown
## Workstreams
- [ ] Contract: #... / PR #...
- [ ] Database: #... / PR #...
- [ ] Backend: #... / PR #...
- [ ] Frontend: #... / PR #...
- [ ] Cross-layer integration: #... / evidence
- [ ] Deployment
- [ ] User acceptance

## Contract freeze
- Contract PR:
- Contract SHA:
- Frozen by:
- Frozen at:
- Applies to: #..., #...
- Compatibility:

## Dependency DAG
- #... blocks #...
- Merge order: Contract -> DB -> Backend -> Frontend -> Integration

## Integration environment
- Environment:
- DB migration/head:
- Backend SHA:
- Frontend SHA:
- Snapshot/seed:
- Verified at:
- Owner:
```

父 Issue 只聚合可复核状态，不复制三个子 Issue 的完整日志。

## 3. 实现子 Issue 字段

每个实现子 Issue 必须明确：

```text
Parent feature:
Workstream: contract / database / backend / frontend / integration
Consumes: 上游 contract、artifact 和准确 SHA
Produces: 给下游的 contract、artifact 或行为
Contract SHA:
Owned paths:
Forbidden paths:
Depends on:
Blocks:
```

字段不明确时，Agent 只能做只读诊断。涉及共享边界文件时，先由 Contract/Integration Owner 建立或更新 contract/integration 子 Issue。

## 4. Claim

先只读检查子 Issue、父 Issue、开放 PR、远端 branch 和路径重叠。取得本次 GitHub 写操作授权后，在实现子 Issue 留下：

```markdown
## Agent claim
- Parent feature: #...
- Workstream: contract / database / backend / frontend / integration
- Executor: Kimi Code / ZCode / OpenCode / Codex / Claude Code
- Model: <model>
- Base SHA: <full remote SHA>
- Branch: <branch>
- Worktree: <absolute or host-qualified path>
- Consumes: <contract/artifact + exact SHA>
- Produces: <contract/artifact/behavior>
- Contract SHA: <full SHA or not-frozen>
- Owned paths: <paths>
- Forbidden paths: <paths>
- Depends on: #...
- Blocks: #...
- Next checkpoint due: <ISO-8601 with timezone>
```

Claim 不是全仓库锁：

- 同一实现子 Issue 同一时刻只有一个 Write Owner。
- 不同 Workstream 的子 Issue 可以由不同 Agent 并行实现。
- 第二个 Agent 可以只读 Review，但不得修改作者 worktree。
- 接力前，原 Owner 先写 checkpoint，并标记 `Released for handoff`；新 Owner 再 Claim。
- Claim 中不得写凭据、连接串、生产数据或其他秘密。

## 5. Checkpoint 与失联回收

子 Issue 的 checkpoint：

```markdown
## Agent checkpoint
- Head SHA: <full SHA or uncommitted>
- Contract SHA consumed:
- Completed:
- Produced:
- Evidence:
- Remaining:
- Blocked by:
- Next step:
- Files touched:
- Next checkpoint due:
- Handoff status: continuing / ready-for-review / released-for-handoff
```

如果 `Next checkpoint due` 已过：

1. Task/Integration Owner 只读核对 Issue、Draft PR、远端 branch 和最后可见 SHA；
2. 在 Issue 提醒原 Owner；
3. 确认无人继续写后，基于可见事实发布 checkpoint，并标记 `Claim released as stale`；
4. 新 Owner 从记录的 SHA 重新 Claim。

逾期不触发自动删除、force-push、覆盖 worktree 或推断未提交进度。

## 6. 常用只读命令

```bash
gh issue view <number> --comments
gh issue list --state open --json number,title,labels,updatedAt
gh pr list --state open --json number,title,isDraft,headRefName,baseRefName,updatedAt
gh pr view <number> --json body,comments,reviews,commits,statusCheckRollup
gh label list
git ls-remote --heads origin
```

创建 Issue、建立父子关系、发表评论、改标签、推送分支、创建或修改 PR 都会改变 GitHub 状态，必须先获得相应授权。授权后的任务内更新应保持在已确认的父/子 Issue 范围内。

## 7. 完成与关闭

实现子 Issue 可在自身验收完成且 PR 合并后关闭，但这不代表父 Feature 完成。父 Issue 只有在以下条件均满足时才关闭：

1. 必需子 Issue 与 PR 全部达到要求；
2. 最终 DB、Backend、Frontend 实现与冻结 Contract SHA 一致；
3. 跨层 tracer-bullet 有准确环境和 SHA 证据；
4. 迁移、回填、重导、发布等后续动作已明确记录；
5. 若范围包含生产生效，已记录 deployed SHA 与生产验证；
6. 若范围包含用户验收，已记录真实用户结果。

`单层可合并`、`Feature 集成通过`、`已部署` 和 `已验收` 是不同状态。
