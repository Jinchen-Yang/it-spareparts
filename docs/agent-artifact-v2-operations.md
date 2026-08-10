# Agent Artifact Delivery v2 运维与回滚

## 当前发布边界

`AGENT_ARTIFACT_V2_ENABLED` 默认是 `false`。当前提交只提供 #220 的
service/store、完整性绑定、审计与对账地基，不代表模型侧生成制品已经可用，也不接受模型提供的
provenance 作为可信来源。

生产开启至少同时满足：

1. #222 的受限流式上传、解析沙箱与资源预算通过安全验收；
2. #223 Durable Agent Task 已接线，创建/重试/取消路径均有持久状态与恢复语义；
3. #224 Query Broker 对业务导出返回真实 Query Evidence，并通过授权撤销与来源失效验收；
4. #230 的服务端 operation idempotency、并发唯一性与崩溃恢复门禁均完成；
5. 数据库迁移完成，对象目录权限、下载、预览、拒绝路径和外部探针验收通过；
6. 完整性 keyring 已按下文配置，backend 启动检查通过。

在 #223/#224/#230 完成前，`artifact_create` 必须同时保持 **tool schema 数量为 0、handler
数量为 0**；不能仅凭 #222 完成、代码合并、CI 通过或迁移成功而向模型暴露。未知工具调用继续走
统一拒绝路径。

关闭开关时，UUID v2 制品的创建、读取、预览和下载均失败关闭（HTTP 503）。12 位十六进制
legacy 制品在所有普通 service/API 入口也一律拒绝，不提供 owner-only 回退、自动 adoption、重签或
新 legacy 写入。`_load_meta` 只保留给离线 forensic/数据形状检查；不得把 sidecar 当成认证事实后重新
开放。

## 完整性密钥轮换

`AGENT_INTEGRITY_KEYS_JSON` 的每个 key 使用
`{"key":"<base64url-32+-bytes>","status":"active|verify_only|revoked"}`，
`AGENT_INTEGRITY_ACTIVE_KEY_ID` 指向唯一用于新签发的 `active` key。密钥环与登录
`SECRET_KEY` 分离。

轮换顺序：

1. 先加入新 key 并标记 `active`，把 `AGENT_INTEGRITY_ACTIVE_KEY_ID` 切到它；
2. 把旧 key 改为 `verify_only`，使历史 envelope 可验证但不能再签发；
3. 在使用旧 key 的全部制品留存期结束、相关审计与恢复窗口关闭后，才可改为 `revoked`；
4. 分批重启并验证新签发使用新 key、旧 envelope 仍可读取，再扩大发布。

任何配置错误都必须拒绝启动/签发。不得在日志、异常、工单、命令行参数或发布证据中记录 raw
key、完整 keyring JSON，或对含密钥对象直接 `repr`；只记录非敏感 key version/轮换结果。

## 正确回滚方式

正常应用回退是关闭路由、保留当前 c2 schema，再 forward deploy 修复；不要把 Alembic downgrade
当作生产回滚。

旧镜像可能不认识 `AGENT_ARTIFACT_V2_ENABLED`，还可能恢复跨 owner 访问或敏感日志。若事故处置
必须切换旧 backend/frontend 镜像，顺序如下：

1. 在公网、Tailnet/内网和直连 upstream 阻断整个 `/api/agent` 与 `/api/agent/*`；
2. 隔离主模型、Vision、GPU 推理出口及其凭据；
3. 从未登录、普通 owner、admin/boss 三类探针验证 Agent API 全部被入口拒绝并保存证据；
4. 保持当前数据库 schema，确认封锁持续生效后才切换旧镜像；
5. 只能 forward deploy 到通过 owner-only、日志脱敏、出境门禁和下载验收的新镜像后再开放。

c2 迁移 `c2f8a4d6e9b1` 会把历史行的 `binding_envelope` 留为 `NULL`，运行时一律拒绝；迁移
不会替历史数据伪造签名。`agent_artifact_audit` 一旦存在任何记录，c2 downgrade 会在同一锁事务中
失败关闭并保留 schema 与数据。只有经过独立审批、完整外部归档和恢复演练，且能证明 audit 表为空
时，才可讨论破坏性的 schema 降级；继续降过 `b1e7c9d4f2a8` 还受 `agent_artifact` 空表守卫约束。

`agent_artifact_audit` 的 PostgreSQL trigger 禁止普通 `UPDATE`、`DELETE`、`TRUNCATE`，应用只追加
事实。这不是对数据库 owner 被攻破的绝对防篡改边界：同一高权限 owner 可以停用或删除 trigger。
生产应分离 application role 与 migration/owner role，限制 DDL/trigger 权限，并把审计备份送往独立
保留域。

## 崩溃对账

在 backend 目录先 dry-run：

```bash
uv run --frozen --offline --no-config --default-index https://pypi.org/simple \
  python scripts/agent_artifact_reconcile.py
```

人工核对 JSON 后才可显式 apply：

```bash
uv run --frozen --offline --no-config --default-index https://pypi.org/simple \
  python scripts/agent_artifact_reconcile.py --apply --grace-minutes 60
```

宽限期只接受 5 分钟到 30 天。任一 `errors`、`unresolved`、`requires_operator=true`，以及 dry-run
中任何 `planned.delete_* > 0` 都要求人工处理，CLI 退出码为 2；不得通过缩短窗口、重复 apply 或
手工 `rm` 绕过。

对账器的权限判断是 `AUTHORIZED / DENIED / UNKNOWN` 三态：只有被确定拒绝的陈旧
`prepared/validating` 才可锁行转 `failed`；暂时性存储/鉴权故障属于 `UNKNOWN`，保持原状态并报错。
已存在对象但缺 publisher completion receipt 的中间态只写幂等 observation，保持 unresolved；
**对账器永不制造 `ready`**，只有正常 publisher 路径在同一对象句柄校验、live authorization 和绑定
重签全部成功后才能完成 `validating -> ready`。

每个 `ready` 行（包括已到期行）都要先在锁内复核绑定和对象。异常 observation 与允许的
`ready -> expired` 状态迁移在同一数据库事务中提交；绑定无效或存储状态未知时保持 `ready` 供人工
处置，不借到期掩盖异常。

当前存储层没有可证明的 conditional delete，因此对账器**从不物理删除** failed/expired 对象、
orphan 或 `.part`。它只追加幂等 delete `intent` 与 `disabled` 审计并返回
`applied_with_disabled_actions`/`requires_operator`。工具只识别严格 UUID 对象键和服务器命名的临时
`.part`，不会采信 legacy sidecar 或碰未知文件。

## 当前保守限制

生成制品必须来自服务器签发、可验证的 source snapshots；没有真实 Query Evidence 的生成件保持
`unclassified_deny`。当前地基不能把“创建者当时可见范围”或模型参数降格成“本次实际读取字段”的
证据。#224 应让业务只读工具返回实际资源、字段、条件和 source envelope，再由制品层按精确来源并集
生成最小权限 provenance。

当前也没有可证明的 durable artifact idempotency key。任务自动重试不得盲目重复创建；后续需要
服务端 operation key，并以 `(owner_sub, operation_key)` 唯一约束返回同一制品结果。
