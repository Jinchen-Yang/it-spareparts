## 父子 Issue 与执行信息

- Parent Feature: #
- Implementation Issue: #
- Workstream: contract / database / backend / frontend / integration
- Executor:
- Base SHA:
- Head SHA:
- Status: Draft / Ready for review

## 上下游契约

- Consumes:
- Contract PR:
- Contract SHA:
- Upstream Issue/PR/SHA:
- Produces:
- Blocks / unlocks:
- Contract changed by this PR: no / yes（若 yes，必须重新冻结）

## 路径所有权

- Owned paths:
- Forbidden paths:
- 实际修改路径：
- 共享边界文件及批准人：
- 明确未修改：

## 问题与目标

<!-- 用业务语言说明该 Workstream 解决什么、依赖什么、给哪个下游消费，以及本 PR 明确不解决什么。 -->

## 数据链路与备件影响

- 链路判断：A Excel -> DB / B DB -> Service -> API -> UI / C 跨链路 / 非数据链路
- 备件如何进入、关联、计算和展示：
- 若与备件无关，原因：

### 表结构事实

| 表 | 关键字段 | 本 PR 中的职责 | 读/写 | 证据来源 |
| --- | --- | --- | --- | --- |
| | | | | |

## 分层实现与兼容性

- 本层实现：
- API / Excel / DB contract 兼容性：
- Alembic migration：
- 历史回填或重导：
- Mock / fixture 与真实实现差异：
- Feature flag / 灰度：
- 发布或人工确认：

## 依赖与集成顺序

- Depends on:
- Expected merge order:
- 下游可开始的条件：
- 下游必须重测的条件：
- Integration Owner:

## 验证证据

| 层级 | 命令、环境或证据 | 使用的准确 SHA | 结果 |
| --- | --- | --- | --- |
| Contract 校验 | | | 未运行 / 通过 / 失败 |
| 本 Workstream 测试 | | | 未运行 / 通过 / 失败 |
| 前端 typecheck/build | | | 不适用 / 未运行 / 通过 / 失败 |
| GitHub CI | | | 未运行 / 通过 / 失败 |
| 共享集成环境 | | DB / BE / FE / Contract | 不在范围 / 未验证 / 通过 |
| 跨层 tracer-bullet | | DB / BE / FE / Contract | 不在范围 / 未验证 / 通过 |
| 生产验证 | | deployed SHA | 不在范围 / 未验证 / 通过 |
| 真实用户验收 | | | 不在范围 / 未验收 / 通过 |

## 集成环境清单

<!-- 本 PR 未使用共享环境时写“不适用”。不得填写凭据或生产敏感数据。 -->

- Environment:
- DB migration/head:
- Backend SHA:
- Frontend SHA:
- Contract SHA:
- Snapshot/seed:
- Verified at:
- Owner:

## Review 重点

- 请重点检查：
- 已知风险或待确认：
- Reviewer 使用的 head SHA：
- 是否满足下游解锁条件：

## Checkpoint / Handoff

- Completed:
- Produced:
- Remaining:
- Blocked by:
- Next step:
- Next checkpoint due:
- Write Owner status: continuing / ready-for-review / released-for-handoff

## Gate 清单

- [ ] 父 Feature 与实现子 Issue 均已关联
- [ ] Consumes/Produces、Contract SHA、依赖与阻塞关系准确
- [ ] 只修改 Owned paths；共享边界文件已有明确 Owner
- [ ] 数据库/模型/迁移事实已按本层范围核对
- [ ] API 变化已同步检查 schema、OpenAPI、前端类型和消费者
- [ ] Mock 与真实 Backend 的差异及重测要求已记录
- [ ] 验证结论区分单层 CI、Feature 集成、部署和用户验收
- [ ] 文档、Issue 和 PR 中没有凭据或生产敏感数据
- [ ] 迁移、回填、重导、发布和人工确认已列出

## 最终结论

<!-- 只能选择有证据的一项。单层 PR 绿色不等于父 Feature 已集成。 -->

- [ ] 子 PR 尚不可合并
- [ ] 子 PR 本层可合并（父 Feature 尚未集成）
- [ ] 父 Feature 跨层集成已验证
- [ ] 已验证生产生效
