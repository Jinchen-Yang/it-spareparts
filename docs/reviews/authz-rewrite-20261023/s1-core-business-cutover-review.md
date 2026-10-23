# S1 核心业务与切换第三角审查

结论：本次范围内发现的 **1 项阻断已由核心实现者修复**，原反例重跑通过；当前没有剩余阻断。此结论仅覆盖 S1 核心与销售接入，不表示 391 个入口或其余 planned 模块已迁移完成。

固定对比基线为 `16c38c3063c2f971d3c0cce65f02924f1250e053`，审查对象为 `codex/authz-s1-control-plane` 未提交工作树。依据 D-0027、[总契约](../../plans/2026-10-23-authz-full-migration-contract.md)、[冻结 wire](s1-wire-contract.md)。本审查者负责管理编排实现，因此本文对自己的管理代码**不宣称独立审查**；管理证据另见 [阶段记录](s1-management-implementation-report.md)。本文独立检查由根实现的 catalog/runtime/principal/service/engine、销售 adapter 和自助改密。

## Standards 轴：权威边界与失败方式

核心允许链为：验签得到 typed `VerifiedIdentity` → 读取当前用户并校验 epoch/active/deleted → 读取有效 Grant 与实时关系 → Cerbos CheckResources/PlanResources → 资源内字段与 SQL predicate。旧角色没有进入 PDP role 列表；PDP principal 只有 `registered_account`，各资源 Grant 和关系属性分别按资源配对。管理员角色仍用于本阶段控制面安全能力，不直接给予销售读取或导出。

`service.sales_access` 只把已获得的资源授权投影到 `UserContext`，写入完整显式字段图和 `explicit_field_policy=True`。因此遗留脱敏函数可继续复用，但不能从旧 admin、旧 token permissions 或 `ENABLE_RBAC=False` 扩大该图。销售八个入口都先取新授权 predicate，再调用现有业务查询和字段处理；读与导出分别请求 `read` / `export`，未知动作没有 PDP 请求机会。

快照按资源批量读取 marker、Grant，并一次进行批量 PDP 检查。缺 Grant、空 Grant、无效身份、无效配置都没有旧权限兜底；快照的旧 `sales_analysis` 顶层字段由同一结果派生。PDP 回应需要精确 resource/action/输出配对，输出 fields 不能超出显式 Grant，且 scope/version 必须对应。对象范围只给 `requires_context`，不把模块探测当成对象许可。本审查静态确认这些公共边界；异构资源及损坏 PDP 回应的独立动态证据属于 [另一位独审者](s1-implementation-independent-review.md)，不重复归属。

凭据写在用户最终行锁后再校验 active/deleted/epoch；权限写持有的用户锁与数据读取 SHARE 锁能够形成一致的先后关系。此处额外用真正 PostgreSQL 行锁等待证明，旧会话不能在撤权提交后通过自助改密取得新会话。

## Spec 轴：发现并闭合的阻断

**P1：目录描述能够覆盖已存在的 cutover 权威。** 修前 `runtime_states` 只有在 marker 存在且描述仍为 implemented 时才使用新链，其他情况回 legacy；启动只校验 enabled/suspended 名称属于 `RESOURCE_DEFINITIONS`，没有验证描述和执行闭集一致。

最小实证：创建没有销售 Grant 的实名管理员；正常已切换环境访问 `/api/sales/analytics/options` 返回 403。保持数据库 marker 不动，仅把候选目录中该资源的 `migration_state` 错标为 planned，同一 HTTP 请求返回 **200**。此毒针模拟候选构建的描述/执行目录漂移，不是修改数据库标记或伪造 PDP allow。它违反 wire 中“已切换不能恢复旧允许”及“先校验完整执行目录，再提交激活”的要求。

同一轮五项启动反例也失败：implemented→planned、描述漏 export、未知动作依赖、未知字段依赖、缺 denial_scope。启动均未拒绝，并已写入 marker。修前共 **8 passed / 6 failed**，日志 `/tmp/itsp-authz-s1-business-cutover-before.log`。

根实现者随后在公共 `catalog.validate_catalog` 中校验目录 ID、依赖闭集、跨资源 surface 引用，以及 implemented 与执行定义的动作/范围/字段/依赖/denial_scope 精确一致。运行配置解析先调用该校验，错误为 `503 AUTHZ_CATALOG_INVALID`；启动与运行态读取也拒绝任何没有执行定义的已切换 marker。planned 描述仍未变成可授予资源。

原 14 个测试**没有修改断言**即重跑通过。目录错误现在在 marker 写入之前拒绝；已 marker 的目录回退也返回 503，不再选择旧链。

## 独立动态验证

测试文件：`backend/tests/test_authz_s1_business_cutover_review.py`。使用真实 PostgreSQL、签名 HTTP token 与真实 Cerbos；无 dependency override，没有 mock 允许结果。唯一中断式 mock 是无 Grant 场景下将 PDP client 替换为立即失败断言，用来证明缺 Grant 不会接触 PDP。目录毒针仅改测试进程中的候选描述，不改业务数据库事实或实现源码。

| 用例组 | 验证内容 | 实际证据 |
| --- | --- | --- |
| 6 例旧角色反例 | admin/boss/sales × 无 Grant/空 actions，旧 token 全字段开启仍然 `/me` deny；options/orders/export 均 403 | 修后 14 例批次包含 |
| 1 例范围/字段一致性 | 旧 admin、旧 token 全字段、RBAC 旁路同时存在；新 Grant 只给一个客户与 customer 字段，快照和订单一致，只见该客户两单，金额隐藏；导出、金额筛选、直方图拒绝，越界订单 404 | 修后 14 例批次包含 |
| 1 例跨实例可见性 | 先在已有 PostgreSQL Session 读取“未切换”；另一启动事务提交 marker；原 Session 下一次读取马上 authoritative+suspended，不缓存旧无标记状态 | 修后 14 例批次包含 |
| 5 例启动目录毒针 | 描述/执行或依赖不一致时拒绝，marker 数仍为零 | 修后 14 例批次包含 |
| 1 例已有 marker 防回退 | 描述误回 planned 后，原本无 Grant 的管理员不能从 403 变旧允许 | 修后 14 例批次包含 |
| 2 例真实锁等待 | 用 `pg_blocking_pids` 确认自助改密在用户 `FOR UPDATE` 上等待；先提交 epoch 推进或软删除，随后旧请求 401，密码保持且不续签 | 另一次 targeted 批次 |

执行结果：

- `/tmp/itsp-authz-s1-business-cutover-final.log`：**14 passed，11.56s**。
- `/tmp/itsp-authz-s1-business-password-lock.log`：**2 passed，14 deselected，6.60s**。

当前文件共 16 例，证据为 14 + 2 两个批次；不表述成最后一次单批 16 全跑。测试基库为 `127.0.0.1:32780/spareparts_test`，conftest 按进程创建隔离库；PDP 为本机 Cerbos 二进制；合成业务事实复用既有 D-44 fixture。没有生产连接或操作。

## 检查点与剩余门槛

最终实读的核心文件 SHA256：

| 文件 | SHA256 |
| --- | --- |
| `app/authz/catalog.py` | `b8aca8812c871c61c8f0cd6950990db84244887ac98361d52a2f045a32c2ccbd` |
| `app/authz/runtime.py` | `a6e036781540f050b71d2e0be312bd9c3fc635d96d8dffca0781e380236fac4a` |
| `app/authz/principal.py` | `46b67103f3b87f0b545b843c86667633ce5ad8ecbd62230750a47e560a6f4d2e` |
| `app/authz/service.py` | `6ddef68f95f9f3fcc87985a907759585437ddcd2096df6f47178500bfe9871bd` |
| `app/authz/engine.py` | `2fa660a3465306d21ba652bc5805e6bb87555c0d8c30736905dc7854cf44576d` |
| `app/api/sales_analytics.py` | `32dc96462117e1db960dda97ea9eab70127b8232967de85c7d720861281965d7` |
| `app/auth.py` | `3498c7385298696c9a181fa82884887c035871e85235b3ae41d6a95012d5d58f` |

根作者的 `/tmp/itsp-authz-s1-root-final.log` 80 passed 属作者回归；另一位独审者的 `/tmp/itsp-authz-s1-independent-all-final.log` 36 passed 属其独立证据，两者均不计入本次独立 16 例。目录校验修订后的全套整合回归与精确 SHA CI 仍由根汇总。

S1 可继续整合验证；S2 不在本次实施范围，仍需自己的分阶段入口/范围/字段对拍。全部模块完成后仍需统一用户验收；本报告不表示可部署或已上线。
