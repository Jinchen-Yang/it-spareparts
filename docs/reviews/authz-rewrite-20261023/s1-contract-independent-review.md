# S1 契约独立复审：控制面与领域接缝

复核时间：2026-10-23 16:15 UTC。代码基线 `1be345dd2e6396714de692ac5765a6920d7a272b`；同时审阅工作树 D-0027、full-migration-contract/execution 及 backend/frontend/non-http 三份入口草案。本报告只读核对业务代码；不修改实现，不证明 S1 已通过运行测试。S0 六个文件保持独立复测冻结。

结论：统一方向可实现，但以下四项应在控制面 DTO/事务实施前闭合，另两项是通用核心的实现准入条件。新增契约已处理批量快照 N+1、按资源配对的 PDP Grant、可执行目录与三态开关，不再将这些已修订事项重复列为设计缺陷。D-07 被替代、D-03/D-25 保留的引用正确。后端 87 与前端 85 个资源的差额恰为两个 retired 资源，不是遗漏两个页面。

## 阻断 1：普通账号管理与改权的分界仍漏了范围属性和凭据

**代码事实。** `backend/app/api/accounts.py:374` 的 `update_account` 把 role/template/overrides/permissions 视为 `perm_change`，但 `salesperson_name` 在该分支外写入；代码第 435 行也明确承认其参与维保/补货行范围。`reset_password`（448 行）只额外保护目标 `role=admin`，持旧 `action_account_manage` 者可以重置其他账号密码。`_apply_template`（164 行）同时修改 role；bulk（496 行）仍按旧 HIGH_RISK_KEYS 分辨授权写。backend 草案把这些路由统归 `account_lifecycle.update/bulk_update/reset_password`，`parameter_branches` 均为空；模板 sync 仍归可委派的 `legacy_template.sync`。

**可复现的协议反例。** 非授权管理员如果还能把自己的 `salesperson_name` 改成另一个销售，已有 canonical-sale 范围立即变化，epoch 递增只会要求重新登录，不会阻止扩权。若该管理员能替具有更多规范 Grant 的非 admin 用户设定密码，就能取得其身份；单独拒绝 Grant PUT 无法提供“普通资料管理不含改权”的隔离。这里是现有能力与新边界的冲突，不能声称本次已新增一个线上漏洞。

**最小修订（根已确认采用）。** 将 profile-only 字段与 security mutation 显式分类：display_name 可以按 account_lifecycle 管理；role/模板/权限包/`salesperson_name`/客户关系都进入 canonical 命令，并要求实时实名授权管理员。管理他人凭据 reset_password 同样仅允许实时授权管理员；本人自助改密保留既有协议，不能混同管理员重置。不能只靠 DTO 名叫 profile 放行。所有旧入口须按 payload 分支标注转交/拒绝/仍合法的 legacy 子集；混合请求不允许部分写成功。目录动作应列出其可写属性及最终授权复验所在应用服务，而非只给路由一个笼统 update 名称。

**验收。** 非管理员自改销售身份、改角色/套模板/bulk/sync/重置更高授权账号凭据均不能取得新的业务能力；仅改 display_name 不触发授权配置重算；旧布尔 payload 不返回假成功。用 HTTP 行为测试验证，不只检查调用了某个 helper。

## 阻断 2：账号共享锁不能原样推广到领域写事务

**代码事实。** `backend/app/authz/service.py:41` 的 `_load` 持有 `SysUser FOR SHARE` 直到请求事务结束。`backend/app/services/maintenance_project_assignments.py:382` 已明确采用 `workbook state → project → assignment → target user FOR UPDATE`；archive 路径也先锁 state/project。现账号 bulk/template sync 按 username 锁目标，通用授权 PUT 按数值主键锁 actor/target，账号降级/停用在锁目标后再取 active-admin-roster advisory lock。

**具体循环。** 新领域写请求 T1 先通过通用 access 锁住用户 U，然后等待项目 P 的 state；T2 已锁住 P 的 state，改派负责人给 U 时等待 U 的排他锁。把所有业务入口简单包一层现 `_load` 会引入这个锁序反转。“按账号主键排序”不能解决跨表的循环。

**最小修订。** 统一主体预检可以无锁，但领域写的最终授权复验必须与其既有锁协议接合；不能在 FastAPI dependency 中提前永久持有账号共享锁。纯授权管理事务采用固定 `roster（确实需检查时）→ template（稳定顺序）→ 全部 actor/target user（数值 ID 顺序）→ grants/客户关系`，不再进入项目写事务；领域事务保持既有 `state/project/assignment → 完整参与用户集合`，取得锁后重查 actor、epoch、Grant 与关系并执行写入。HTTP handler 不预先锁自己再调用编排器。只读快照可继续短事务共享锁，长任务不能沿用该锁跨 LLM/等待阶段。

**对根方案的剩余检查。** “先领域锁，后持共享账号锁的 resource_access”方向正确，但需要把现有 owner/target 用户锁也包含进去。不能先锁 target B，再锁 actor A；另一个项目上 B 改派给 A 会持 target A 后等待 actor B。也不能两个事务先各持同一用户 SHARE，再都升级 UPDATE。领域锁完成后，按稳定 ID 一次取得 actor/target/owner 完整集合各自最终所需的锁强度（首版可统一 UPDATE，代价是短事务内更强串行），resource_access 复用这些已锁实时记录；禁止后续追加更小 ID 或再升级锁。授权拒绝必须回滚整个根事务，包括 lock_workbook_states 可能新建的状态行，不能发生中途 commit。并发验收应补“两个不同项目互换 actor/owner”与“同账号同时写两个项目”，不是只测同项目争锁。

**验收。** 双管理员互改、bulk 与单改、模板保存与 sync、项目改派与已授权领域写并发；使用两个真实 PostgreSQL connection 和同步屏障证明没有循环等待、旧版本写被拒绝、最后管理员不丢失。先统一各旧入口锁序再开放新写服务，不能靠自动重试掩盖锁序错误。

## 阻断 3：整资源 override 的存在性不能由数值差推导

**代码事实。** `backend/app/api/role_templates.py:300` 的旧 sync 会删除与新模板值相等的 override；`accounts.py` 的 `reset_to_template` 恢复账号已保存的 template snapshot，而不是当前最新模板。旧 bulk 指纹只包含操作、模板 code、键和 after 权限图；sync 也未绑定所有账号 epoch/资源版本。现 JSONB `template_perms/perm_overrides` 是布尔稀疏差，不适合原样承载规范资源包。

**反例。** 用户整资源覆盖为 A，模板 v2 也恰好为 A；若同步时“压缩掉相等覆盖”，模板 v3 扩成 B 后默认 sync 会把用户升为 B，违反“保留整资源个人调整”。另一个反例是显式拒绝覆盖 `actions=[]` 被当作空/无 override，随后重新继承允许模板。

**最小修订。** 固定三种不同命令：`replace_resource(config)` 创建/更新完整 override；`revoke_resource` 写显式拒绝 override 与版本 tombstone；`clear_override` 只移除覆盖并恢复**账号保存的模板快照**，没有该资源则拒绝。追随最新模板必须走 `sync_template` 预览。相同值的显式覆盖仍保留来源存在性。模板移除资源时也需预览撤销，不能因为缺键就“保留上次允许”；保存模板不写账号。

模板/账号来源元数据可以采用独立 JSONB 包：模板 `authorization_bundle + authorization_version`，账号 profile `template_code/version + template_snapshot + resource_overrides`（覆盖 map 的键存在性有意义）。运行时依旧只读 `SysAuthorizationGrant`，来源包绝不参与 fallback allow；profile 与有效 Grant 在同一事务更新，并对有效结果做一致性断言。无需为此改动旧布尔列的 legacy 子集语义。

预览指纹至少绑定规范化命令、明确的目标稳定 ID 集合、目录版本、模板版本、目标 epoch、每条资源版本和覆盖存在性；模板行在 execute 时锁定并复验。目标集合必须与预览完全相同；预览之后新增模板成员不得静默加入。元数据来源改变但数值相等时，仍需推进能保护后续 preview CAS 的版本，不能只在 changed_keys 非空时更新版本。

## 阻断 4：项目关系撤权没有统一映射到 account epoch 或 project.version

**代码事实。** 项目改派只在同步销售名称实际变化时递增 `project.version`（assign 第 480 行）；单纯换负责人更新 assignment 并推进 workbook revision（531 行），archive 同样推进 workbook revision。它们不递增被撤权用户的 token_version。因此仅使用 `(principal, epoch, project_id, project.version)` 缓存完整工作簿能力仍可保持原键。契约已要求实时读关系，这一点正确，但 wire schema 不能把 project.version 解释成所有授权关系的版本。

**最小修订。** 对象能力使用领域提供的 `authorization_revision`，维保至少绑定覆盖该动作的 workbook revision/有效 assignment 状态；不能给所有域统一伪造一个 project.version。服务器每次业务请求仍重查关系，前端 revision 仅隔离缓存。任务 delegation 的 epoch 只是主体/Grant 快速失效条件，发布/提交时还必须重跑资源和对象关系决定；`epoch 未变` 不等于授权仍有效。多资源生成产物保存每个实际依赖的资源、对象集合、字段合同及关系版本；纯 chat/use 复验不能授权历史敏感文本重放。

**验收。** 同一账号 epoch 与项目主档 version 均不变时撤销 manager/viewer，随后工具、旧对象 capability、排队导出、原件下载和历史结果重放均不得沿用旧允许；对已经发送字节的边界维持 D-0027 已声明限制。

## 通用核心的两项实施准入条件

1. **类型化身份必须连到可信来源，Access 必须带资源标签。** 当前 `UserContext` 是可构造的可变 DTO，`SalesAccess` 内仍携带它；`security.is_scoped_maintenance` 还会按 admin/boss 直接关闭范围，`_hidden_fields` 按全局同名字段映射。S1 不应仅把 UserContext 改名为 Principal。建议 HTTP 只有认证适配器能从已验签凭据解析稳定 user ID，任务只能从服务器保存的 delegation ID 重载；通用入口拒绝 UserContext/dict。不可变 `ResourceAccess` 至少携带 `subject_id/epoch/resource/action/grant_version/catalog_version/predicate/field_plan/relation_revision`，领域适配器核对 resource/action 后才使用字段和 SQL；不能只把 context 裸传给下一个域。Python 类型不是防御任意恶意进程内代码的安全沙箱，验签与数据库重查才是外部身份边界。D-03 同对象可以选择一条完整合法合同，但不能取 managed 的字段与 general 的范围；field plan 也不能靠 `explicit_field_policy=True` 自动获得跨域可信度。
2. **批量 PDP 与可配置目录要有真实双资源测试。** 当前 engine 的 principal 只有一个 `attr.grant`，不是批量 map；`_check` 还校验 scope/version/字段子集。批量实现必须保持每个 response 对应唯一 resource ID、动作集合和 Grant 版本，未知/重复/缺失/错资源结果不能继续允许；`/me` 不调用每域 PlanResources 或加载全项目集合。以 1/多资源真实 policy fixture 比较 SQL/PDP 往返数，并给 A/B 不同 scope/fields 的交叉输出毒针。当前 grant 表只允许 `all/assigned_customers`：扩范围要随执行定义和政策迁移，而不是让所有设计资源立即可写。前端草案中的 `editable_staged` 必须独立于运行 Grant，S1 如不实现 staging storage 就不返回该状态，避免“待启用”记录被运行时误读成有效允许。

## 最小可执行接口与所有权建议

下列名称用于冻结实施接缝，不要求增加第二套 HTTP 路由。

| 接缝 | 最小输入/结果 | 建议所有权 |
|---|---|---|
| principal resolver / resource access | verified identity 或服务器 delegation；输出带资源标签的不可变 Access；无外部 role/perms 参数 | 核心代理独占 `backend/app/authz/*`、认证适配与政策通用协议；领域代理只提交 bindings/关系 provider |
| canonical account command | 显式 operation、稳定 targets、expected epoch、资源完整配置与 expected version；关系字段 omitted/empty 区分；返回新 epoch/版本/session_revoked | 同一控制面代理独占 `backend/app/api/accounts.py`、`role_templates.py`、`authorization.py` 与规范写服务；禁止另一个代理各自加写 helper |
| template command / batch preview-execute | metadata 与模块包原子保存；sync 与保存是两个明确事务；preview fingerprint 包含完整目标/版本/意图 | 控制面同一所有者；数据模型与 Alembic 由根指定单一所有者，数据库新增列必须与 DTO 同次评审 |
| account/template source profile | 模板 snapshot、整资源 override 存在性、source revision；只作编排元数据 | 控制面负责；运行授权读取不得依赖该表回退 |
| object capabilities / delegation checkpoint | 同一 resource/action/object 的当前关系决定；authorization_revision 由领域定义 | 维保等领域代理拥有各自 service/对象 DTO；核心不主动获取所有域关系 |
| 前端 editor / Provider / legacy forms | 同一 schema_version、状态枚举、模块包、server preview；诊断目标不能注入操作者快照 | 一个前端代理独占 API adapters、authz Provider、accounts 页面与导航公共接缝 |

S1 可以先交付 sales live + 其余只读 descriptor 的通用控制面，但必须同时封闭涉及 sales 的旧账号/模板/关系写入口。账号批量必须仍是一次服务器事务，不能由前端循环多个 PUT 代替。三份入口草案仍是设计账本：`parameter_branches={}`、named management actor 或 checkpoint 标签不是实现/覆盖证据。以上每项需在真实 HTTP/数据库/PDP 反例通过后才能标记已闭合；本报告不替代精确 SHA 全量 CI 与跨模型复审。
