# S3 核心对象授权边界（只读设计，尚未实施）

本文件补充 `s3-execution-spec.json` 的核心实现接缝，不替代领域 action→context 注册表，也不是 S3 开工证明。S2 精确提交完整 CI 全绿后才开始应用代码及测试实现。

## 共用类型和事务

核心持有不可变 ObjectRef、ResolvedContext、ContextBatch、ContextAccessSet；领域持有真实对象解析、parent、owner、完整 selection、关系 revision 和 SQL bindings。双方共用类型，领域不能通过字典或旧 UserContext 伪造已验证主体。

ObjectRef 使用封闭 kind、规范字符串 ID 和深度不可变 selector；序列化前转为规范 JSON。当前对象与原始字段投影分开登记，不因同 resource 而合并不同 project。每个 action 必须接受该实际 kind/purpose，collection 不自动兼容 object，字段不能从其他 resource 借用。

纯读由核心提供短 `REPEATABLE READ READ ONLY` 会话。专用 ReadSnapshotProof 绑定同一 Session、同一活动事务，仅该显式 proof 允许主体读取不加 FOR SHARE；不能全局移除锁或用普通 locked_user 参数冒充一致快照。调用方必须在会话内读完关系、Grants、事实并物化返回 DTO，不把 predicate/懒加载 ORM 返回到另一个 READ COMMITTED 事务执行。读取期间没有 domain 或 actor 显式行锁，不跨外部 LLM、下载或网络等待保留该事务。

写路径保持既有业务锁与逐行提交。先收齐所有旧/新 parent、child 和必要 domain 锁，再一次按稳定 ID 及最终强度锁 actor/target user；callback 只重验和投影，不能增加领域锁、升级 SHARE 为 UPDATE 或换连接。发现新的父对象必须冲突回滚重试，不能在用户锁后补锁。

## PDP 与 SQL

单对象的真实布尔关系放入 Resource.attr，module_probe=False；同 `(ref,resource)` 的动作合并在一项中。集合使用真实 PlanResources 与 SQLAlchemy adapter，谓词在统计、排序和分页之前执行。关系 EXISTS/比较先成为派生子查询的 labelled Boolean 列，再作为 QueryBindings 属性；不能把无 `.table` 的 SQL 表达式直接喂给 adapter。

多项目的授权以各对象为单位判断后全称验证。不能先对 `is_manager/is_viewer/is_sales` 分别 any/all 聚合：A 的 manager 与 B 的 viewer 必须能各自满足 `(manager OR viewer)`；A 有权、B 无权则整件完整文档拒绝。项目之外的任务 owner 是独立 AND 条件，不能被项目范围替代。

[官方 Cerbos API 文档](https://docs.cerbos.dev/cerbos/latest/api/index.html) 规定 CheckResources 默认每请求最多 50 个资源。因此 S3 的 `/me` 和对象批量检查需共用有界批处理：同连接、同一数据库观察与 Grant 输入，每批最多 50 个 `(context,resource)`，至少 `ceil(pair_count / 50)` 次请求；任何批缺失/重复/额外返回、非法字段或 PDP 故障都使整个调用失败，验证完全部响应前不发布任何部分允许结果。不以抬高部署上限掩盖客户端无界请求。

## 来源与异步主体

现有 provenance 扩展 schema 2，依赖按 `(resource,canonical ObjectRef)` 去重，保存每个真实 revision。schema 1 仅兼容原全局来源；对象信息不能丢弃后降成 schema 1。重验必须重新解析真实域及当前 policy，revision 相等本身不等于允许。

任务恢复通过服务器 `resume_task_identity(owner_user_id,enqueued_epoch)`，加载当前活动实名账号并精确校验 ID 和 epoch；VerifiedIdentity 增加可信 expected_user_id 约束，后续每次主体读取也核对，以防用户名改名或复用。共享文件 reader 依据实际 link 和完整内容合同重新授权，不替换 producer provenance 的 principal_id。

## 必需反例

- 50/51 个批项、同资源多个对象、第二批失败时整次无部分结果。
- 两项目 manager/viewer 混合允许；第二项目无权拒绝；同项目不同资源字段不合并。
- 只持 collection 能力直接调用实际 child/owner 动作拒绝；错 kind/purpose/selector 拒绝。
- 同一 RR 事务内关系/Grant/事实一致；proof 对不同 Session、事务结束后复用拒绝。
- 写入等领域锁时撤权可提交，释放后旧身份拒绝且不产生业务或审计写入。
- 同 username 新 ID、旧 epoch worker、旧 generation 发布、schema 降级及未知历史来源拒绝。

以上为待实现与验证的合同，不是通过结果。


## 创建前与整批命令的有限启动

`operation_input` 是服务端本次校验实际 bytes 或规范 command plan 的短期证据，不假装已有数据库对象。只允许逐动作登记的 preview/create/整批 apply 场景。对应的具体业务路径可用 `view=authorization` 完成安全输入解析、必要 parent/ID 定位及当前授权，返回最小 context；此视图不写 preview/job、不调用外部 LLM、不返回事实 payload。前端同一次用户意图在收到 allow 后，以相同文件/参数和 precondition 调用正式命令；期间 actor/input 改变则中止，409 不自动重放。

整批命令的 discovery collection 只用于找全目标。命令 SHA 相等不构成写许可：必须对完整实际 child/project subjects 逐一通过当前策略，锁后重算目标集合与关系，漂移即冲突。不得以当前分页、任一项目允许或空目标集合代替完整核验。

仓库预览保持现行纯函数零写入：它返回的确定性 import_id/HMAC 不是授权。apply 重新收到全部 bytes、验证当前 SHA/adapter/HMAC 后，再以当前 typed 主体授权；存在实际 import 时，重放还须验证真实 batch 和完整结果内容。

短期 input 不能单凭其保存的 SHA 被 resolver 重建成历史许可。只有纯预览且不发布历史的调用可结束于 transient ref；需持久发布或重放时必须先绑定实际 batch/sidecar、输入 hash 与完整 manifest。Collector 不得静默丢依赖，未完成转换的 transient ref 禁止 freeze/publish。
