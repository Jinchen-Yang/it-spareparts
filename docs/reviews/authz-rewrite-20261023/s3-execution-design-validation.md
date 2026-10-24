# S3 执行设计静态校验

本轮只修改设计文档及文档目录内的验证脚本。S3 应用、迁移和运行测试尚未实施；实施仍以根代理明确开工为准。

[完整规格](s3-execution-spec.json) 是资源、动作、seed、逐入口 context 和 resolver 白名单的唯一设计源。[十项差分](s3-execution-contract-delta.json) 与 [47 候选盘点](s3-resource-seed-gaps.json) 保留历史证据，不再维护第二份上下文协议。完整规格约 2 MB，含重复的源码证据、选择器定义和入口索引；这是审计数据体量，不是必须手写或复制进 runtime 的代码量。实施提取最小可执行闭集即可。

## 本次检查结果

运行命令：

```sh
python docs/reviews/authz-rewrite-20261023/verify_s3_execution_spec.py
python docs/reviews/authz-rewrite-20261023/verify_s3_execution_spec.py --self-test
```

机器结果分别见 [静态结果](s3-execution-static-validation.json) 和 [负向检查](s3-execution-static-negative-checks.json)。结果文件保存完整规格与前端 fragment 的实际 SHA256。

| 项目 | 结果 | 含义 |
|---|---:|---|
| 原候选 → 实际资源 | 47 → 73 | 所有候选恰好覆盖完整资源集；按 scope、字段和对象合同差异拆分 |
| 业务动作 | 232 | 128 个 data、104 个 mutation；没有用 show 动作替代数据授权 |
| human / retired | 228 / 11 | 逐 method/path 对照原清单；human 中保留 D05 固定业务拒绝入口，不为其发明 Grant |
| 源码 SHA256 | 48 文件 | 39 API 与 9 个权限/关系 helper 的当前字节与审计证据一致 |
| 实际 collection refs / 用途 | 38 / 79 | 每个用途有真实路径、封闭 selector、resolver 与动作引用 |
| object types | 33 | project/collection 之外的封闭子对象种类；逐 action 精确绑定，资源级 kinds 并集不是准入条件 |
| 授权视图 | 13 基础 + 23 输入 | 复用真实路径的 `view=authorization`；输入路径逐项登记，无通用任意操作端点 |
| 旧账号输入向量 | 612 | 调用真实纯函数 `effective_for_user`、`runtime_safe`；覆盖角色、旧权限图、模板快照与逐位覆盖 |
| 资源 × 输入检查 | 44,676 | 规范 scope、action/field 依赖与字段闭集均成立 |
| 前端 surface / workspace blocks | 106 / 6 | 资源、动作、字段引用闭合；保留 OR-of-AND 与每块独立字段合同 |
| 静态毒针 | 12 / 12 拒绝 | 内存副本上注入异常，不改应用、数据库或原规格 |

静态毒针覆盖源文件漂移、未注册动作、孤立动作、无上下文的 all scope、跨资源字段、把 discovery 当批量权限、空集合省略根授权、任意 input purpose、开放 selector、分页冒充完整目标、仓库分支扁平化，以及回款计划遗失完整 contract 目标。

## 收口的语义差分

- D03 保留 managed 与 ordinary 两份完整合同。文档按实际子项目选择一个完整合同，来源仍记录真实文档 carrier；revision 覆盖整份文档与全部 parent 拓扑，不因某项目选择另一份资源而丢掉 child/link 变化。普通资源不能借用 managed 的财务字段。
- 每个项目先独立计算完整 scope 表达式，再对整件内容执行 ALL。A 是 manager、B 是 viewer 可以各自满足 M∨V；A 有权、B 无权必须拒绝。禁止先合并四个关系布尔值。批次 owner 是额外 AND，不由项目权限代替。
- `operation_input` 只接受当前服务端验证的 bytes 或规范 command plan。23 条输入路径包含上传/创建、实际持久化预览、具名完整目标批量命令，以及无业务写入的全局工作簿 validate。两个纯搜索不在输入白名单。输入授权视图只解析与授权，不创建 job/preview、不调用 LLM、不返回事实 payload。
- 即使目标集为空，仍先检查具名动作、同资源字段、当前主体和 epoch。仅当服务器证明这是完整空集合时才可返回无操作成功；空、未知、待解析和当前页结果不能互换，也不因此返回全库计数。
- AI 以实际 doc_type 选择 document/ledger 分支；仓库 apply 以实际 import 存在性选择 existing object/new input。注册表中的动作集合只是可能引用列表，不能扁平为 AND。分支、命令计划或 revision 漂移返回 409，前端不自动重放。
- master-workbook 的 source assignment 额外解析所有旧/新 source-order parents；collection-plan apply 解析完整持久化计划及替换合同。仅路径 project 或 uploader owner 不能代替这些实际目标。
- action kind 以副作用区分：acceptance/followup search、attachment download、evidence read 是 data；保存 validation/import batch 的预览仍是 mutation；纯 workbook validate 与只改变查询估值参数的动作是 data。字段投影不因动作名字含 write/preview 自动放宽。
- 旧角色只参与 seed。真实 persisted admin 经 `effective_for_user` 得到普通财务字段，不能构造一个该函数不可能产出的 admin F=false 来扩资源；Beta 与账号白名单动作仍按其实际 helper。F-only 合同不经过会再次裁掉它的旧 `runtime_safe` 投影。

## 公共接缝与实施门槛

基础与根核心共用 `ObjectRef`、`ContextBatch`、`ContextAccessSet` 和 `ReadSnapshotProof`。纯读在同一个短 REPEATABLE READ READ ONLY 事务里完成主体、Grant、关系、事实查询和 DTO 物化；不能只取一个 proof 后回外层 READ COMMITTED 查询事实。写路径先锁完整旧/新领域拓扑，再按稳定 user_id 及最终强度锁用户；callback 不追加领域锁、不升级 SHARE、不换连接。

CheckResources 按每批最多 50 个展开项发送，同一观察与 Grant 输入，全批验证后才发布结果。集合 SQL 使用带 `.table` 的派生 Boolean 列交给官方 adapter，过滤先于聚合/排序/分页。该 adapter、真实对象批量检查与空目标根准入还需要 S3 的真实 PDP/PG 反例证明。

文件和任务复用 [根核心边界](s3-core-context-boundary-design.md) 与 [sidecar schema](s3-artifact-sidecar-schema.md)。六个持久 owner 列由根的单一迁移维护；领域命令键和任务恢复使用稳定 user_id，历史 username 仅作展示/审计。短期 input 依赖必须转换成真实 batch/sidecar generation 与完整 manifest 才能持久发布，不能删除依赖后保存空来源。

独立复核见 [artifact/context 审查](s3-artifact-protocol-and-adversarial-constraints.md) 第 7 节。当前结果证明设计引用闭合及选定旧权限输入的一致性，不证明全部旧业务语义等价；真实 SQL/PDP、迁移 roundtrip、锁竞争、HTTP/Agent/任务共享边界和 artifact 生命周期仍是实施后的必需验证。
