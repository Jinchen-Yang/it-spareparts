# B3 共享 PN 权限接缝候选

本工作树基线是 B1 + 新权限会话 S1 `bad6976`。这里只实现现有 `VerifiedIdentity → Principal → Grant → Cerbos` 的领域适配，不建立第二套角色或权限中心。旧 `/parts`、`/governance`、ETL 与后台写入必须另行收口；在这些入口统一接入并完成验收前，**不能宣称主数据权限迁移已完成**。不自动启用资源，不操作生产。

## 资源与领域责任

| Resource | 当前领域动作 | 字段与范围 |
| --- | --- | --- |
| `part_master` | read/create/edit/normalize/confirm_distinct、停用状态、审计、候选创建与审阅、文本研究、研究任务管理 | profile 明确字段、有限 specs 字段、availability/candidates；scope=all |
| `part_identity` | read/rename/merge/unmerge/review_alias/rebind_facts、身份重算任务管理 | identity/aliases；scope=all |
| `part_content` | read/edit/migrate | `content.good.title/description` 和 bad 对应字段；scope=all |
| `part_gallery` | gallery、uploads、inbox、媒体研究/识别/打包、媒体任务管理的具体动作 | gallery/uploads/inbox；scope=all，素材所有者与共享授权由领域服务继续检查 |

`catalog.py` 仍是唯一可执行目录，`pn_resources.py` 只提供该目录的闭集数据和领域动作映射。每个动作仍读取当前 `SysAuthorizationGrant` 并调用 Cerbos；admin 没有业务数据自动全开。客户端能力仅作为服务接入上限与当前用户 Grant 相交，不能增加用户权限。`governance` 整个资源仍 planned。

迁移历史证据专用 `stage_migration/materialize_migration/read_legacy` 动作，均明确要求 `legacy_evidence` 字段；标签注明它包含全部历史源载荷、已删除记录及原审计，不能按普通 profile 字段过滤。普通编辑不会自动获得该权限。它们与 `migrate_content` 只接受 client_id=itsp、entrypoint=migration、provenance.kind=migration 的本机可信 actor。shop 能力配置如果包含这些动作或 legacy_evidence，适配器直接失败关闭。

`manage_identity` 是查看涉及 PN 的基础动作，真正变更还需每个闭包 PN 的 `identity.{operation}` 与 `govern_facts → rebind_facts`。`confirm_distinct` 独立授予，并依赖 create/read。`list` 映射 read；scope=all 是当前真实支持范围，不宣称已支持任意行过滤。

JobAuthorize 按数据库当前 job.kind 决定使用同一权限框架中的资源。research 使用 part_master 的 job.*（内部领域映射 research.job.* 保留 kind 信息），identity 使用 part_identity，媒体与其他 enrichment 使用 part_gallery。任务管理限当前提交人，仍复核 inbox/upload 所有权、图库状态及隐藏状态、身份闭包。任务上下文必须保留原 authorization_epoch/mapping_version/client_policy_version；执行器变更不能刷新原授权版本。

## 接口

`app.services.pn_catalog_access` 暴露：

- `resolve_catalog_access(request, db) -> CatalogAccess`：HTTP host 安装到 app.state；识别 Bearer 原登录凭据与 PN-Delegation 专用委托。
- `resolve_native_access(db, VerifiedIdentity)`：可信内部本地入口。
- `resolve_migration_access(db, VerifiedIdentity)`：从已验证本机账号构造迁移 actor 与匹配 authorize，保留原主体和 epoch；没有公开请求参数开关。
- `resolve_job_access(db, ActorContext)`：使用保存的原始可信身份版本，并再次检查当前账号、映射、客户端上限和具体资源动作。
- `exchange_delegation(db, proof: str) -> {token, expires_at, token_type}`：host 必须提交事务成功后才返回 token。
- `set_subject_binding(db, VerifiedIdentity, *, subject: UUID, user_id: int, active: bool, expected_version: int, reason: str)`：实名管理员显式建立/停用 UUID subject 到数字账号 ID 的映射，CAS 与审计同事务。先取得顺序用户 UPDATE 锁，不能先经过持有 actor SHARE 的读 dependency。

上述函数不 commit。B2 适配后 `CatalogAccess.authorize_job` 提供 job 专用授权，不用通用 part_id 授权替代未归档素材所有权检查。

## Authority 短期委托

两类 token 各用独立 SecretStr 配置密钥，不能为空、短于 32 字节、相互相同或与正常登录 secret_key 相同。

- `pn_shop_proof_secret`：只在 shop 服务与 authority 持有，用于签名当前服务端已经验证的 shop 会话证明。
- `pn_delegation_secret`：只由 authority 持有，用于签发业务委托。shop 无法自行制造 canonical user_id/epoch。
- `pn_shop_issuer/client_id`：明确 issuer 与 client，禁止保留的 itsp 名字。
- `pn_shop_allowed_actions`、`pn_shop_allowed_fields`：默认空；只容纳已注册动作/字段。上限、issuer 或密钥改变会自动改变 client_policy_version（内容 SHA256）。
- `pn_confirmation_secret`：近似 PN 确认的独立签名密钥，未设置则不开放该流程。

证明 wire：`base64url(JSON bytes without padding).base64url(HMAC-SHA256(JSON bytes, proof key))`。
`ShopProof` 是严格闭集 schema，字段如下：

```json
{
  "purpose": "pn-shop-session-proof:v1",
  "audience": "itsp:pn-catalog:v1",
  "issuer": "shop",
  "client_id": "shop",
  "subject": "<shop user UUID>",
  "session_id": "<currently verified session UUID>",
  "nonce": "<fresh proof UUID>",
  "issued_at": 0,
  "expires_at": 0,
  "binding": {
    "method": "PATCH",
    "target": "/api/pn-catalog/v1/parts/1/profile",
    "body_sha256": "<SHA256 of exact outgoing body bytes>",
    "request_id": "<UUID also used in X-Request-ID and write Idempotency-Key>",
    "if_match": "<exact If-Match string, or null>"
  }
}
```

整数时间为 Unix seconds，证明最长 30 秒，不接受未来 issued_at；nonce 在数据库唯一约束消费，同一并发证明只能一个事务成功。authority 查询显式 `(issuer, subject UUID)` 映射、当前 active 账号，然后签发最长 60 秒、不同 purpose 的 token。正常登录 token 与委托不能混用。

业务请求使用 `Authorization: PN-Delegation <token>`。target 包含原始 path 与 query string；不对 URL 或 body 重新序列化。中间件在有界读取 body 后设置可信 `request.state.pn_catalog_body_sha256`，同步 resolver 不信客户端自报摘要。每次业务调用重新检查账号 active/epoch、映射 active/version、客户端上限版本，并读取实时 Grant。原账号角色/名称不用于跨系统匹配。委托始终留在 shop 服务端。

接收委托的每个 GET 也要求 X-Request-ID；写操作 X-Request-ID 与 Idempotency-Key 相同。重试业务操作应保持幂等 key 和字节级请求内容，但可重新验证会话、取新 nonce、兑换新委托。丢失 exchange 响应后使用新 proof nonce，不能重放已消费 proof。

## 数据与锁

迁移 `d6f9b2e5a8c1` 目前合并 `(e1a4c7d9f2b6, a7c2e9f4b6d1)`；根代理集成 B2 后将第一父节点改到 B2 真实 head。新增映射与 nonce 表，不推断历史身份、不回填 Grant、不写 cutover marker。映射安全变化必须递增 version；禁止 DELETE 后重建复活旧 token，停用必须保留记录。已采用映射拒绝 downgrade。

读取与业务操作按 user SHARE → binding SHARE → 领域锁；管理映射按 advisory binding key → 顺序 user UPDATE → binding UPDATE。现有改权命令更新用户 epoch 的锁因此与在途业务互斥。在途已授权事务结束后，后续请求与任务立即看到撤销，不在同事务中悄悄换新身份版本。

## 验证状态

初轮真实 PostgreSQL + Cerbos 0.57.0：新适配、S1 engine/service/adversarial 与 B1 core 共 135 项通过。新增35项均通过，覆盖请求字段绑定、nonce竞争、撤权锁、映射ABA、实际facade receipt/list；测试位于 `tests/test_pn_catalog_access.py`。

B2 job adapter 使用延迟导入；必须在集成 B2 后实际运行其媒体/enrichment/identity 测试，单独 B3 工作树不能证明这些不存在的模块已集成。HTTP exchange 路由、shop 签名、middleware SHA 以及旧入口收口由各自 host 集成；这份接缝不代表上线或最终验收。
