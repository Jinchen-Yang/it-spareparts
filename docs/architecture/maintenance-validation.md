# 维保重构候选验证回执

基线：`7a9fd2098acb6be0009a59a84e7d7330bb960d6b`。分支：`codex/maintenance-modularity`。验证采用隔离worktree、任务专用PostgreSQL16容器、每pytest进程独立数据库和合成业务数据；未连接生产。前端在隔离副本按锁文件安装依赖后运行，源码与候选保持一致。

## 后端结果

各行是独立运行，部分测试重复，不能将表中数量相加称作去重测试总数。

| 测试范围 | 结果 | 证据日志 |
| --- | --- | --- |
| Agent既有行为、行级筛选、有效读、row workbench、原件同步及HTTP、父实体访问 | 226 passed / 1 skipped | `/tmp/maintenance-modularity-test.log` |
| 架构契约、总表V1/V2编辑、原始回执、源取消级联、角色范围、新旧行过滤 | 284 passed | `/tmp/maintenance-extended-test.log` |
| 保持工具说明顺序后的Agent/catalog回归 | 29 passed / 1 skipped | `/tmp/maintenance-tools-final-test.log` |
| 中立错误、native返还要求、合单错误路径定向组 | 27 passed / 42 deselected | `/tmp/maintenance-error-contract-test.log` |
| 传递依赖约束 | 1 passed | `/tmp/maintenance-transitive-boundary-test.log` |
| 最终工具注册加固与top边界 | 32 passed | `/tmp/maintenance-architecture-artifacts/agent-hardening-green.txt` |

跳过项是既有测试的“合成测试库无指定型号”分支，不是生产缺失事实。上述运行已正常退出并清理各自数据库；清库期间出现checkpoint等待，属于专用容器I/O等待，不据此推断应用死锁。首轮10项公共架构测试也通过，已被扩展组覆盖。

新增直接证据：原18工具schema及顺序一致；启动重名/畸形required/异步handler拒绝；新工具通过真实runtime执行并保留审计；无页面权限时三工具均不触发业务service；top=-1时基线100行样本返回99行、修复后返回1行；项目审计savepoint回滚；旧路径和新路径WorkbookError是同一类型、属性及HTTP映射保持；领域读取入口无工作簿传递import路径。

## 前端结果

- 验收原有与新增生命周期测试：2文件、16 passed。
- 面板/回款/看板相关回归：4文件、143 passed。
- `npm run build`（`tsc && vite build`）：退出码0。
- 切项目预检迟到反例先复现，再测试修复；覆盖卸载、旧确认onOk、apply后切换、读回中切换、同项目新预检取代旧结果。实际请求已发出后不承诺撤回后端事务，只阻止旧UI上下文继续操作或冒称当前项目已刷新。
- 首次借用已有node_modules时缺user-event且备注可见性测试失败；隔离副本执行锁文件`npm ci`后类型检查/构建及相同相关组合全部通过。未改备注组件或相关断言以掩盖失败。
- 日志：`acceptance-repro.log`、`acceptance-fixed.log`、`frontend-related-tests-final.log`、`frontend-build.log`，均在 `/tmp/maintenance-architecture-artifacts/`。

已知警告：既有openpyxl copy弃用、Starlette/httpx弃用、AntD测试环境警告及Vite大chunk提示。它们不是本次新增业务失败，也不被报告为已解决。

## 复审与交付状态

- 三路独立审查与相互证伪，详见 [总矩阵](maintenance-cross-review.md)、[后端](reviews/maintenance-backend.md)、[前端](reviews/maintenance-frontend.md)、[Agent](reviews/maintenance-agent.md)。
- 属于同模型多代理复核，未冒称满足合并前跨模型复审要求。
- 无数据库迁移、依赖版本更新、业务数据更改或生产发布。代码回滚可按本PR提交还原，既有HTTP/工作簿/工具协议兼容。
- 本地受影响测试通过，不等于全仓CI已通过。候选以draft PR交付；全仓必需CI、strict同步、跨模型复审、UI实际点按验收均按仓库规则继续执行。
- 当前判定：**不可合并**（待全仓CI和跨模型复审）；未部署。此为交付状态，不代表已经发现新的阻塞性业务缺陷。
