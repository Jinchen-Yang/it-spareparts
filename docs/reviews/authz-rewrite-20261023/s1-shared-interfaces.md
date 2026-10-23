# S1 后端共享接口接缝

状态：实施前冻结、现已由 S1 候选实现的接口约定。wire schema（含 `denial_scope`）及 45 个 DTO 样例已获根代理批准；前端对拍和独立复核通过。这里只定义 core 与 management 的协作边界，接口实现证据见阶段实施报告；本文本身不能代替 S1 精确提交 CI。[wire contract](s1-wire-contract.md) 是 HTTP 真值；本文不增加 HTTP 端点、不改变其 DTO。

## 1. 依赖方向和所有权

```text
current_verified_identity → VerifiedIdentity → load_principal → Principal
                                               ↓                    ↓
                  catalog + runtime ← management 写编排      service 业务/能力快照
                                           ↓                       ↓
                                 profile + Grant 同事务          Grant → Cerbos
```

- root：`authz/catalog.py`、`engine.py`、`service.py`、新 `principal.py/runtime.py`，以及 auth/security/config/main 启动、销售入口 `enabled(db)` 适配；测试拥有 `test_authz_engine/service/sales_integration`。
- 控制面代理：`authz/management.py`、models、一个 Alembic migration、`api/accounts.py/role_templates.py/authorization.py`；测试拥有 `test_authorization_management` 及新增 management 测试，并在 `conftest._TABLES` 登记无 FK 的 cutover 表清理。root 的 sales integration 若调用旧 S0 PUT，由控制面代理提供 canonical body/helper 接缝，root 修改自己测试，避免同时写同一文件。
- `management` 依赖 core；core 不导入管理 HTTP handler，也不从 profile/模板产生运行允许。暂不引入另一个目录、主体类、PDP 包装或模式判断。
- 本文标作“建议”的可选参数可由 root 保持更小签名；实现前通知双方即可，不影响已冻结 wire。

## 2. root 提供的稳定入口

| 模块与名称 | 参数/结果 | 控制面调用方式 |
| --- | --- | --- |
| `catalog.CATALOG_VERSION` | opaque `str` | 所有新 envelope、请求目录 CAS、指纹均取此值；移除 API 私有目录版本 |
| `catalog.RESOURCE_DEFINITIONS` | 只读的**可执行**资源定义 map | 不把 planned 描述条目变成可执行资源；S1 仅已有实现可进入运行校验 |
| `catalog.get_resource_definition(resource)` | `str → ResourceDefinition`，未知资源拒绝 | 定义包含 actions/scopes/fields/dependencies 及 `denial_scope`；不能根据字符串前缀猜注册 |
| `catalog.validate_grant(resource, actions, scope, field_permissions)` | 保留当前四参数纯函数，合法返回 `None`，非法抛 `ValueError` | DTO 严格类型检查后调用；用于 replace/模板包/保存快照/物化结果，不在 management 复制依赖规则；失败映射稳定 422 和 JSON Pointer |
| `runtime.authorization_mode()` | `legacy_compat\|unified_modules` | 只决定控制面协议，不证明资源是否仍能走 legacy |
| `runtime.runtime_states(db)` | 一次批读 marker，返回资源 ID 对应的 migration/runtime/configurable 状态 | 一次管理命令或快照复用同一份 request-local 结果；不得进程缓存“无 marker” |
| `runtime.is_authoritative(db, resource)` | `bool` 或失败关闭异常 | root 销售入口用作“是否必须进入新授权路径”；marker 已存在即使配置撤回也不能返回 false；配置显式启用但 marker 缺失也不能导向 legacy |
| `runtime.require_configurable(db, resource)` | 合法返回 `None`，非法按 wire 拒绝 | 每个明确修改/移除资源及模板物化影响项均校验；不是只验证传入的一个 page key |
| `principal.current_verified_identity` | FastAPI dependency → `VerifiedIdentity` | canonical 路由以此替换 dict 身份依赖；请求体不能构造类型化身份 |
| `principal.load_principal(db, identity, *, locked_user=None)` | `VerifiedIdentity → Principal\|None` | 默认无行锁预检；传入本事务已锁账号后重新验证，不新增锁/查询；不得传 dict 或 `UserContext` |
| `principal.require_named_admin(principal)` | `Principal\|None → Principal`，不满足则 403 | canonical 改权、模板、建号、安全属性及他人改密统一入口；不能用请求/token 的 role 替代 |
| `principal.actor_capabilities(principal)` | wire 中同名 capability → Decision map | `/me` 与账号普通资料读写使用同一规则；普通资料不能误提升为安全命令资格 |
| `service.my_snapshot(db, identity)` | `VerifiedIdentity →` 完整 `/me` wire success object | API 只设 no-store 后返回；不得二次拼装允许字段或从目标诊断覆盖操作者 |
| `service.catalog_snapshot(db)` | 完整 `/catalog` wire success object | API 在实时管理员准入后调用；planned 描述和 executable 定义由 root 统一维护 |

`runtime.require_configurable(..., *, states=None)` 是批量写的建议优化：传入同一命令刚批读的状态 map，防循环内逐资源查 marker。若 root 不增加参数，需以另一种 request-local 批量实现保持相同查询上限，不能依赖跨请求旧快照。

统一模式下 `implemented+enabled` 与 `implemented+suspended` 均可配置；后者允许管理员修正配置，但业务仍 deny。`implemented+legacy` 和 planned/retired 不可配置，不新增 staging。

`denial_scope` 已批准：`sales_analysis=assigned_customers`，planned/retired 描述为 null。执行资源的合法空配置为 `{actions:[],scope:denial_scope,field_permissions:[]}`；它是撤销 tombstone，不赋予行范围或字段。management 不维护第二份默认 scope 表。

### 类型化身份的准确边界

- `VerifiedIdentity` 由 root 的可信入口创建，至少提供 `username/token_version/fallback`；可提供只读复制的 `legacy_claims` 给尚未迁移分支。不能 `VerifiedIdentity(**request.json)`，不能由 token role/perms 直接制造 `Principal`。
- `load_principal` 只接受这一类型。有效共享回退可返回 `None`；过期、停用、删除、用户名/版本不匹配的实名身份返回 401。`require_named_admin(None)` 返回 403。
- `Principal` 至少向 management 暴露稳定数字 ID、username、实时 role 和 authorization_epoch（映射 token_version）；显示名等只用于输出/审计。具体类字段以 root 实现为准，管理服务不另存主体副本作授权依据。
- 已锁重验时 `locked_user` 必须来自本事务完整锁集合、`populate_existing=True`；若 actor 不在锁结果中，管理服务直接拒绝，不以 `None` 触发无锁重新加载。root 重验必须核对行与身份的对应关系。
- `actor_capabilities` 从当前账号规则计算普通 `page_accounts/action_account_manage` 资料能力；新授权/模板/凭证安全能力仍需实名管理员。它既不能信旧 token perms，也不能给新业务资源 admin 自动全开。

## 3. 管理编排如何复用

单写、bulk、template sync、原子建号和安全命令走同一 `management`，HTTP handler 只负责严格解析、调用、no-store 和 DTO 输出。

1. 从 typed identity 预检实时 actor，无锁；校验 schema/catalog 和严格命令类型。错误按字段出现判断，空旧字段也不能吞掉。
2. 得到完整目标/模板/资源集合；必要时先取管理员 roster advisory lock，再按模板稳定顺序锁，最后按用户数字 ID 一次取齐最终所需锁强度。不能依赖已经持有 actor SHARE 锁的 dependency。
3. 用传入的 `locked_user` 重验 actor；目标 stable ID、username、epoch、模板 version、资源 version 及 preview fingerprint 均在锁后校验。业务保护由 management 实施，身份/PDP/目录规则仍由 root 提供。
4. 一次读取 runtime 状态；校验本次影响的可配置资源，并用同一 `validate_grant` 校验最终完整配置。保存 snapshot/override **key 存在性**，按 wire 原子更新 profile、Grant、关系、审计及 epoch。
5. management 组装账号/模板的**管理态**读模型；这些展示 configuration/source，不是操作者允许快照。响应中 self revoke 由 actor ID 与本次 epoch 变化计算，不再拿失效会话另 GET。

root 的 `/me` 由批量 Grant + marker + 当前主体生成；不得调用 management 的来源合并函数来补 allow。management 不调用 `sales_snapshot` 判断模板合法性，也不为保存配置请求 PDP 代替目录校验。

### 统一失败与事务

core 的 HTTP 边界错误应遵循 wire 的 `detail:{code,message,committed:false,conflicts:[],field_errors:[]}` 及 no-store；`validate_grant` 保留纯 `ValueError` 是唯一在 management 包装的目录校验例外。root 若使用统一异常构造 helper，management 直接复用；不能出现 runtime 拒绝字符串、API 另猜文案码的双规则。

management 命令持有一个根事务，失败整体 rollback。core 的 load/validate/runtime/snapshot 不隐式 commit、rollback 或写 cutover。启动初始化写 marker 由 root 独立短事务负责，不复用普通请求 session。读取能力快照可使用短 SHARE 锁，但不能把该快照函数作为写命令的前置依赖。

## 4. 模型和迁移供 root 使用

| 模型/字段 | 保证 |
| --- | --- |
| `SysAuthorizationCutover` | 定义于 `models/authorization.py`；建议表名 `sys_authorization_cutover`；`resource` 主键、`activated_catalog_version`、`activated_at`；不以 Grant 存在替代 marker |
| `SysUser.authorization_profile` | JSONB；规范 `schema_version/template_snapshot/resource_overrides`；migration 将既有 sales Grant 记为显式 override，保留空动作及与模板相等值；runtime 不以此允许 |
| `SysRoleTemplate.authorization_bundle` | JSONB 资源 map；沿用 `SysRoleTemplate.version`；旧模板 bool 不猜成规范包 |
| `SysAuthorizationGrant` | 继续为唯一运行允许来源；原 user/resource 唯一键与递增 version；不为 planned 描述放开 DB scope 约束 |
| `SysUser.token_version` | 直接作为 authorization_epoch；本次来源变化亦推进，不新增 source_revision |

迁移不自动 cutover、不写部署状态、不改变旧 role/template 快照；root 启动在 schema 就绪后按已批准流程激活。数据库列默认、JSON 形状/回填与实际 Alembic head 在开工后实现测试，不能把此表当作已存在。

## 5. 接口联调的最小反例

- canonical handler 传 dict/`UserContext` 给 principal loader 被拒；合法共享回退不能管理；锁后 actor epoch 或角色变化使整批零提交。
- 一次 20 资源命令只能批读状态，不按资源读用户/marker；`/me` 仍只读当前主体，目标 modules 不污染其能力。
- 另一实例写入 marker 后，本实例下次 legacy 销售请求立即进入新路径；配置撤回、启动未完成、PDP 故障都不能恢复旧允许。
- 模板保存、clear、sync、相等值 override 均调用同一目录校验；未知/待实施资源与依赖缺失不能只在前端阻挡。
- 失败协议与成功 envelope 分开，所有失败 committed=false；管理响应不是 PDP 能力证明。

这份接缝不替代 [独立审查要求](s1-wire-independent-review.md) 中的真实 PostgreSQL 并发、PDP 异常、迁移和原子性测试。
