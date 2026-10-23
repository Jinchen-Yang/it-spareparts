# B3 共享 PN 权限接缝候选

当前隔离工作树已合入 B2、新权限会话 S1 `bad6976` 及其后续修复。共享 PN 通过现有 `VerifiedIdentity → Principal → Grant → Cerbos` 接入，不建立第二套角色或权限中心。旧 `/parts`、`/governance`、ETL 与后台写入正在另行收口；在这些入口统一接入并完成验收前，**不能宣称主数据权限迁移已完成**。不自动启用资源，不操作生产。

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

HTTP host 在 `main.py` 安装真实 resolver。`POST /api/pn-catalog/v1/auth/delegations` 接受严格 `{proof}`，事务成功后返回裸 `{token, expires_at: Unix seconds, token_type: "PN-Delegation"}`。它使用独立 `pn-catalog-authority` router/tag，标记 `x-server-only: true`，从业务 OpenAPI、Shop 公共 BFF 和浏览器生成器排除；响应及错误均 no-store，继续受 64KiB 有界 body 和统一错误码保护。

`GET /api/pn-catalog/v1/parts/{part_id}/access` 返回 `{data: {part_id, allowed_actions, readable_fields, writable_fields}, meta}`。动作是 facade 领域名，字段是 profile 原名、`specs.*`、`content.good|bad.title|description`、gallery/uploads/inbox 等。它复用框架批量 PDP 决策，再与客户端上限相交；不返回迁移动作及 legacy_evidence，writable 是 readable 的子集。它只表达当前 all-PN 能力，不授予未分配素材/任务所有权，业务命令仍逐次重验。

`GET /api/pn-catalog/v1/jobs` 恢复本人提交的当前及历史任务，返回 `{data: JobSnapshot[], meta}`，无内部 payload、lease、actor claims。筛选 `part_id` 与 `inbox_id` 互斥；前者含 identity 任务闭包中的 PN，后者只含该收件箱的识别任务；`status` 可重复（最多 6 个），默认所有状态。limit 默认 20、最多 50。SQL 在分页前限定可信 principal owner 和筛选，固定按 created_at、id 降序；逐条复核当前 JobAuthorize。单页最多扫描 200 个本人任务、输出 512KiB，遇到已撤权任务仍推进扫描边界，**客户端必须按 next_cursor 判断是否结束，不能把空 data 当作末页**。签名游标绑定主体、客户端和筛选，载荷只含本人扫描边界及不透明查询指纹；不返回未授权任务或计数。任务成果的共享应由 PN candidates 或 inbox 安全投影承载，不因此开放他人的任务管理。

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

迁移 `d6f9b2e5a8c1` 合并 `(c5e8a1d4f7b0, a7c2e9f4b6d1)`。新增映射与 nonce 表，不推断历史身份、不回填 Grant、不写 cutover marker。映射安全变化必须递增 version；禁止 DELETE 后重建复活旧 token，停用必须保留记录。已采用映射拒绝 downgrade。任务历史索引迁移 `f8b1d4e7a0c3` 接在导入 actor epoch 迁移 `e7a0c3f6b9d2` 之后，仅增加 owner/created_at/id 表达式索引，不改历史 actor。

读取与业务操作按 user SHARE → binding SHARE → 领域锁；管理映射按 advisory binding key → 顺序 user UPDATE → binding UPDATE。现有改权命令更新用户 epoch 的锁因此与在途业务互斥。在途已授权事务结束后，后续请求与任务立即看到撤销，不在同事务中悄悄换新身份版本。

## 验证状态

初轮真实 PostgreSQL + Cerbos 0.57.0：新适配、S1 engine/service/adversarial 与 B1 core 共 135 项通过。新增35项均通过，覆盖请求字段绑定、nonce竞争、撤权锁、映射ABA、实际facade receipt/list；测试位于 `tests/test_pn_catalog_access.py`。

集成后的新增 HTTP 5 项、JobAuthorize/worker 8 项及历史分页 6 项真实 PostgreSQL + Cerbos 测试通过：原生与委托请求、字节绑定与重放、客户端动作/字段上限、实际账号 epoch/映射/Grant 在外呼后撤销、原始身份上下文持久化、媒体所有权及 worker、身份 operation 权限交集、隐藏记录后继续分页、owner 索引实际查询计划及大结果分包无遗漏。HTTP/权限/queue 回归组合 74 项通过。旧 router 的 CatalogError 由专用全局 handler 保留 403 等领域错误码和安全 envelope，现有 HTTPException 行为不变。provider/OSS 均使用本地替身，验证外部 I/O 时无数据库 checkout；这不代表真实 OSS/provider 联通或生产验收。旧入口收口与两端页面统一验收仍须合并独立证据。
