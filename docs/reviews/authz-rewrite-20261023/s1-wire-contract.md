# S1 控制面 wire contract

状态：实施前冻结的协议真值；45 个 DTO 为协议样例，不能当作真实请求证据。S0 最终 `16c38c3` CI 通过后，S1 已按此实现并完成本地验证，阶段精确提交 CI 与最终结果见执行账本和实施审查报告。本文细化 [总契约](../../plans/2026-10-23-authz-full-migration-contract.md)，闭合 [S1 独立审查](s1-contract-independent-review.md) 的控制面问题。后端协作签名见 [共享接口接缝](s1-shared-interfaces.md)。

## 1. 唯一状态、类型和版本

| 字段 | wire 类型 | 含义及约束 |
| --- | --- | --- |
| `schema_version` | integer，固定 `1` | 本稿新协议版本；不支持的版本整体拒绝，不能尝试宽松转换 |
| `authorization_mode` | `legacy_compat` / `unified_modules` | 服务器控制面模式；客户端不得用请求参数切换模式。统一控制面不表示所有业务域已迁完 |
| `catalog_version` | opaque string | 本次完整目录/依赖合同版本，客户端只判相等；请求缺失或旧版本不能写 |
| `principal.id` / `subject.id` / `user_id` / `customer_ids[]` | positive safe integer | 稳定数据库主键，继续沿用现有 numeric ID；拒绝浮点、布尔、字符串和超过 `Number.MAX_SAFE_INTEGER` 的数字，不舍入 |
| `authorization_epoch` / `expected_epoch` | nonnegative safe integer | 直接对应 `SysUser.token_version`；源元数据、Grant、客户关系、范围身份、角色/凭据/有效性变化同事务推进，并撤销旧会话 |
| resource `version` / `expected_version` | nonnegative safe integer | `0` 仅表示该账号从未有此资源 Grant 行；首写为 1。撤销保持行并递增，禁止删行重建产生 ABA |
| template `version` / `expected_template_version` | positive safe integer | 复用 `SysRoleTemplate.version`，保护 metadata 和完整规范包；不另设 authorization/source version |
| `version`（仅 `/me` 顶层） | opaque string | 兼容现前端的整份能力快照标记；包含实际主体、epoch、目录/政策/运行配置版本，不作数值大小比较 |

统一模式的新请求对象使用 `extra=forbid` 语义；数组不得重复；资源、action、scope、字段 ID 必须是目录闭合集合。动作/字段字符串不做大小写纠正或旧 key 猜测。响应数组按目录固定顺序，规范 Grant 内动作/字段按 ID 排序；对象 key 顺序不承载语义。

`ResourceConfiguration = {actions: string[], scope: string, field_permissions: string[]}` 是**不可拆分**的完整资源配置。`actions=[]` 是显式拒绝，仍是配置对象；`null` 只表示尚无配置，不能与空对象、空数组互换。字段列表只在该资源及该范围内有意义。依赖不合法返回错误，客户端不自动补 read、成本字段或更大范围。

### 资源状态组合

下表的 configurable 列针对 unified_modules；legacy_compat 下管理配置统一为 false。

| `migration_state` | `runtime_state` | `configurable` | 新协议行为 |
| --- | --- | --- | --- |
| `planned` | `legacy` | false | 目录只读描述；不能保存 canonical Grant，`/me` 新决定均 deny；尚未迁移的业务界面仍按其既有 legacy 入口工作 |
| `implemented` | `legacy` | false | 已具备执行代码但尚未切换；S1 不提供 staged grant 编辑/存储 |
| `implemented` | `enabled` | true | 可配置规范 Grant；实际允许由当前主体、Grant、PDP、领域关系决定 |
| `implemented` | `suspended` | true | 管理员可修正 Grant；所有业务决定 deny，不能恢复旧 role/perms 放行 |
| `retired` | `legacy` | false | 只保留退役账本，永不生成允许；既有 410 行为保留 |

以上为 S1 闭合集合；其他组合是协议错误。`configurable` 为服务器最终结果，前端不从枚举自行制造可写状态。`legacy_compat` 下通用目录和读取可用，通用写返回 `409 AUTHZ_MODE_NOT_UNIFIED`；只对尚未切换的 legacy 子集保留原协议，不重新打开已经退役的 Grant 写旁路。切换后已迁资源无旧权限回退。S1 只有 `sales_analysis` 为可执行资源，其余人工登记描述不因出现在目录就成为可授予能力。

`authorization_mode` 仅选择控制面协议，不能改变资源运行授权权威。每个可切换资源有持久 `SysAuthorizationCutover` 标记，不能以“存在 Grant”推断已切换。可信部署配置明确请求启用时，由服务启动在**独立短事务**幂等记录 `resource/activated_catalog_version/activated_at`；先完成配置/执行目录校验并提交激活，再进入 ready。失败不 ready；配置要求启用但没有标记时，普通请求失败关闭，不偷偷写标记或走 legacy。

已存在标记且配置改回 legacy/false（包括 control plane 改成 legacy_compat）时，资源必须 suspended 或拒绝启动；不能回旧允许。没有标记、配置未请求启用时才可保留 legacy。普通请求批量读取标记或使用有跨实例失效证明的缓存，不永久缓存启动时“无标记”；另一实例启动激活后，本实例也不得继续旧允许。没有新业务 HTTP activation 端点。旧销售开关与新统一资源配置由根的单一运行态解析器解释，前后端只消费上述最终状态；冲突配置在启动前拒绝，不由各调用者自行选优先级。具体部署变量名归核心实现，wire 不以纯环境变量声称具有历史不可回退保证。

S1 migration 建立 canonical profile 后，旧 S0 `PUT /authz/users/{username}` 永久返回 `409 AUTHZ_LEGACY_ENDPOINT_RETIRED`，包括 legacy_compat；该端点不能直接改 Grant 而绕过来源 profile。已 cutover 资源的任何旧布尔写旁路跨 mode 都必须拒绝，suspended 时也一样。若仍保留未切换旧子集的账号写，必须分流到同一管理编排并维护 canonical 来源一致性；混合载荷整体拒绝。mode 不能成为恢复旧写实现的开关。

## 2. 读取协议

所有**新成功响应**携带 `{schema_version, authorization_mode, catalog_version}`；错误只使用第 7 节的 `detail` 形状，不提供可用于放行的能力 envelope。成功、失败均 `Cache-Control: no-store`。`GET /api/authz/me` 只能读当前已验签主体；目录、他人 modules、模板 modules 需要服务器实时实名管理员。账号资料管理权不自动包含新授权管理/模板管理/他人改密。

### `GET /api/authz/me`

字段：`principal: {id, username, display_name}|null`、`authorization_epoch: number|null`、`actor_capabilities`、`resources`，以及 S0 兼容字段 `version/username/can_manage/sales_analysis`。

- `actor_capabilities` 是服务器产生的具名决定 map：`read_accounts`、`edit_account_profiles`、`manage_authorization`、`manage_templates`、`create_accounts`、`reset_account_passwords`、`manage_account_lifecycle`。决定统一为 `{effect: allow|deny|requires_context, reason: {code,message}|null}`。准入只在服务器判定，前端不根据 role 补 allow。`requires_context` 不启用按钮。
- `resources[resource_id] = {migration_state,runtime_state,configurable,grant_version,decisions,scope_kind,field_permissions}`。`decisions` 包含该目录资源每个已登记 action；未知/缺 action 按 deny。无 Grant 用 `grant_version=0, scope_kind="none", field_permissions=[]`；拒绝/暂停时也不返回可用字段集合。
- `scope_kind` 只是类别，如 `assigned_customers`；不含 SQL、其他人的客户/项目名单。不为了 `/me` 逐域加载所有项目 ID 或执行查询计划。全局 scope/fields 摘要不能作为任意对象的字段许可。
- 有合法 Grant 但负责客户为空：读取能力可以 allow，查询范围仍为空；可公开 reason `EMPTY_ASSIGNED_SCOPE`。不能把空集合转 all。
- 失效/停用/删除实名会话返回 401；合法共享回退身份可以收到 `principal=null, authorization_epoch=null` 的拒绝快照，不能获得新允许。PDP 故障返回 503，不返回部分 allow，也不回退旧授权。
- S0 `sales_analysis{enabled,read,export,scope,customer_count,reason,field_permissions}` 由**同一**权威结果派生；suspended 时 `enabled=true` 且 read/export=false，阻止旧前端回退。unified 前端只用 `resources.sales_analysis`，旧顶层不是另一份授权真值。缺新 schema 不允许静默兼容。
- 会话绑定以请求捕获 token、请求序号和当前会话为准；目标账号 modules 绝不能进入当前操作者 Provider。

### `GET /api/authz/catalog`

返回 `modules: ResourceDescriptor[]`。每个 descriptor 含 `id,label,migration_state,runtime_state,configurable,actions,scopes,denial_scope,field_permissions,surfaces,legacy_sources,notice`：

- action：`{id,label,kind:data|display|mutation,requires_actions:string[],requires_fields:string[]}`。两项依赖数组必填、不得重复，引用必须属于本资源已登记动作/字段闭合集合；未知引用使目录无效，不能忽略。配置中选择该动作必须同时满足全部动作依赖和字段依赖，前后端同规则校验。`ModuleGrantEditor`解释缺失项并禁止提交，不自动增加字段。S0/S1 的 `read/export` 均为 `requires_fields=[]`；S2 的直方图成本依赖、池政策治理价格依赖只属于 planned 描述，不因此提前成为可配置动作。
- scope：`{id,label,relationship_kind:string|null,requires_context:boolean}`。
- `denial_scope`：执行目录指定的合法拒绝 scope；sales 固定 `assigned_customers`，planned/retired 可为 null。没有保存快照时的 clear/revoke 使用 `{actions:[],scope:denial_scope,field_permissions:[]}`，不从旧角色或 scope 数组顺序猜测。
- field：`{id,label,requires_fields:string[]}`。
- surface：`{id,label,display_actions:string[],data_requirements:[{resource_id,actions:string[]}]}`。pure display 不代替真实数据动作；读同一信息的其他面板也要相同数据资格。
- `legacy_sources` 只作迁移解释，不是自动映射规则。planned descriptor 的 ID 不能被当作可执行定义。示例 fixture 是三资源最小测试目录，不冒充全站 87 资源实现。

客户搜索继续复用 `GET /api/authz/customers?q=...&limit=...`，保持 `{items:[{id:number,name:string,raw_name:string}]}`，不改成字符串 ID，也不增加客户关系复制端点。

### `GET /api/authz/users/{username}/modules`

返回 envelope + `subject:{id,username,display_name,role,salesperson_name,is_active}`、`authorization_epoch`、`template:{code,version,current_version,stale}|null`、`customer_assignments:{ids,customers}`、`resources`。

每个 `resources[id]`：

```json
{
  "version": 3,
  "configuration": {"actions":["read"],"scope":"assigned_customers","field_permissions":[]},
  "source": "personal_override",
  "template_snapshot": {"code":"sales_team","version":2,"configuration":{"actions":["read"],"scope":"assigned_customers","field_permissions":[]}},
  "personal_override": {"present":true,"configuration":{"actions":["read"],"scope":"assigned_customers","field_permissions":[]}}
}
```

`source` 为 `personal_override|template_snapshot|none`。`template_snapshot=null` 表示未应用 canonical 模板；已应用但该资源不在保存包中，则 snapshot 对象存在而 `configuration=null`。`personal_override.present=false` 必须伴随 `configuration=null`；true 必须伴随完整配置，包括 `actions=[]` 的拒绝配置。与模板数值完全相等的覆盖仍然是 present=true，不得压缩删除。

读取会包括已登记只读资源的空条目供编辑器展示；不能借读取自动创建 Grant。非法数据库 Grant 的诊断不得继续给予 allow，管理读取应显示可识别错误而非从旧权限补值。

### `GET /api/authz/templates/{code}/modules`

返回 envelope + `template:{code,name,description,base_role,is_system,is_active,locked,version}`、`modules:Record<resource_id,ResourceConfiguration>`。模块包是稀疏 map：没有 key 表示模板不提供此资源；拒绝配置仍有 key。`admin` 模板完全锁定，不能编辑/应用/同步；系统模板既有不可归档及基础角色保护保留。

## 3. 单账号资源写入和 epoch

`PUT /api/authz/users/{username}/modules`：

```json
{
  "schema_version":1,
  "catalog_version":"authz-s1-v1",
  "expected_epoch":7,
  "updates":[{
    "resource_id":"sales_analysis",
    "expected_version":3,
    "operation":"replace_resource",
    "configuration":{"actions":["read"],"scope":"assigned_customers","field_permissions":[]}
  }],
  "customer_assignments":{"ids":[7001,7002]}
}
```

`updates` 为非空且无重复资源的 patch；未列出的资源保持。每条 operation 为：

| operation | 必需载荷 | 效果 |
| --- | --- | --- |
| `replace_resource` | 完整 configuration + expected_version | 创建/替换整资源个人覆盖；值等于模板也保留 present=true |
| `revoke_resource` | 完整 configuration（actions 必须 []）+ expected_version | 保存明确拒绝覆盖与 Grant tombstone；不能删除源覆盖或 Grant 行 |
| `clear_override` | expected_version；禁止 configuration | 移除个人覆盖，恢复**账号保存的**该资源模板快照；没有快照配置时物化拒绝，不能追当前模板或恢复旧角色允许 |

`customer_assignments` 缺省保持；提供 `ids=[]` 清空；不得传 null。客户关系不归某模板所有，所有客户关系写入口都锁住账号、复验 expected_epoch 并推进 epoch。S1 不新增只写关系的端点；只改客户也提交完整 sales resource update 与其 expected_version，前端不得猜版本。

成功返回 envelope + `{committed:true, actor_session_revoked:boolean, result:<完整 AccountModulesRead>}`。更新与审计仅一次 commit；任一资源、客户、版本或保护失败全事务零变化。返回响应中提供已提交结果，即使操作者修改了自身而旧会话随提交失效，也不靠第二次请求取结果。前端若 `actor_session_revoked=true` 清理会话，不自动重放写请求。

每个接受的非空资源命令将所列资源版本各推进一次，并将目标 epoch 推进一次；即使 before/after 数值相同也如此。source-only 改变（例如写入与模板相同的 override、清除该 override、更新相同值的新模板快照）必须受 CAS 保护。客户关系与多个资源同时修改只推进一次目标 epoch。单写成功以完整 result 为准，不额外要求 `effective_changed/source_changed`；这两个标志仅用于第 5 节批量预览。相同值保存不能在 UI 宣称实际权限已扩大/缩小。

## 4. 模板保存、创建/复制与移除

`PUT /api/authz/templates/{code}/modules` 和 unified 模式既有 `PUT /api/role-templates/{code}` 转交同一 canonical 命令，不能保留两个写实现。请求：

```json
{
  "schema_version":1,
  "catalog_version":"authz-s1-v1",
  "expected_version":3,
  "metadata":{"name":"销售组","description":"只读销售分析"},
  "updates":[{"resource_id":"sales_analysis","operation":"replace_resource","configuration":{"actions":["read"],"scope":"assigned_customers","field_permissions":[]}}]
}
```

metadata 可省略；updates 可省略表示不改包；二者至少一个有真实请求项。updates 中 `replace_resource` 带完整配置，`remove_resource` 不带 configuration、明确删除模板包 key。资源重复、null、空 updates 数组一律 422。metadata 与包同事务保存，模板 version 只推进一次；完全不修改账号 epoch/Grant。仅改 metadata 也须 expected_version，防旧模板预览继续执行。

移除 key 不会即时改变账号；下一次同步必须在预览中产生该账号原继承资源的明确 revoke diff，再物化拒绝 Grant。个人覆盖默认保留，模板移除不能覆盖其独立配置。不能因为新模板缺 key 而保留旧继承允许。

创建/复制继续 `POST /api/role-templates`，返回 201。统一请求为 envelope 请求字段 + `metadata:{code?,name,description?}` 和 `authorization`：

- `{"kind":"explicit","modules":{...}}`：明确的完整初始包；允许 `{}`，表示模板不提供新能力。
- `{"kind":"copy_template","code":"sales_team","expected_version":3}`：锁定来源模板版本并原子复制 canonical 包；metadata 仍需明确新名称，可省略 code 由原规则生成。复制不携带账号/客户/项目关系。不能同时 copy 再用旧布尔覆盖。

成功返回 envelope + `{committed:true,actor_session_revoked:false,result:<TemplateModulesRead>}`。`modules` 中不得放 planned/retired 资源或未知字段。创建模板不赋权账号。保存后“同步账号”是另一个显式预览/执行事务，不能伪称保存模板与同步账号为一次原子事务。

既有 `POST /api/role-templates/{code}/archive` 和 `/restore` 也转交 canonical 服务，body 固定 `{schema_version,catalog_version,expected_version}`，实时管理员、模板行锁及 CAS。状态实际变化推进模板 version；archive 不写账号 profile/Grant/epoch。已归档模板不可 apply/sync/copy，新建/编辑错误与原系统模板保护保持；restore 不自动同步账号。重复相同状态返回明确 `changed:false` 且 version 不变，不伪称已更新。

## 5. 单目标套模板、批量与同步

复用 `POST /api/accounts/bulk`。单目标 apply 也用此入口，不新增 `/apply`；一次服务器事务处理全部目标，前端不可循环 PUT 冒充批量。统一模式预览请求：

```json
{
  "schema_version":1,
  "catalog_version":"authz-s1-v1",
  "dry_run":true,
  "targets":[{"user_id":41,"username":"alice","expected_epoch":7}],
  "operation":{"kind":"apply_template","template_code":"sales_team","expected_template_version":3,"override_policy":"preserve"}
}
```

targets 非空、稳定 ID 与 username 都要匹配；不得重复。所有目标必须显式给出，不支持省略目标自动展开“当前所有人”。保留原内置 admin / 管理员不参与批量、不能自锁、最后有效管理员等保护。其他操作：

- `replace_resources`：`updates:[{resource_id,configuration}]`，每项完整覆盖；scope/fields 不做跨账号或跨资源 union。
- `revoke_resources`：`resource_ids` 非空，服务器按该资源合法拒绝配置产生明确 override 和 tombstone。
- `clear_overrides`：`resource_ids` 非空，各目标恢复自己保存的模板快照；不恢复当前最新模板。
- `apply_template`：固定 code/version，`override_policy` 必须明确 `preserve|clear`，不依赖数值是否相同判断覆盖。全量替换 canonical 模板快照，未提交的独立覆盖按所选 policy 保留/清除，不支持一次混合多个模板的局部快照。

根已冻结：unified 的 apply/sync **只改 canonical 包及来源**，既有 `SysUser.role`、legacy `template_code/template_version/template_perms/perm_overrides/permissions` 原样保持，不在模板应用时做双域隐式编排。`base_role` 在 unified 模板 metadata 中只读。旧角色仍影响尚未迁域时，这一限制避免套新模板暗中改变旧域范围；最终统一验收前所有域应完成显式 Grant 迁移。显式角色安全命令是另一个需预览的操作，不能从模板名推导。

同步使用总契约已有 `POST /api/authz/templates/{code}/sync`；统一模式既有 `POST /api/role-templates/{code}/sync` 只接受同一新 body 并转交同一服务。body 同 bulk，operation 固定 `sync_template`，code 与路径一致、expected_template_version 必填、override_policy 必填；每个 target 当前 canonical template code 必须等于路径 code。预览之后新增模板成员不会加入已冻结目标；原目标中途换模板或任何 epoch 变化均冲突。

预览响应包含 envelope、`dry_run:true,committed:false,fingerprint,command,affected,preview`：

- `command` 是规范化后的完整命令，targets 仍是预览中的精确集合，并为每个 target 附 `expected_resource_versions`（全部实际受影响资源）。前端 execute 发送该 command 加 `dry_run:false,fingerprint`；不自行推测影响集或重算指纹。本节仅针对不含秘密的批量命令；第 6 节建号密码只在 execute 本地补入，管理员密码重置不使用 preview 回显协议。
- 每个 preview 项含 `subject,epoch_before,epoch_after,template_before,template_after,effective_changed,source_changed,session_revoked,resources`。
- 每个资源 diff 含 `resource_id,version_before,version_after,configuration_before,configuration_after,override_before,override_after,change:replace|revoke|clear_override|inherit|unchanged` 和明确 `actions_added/removed,scope_before/after,fields_added/removed`。包含数值相同但 source-only 的项；不能只给 changed_keys/count。
- 指纹采用服务器规范 JSON 的完整 SHA-256，至少绑定 schema/catalog、实时 actor ID+epoch、规范 operation、模板 code+version、完整目标稳定 ID+username+epoch、受影响资源版本、保存快照、覆盖**键存在性及值**、规范 after 结果。execute 在取得完整锁后重读重算；前端传来的 preview 内容不是授权证明。
- 预览不写 Grant/profile/epoch/审计；execute 只接受与 preview 一致的命令，不同目录/模板/目标/epoch/版本/覆盖存在性整体 409。没有预览指纹不能执行；不得自动重试或替换成新指纹。

execute 成功返回 envelope + `dry_run:false,committed:true,actor_session_revoked,applied,results:[AccountModulesRead]`。被目标保护规则拒绝时整个批次失败，不返回部分成功。每个目标只推进一次 epoch；每个实际影响资源推进一次版本，即使 source-only。不受影响资源不推进；preview 应给出准确影响集。

## 6. 建号、资料与旧入口

`POST /api/accounts` 复用原建号入口，统一 body：`schema_version,catalog_version,profile:{username,password,display_name?},authorization`。authorization 必填（UI 默认选择 `none`，不可默选 readonly 模板）：

- `{"kind":"none"}`：新账号无 canonical 模板/覆盖/Grant，epoch=0。
- `{"kind":"explicit","modules":{...}}`：完整初始资源包，生成显式整资源 override；可选顶层 `customer_assignments:{ids}`。
- `{"kind":"apply_template","template_code":"sales_team","expected_template_version":3}`：原子写账号、保存模板快照、物化 Grant；客户关系不能从模板复制，可另外明确提供。

显式包/模板含不可写资源、来源模板过期、客户不存在或用户名冲突时，新账号也不能落库；不要前端先建空号再发第二个赋权请求。赋权/来源存在时新 epoch=1，初始 Grant version=1；纯 none 且客户关系缺省为空不隐式赋权。建号本身涉及新凭据，仅服务器实时实名管理员可用，不能借旧 account_manage 创建/接管其他身份。成功 201 返回 envelope + `{committed:true,actor_session_revoked:false,account:<AccountModulesRead>}`，绝不返回 password/hash。

`PUT /api/accounts/{username}` 的 profile-only 统一请求仅 `{schema_version,catalog_version,profile:{display_name:string|null}}`。这不是权限资料，沿用现有并发覆盖行为，不声称全资料 CAS，也不推进授权 epoch。混入 `salesperson_name/role/template_code/permissions/overrides` 不能部分保存显示名后返回成功。

范围身份、角色、停启用和他人重置凭据是 security mutation：复用现有账号路径，但必须进入 canonical 编排，由实时实名管理员复验 actor，按目标 expected_epoch 执行并推进 epoch。`salesperson_name` 不是普通 profile；null 清空与 omitted 保持须区分，不能在实施时靠旧 `perm_change` 猜测分支。

### 安全属性命令（复用现有路径）

`PUT /api/accounts/{username}` 用另一条互斥 body：`{schema_version,catalog_version,expected_epoch,dry_run,security_change:{role?,salesperson_name?},fingerprint?}`。security_change 必须非空；role 仅允许既有合法分类，不能为 null；salesperson_name 可明确为 null 以清空。与 profile/modules/旧布尔字段混发整体 422；前端不得先保存 display_name 再假装安全变更也成功。

preview 只返回安全属性 before/after、epoch_before/after、session_revoked、规范 command 与 fingerprint；指纹绑定 actor/target 稳定身份、双方 epoch、角色/销售映射完整前后状态、目录版本及原有管理保护相关状态。execute 必须带同一命令和指纹，锁后重查保护与版本。Grant 数值和资源版本保持，目标 epoch 推进一次；显式角色变更可能改变尚未迁域的旧角色行为，预览要显示 `legacy_role_behavior_may_change=true`，不能称普通资料变化。切换到 admin 不自动制造新业务 Grant，只授予实名授权管理的既定资格。

新账号默认 readonly。显式管理员创建/初始销售映射复用 `POST /api/accounts` 的 `security_change` 分支，要求实时管理员及 dry-run/execute：预览时 profile 不带 password，校验 username 尚不存在、完整模块/模板意图并返回指纹；执行同一规范意图时才附 `profile.password`。指纹不包含口令，也不返回或存储其摘要；执行时仍验证新口令及用户名唯一性。模板/账号条件变化整体拒绝，新账号与 Grant 一次 commit；不存在先落库再升管理员的隐式中间状态。普通创建不能通过 profile.role/template_code/salesperson_name 越过此分支。该分支不接受任意非 admin 初始角色；其他角色变更在建号后走明确 security_change。

| 既有路径 | unified 请求 | 成功结果补充 |
| --- | --- | --- |
| `PUT /accounts/{username}/password` | `{schema_version,catalog_version,expected_epoch,password}` | `{committed:true,actor_session_revoked,subject:{id,username},authorization_epoch,reset:true}`；无 password/hash 回显；仅实时管理员重置他人，本人自助仍走原 auth 协议 |
| `PUT /accounts/{username}/active` | `{schema_version,catalog_version,expected_epoch,is_active}` | 同一 envelope 下返回 committed、actor_session_revoked、subject、authorization_epoch、is_active；改变有效状态推进 epoch，重复相同状态明确 no-op，不声称撤权发生 |
| `DELETE /accounts/{username}` | JSON body `{schema_version,catalog_version,expected_epoch}` | 返回 committed、actor_session_revoked、subject、authorization_epoch、deleted:true；保留软删除/最后管理员/不可自删保护 |

三个路径都只允许 canonical 服务提交，不保留先锁目标再取 roster 的旧锁序。复用现有端点不等于沿用旧布尔准入。

旧接口在 unified 模式的处理必须显式：

| 旧入口/载荷 | unified 行为 |
| --- | --- |
| `PUT /authz/users/{username}` 旧 S0 sales body | `409 AUTHZ_LEGACY_ENDPOINT_RETIRED`，指向 `/modules`；缺 expected_epoch 无法安全猜测，不在后台偷偷适配 |
| 新 body 的 `/accounts/bulk`、`/role-templates` create/update/sync | 同一 canonical 服务，原子模块包及 CAS |
| `permissions/overrides/keys` 等旧 bool 写体，即使新旧字段混发 | `422 LEGACY_AUTHORIZATION_PAYLOAD_UNSUPPORTED`，整体未写；按字段**出现**判定，`permissions:{}`、`overrides:{}`、`keys:[]` 也拒绝，不能按 falsy 忽略后 2xx |
| 账号 display_name-only | 原资料能力可保留；服务端确认当前身份，混合安全载荷整体拒绝/走统一安全命令，不能部分成功 |
| `role/base_role`、salesperson_name | 不是普通资料；只在上述允许的 security_change 路径接收 role/销售映射，base_role 只读；profile 顶层 `role:null/template_code:null` 同样因字段存在拒绝 |
| 自助改密 `/auth/change-password` 及既有 preauth 协议 | 保留原协议、本人当前密码验证和 token 更新，不混为管理员 reset；需共用 epoch/锁序规则 |
| 账号/template 现有列表、`_meta`、template accounts | 保留既有读取 transport，按下表增加批量摘要，不逐账号 GET modules 造成 N+1；不返回密码/所有对象归属 |

待实施资源在新编辑器只读，不存在 `editable_staged` 或 staged 写入。尚未迁域运行时可继续读其旧权限；统一模式拒绝旧布尔改权不意味着自动删除、清空或重解释这些存量列。

### 既有列表的准确摘要接缝

- `GET /accounts` 仍为数组；每行 additive 增加 `id,authorization_epoch,authorization_summary:{template_code,template_version,template_current_version,override_resources:string[]}`。summary 只来自 canonical profile，空来源均为 null/[]；旧顶层 template 字段保持 legacy 含义。统一 UI 只用新 summary，或隐藏旧模板/调整列，点击才 GET modules。
- `GET /role-templates` 仍为数组；每行已有 `code,name,version,is_active,locked,usage_count` 可用于选择。增加 `schema_version,authorization_mode,catalog_version,authorization_summary:{configured_resources:string[],canonical_usage_count:number}`。`version` 就是 metadata+canonical 包唯一 CAS，禁止另拿用户旧 template_version 作保存版本。既有 usage_count 如继续代表 legacy 成员，必须原名旁标明；统一 UI 使用 canonical_usage_count。
- `GET /accounts/_meta` 保留原对象结构，additive 增加 schema/mode/catalog，以及与上一条完全相同的 template summary。不能只从 `_meta.permissions` 推断可执行模块目录。
- `GET /role-templates/{code}/accounts` unified 返回原对象加 schema/mode/catalog，`accounts` 由 canonical profile.template_snapshot.code 选择，行含 id/username/display_name/is_active/authorization_epoch/template_version/stale/override_resources；不混入仅 legacy template_code 同名的账号。legacy_compat 保持原选择逻辑。目标集合必须由预览显式冻结。
- 这些兼容列表是第 2 节“新成功 envelope”的明确 transport 例外：数组不突然包成对象；新前端模式来自已校验 `/me`，每项新 summary 的定义固定，缺项不可臆造版本或允许。

## 7. 错误、事务与对象边界

新协议错误使用 `{"detail":{"code":string,"message":string,"committed":false,"conflicts":[],"field_errors":[]}}`。无相应明细时数组为空；UI 使用 code 分支与 message 展示，不能根据中文文案猜状态。

| HTTP | 稳定 code 示例 | 处理 |
| --- | --- | --- |
| 401 | `AUTHN_SESSION_INVALID` | 当前请求所属会话失效；保留迟到 401 不影响新会话的现有保护 |
| 403 | `AUTHZ_NAMED_ADMIN_REQUIRED` / `AUTHZ_FORBIDDEN` | 权限/身份拒绝，不自动刷新然后重放写入 |
| 404 | `ACCOUNT_NOT_FOUND` / `TEMPLATE_NOT_FOUND` / `CUSTOMER_NOT_FOUND` | 无目标或已删除；不泄漏非管理员诊断 |
| 409 | `AUTHZ_CATALOG_CONFLICT` / `AUTHZ_EPOCH_CONFLICT` / `AUTHZ_RESOURCE_VERSION_CONFLICT` / `TEMPLATE_VERSION_CONFLICT` / `AUTHZ_PREVIEW_CONFLICT` | `conflicts:[{kind,user_id?,resource_id?,expected,actual}]`，整体未提交；重新载入/预览，由用户重新确认 |
| 422 | `AUTHZ_INVALID_CONFIGURATION` / `AUTHZ_RESOURCE_NOT_CONFIGURABLE` / `LEGACY_AUTHORIZATION_PAYLOAD_UNSUPPORTED` / `ACCOUNT_SECURITY_FIELDS_FORBIDDEN` | `field_errors:[{path,code,message}]`，path 用 JSON Pointer；不自动补高权限 |
| 400 | `ACCOUNT_PROTECTION_VIOLATION` | 内置 admin、最后管理员、自锁、批量 admin 等已有业务保护；可附逐目标 field_errors |
| 503 | `AUTHZ_ENGINE_UNAVAILABLE` | 无可确认能力；停止新受保护请求，允许显式重试快照，不 fallback |

纯授权管理事务：无锁身份预检 → 必需的 active-admin roster advisory lock → 参与模板按稳定顺序锁 → actor 与全部 targets 按数字 user ID、一次取得最终所需锁强度 → Grant/客户关系 → 重查身份与 CAS → 原子写 profile/Grant/关系/审计/epoch → commit。不得先锁 actor 再补较小 ID，不能在旧 HTTP dependency 中持 SHARE 等待进入新写服务；不能两个管理写各持 SHARE 再升级 UPDATE。

领域写事务不套上述账号优先锁序：依各域 state/project/assignment 锁完成后，一次按 ID 取得 actor/target/owner 完整用户集合最终锁强度，复验授权；拒绝回滚根事务，包括新建的 workbook state 行。S1 控制面不进入项目挂靠事务，不通过模板修改负责人关系。

对象 capability 后续至少绑定 `{principal_id,authorization_epoch,resource_id,action,object_id,authorization_revision}`；revision 必须由领域提供，不能把 `project.version` 伪称完整授权关系版本。manager/viewer 改派未动账号 epoch 时，每次对象/API/任务/产物仍重查有效关系；`epoch 未变` 不是关系授权仍有效的证据。范围/字段始终按资源配对；D-03 managed 与 general workbook 两条合同不能做 scope×fields 并集。

## 8. 数据模型接缝与 fixtures

已批准最小 schema：

- `SysRoleTemplate.authorization_bundle`：JSONB 规范资源 map，使用既有 `version` 做 metadata+bundle CAS；空包不是隐式套旧权限。
- `SysUser.authorization_profile`：JSONB `{schema_version:1,template_snapshot:null|{code,version,modules},resource_overrides:{resource_id:ResourceConfiguration}}`。覆盖 key 存在性是业务语义；不压缩与模板相等的值。profile 只是编排来源，运行时绝不从它 fallback allow。
- `SysAuthorizationCutover`：resource 字符串主键、activated_catalog_version、activated_at。由根的启动/运行态接缝负责激活和读取，本控制面代理只拥有模型及单一迁移；Grant 创建、模板保存、`/me` 或业务读请求都不写该标记。无 HTTP 激活能力。
- `SysAuthorizationGrant` 继续唯一运行时允许来源。来源 profile 与物化 Grant 同一事务更新并检查一致；撤销行不删除。扩 scope 的 DB 约束随真正执行定义推进，不能在 S1 为 87 个描述全面放开。
- 保守 migration：已有 sales Grant 按原配置记录为 explicit override；旧 template_perms/perm_overrides 不猜成新模板授权。现存旧角色/模板字段保留其未迁域语义，不宣称 salesperson_name 已主键化。
- `authorization_epoch` 直接映射 token_version，无新增 epoch/source_revision 列。模板或账号源变化数值相同也推进已有 CAS 版本。

文件所有权：根代理独占 `app/authz/catalog.py,engine.py,service.py`、新 principal/runtime、认证/配置/security/main 启动适配；控制面代理独占新 `app/authz/management.py`、模型、单一 Alembic 迁移和 `api/accounts.py,role_templates.py,authorization.py`。共享类型/验证依赖由这份合同协调，不各自复制目录或主体判断。前端代理独占新旧 API adapters、Provider、权限中心与公共 UI 接缝。

`s1-wire-fixtures/` 下 JSON 是**独立 DTO 合同样例，不是服务响应实录、完整数据库 seed 或可复算服务器指纹 test vector**。每个文件采用 `fixture_format:1,catalog_version,cases:[{id,request,response,assertions,initial_state?}]`；少量 initial_state 只强调反例前提，不能按文件顺序假定已写数据库。响应中的版本/主键是安全整数，目录 fixture 版本固定 `authz-s1-v1`，运行中版本仍是 opaque。golden 消费者直接取 `request.json/response.json` 做前后端形状与状态消费对拍；fingerprint 作为服务器 opaque 值原样回传，真实后端并发/指纹测试必须另建完整初态。没有 SQL、真实账号口令或生产关系。

实施验收必须覆盖：来源值相同仍保留 override；空动作 override；clear 回旧快照、sync 追新快照；template removal 明确撤销；新增成员不进入旧预览；catalog/epoch/template/resource CAS；双管理员互改/批量与单改真实 PostgreSQL 并发；旧入口含空值毒针拒绝且零变化；原子建号/模板元数据+包；操作者自撤权会话清理；scope/字段跨资源毒针；PDP 缺失/错资源/重复响应；Provider 目标诊断不污染操作者；启用后重启把两旧销售开关置 false/mode legacy 仍 suspended/拒启动；另一实例激活立即阻止本实例旧允许。文档 fixtures 不替代这些运行测试。
