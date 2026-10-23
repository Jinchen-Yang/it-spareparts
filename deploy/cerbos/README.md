# 本地授权 PDP（D-0026）

固定 Cerbos 0.57.0，Python SDK 0.16.1，SQLAlchemy adapter 0.4.0。策略随代码发布，禁止接收用户上传策略；PDP 不连业务数据库，也不接收登录口令。HTTP 只开放给后端所在的内部网络，禁止在主机公网发布端口。Admin API、API Explorer、payload logging、CORS、telemetry 关闭，strict evaluation 开启。

`config.yaml` 使用 `/policies` 只读目录，启动时加载，`watchForChanges=false`；更新政策应发布并重启固定版本 PDP。配置与政策文件由同一个发布版本提供。

核心层 `app.authz.engine` 提供无数据库依赖的 `Grant / Decision / Plan / QueryBindings` 与 `check_access / plan_access`。资源必须先登记到封闭的 `ResourceDefinition` 目录；旧页面键、任意资源字符串和未知字段不会自动启用。`check_sales / plan_sales` 仅包装当前销售资源的客户属性、整数 ORM 绑定。下一模块必须登记能力、范围、字段和查询属性，编写对应政策、可信数据装配及端到端测试，不能仅增加目录项就宣布迁移完成。

`module_probe=true` 只由后端发给 PDP，判断页面/动作能力；它不用于业务数据查询。真实查询固定发送 `module_probe=false`，查询计划再由官方 adapter 绑定 `FSalesOrder.customer_id`。因此空客户归属仍可打开页面、管理归属，而业务 SQL 返回空集合。一个用户/资源仅一条 grant，scope 与字段来自同一个 policy output，禁止合并不同授权的范围与字段。

客户 ID 以无损字符串进入 PDP；SQL 绑定仅把严格验证后的十进制常量转换为整数。官方计划会把单元素 `in` 简化为 `eq`，两种形式均覆盖，不转换索引列。`NULL` 客户不属于 assigned_customers。

字段输出只是授权决定，业务层仍必须实施字段投影、派生信息保护以及过滤、排序、分组、聚合和导出校验。这里不是任意字段自动脱敏框架。异常、未知资源/动作、无效字段/计划都不能回退旧允许；PDP 不可用返回 503。

模块启用后，临时暂停使用 `AUTHZ_SALES_ANALYSIS_SUSPENDED=true`，保持新链路权威并拒绝访问。`AUTHZ_SALES_ANALYSIS_ENABLED=false` 仅用于尚未切换的部署，不能作为已切换模块的安全回滚方式。

真实 PDP / PostgreSQL 回归：设置 `CERBOS_BINARY` 为校验过的 0.57.0 可执行文件，设置隔离测试 PostgreSQL 的 `PYTEST_DATABASE_BASE_URL`，运行 `python -m pytest tests/test_authz_engine.py tests/test_authz_service.py`。`tests.authz_pdp_helpers.cerbos_url` 自动加载本目录策略，将 HTTP、gRPC、debug 限制到随机 loopback 端口，并在结束后停止进程。CI 必须提供该二进制；本地没有时明确 skip，不以 mock 代替这项验证。
