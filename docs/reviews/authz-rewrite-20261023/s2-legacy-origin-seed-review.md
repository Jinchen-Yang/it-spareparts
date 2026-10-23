# S2 legacy seed 来源判别：实施前阻断与最小机制

状态：来源机制已由根批准并补入 S1 修订，修订基线 `bad69768ea63e9369fc62817bf7b12e510f671ff`；待根整合新提交及精确 CI。本文最初的独立盘点发现：**原 S1 持久数据不足以区分明确创建的无权对象与待迁移的 legacy 对象；S2 不能按空值猜测后补旧角色权限。** 下述来源列及创建路径现已实现，S2 seed 仍未实现。

## 1. 不可区分的两个输入

| 对象 | 应允许下一阶段 legacy seed 的来源 | 不应自动获得旧权限的来源 | 当前可相同的持久状态 |
| --- | --- | --- | --- |
| SysUser | S1 迁移前已存在、没有 sales Grant 的旧账号 | S1 canonical `authorization.kind=none` 新建账号；也包含 explicit 空模块包 | role=readonly，profile 空、Grant 无；epoch 不能可靠区分，改密/授权都会改变它 |
| SysRoleTemplate | 只有旧 permissions/base_role 的原模板 | S1 canonical explicit `{modules:{}}` 或空包 copy 的新模板 | authorization_bundle={}；同名、is_system、version 都不是授权意图的证明 |

因此“现有 Grant 缺失才 seed”仍会给明确 none 的账号新增能力；“bundle 为空才从旧 permissions 转换”会把 canonical 空模板变成有权模板。检查 epoch、名字、created_at 猜时间点或审计有无事件，都不能可靠覆盖恢复/导入/历史缺记录场景。

## 2. 推荐的最小持久机制

S1 增加两列 `authorization_legacy_seed_eligible: bool NOT NULL`，分别位于 SysUser 和 SysRoleTemplate。它是**来源事实**，不直接产生业务 allow；服务不得从该列给 runtime Grant。

- ORM 与数据库默认均 false，遗漏的新写入口失败关闭。
- S1 迁移在增加列后，对**该迁移开始时已经存在的行**显式回填 true。这是精确迁移边界，不是后续按日期猜测。
- 旧协议建号、旧协议模板创建明确 true；canonical 建号所有分支（none / explicit / apply_template，含安全属性预览执行）都明确 false。
- canonical 模板 create/copy 明确 false，即使复制来源是 legacy eligible 模板，也不能继承未来默认授予资格。
- 已有行的资料修改、canonical apply/sync、改密、归档、单模块覆盖不改该来源标记。旧账号在 S1 写过 sales，并不意味着其余 legacy 域全已放弃迁移；但已存在的 canonical 资源覆盖/Grant 必须逐项优先，尤其空 actions 拒绝。
- S2 完成后不把所有 true 全部改 false：S3 尚有未迁域。幂等与各资源已迁状态另由版本化迁移/显式存在性处理，不能把来源字段兼作任务进度。

当前实际构造点只有：

| 文件 / 构造 | 来源 |
| --- | --- |
| `app/authz/management.py` canonical user 与 template 构造 | false |
| `app/api/accounts.py` legacy create | true |
| `app/api/role_templates.py` legacy create/copy | true |
| `app/seed_users.py` 脚本创建 | 根确认保留 legacy bootstrap，已明确 true；只对新行记录，已有行不改来源、role、密码或 canonical profile。canonical 建号使用管理 API |

S1 目前尚未生产部署，因此在未部署 revision 内补齐最清晰。若某环境已经运行过未标来源的 S1，S2 **不能无条件把全部现存行设 true**：canonical 创建对象已混入，只有可信逐对象迁移清单或默认 false 后显式授权可安全恢复来源。当前没有生产部署不是算法可以依赖的恒定前提。

## 3. S2 及后续迁移的消费规则

用户与模板各自只对 eligible=true 的对象运行已冻结的旧解释器；false 不从 role、旧 permissions、旧 template_perms 补任何新资源。eligible=true 仍不意味着所有资源允许，实际旧 page/role/scoped/fields/参数谓词决定每项允许集。

每项 seed 先识别 canonical 覆盖与物化行：包括空 actions、相同值 override 和完整保存快照。存在显式 canonical 配置就保留，不以新迁移解释覆盖。仅当目标资源从未配置且此对象 eligible 时，才能形成来自 legacy 的首次配置。损坏或来源/物化不一致的对象应诊断停止，不将其视作“未配置”。

模板 seed 的更新不得自动同步到 S1 canonical 新账号的保存快照；该账号即使引用同一个 template code，也只保存当时明确应用的 canonical bundle。追最新模板仍需显式 sync。旧账号的 legacy template_perms 也必须从其保存的旧快照解释，不能直接读取后来扩大的同名模板。

已 cutover sales 的 S2 八动作与 show_* 升级始终基于现有 canonical source/Grant，与 eligible 无关；false 的账号若有明确 sales Grant，仍应按其实际 Grant 正确升级。false 不是禁用已明确授予的权限。

## 4. 必需验证向量

1. 迁移前旧 readonly 无 sales Grant → eligible=true，可按冻结旧解释器迁入它原本拥有的商业资源。
2. canonical kind=none 新 readonly → eligible=false；S2 后仍无新资源，不能因 readonly 旧默认模板有 page 权限而获得权限。
3. canonical explicit 空包与空模板 copy → false；模板/账号保持明确空包。
4. legacy create（迁移后但仍旧协议）→ true；与 canonical 创建形成同样空状态时仍可无歧义区分。
5. eligible=true 账号对某资源有空 actions override → seed 保留显式拒绝；其他从未配置资源按旧解释器迁移。
6. canonical 新账号保存旧 eligible 模板快照；S2 填充模板新资源后账号快照不自动变化，必须显式 sync。
7. false 但持有 canonical sales Grant → S2 正常升级八动作及合法 show_*，fields/scope/客户关系不扩大。
8. S2 幂等重跑、S3 后续迁移保留来源标记；不重复 bump，不遗漏后续 eligible 域。
9. 默认插入未声明来源的新对象 → false；历史不可证明来源不得被自动回填 true。

其中 1–4、9 的 S1 来源区分已通过真实 PG/HTTP 回归；5–8 中未来资源 seed/升级消费行为仍是 S2/S3 实施门，不宣称已执行。

## 5. S1 修订落点与降级保护

两列均为 NOT NULL，ORM 与 server default=false；`a7c2e9f4b6d1` 同一未部署 S1 migration 仅将其执行前已有 user/template 置 true。canonical account/template create/copy 显式 false，两个 legacy 创建 API 及 `seed_users` 新建显式 true。字段没有加入 wire DTO/response，也没有加入 runtime 授权判断。conftest 重播内置模板时显式 true，因为该动作模拟历史迁移内置模板，而非 canonical 新建。

交叉审查还证明：只有 cutover marker 的 downgrade 保护不够。即使 unified 模式尚未启用资源，canonical none 账号和空模板也可已经存在；删除来源列再 upgrade 会错误把这些 false 行设为 true。现 downgrade 在任何检查前，按固定顺序对 cutover、role_template、user 获取事务级 ACCESS EXCLUSIVE 表锁；若存在 marker 或任一 false 来源行则明确拒绝，零 DDL。表锁让并发 activation/canonical 插入无法穿过检查与删列之间。无 marker 且全为 legacy true 的旧数据仍可正常迁移 roundtrip。

来源新增不会推进 epoch、模板 version 或创建 Grant；原 S0 Grant→profile 回填及其 epoch+1 规则保持。已有 canonical 配置、快照和 override 不因来源列修改而被覆盖。若某环境曾运行未标记的旧 S1 revision，不能以本次默认回填推断混合来源；须按前文可信逐对象清单处理，不把“当前未部署生产”当通用迁移算法。

本实现者真实 PG 回归：新增来源 16 例 + 管理完整 66 例单批 **82 passed / 24.49s**，日志 `/tmp/itsp-authz-s1-origin-management.log`；既有 token_revocation/accounts/Beta/permission_center 单批 **91 passed / 28.66s**，日志 `/tmp/itsp-authz-s1-origin-legacy-regression.log`。82 例在最后添加表锁前运行，表锁后的定向迁移结果见 [S1 实施报告](s1-management-implementation-report.md)。独立审查者在 false 来源降级红例修复后单批 **45 passed / 22.83s**，日志 `/tmp/itsp-authz-s1-independent-origin-final.log`，其额外并发锁审查单独记录，不冒充本实现者独立结论。

表锁加入后本实现者真实迁移定向 **2 passed / 3.90s**（`/tmp/itsp-authz-s1-origin-locked-migration.log`）。独立审查者通过 `pg_blocking_pids` 实证 downgrade 等待未提交 false 来源 writer，随后拒绝删列；其含历史修复的组合 **84 passed / 41.69s**（`/tmp/itsp-authz-s1-independent-lock-regressions.log`），无剩余来源设计/实现阻断。精确新提交 CI 仍由根推进，不能据本地结果宣称远端已绿。
