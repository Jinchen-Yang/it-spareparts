# 权限首批实现契约（D-0026）

基线 `7a9fd2098acb6be0009a59a84e7d7330bb960d6b`。选择依据见 `../reviews/authz-rewrite-20261023/implementation-tradeoffs.md` 与 `implementation-feasibility.md`。

## 对外接口

- GET `/api/authz/me`: `{version: string, username: string|null, sales_analysis: {enabled: boolean, read: boolean, export: boolean, scope: "all"|"assigned_customers"|"none", customer_count: number, field_permissions: string[], reason: string}, can_manage: boolean}`。关闭模块切换时 enabled=false，前端保持旧页面逻辑；启用时只认该快照。响应 no-store。
- GET `/api/authz/users/{username}`: 管理员读取 `{username, version: number, actions: string[], scope: "all"|"assigned_customers", field_permissions: string[], customer_ids: number[], customers: {id:number,name:string,raw_name:string}[]}`；无记录返回版本 0、空动作、assigned_customers、空字段/归属。
- PUT `/api/authz/users/{username}`: `{expected_version:number, actions:("read"|"export")[], scope:"all"|"assigned_customers", field_permissions:string[], customer_ids:number[]}`。完整替换单资源授权与该账号客户归属，原子审计/版本冲突 409；客户 ID、字段及动作严格白名单，export 必须同时 read，data_profit 和 data_reconciliation 各自依赖 data_purchase_cost。响应同 GET。限实名管理员。
- GET `/api/authz/customers?q=&limit=50`: `{items:{id:number,name:string,raw_name:string}[]}`，仅管理员；查询支持已有客户规范名，不创建新客户。
- GET `/api/authz/catalog`: 显式列出 migrated/legacy 模块状态与可配置资源；不得把 legacy 项显示为已受新引擎保护。

## 内部接口及所有权

- `app.authz.service.sales_access(db, ctx, action="read") -> SalesAccess`：实名查询、显式授权、客户 ID、PDP 查询计划。SalesAccess 含 `predicate`（绑定 FSalesOrder.customer_id 的 SQLAlchemy 条件）、`context`（用授权字段构造的 UserContext）、`scope`、`customer_count`。无授权抛 403；PDP故障 503；空归属允许页面但 SQL false。
- `app.authz.service.sales_snapshot(db, ctx) -> dict` 返回 me.sales_analysis。`app.authz.service.enabled()` 在 enabled 或 suspended 任一开启时返回 true；暂停仍保留新授权权威，快照返回不可读/导出、数据 API 返回 503，不回退旧权限。
- `app.authz.engine` 不访问 DB；传入可信 subject/grant/assignment 并使用官方 adapter；不接受用户上传政策或任意 SQL。
- 模型 `app.models.authorization.SysAuthorizationGrant`：id、user_id、resource（unique user/resource）、actions JSONB、scope、field_permissions JSONB、version。
- 模型 `SysCustomerAssignment`：user_id/customer_id 复合主键、外键。新表的审计使用 SysAuditLog。迁移只能按已记录业务规则生成 grants，归属初始化为空。
- sales_analytics/sales_dashboard 各服务函数统一可选 `authorization_predicate` 参数，必须传播到每个事实查询，兼容未迁移内部调用；API 启用模块后传入该参数并改用 SalesAccess.context 字段政策。禁止以用户筛选参数代替此条件。
- 根代理拥有配置/main路由/pyproject/lock/Compose/销售 API/服务 SQL 集成与总测试；核心代理拥有 authz 包和政策；管理代理拥有模型/迁移/authz API 及相应测试；前端代理拥有新 Provider/管理页面/App/nav/API 接入及测试。
