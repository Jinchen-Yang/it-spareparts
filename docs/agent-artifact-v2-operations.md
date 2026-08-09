# Agent Artifact Delivery v2 运维与回滚

## 发布边界

`AGENT_ARTIFACT_V2_ENABLED` 默认是 `false`。**GitHub #222（受限流式上传、解析沙箱与资源预算）完成并通过安全验收前，生产环境禁止把它设为 `true`。** 数据库迁移完成、对象目录可写、#222 门禁通过、下载与预览验收通过后，才可在生产环境显式开启并重启 backend。代码合并、CI 通过或迁移成功都不能单独解除这道门禁。

关闭开关时：

- UUID v2 制品的创建、读取、预览和下载稳定返回“已停用”（HTTP 503）；
- 既有 12 位十六进制 legacy 制品仍按 owner-only 规则只读，便于回滚期间继续访问；
- 对账工具不依赖开关，仍可在路由关闭时修复半成品和清理已证明的孤儿对象。

## 正确回滚方式

正常回退只关闭 `AGENT_ARTIFACT_V2_ENABLED`，然后用 forward deploy 修复并重新开启。不要对已有制品数据执行 Alembic downgrade。

**不得把该开关当成旧镜像回滚门禁。** 旧镜像可能根本不认识
`AGENT_ARTIFACT_V2_ENABLED`，并可能恢复管理员/老板跨 owner 访问或把原始查询、工具参数写入日志。
若事故处置必须切换到旧 backend/frontend 镜像，必须先完成下列 fail-closed containment，且顺序不可倒置：

1. 在所有可达入口（公网、Tailnet/内网及直连 upstream）阻断整个 `/api/agent` 与
   `/api/agent/*` 路由，而不是只关闭聊天页面或 `ENABLE_AGENT`；上传、预览、下载、会话和任务接口
   都必须覆盖。
2. 撤销或隔离主模型、Vision 与 GPU 推理出口及其服务凭据，确保旧进程即使启动也不能产生模型出境。
3. 从外部未登录、普通 owner、admin/boss 三类探针验证所有 Agent API 均被入口拒绝；同时确认业务非
   Agent 健康检查仍符合事故方案。验证证据须随发布记录保存。
4. 只在上述拒绝持续生效后切换旧镜像；数据库保持当前 schema，禁止 downgrade 或删除
   `agent_artifact` 数据。
5. 重新开放前只能 forward deploy 到已经通过 owner-only、日志脱敏、出境门禁和下载验收的新镜像，
   再逐项恢复凭据与入口。任何一项验收失败都保持入口封锁。

现有镜像回滚脚本不会自动证明以上 Agent containment；发布负责人必须把入口封锁、凭据撤销和外部探针
作为独立前置条件。无法证明时不得执行旧镜像切换。

迁移 `ad8f6c2e1b47` 仅允许在 `agent_artifact` 空表时 downgrade；表内存在任何记录都会失败关闭并保留数据。该迁移不提供数据搬迁式 downgrade，也不允许以删表作为生产回滚手段。

## 崩溃对账

在 backend 目录执行：

```bash
python scripts/agent_artifact_reconcile.py
python scripts/agent_artifact_reconcile.py --apply --grace-minutes 60
```

第一条永远是 dry-run；检查 JSON 中的 `outcome`、`planned`、`errors` 后，才运行带 `--apply` 的第二条。宽限期只接受 5 分钟到 30 天。工具只处理严格 UUID 对象键和服务器命名的临时 `.part`，不会碰 legacy 根目录文件或未知文件。若过期状态提交后对象删除失败，后续运行会依据 `expired` 状态继续重试。

## 当前保守限制

生成制品的访问快照目前记录创建者当时的完整 data/page/field 可见范围，而不是本次工具实际读取到的字段集合。这个策略会在权限缩小时过度收紧，但不会扩大访问；在实现字段级 provenance 前，不得自行缩小快照。后续应让每个只读工具返回实际字段来源，再由制品层聚合最小权限集合。

当前还没有可证明的 durable artifact idempotency key。任务自动重试时，不应自动重复执行制品创建；后续需要增加服务端 operation key，并以 `(owner_sub, operation_key)` 唯一约束返回同一个制品结果。
