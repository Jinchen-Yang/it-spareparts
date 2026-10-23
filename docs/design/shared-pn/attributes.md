# 自定义参数与 PN 资料确认状态

自定义参数复用主资料资源的 `read` / `edit` 动作，另以 `attributes` 字段组显式授权。
业务动作 `read_attributes` / `update_attributes` 没有独立角色或权限副本。

`GET /parts/{part_id}/attributes` 返回独立的 `revision`、当前 `identity_revision` 和 `items`：
每个键对应 `value`、`deleted`、`version`、`protected`、`provenance`。一次 SQL 读取完整快照，
GET 不初始化状态，支持只读事务。

`PATCH` 需要 `expected_identity_revision`、与 `changes` 键集完全相同的 `field_versions`。
变化显式区分 `{operation:"set",value:null}` 与 `{operation:"delete"}`。
值保留 JSON 类型，不推断单位，不转为标准规格字符串。标准规格仍使用原 `specs` 接口。
最多 256 键（含墓碑）、键长 256 字符、JSON 深度 8、节点 4096、规范 JSON 总量 48 KiB。
单请求仍受统一 64 KiB 网关限制。组合修改也验证最终聚合容量。

父 PN 锁保护身份；每键 CAS 允许不同键的并发修改，旧版本不能覆盖删除、恢复后的值。
整个命令与审计、请求回执共用调用者事务。重放检查当前动作与字段权限，回执只投影当前可读内容。
来源字段来自可信服务端 ActorContext；公共 PATCH 只接受人工确认。迁移由专门的冻结源记录入口处理，
不向浏览器开放任意历史状态写入。

`fill_status` 与 `fill_status_revision` 是 PN 资料的独立元数据，不等于好件/坏件文案的状态。
历史未建立状态的记录保持 null；新建空 PN 为 pending；人工有效资料编辑、人工采用和参数编辑推进
confirmed，Excel 写入和 AI 候选生成不自行确认。状态变化写入同一审计的独立修订轴。
只有获授 `fill_status` 的读取者得到其值。旧命令 DTO 未新增字段，所以历史命令摘要和重放键保持兼容。
