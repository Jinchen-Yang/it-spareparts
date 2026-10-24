# S4 平台与控制面实施前独立审查

审查基线：`c0396fc08f6d5084358ebf352ffedad717980274`。对象为 [平台合同](s4-platform-and-context-contract.md)、当前真实 API/管理原语、S1 测试和前端调用。这里只做源码与合同审查，没有新增或运行测试，也没有修改应用；S3 修订 CI 仍是下一阶段开工门。

结论：两项普通 Grant 与实名管理员控制边界的拆分可行。必须把以下兼容矩阵、输出白名单和恢复入口接缝落实后，才可宣称旧布尔真值已迁出。不能仅替换 `_read_gate` 或把原路由整体登记成 named-admin。

## 1. 已与根代理冻结的逐资源兼容矩阵

管理协议与业务授权分别判定。`canonical_management_required()` 自 S1 起即可为真，并不意味着 `account_directory`、`account_profile` 已切换。`principal` 纯函数不查 DB、不猜模式；调用层提供经过 runtime 确认的每资源状态。

| 本次能力 | never-cutover / runtime=legacy | enabled | 已有 marker，suspended 或撤回模式/配置 |
| --- | --- | --- | --- |
| 目录读、活动读 | 当前实名账号有效旧 `page_accounts`；真正旧协议分支仍保留既有旧门 | 分别检查 `account_directory.read`、`read_activity` | 业务 API 503；快照 deny；禁止旧布尔或 admin 补 allow |
| display_name 修改 | 当前实名账号旧 `page_accounts AND action_account_manage` | 仅 `account_profile.update`，再执行目标保护 | 同上；资料与审计零写 |
| 授权、模板、安全属性、生命周期 | 按 S1 管理协议是否已永久切换分派；canonical 分支始终实名管理员 | 同一管理边界，与两项普通 Grant 无关 | 管理边界不因普通模块暂停而变回旧布尔写 |
| 账号 capability 快照 | 仅尚未切换的对应资源可由旧有效图产生该能力 | 从 `/me` 同批 PDP 对应动作结果派生 | 对应普通 capability deny，不继承角色 |

`read_accounts` 只映射目录 `read`，不把 `read_activity`、`show_*` 或任意控制能力 OR 成普通目录读。`edit_account_profiles` 只映射 `update`。`show_*` 与数据能力仍分别消费。一个资源启用、另一个仍 legacy 的混合状态必须可独立工作。

实际回归依据：`test_authorization_management.py` 的 `client` 只启用并暂停销售，`test_profile_security_lifecycle_and_reset_password` 仍要求实名管理员无 account Grant 时可保存显示名，且 epoch 不变。若按全局统一模式一刀切要求两项新 Grant，会破坏这一既有阶段合同。`principal.actor_capabilities` 当前旧能力来自实时 `runtime_safe(effective_for_user(user))`，不是 JWT 中历史布尔。

迁移仍按冻结来源资格补种：eligible=true、未删除、没有已有 Grant/覆盖/保存快照键；inactive 保留 entitlement；canonical none/复制对象不补种；显式空拒绝不覆盖。目录四动作由有效 `page_accounts`，资料两动作由有效两键交集生成。admin 的旧常规 effective 全开只属于历史 seed/never-cutover 语义，不能成为 enabled 的运行捷径。全局 RBAC 关闭也不转换成“为所有实名账号补全新 Grant”。

## 2. 恢复入口与旧元数据退役

根代理已冻结恢复可用性边界：PDP 正常而普通 Grant 空或撤销时，真实 control capability 仍能进入权限中心，经 `GET /api/authz/accounts` 找到目标；普通 `/accounts` 不因此放行。PDP 整体故障时 `/me` 仍整体 503，前端可明确显示不可用；实名管理 API 不依赖 PDP，可直接用于恢复。本轮不新增第二个控制快照、缓存或故障专用 UI，不能声称 PDP 故障时图形界面也可恢复。

真实前端仍有两个必须改的调用点：`App.tsx` 的 accounts 导航及 `PermissionCenterPage.tsx:69` 都要求 `read_accounts`；`UnifiedPermissionCenter.tsx` 无条件请求 `/accounts`。管理入口应由服务器 control capability 进入并改用 `/authz/accounts`，不得从本地 role 推 allow。普通目录面仍使用自己的 show/read。当前统一分支没有 `_meta` 依赖；唯一调用位于 `LegacyPermissionCenterPage`。

接受 `/accounts/_meta` 在 `canonical_management_required=true` 时固定 410（typed error、no-store、不读模板权限），真正 never-canonical 旧协议保持原响应。已有 marker 后即使 mode 改 legacy，也不能重新开放该旧矩阵或旧写接口。`/role-templates` canonical 读取必须移除普通目录 `_read_gate` 依赖，改为实名管理员与闭合 DTO；legacy 分支保留既有委派管理测试口径。

**根代理已采纳的恢复细节：** 新 finder 与普通目录使用相同窄账号 shape，不能直接复用 `management.account_summaries()` 的全表失败语义。该函数对每个 profile 调严格校验，任一损坏来源就抛 409，其他目标也消失。两列表每行固定 `authorization_summary` 为现有对象或 null，新增 `authorization_summary_status: available|invalid`；坏来源保留真实定位元数据并标 invalid，详情仍明确 409，不把坏来源猜成空 Grant。包含停用但未删除账号，以便重新启用；稳定 id 与 username 配对，显示名不是身份。该规则不授权自动修坏数据。

## 3. 普通目录、活动和修改回执的精确隔离

当前 `accounts._view()` 除身份资料外返回 `last_login_at`、旧权限图、模板权限与来源；`activity()` 则返回整个 SysAuditLog 的 before/after。如果仅换 action 门，新独立动作仍不成立。

| 合同 | 实施必须固定的投影 |
| --- | --- |
| `account_directory.read` | 根代理已冻结与 finder 同形的 canonical 窄目录。不得附送 last_login_at/活动计数/最近事件，也不得复用 `_view` 全部旧权限 blob；字段存在与否不能由 admin 身份补齐。 |
| `read_activity` | 独立准入，可按已知 username 读取，不要求 `read`。只 SELECT 明确活动列；不加载 `SysAccessLog.detail/user_agent` 或完整审计 JSON 后再删。根代理已冻结 before/after 不读取、不返回；自由文本 reason 仍须采用固定安全投影或省略，不能透传规范改权中的客户归属、授权包等内容。 |
| `account_profile.update` | 仅 display_name（包括显式 null），保持 S1 固定 subject 回执；不借此返回权限 blob、活动或模板全包。无 directory.read 不阻止已知 username 的直接 API 更新。 |

活动 `recent.resource` 是历史任意字符串，模型注释为“型号/客户/维度”；旧 `test_accounts.test_activity_logged` 明确要求 PN 文本。实施前须明确这是被 `read_activity` 授予的有限活动标识，还是改为安全分类标签并记录收紧差分；不能泛称“metadata”就假定不会包含业务名称。至少保留登录/事件时间、动作及准确计数的既有用途，不用 0 伪造被隐藏事件数。目录不附送这些能力。

普通页面目前没有 activity-only / update-only 的 username 定位壳；根代理已冻结补 `/accounts?username=...` 或准确 username 输入壳，分别由 `show_activity + read_activity`、`show_edit + update` 进入，两个 surface 标为 route_entry=true。不自动发现账号，不反向给数据动作添加 read 硬依赖。profile-only S1 请求没有 expected_epoch，不应为了复用前端表格伪造目标 epoch 或宣称新 CAS；不需要新增目标元数据 API。

## 4. 写入锁序：禁止把新的读授权锁带进最终账号锁

当前 `management.update_account()` 先无锁 actor 检查，再按稳定 id 一次锁 actor+target，锁后重验；`lock_users()` 使用 `populate_existing=True`。核心 `resource_access()` 若没有 snapshot/locked_user，则 `_read_principal()` 会取得 actor FOR SHARE。

因此不能在原事务开始直接加一次普通 `resource_access(profile.update)` 再调用 `lock_users()`：两个账号互改会各持自身 SHARE，随后按序升级/等待另一账号 UPDATE，重引入 S1 已消除的死锁。

推荐实施顺序：

1. 独立短 RR/READ ONLY 预检真实 actor 与相应 Grant，结束该事务；无权者在目标资料/业务保护校验前拒绝。
2. 原写事务按既有顺序处理 roster/template 等必要前置锁，再稳定 id 收齐 actor+target 最终用户锁，禁止先 SHARE 后升级。普通 display_name 不需要新增 roster 或模板锁。
3. 用已锁 actor 重验 live identity、目标 current state 与精确 `profile.update`；读取当前 Grant/PDP，不重复取 actor 锁，不借预检结果。缺 Grant 403，失效 token 401，PDP/暂停 503；任何失败目标与 audit 都零写。
4. 保存显示名和 audit 一次提交；epoch、Grant version、template/source 不改变。目标在等待期间被提升 admin 时，普通操作者仍被现有目标保护拒绝。

保留内置 admin 除改密不可变、非 admin 不可修改管理员、最后管理员/不能停用降级自己、混合安全字段整体拒绝、空值毒针也拒绝的既有规则。`PUT /accounts/{username}` 是真实双合同入口：profile body 属普通 Grant，security_change body 属实名管理员；边界 registry 需要固定 body 分支，不能把整条 method/path 标成管理员或 ordinary update。`profile + security_change`、`permissions:{}`、`role:null` 等不能靠字段 falsy 分派出部分成功。

## 5. 边界目录不得成为可赋权的普通资源

ID 互斥和 catalog digest 方向正确，但以下通道都要验证：单账号 replace/clear、bulk、建号 explicit/template、模板 create/copy/update/sync、保存快照与个人覆盖（包括 actions=[]）、启动 enabled/suspended 配置。boundary ID 不能因空动作包被当成有效拒绝资源存入，不能生成 Cerbos 业务策略。

直接损坏 DB 的 boundary Grant 也不能触发 allow；对正常业务操作应按现有无效配置/来源诊断失败关闭。边界 metadata 只描述既有执行适配器，目录返回 `authority=named_admin/service/public` 不是实际请求的允许证据。实际 method/path 要按分支归属检查，不能因为与边界同路径而遗漏普通资料入口。

目录版本须同时包含模块定义、boundary descriptor 与 exact boundary entry 合同。修改边界动作/入口后旧 catalog_version 管理命令 409；重新生成类型不能接受 unknown boundary、未知 action 或两目录重复 ID。机器 action 白名单及 framework/public 例外仍各自验证；不能从“不是员工 Grant”推出跳过身份验证。

## 6. S4 应增加的反例与必须保留的旧测试

| 反例组 | 验收要求 / 当前可复用依据 |
| --- | --- |
| 逐资源状态 | unified 但两资源无 marker；只目录启用/只资料启用；已有 marker 后 mode 与配置撤回；旧 bool 全 true、Grant 空；admin 无 Grant。与 S1 `test_profile_security_lifecycle_and_reset_password` 同时保留。 |
| 独立动作 | read-only 不读活动 SQL；activity-only 已知目标可读；update-only 已知普通目标可写但不能改安全属性或查目录；show-only 不产生数据许可。 |
| 控制恢复 | admin 普通两 Grant 空，/accounts 与 activity 拒绝，/authz/accounts、模板与规范管理仍可用；普通 profile 用户、共享 fallback、陈旧/停用/删除实名均不能用 finder。PDP 断开普通接口/me 503、控制 API 不调用 PDP。 |
| 投影毒针 | 将客户归属、密码样式秘密、嵌套模块包放入 audit JSON/detail，普通活动既不 SELECT 全 JSON 也不输出；目录缺活动信息；坏来源账号不让 finder 全列表消失。 |
| 并发 | A/B 两个普通 profile 操作者互改不死锁；预检后撤 actor epoch/Grant、target 被提升 admin、锁等待后变化均拒绝且零写。复用 `test_two_admins_cross_edit_without_lock_upgrade_deadlock`、`test_actor_is_revalidated_under_final_lock` 的真实两连接模式，不只 mock 最终判断。 |
| 管理保护 | `test_mixed_profile_legacy_fields_reject_whole_command`、`test_ordinary_account_cannot_reset_credentials_via_legacy_management_gate`、最后管理员、archive/sync 模板先于用户锁、自助改密与改权竞争等保持，不因 S4 普通 Grant 放宽。 |
| 历史兼容 | `test_accounts.py`、`test_permission_center.py` 在 genuine legacy 状态保留委派/模板/活动旧口径；S4 canonical 收紧另测，不能全局重写旧期待为拒绝。`_meta` canonical410 与 legacy 原 exact DTO 各测。 |
| boundary 注入 | 上述每个规范写通道注入 boundary ID（含空动作）整体零写；启用配置拒绝；普通 GET 目录与 boundary DTO shape 严格闭合。 |

此清单是实施要求，不是已通过的运行证据。PDP 故障时 UI 恢复被明确排除；控制 API 可恢复也不表示允许修改可信运行配置或绕过无效 catalog。
