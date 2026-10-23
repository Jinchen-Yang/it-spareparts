# F1：维保异步回调的上下文所有权

决策: 无

基线 main `7a9fd2098acb6be0009a59a84e7d7330bb960d6b`，2026-10-23 按 #452 近期口径台账实施。没有新增业务状态、后端权限或回执规则。UI 候选自动检查完成后仍待用户点按验收。

## 边界

项目、账号或上传权限变化后，旧请求仍可能返回；Modal.confirm 不随页面自动卸载。此前 WorkbookRoundTrip 自管 mounted/epoch/portal，AcceptanceTab 仅有读取序号。现在两种实际流程共用回调有效性和门户清理，API、业务数据、权限、幂等凭据、强制接管及刷新仍由各组件负责。

```mermaid
flowchart TD
  P[维保总览与项目面板] --> W[WorkbookRoundTrip]
  P --> A[AcceptanceTab]
  W --> C[useAsyncContext]
  A --> C
  W --> API[各自 API 和业务确认流程]
  A --> API
  C --> R[React 生命周期与本地 ref]
```

WorkbookRoundTrip 的六个挂载点为总览、项目面板、备件订单、费用、回款、现场归还。AcceptanceTab 是不同的真实流程：上传需求清单、确认整表替换。没有为假想流程增加 registry 或通用业务状态机。

## 接口和变量

| 名称 | 类型 | 用途 | 为何这样定义 |
| --- | --- | --- | --- |
| scopeKey | string | 当前项目/账号/权限的组合 | 调用方用 JSON 数组编码，hook 不认识业务字段 |
| generation | number | scope 每次变化的世代 | A→B→A 后旧 A handler 不能重新捕获当前上下文 |
| epoch | number | 挂载/手动失效的生命周期 | StrictMode 清理后第二次 effect 可用，第一次回调失效 |
| sequence | number | 同范围最新请求 | 新请求退役旧请求的本地落地权 |
| mounted | boolean | effect 是否有效挂载 | 卸载后不得继续旧页面副作用 |
| capture() | () => boolean | 捕获上下文有效性 | 不使同范围其他读取失效 |
| begin() | () => boolean | 开始请求、退役旧确认 | 只保留最新请求，旧 scope handler 不影响新请求 |
| invalidate() | () => void | 清理当前生命周期/门户 | effect cleanup 使用，不碰业务数据 |
| ownConfirmation(close) | (() => void) => (() => void) | 登记门户清理、释放所有权 | 旧框结束不能注销后继框；替换框由具体流程负责 |
| readContext / downloadContext | hook 返回对象 | 读取范围 | 不依赖上传权限；撤销上传不等于禁止读取 |
| uploadContext | hook 返回对象 | 写流程范围 | 权限变化使迟到预检、旧确认及旧刷新失效 |
| activeUpload | ref<AsyncGuard或null> | 工作簿单次上传 | 已排队的原生 change 不能只靠按钮 disabled 拦截 |
| importAllowed / uploadAllowed | ref<boolean> | 当前 render 的资格 | await 后核验，避免旧 prop 闭包 |
| isCurrent / operationCurrent | () => boolean | 组合上下文、请求与实时 token | token 改变但组件尚未重绘时也拒绝旧结果 |

不承诺中止网络请求或撤销服务端已提交写入。后端仍执行最终权限校验；hook 仅管理本地回调和门户生命周期。

## 提取与有意修正

- 明确复用未合并 #451 的 AcceptanceTab 和 Lifecycle 测试两文件窄补丁，再接入 hook；没有带入其他 #451 代码。
- 工作簿原有预检、合单 token、冲突/force_takeover、原回执错误、保留文件、取消及同文件重选保留。
- 验收清单增加项目/账号/权限迟到结果屏蔽。旧确认不写入，成功写后的旧父级刷新不继续；读回开始时使旧清单失效。
- 上传权限在写后读回期间变化时，重启当前只读加载。旧写流程退役后不能把真实存在的清单显示成“尚未导入”。
- 工作簿对同一 render 排队的重复原生上传回调只接受一次。把主线旧组件临时用于新增反例，出现两次预检；候选一次。此为受控回调竞争复现，不外推为生产事故。
- A→B→A 不复活已捕获 guard，也不允许保存的旧 context handler 启动当前操作；StrictMode 清理和重新挂载分别验证。

## 验证和回退

反例覆盖迟到预检、已发送 apply、写后读回、账号/项目/权限变化与恢复、门户销毁/后继确认、重入上传和真实组件文件 input。现有测试保留 API 参数、业务确认和刷新失败断言。

首轮全前端 1,919 项通过；追加撤权读回用例和旧 handler 世代修正后相关 70 项通过。最终全量、TypeScript、构建及生产依赖扫描结果随 PR 记录。后端/schema/文件协议未改，当前提交 CI 仍验证后端与迁移，不把前端 mocks 当数据库证据。常规门禁后另安排两名独立测试设计者。

可单独 revert，无数据转换。用户点按验收和生产发布均未发生。
