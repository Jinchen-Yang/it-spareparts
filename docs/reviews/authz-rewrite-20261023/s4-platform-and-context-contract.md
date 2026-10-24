# S4 平台边界与公共上下文合同

状态：S3 精确提交 `2cb6fa28109f0ea91858f3ad5b6430f037f239a0` 完整 CI 运行期间的只读设计。本文不是实施或验收证据。S3 全部检查通过后才进入 S4 应用与测试。根代理负责本包；共享业务目录、seed、模型和单一迁移由 backend owner 落地，前端由 frontend owner 落地。

## 一、普通资料权限与授权管理职责

S1 已统一授权写入事务，但 `actor_capabilities.read_accounts/edit_account_profiles` 仍取旧布尔图。这两处必须迁走；把目录标签改成已完成不能消除旧权威。新的普通模块为：

| 资源 | 动作 | scope / 字段 | 迁移依据 |
| --- | --- | --- | --- |
| `account_directory` | `read`, `read_activity`, `show_list`, `show_activity` | `all` / 无字段开关 | 冻结旧实际 `runtime_safe(effective).page_accounts` |
| `account_profile` | `update`, `show_edit` | `all` / 无字段开关 | 冻结旧实际 `page_accounts AND action_account_manage` |

显示动作不授数据；`read_activity` 独立，不被 `read` 或管理员身份推导。`update` 仅处理 display_name，沿用现有针对管理员账号和安全属性的限制。角色资格、salesperson_name、模块包、客户关系、模板、口令和生命周期仍是 S1 的实名授权管理员命令，不下放给资料管理权。对旧来源、显式空覆盖、canonical none/复制对象、inactive/deleted 的 seed 规则沿用 S2/S3；不会以角色名代替迁移资格。管理员同样需要普通资料 Grant，不能用 admin OR read 恢复整页绕过。

为避免普通目录撤权锁死授权管理员的恢复入口，增加专用 `GET /api/authz/accounts`，只允许当前有效实名管理员。响应为既有 envelope 加 `items`，每项只含真实 `id, username, display_name, role, salesperson_name, is_active, authorization_epoch, authorization_summary, authorization_summary_status`；不返回密码、旧权限 blob、最近登录或活动。summary 是既定摘要对象或 null，status 是固定 `available` / `invalid`；单个损坏 profile 仍返回真实定位字段，不能猜空授权或使整个目标目录失败，具体模块读取仍报原 409。username 是定位键，授权仍使用实时稳定身份。权限中心以此发现授权目标，不能为了赋权自动调用普通目录、活动接口。模板管理的读取同样走实名管理边界，不能继承普通目录读门。

普通 `/accounts` 返回明确的 canonical 目录 DTO，`/{username}/activity` 使用独立动作并只读取允许的活动投影；审计 before/after 中可能包含授权来源及历史敏感字段，不能把完整 JSON 递归搬出当普通活动。目录/活动读取在一致短事务内完成授权与物化；display_name 更新在实际目标锁集合完成后重新验当前 Grant。snapshot 中两项 actor capability 由同一批当前 PDP 结果派生；安全管理能力保留既定实名管理条件。不能从纯 principal 函数递归服务，也不增加另一份权限缓存。

### 只读对抗审查后的明确边界

- 普通 `account_directory` / `account_profile` 各自 runtime 为 legacy 且无切换 marker 时，保持实时旧资料能力；enabled 仅依当前 Grant/PDP，suspended 或未知状态均拒绝。全局 `canonical_management_required` 决定控制 DTO 和禁旧写，不能被当成普通资料的资源切换标志。`principal` 的纯函数只接收调用方明确选定的结果，不能自行猜运行模式；`/me` 复用已计算的同批 PDP 决策。
- App 路由、PermissionCenterPage 和实际 loader 三层都要识别真实管理 capability。普通目录 Grant 被撤销时，named-admin 管理 shell 仍使用 `/authz/accounts`；不再无条件请求 `/accounts`。PDP 整体故障仍令 `/me` 503、受控 UI 暂不可用，named-admin 控制 API 保持不依赖 PDP。此轮不增加另一份权限缓存或故障专用 UI，不能声称 PDP 故障时界面恢复可用。
- `/accounts/_meta` 的旧布尔矩阵在 canonical management 分支明确 410；只有真正旧管理模式保留原接口。前端不能因配置撤回而重新选择 LegacyPermissionCenterPage。canonical `/role-templates` 使用独立管理边界和关闭的 DTO，不依普通目录门。
- 普通目录不包含 last_login_at；活动才可读取。活动 SELECT 只取固定允许列：recent 为 action/ip/at，resource 固定 null（该旧列可能是型号/客户原文）；changes 为 action/by/at，reason/before/after 固定 null。自由文本、detail/user_agent 与 before_json/after_json 均不查询；last_login_at、total_actions 仅在独立 read_activity 通过后读取。最小目标发现也不读取密码或整份旧权限。
- 仅获 activity 或 profile 动作的用户可通过 `/accounts?username=...` 或明确用户名输入定位；不为补元数据请求整个目录。show_activity + read_activity、show_edit + update 各自控制入口。profile-only 命令沿 S1 合同无 epoch/CAS，不推进授权 epoch；不得把 UI 当前依赖账号行误当成协议必须读目录。共享 PUT 路径按真实 body 的 profile/security_change 分别登记普通 Grant 与安全管理边界。

## 二、平台能力也是显式边界，但不是员工业务 Grant

以下原 planned 条目移出普通模块数组，登记为独立、封闭、可验证的 `boundaries`，由 `catalog_snapshot` 一并公开其用途。模块目录与边界目录 ID 必须互斥，目录版本包含边界合同摘要。任何员工 Grant、模板包、启用资源配置都不能将这些 ID 变为普通模块。前端不新增 migration_state 枚举，不把平台条件伪装为等待开发的业务权限。

| 边界 | 权威与用途 |
| --- | --- |
| `account_lifecycle` | 实时实名管理员；建号、启停、改密、删除及安全属性命令；最后管理员/自锁/CAS 等 S1 保护保持 |
| `legacy_template` | 实时实名管理员；现行规范模板 CRUD、成员、应用/同步，名字保留以说明旧候选的归宿 |
| `authorization_admin` | 实时实名管理员；目录、目标发现、目标诊断、模块/客户归属、原子批量管理 |
| `authentication` | bootstrap 口令验证、本人改密与登录功能快照；不得由一个业务 Grant 使人无法登录或更改本人密码 |
| `authorization_snapshot` | 只能读取当前验签主体；旧销售专用写入口明确退役，不存在 self.replace 赋权 |
| `health_probe` | 有界 public 探活，无业务记录或内部异常细节 |
| `framework_documentation` | 明确的 API schema/docs 平台例外；只说明结构，不授权调用业务入口 |
| `resale_return_feed` | 独立返件服务凭据、固定只读合同、逐动作配置 |
| `resale_catalog` | 独立 PN 服务凭据、resolve/snapshots/list/search/price_evidence 的闭集，只读 POST 不误标 mutation |
| `resale_price_reference` | 同 PN 服务身份、独立 read 动作，可单独停用；不借员工成本/利润 Grant |

边界登记需要对应实际 method/path、主体类型、动作、执行适配器、输出/副作用限制及测试引用；新增未登记路由在全量覆盖卡口失败。不能将 metadata 本身作为 allow。公开边界和认证边界分别保持自己的验证，不统一为“无需登录即允许”的宽泛规则。

## 三、机器接口的最小实施

保持两把既有 SHA-256 配置凭据互不通用。服务身份验证抽到唯一 typed adapter，逐入口绑定固定 service/resource/action；客户端不能传入资源或动作。新增显式 allowed-action 配置，默认列表只列现有合同，新动作默认不进入；空列表表示全部暂停，未知/重复项或跨服务动作配置失败关闭。合法凭据但动作已撤销在任何业务 SQL 前拒绝；普通 JWT、共享口令、另一把服务凭据都不能替代。

保留既有数据契约与业务口径，PN 价格证据 SQL 改为只选合同允许列，不能把完整销售/采购 ORM（客户、供应商、员工、利润等）读入 Python 再输出白名单。返件源范围、数量/游标、价格时间窗口、序列号/歧义和错误语义不改变。此处是固定服务合同的模块开关，不新建服务账号管理 UI，也不把机器身份伪装成 SysUser。配置变更沿现有部署配置生命周期生效。

## 四、公共上下文底座抽取

`maintenance_authorization` 中的 typed 调度、selection 校验、文档/历史来源复验、读快照与最终用户锁属于跨域公共能力。提取至 `app/authz/domain_contexts.py`，保留一个 registry；maintenance 模块兼容 re-export 相同函数和类型。项目 evidence、SQL 关系表、project revision 留在维修领域。

当前未注册 entry 的 project/collection 默认回落必须改为精确 S3 entry 的显式注册。不能因为 S4 对象同样叫 collection 就读取维修关系、得到伪 revision 或隐式许可。entry/reference/document/query 注册均拒绝冲突覆盖；仅相同 callable 的幂等登记允许。historical ref 仍必须重解真实对象，并严格匹配 entry/branch/动作，operation_input 不能充作持久权限。所有 S3 调用继续指向同一 registry；`provenance` 使用新的公共路径。

ReadSnapshotProof 的 session、活动事务、RR READ ONLY 约束保持；完整领域锁先于最终用户锁，不能在 LLM/网络/慢客户端下载中持锁。未注册 resolver、未知 schema/branch、跨 session proof、错误 carrier 一律失败关闭。抽取不扩展业务对象权限，也不改变已有项目关系语义。

## 五、独立反例与完成条件

1. 普通目录、活动、资料更新独立；管理员撤普通 Grant 后同样被拒，但仍能经最小目标发现管理授权；非管理员不能用 profile.update 改安全属性或调用目标发现。
2. 旧布尔全 true、新 Grant 空/撤销、持久切换后撤配置、PDP 不可用，均不能恢复上述普通模块访问。快照不按模块重复 N 次用户/PDP 查询。
3. 授权前 SQL 不选活动正文/审计 JSON/完整权限列；撤权与资料保存并发在最终锁后拒绝并零写。
4. 服务凭据互换、普通 JWT、空动作、未知动作、停用单一价格动作；失败时业务 SQL 为零。有效请求保持原有 DTO、计算口径和分页，价格证据不物化无关敏感列。
5. S3 实际 ref 经新公共 registry 正常复验；伪 S4 collection/project 没有 resolver 时拒绝；重复 reference 注册拒绝；跨 session 快照拒绝。
6. 完整 HTTP/工具/worker/文件/前端 surface 覆盖账闭合，superseded aliases 明确 retired（`data_import`, `maintenance_source_document`, `maintenance_ai_assist`），无 unexplained planned/pending。全量测试、精确 CI 与跨模型复核通过后才做统一验收及可合并比较；不合并、不生产。
