# S1 后端实现独立对抗复核

复核时间：2026-10-23 17:21 UTC。基线 `16c38c3063c2f971d3c0cce65f02924f1250e053`，对象为 `codex/authz-s1-control-plane` 未提交实现。此报告不能代替完成提交后的精确 SHA CI，也不包含 S2/S3 实现或生产验收。

结论：本轮识别的具体实现问题已修复并经反例复验；独立后端测试 **36 passed**。在本轮覆盖范围内未留下阻断。S1 整阶段仍需作者完整回归、前端验证、其他独立审查及精确提交 CI。

## 已发现并闭合的问题

| 问题 | 修正与独立证据 |
| --- | --- |
| JSONB 默认值的紧凑冒号被 SQLAlchemy 解释成 bind，空库 Alembic upgrade 生成非法 JSON | 作者修正模型及 migration 默认字面量；后续每次独立 pytest 均从隔离空库跑完整迁移链成功 |
| 建号 security_change 接受任意非 admin 初始 role，超出冻结 wire | 限制显式初始角色；公开 POST 的 boss 反例返回结构化 422、账号零落库 |
| operation/kind/override_policy 异型 JSON 可在 set membership 触发 TypeError | 作者增加严格类型校验；单写 operation 为数组、对象、bool、null 均 422、Grant/source/关系/审计/epoch 零变化 |
| 旧 account action dependency 先拦截 canonical 改密请求，返回旧字符串及缺 no-store | canonical 分支由 typed/live 管理准入；无旧管理权、仅有旧委派管理权两类普通账号均得到 canonical 实名管理员拒绝，零写入 |
| context scope 的全局 module probe 可直接产生无对象上下文的 ResourceAccess | 公共 resource_access 要求对象、关系 revision、领域属性与查询绑定；synthetic scope 的真实 PDP `/me` 决定为 requires_context，无上下文业务调用 403 |

另独立核对了 `requires_fields` 的前向 wire 补充，3 个 golden action descriptor 和实际 87 项 catalog manifest 的动作/字段依赖闭集检查通过；S1 executable read/export 不增加字段依赖，S2 描述不能提前执行。施工期间的 service comprehension 语法错误已通知作者并修复，不计为最终设计缺口。

## 独立测试覆盖

测试唯一拥有文件：`backend/tests/test_authz_s1_adversarial.py`。HTTP 使用真实签名 token、数据库实时账号和真实路由，无 dependency override。成功授权来自 pinned Cerbos 0.57.0；没有 mock allow。

| 契约反例 | 本轮运行证据 |
| --- | --- |
| S1-01/02 身份与凭据边界 | shared 即使带 admin/旧允许声明也得到全 deny；epoch/停用/删除/降级后拒绝；旧普通账号管理能力不能重置他人凭据；profile 混发空旧权限/role:null 连显示名也不保存 |
| S1-03/14 闭集与原子性 | 有效 sales 更新后附 planned/retired/未知资源整批失败；非法建号角色拒绝；异型 operation 结构化失败 |
| S1-04/05 来源与 CAS | 相等值 override 保持存在并推进 epoch/version；无模板 clear 物化拒绝 tombstone；旧 version=0 不能复活；模板 v1→v2 不追改用户，clear 回保存 v1，sync preserve 后仍保留独立 A，清除才恢复保存 v2；显式拒绝 sync 后不消失 |
| S1-06/07 批量与客户 | 预览后只改 Bob，Alice 也不提交；客户省略保持、空数组清空、无效客户整个事务回滚；客户为空仍可有页面读资格但范围为空 |
| S1-08 写入竞争 | 两个并发真实 HTTP writer 争相同 epoch/version，恰好一成功一 409、一个审计且无重复 bump；不通过重试掩盖竞争 |
| S1-09/10 切换 | cutover 后撤回 enable 且切 legacy，快照 suspended、sales API 503、旧 S0 PUT 409；缺 marker 的 runtime read 不写 marker，显式启动事务可以激活且幂等 |
| S1-11 跨资源与主体 | 两个 synthetic 执行资源从 PG 加载不同 action/scope/fields，在一次真实 CheckResources 中严格配对；真实响应后注入缺项/重复/错 kind/错 scope 均整体失败关闭；普通 dict/UserContext 不能进入 typed principal loader |
| S1-12 快照与故障 | 1→2 synthetic 资源仍各一次 Grant 批读、marker 批读和 PDP 请求，SQL 数相同且无客户/项目对象枚举；真实 PDP 断连返回 503 而非部分 allow |

两份 synthetic policy 仅存在 pytest 临时目录，不在生产 policy 目录注册真实 planned 业务资源。对恶意响应的注入发生在真实 PDP 调用返回之后，只用于失败关闭验证。测试进程仅使用本机隔离 PostgreSQL 与 loopback PDP。

## 执行结果与边界

```text
PYTEST_DATABASE_BASE_URL=postgresql+psycopg://spareparts:spareparts@127.0.0.1:32780/spareparts_test \
CERBOS_BINARY=/tmp/itsp-authz-engine-feasibility-20261023/binary/cerbos \
.venv/bin/pytest tests/test_authz_s1_adversarial.py -q

36 passed, 1 warning in 15.52s
```

工作目录 `backend/`；日志 `/tmp/itsp-authz-s1-independent-all.log`。唯一 warning 是既有 Starlette/httpx 测试适配弃用提示。

本轮不冒充全面覆盖：最后管理员/双管理员互改、template archive 对 sync 的并发、迁移含历史数据的 round-trip、自助改密竞争及前端迟到响应必须由阶段汇总核对作者/前端测试证据；原 S1-13 属于前端独立验证。S3 对象关系撤销仍需领域接入后的真实反例，不能用这里的 synthetic scope 替代。

审查代理仅新增独立测试及 review docs，未修改作者业务实现，未进行生产操作。

## 第二角度：前端会话与架构接缝

前端已只读复核 `AuthzProvider`、wire decoder、统一控制面、模块编辑器、账号/模板 Drawer、批量及安全命令 Modal、来源 diff 和客户选择器。未修改作者实现；另经作者确认独占新增 `frontend/src/pages/accounts/__tests__/S1ControlPlane.independent.test.tsx`。

发现并由前端作者闭合两项：

- 安全建号 preview DTO 不使用 security_before/after，原 renderer 因此显示 `{}`→`{}`。现按规范 command 显示用户名、管理员/readonly、销售映射、模板固定版本或显式模块配置及客户 ID，不显示密码。
- 请求中关闭窗口或换目标后，原 hook 先按 alive/generation 丢弃已提交的 self-revoke 响应。现先以捕获 token 是否仍为当前会话判断撤销；即使旧窗口已卸载也清掉该会话，而 A 的迟到响应不能清 B。普通数据结果仍由视图存活与请求序号控制。

新增独立前端 **7 tests passed**：关闭/改变请求序号后本人撤权、旧 A 不清 B、卸载后普通成功不污染视图、409 不自动重放、安全建号确认内容，以及无模板但存在个人覆盖时清除来源可用、显示撤销提示且请求带原 epoch/resource version CAS。日志 `/tmp/itsp-authz-s1-independent-frontend-clear.log`。Provider 的 token+sequence、防迟到 401、模块页面 fail-closed，及编辑器不自动补字段、planned 只读、suspended 可改配置但业务拒绝，与合同一致。批量执行只回传服务器规范 command+fingerprint；编辑意图立即清预览，clear 恢复保存快照而 sync 追固定最新版本，目标配置不注入操作者 Provider。

自助改密另按根代理请求复核：`auth.py` 在 `populate_existing=True` 的 `FOR UPDATE` 后重查 deleted_at 与 token_version，避免认证后等待期间被撤销的旧会话再改密续签。独立执行 `test_authz_s1_password_race.py`，真实 HTTP/PG 两个屏障反例 **2 passed**；不只是检查改密返回码，还验证旧密码保持、epoch 不被额外推进、无改密审计。日志 `/tmp/itsp-authz-s1-independent-password-race.log`。

管理实现追加 source↔Grant 诊断及 cutover 后禁止降级之后，本独立后端文件再次 **36 passed in 19.19s**，日志 `/tmp/itsp-authz-s1-independent-all-final.log`。初次组合运行作者前端测试暴露 3 个随 UI 改动失效的断言（重复文本选择器、隐藏 Select 项、原本已选 export 再点击的预期），作者修复后最新组合运行 **4 文件 32 passed in 5.93s**（独立 7、控制面 12、协议 7、Provider 6），日志 `/tmp/itsp-authz-s1-independent-frontend-clear.log`。前端整套与 build 仍由阶段汇总记录。

根代理新增目录与执行定义闭集/依赖一致性校验、已有 cutover 缺执行定义失败关闭后，再次执行独立后端 **36 passed in 15.66s**，日志 `/tmp/itsp-authz-s1-independent-registry.log`。两份 synthetic 测试定义均配完整 wire descriptor，包含动作及字段依赖、范围及上下文、denial scope；不绕过新校验，不改原断言。生产目录未增加测试资源。

架构结论：S1 已把可信身份、持久切换、目录、运行决策与控制面写事务分开。运行 allow 只从 live Grant 经 PDP 产生，模板/profile 只解释配置来源；前端操作者能力与目标账号配置也已分离。通用编辑器使用同一目录依赖，单写/批量/模板都进入同一 canonical 管理服务，原旧入口不再是并行新授权写路径。保留的 sales adapter 与旧模块 bool 属于明示的迁移接缝；S1 并未完成其他业务模块改造，不能据此宣称全模块重构结束。

## 本机隔离栈浏览器开发调试

使用 Playwright skill 的 headless Chromium，真实登录 `http://127.0.0.1:15176`；API/PDP 为本机隔离端口，独有合成数据库。未访问生产，也不是最终用户验收。真实目录返回 87 资源。

- 合成目标账号从无配置保存 sales `read/assigned_customers` 与客户 ID 1，同一 PUT 200，epoch 0→1，个人来源与客户归属正确。
- 无模板清除个人覆盖：UI 可操作且提示撤销；PUT 200，epoch 1→2、资源 version 1→2、actions=[]、override absent、source=none，客户归属仍为 [1]。
- 创建只读模板 POST 201；账号列表重新加载后目标仍 epoch=2、template=null，无隐式同步。
- 显式应用模板 preview 200，界面显示拒绝→读取、保存模板版本及旧登录失效；执行请求与服务器 command 完全相同（仅加 dry_run=false/fingerprint），fingerprint 相同。执行 200，epoch 2→3、模板 v1、无个人覆盖、客户归属保留。
- 截图：`output/playwright/s1-target-saved.png`、`output/playwright/s1-template-applied.png`。浏览器已关闭，服务器留给根代理清理。console 仅有既有 AntD destroyOnClose 弃用提醒及 favicon 404，无授权业务请求失败。npm CLI 曾等待包查询，改用已有缓存后继续；不作为产品失败。

响应证据保存在本机 `/tmp/itsp-s1-browser-{catalog.txt,save.json,clear.json,post-template-accounts.json,preview.json,execute-body.json,apply.json}`，仅合成数据，不包含登录 token。未新增或修改 app/tests。

## S1 来源资格补充独审

补充原因成立：canonical 的空账号/空模板是明确授权结果，不能凭 JSON 为空推断其来自旧角色体系。两表 `authorization_legacy_seed_eligible` 仅保存迁移来源：ORM/SQL 默认 false；本次未部署 S1 migration 的已有行 true；canonical 创建/复制显式 false，legacy 创建及旧 seed_users 新行显式 true。现有账号的保存快照与版本不因增加来源列变化；正常 canonical 更新不重写来源标记。静态检查确认该字段未进入 wire 或运行 allow。

独立新增 9 个真实 PG/PDP 反例：原有空数据与新建 SQL 默认来源分开；旧 snapshot/override/epoch/template version 保留；canonical none 与复制 legacy 空模板均 false；来源 true/false 本身均不能允许资源，合法 Grant 写入不改变来源；三处客户端字段注入 422 且不落库；无 marker 的 canonical 空账号/空模板均拒绝来源擦除式降级。

最后两项独立复现了降级缺口：旧 guard 仅检查 cutover marker，两个反例均因未抛异常而失败（`/tmp/itsp-authz-s1-independent-origin-downgrade.log`，2 failed）。作者已在任何 DDL 前加入两表任一 false 来源即拒绝降级；原样完整重跑 **45 passed in 22.83s**，日志 `/tmp/itsp-authz-s1-independent-origin-final.log`。修复允许空库/全 legacy 且无 marker 的迁移往返；本独审真实 migration 测试也经过该路径。`git diff --check` 通过。

结论：本补充审查无剩余阻断；未来 S2 seed 仍须显式读取此来源资格、逐资源记录迁移完成状态并遵守已冻结差分规则。当前测试不宣称尚未实施的 S2 seed 已验证，也不替代本次补充提交后的精确 SHA CI。

### 降级事务锁与历史 CI 夹具适配

检查条件与删除列之间需要同事务表锁：未提交 canonical insert 对普通 EXISTS 不可见，随后 DDL 才等待它，可能在该 insert 提交后继续删除来源。作者把三表 ACCESS EXCLUSIVE 锁移至所有检查之前。本独立新增真实双连接反例：writer 插入 false 来源但不提交；从 PostgreSQL `pg_blocking_pids` 与等待 SQL 确认 downgrade 正在 LOCK TABLE 处阻塞；writer 提交后 downgrade 抛来源保护异常，列、false 值与起始 Alembic revision 均保留。不是 sleep 猜时序，也未 mock migration/数据库结果。最终定向 **1 passed in 3.99s**，`/tmp/itsp-authz-s1-independent-origin-lock-final.log`。

另对三个历史失败修复做独立 diff 审查与实跑：

- `test_maintenance_project_no_return_note.py`：仅历史 schema 往返 fixture 标明 legacy 来源；升级后新增旧 token 401，真实登录取得新 token 后继续原非空备注降级拒绝、版本与数据保留断言。
- `test_purchase_analytics_permissions_migration.py`：旧 schema 用反射 Table 插入当时存在的列，避免当前 ORM 引用未来列；boss/readonly 的快照、覆盖层及模板全部原断言保持。
- `test_token_revocation.py`：直接 handler 测试改用 dict payload 与 typed identity 配合现签名；没有减少原撤权、显示名不撤权、并发递增及登录竞争断言。它仍不负责证明 HTTP 操作者认证，相关真实接口证据由 S1 独立测试提供。

独立文件（现 46 项）与上述三文件组合 **84 passed in 41.69s**，日志 `/tmp/itsp-authz-s1-independent-lock-regressions.log`。未改业务作者代码，测试修复没有放宽原业务约束；当前独审无剩余阻断，精确 SHA CI 仍由根代理确认。

### 其余历史迁移测试适配静态复核

追加逐项审查 data_quality、collection_reminders、manager_workbook_v3、project_note、contract_amount_remediation、maintenance_roundtrip、replenishment_atomic，以及 maintenance_beta/boss、replenishment_beta、sales_analytics 四个 permission migration diff。改动仅把被测历史账号/模板声明为 legacy 来源；JSON null/array、快照回退、权限覆盖、历史事实与降级拒绝的原断言均未减少。project_note 同 no_return_note，新增旧 token 401 后真实重新登录，继续原非空备注保护验证。

`test_migration_manual_pool.py::preserve_pool_seq` 从仅依赖 migrated 改为依赖标准 db 清理合理：清理发生在每个测试自己建立历史事实之前；同时使用 db 的测试复用同一个 fixture，不会再次清掉本例种入的数据。原池 metadata/policy/multi-pool/manual-source 降级保护及回滚断言全部保留，生产 guard 未修改。跨模块 assignment history 的保护仍由 manager_workbook_v3 用例在本例显式种入后验证，不依赖前序测试遗留数据。此处未重复运行；根代理负责完整 downgrade 节点组合证据，静态审查无新增阻断。

### Modal 入场期间冲突提示的测试修订

独立复核 `ModuleControlPlane.test.tsx` 单文件 diff：冲突测试参数化为已显示/正在入场两种状态；后者明确构造错误已在 DOM、祖先 dialog 尚隐藏，再由一次真实零延时 timer 恢复可见。作者红日志 `/tmp/itsp-authz-s1-modal-red.log` 证明原即时可见断言在入场分支失败；修订使用 `waitFor` 等待实际可见，没有把可见性降为仅存在。原执行按钮禁用、API 总计两次、未成功回调仍保持，并新增点击禁用按钮不重放、未关闭窗口断言。成功等待依赖恢复可见的 timer 已执行，不遗留该 timer；afterEach 继续标准 cleanup。这是组件异步可见性契约的确定性模拟，不宣称验证浏览器动画引擎。

独立运行该文件 **13 passed in 6.74s**，日志 `/tmp/itsp-authz-s1-modal-independent.log`；`git diff --check` 通过。未修改测试或业务代码。本修订无阻断，可提交精确 SHA CI；不等同远端 CI 已通过。
