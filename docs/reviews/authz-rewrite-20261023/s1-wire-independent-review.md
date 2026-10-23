# S1 wire 与 golden fixtures 独立复核

复核时间：2026-10-23 16:40 UTC。业务代码基线 `edd0f3efade00ab457731737f82f68eb282a2226`；复核结束时 HEAD 为 `16c38c3063c2f971d3c0cce65f02924f1250e053`，后者仅追加已单独复核的午夜测试夹具修复。本报告审阅工作树 D-0027、总契约、[S1 wire](s1-wire-contract.md)、五份 JSON fixtures，以及前端 next-stage-map 第 8、12 节与机器消费说明。只新增审查文档，不修改 S1 app/tests，不执行生产操作。

结论：**设计阻断已闭合，可在 S0 精确 SHA 全量 CI 门槛通过后进入 S1 实现。** 这不是 S1 实现验收或合并/生产许可。当前 wire 与 fixtures 是合同，尚不能证明 HTTP、事务、真实 PDP 或前端已经执行这些规则；S1 完成必须经过下表反例与阶段复审。

## 原四项阻断的闭合状态

| 原阻断 | 本次闭合证据 | 仍需证明的实现行为 |
| --- | --- | --- |
| 普通资料管理混入安全属性和他人凭据 | wire 第 6 节区分 display_name-only、显式 security_change、管理员重置密码；role/销售映射不是普通资料。旧 bool 按字段存在性整体拒绝，包括空对象/null；建号与初始授权原子。管理员建号 preview 不携带口令，执行才提交口令；重置密码使用单次 epoch CAS，不复用回显 command | 非管理员不能借旧 account_manage 改销售映射、套模板、重置高授权账号密码；混合请求零写入；任何响应、审计、指纹输入都不能泄露密码或密码摘要 |
| 主体共享锁与领域锁形成循环 | wire 第 7 节明确管理事务 roster → template → 全体 actor/targets 稳定数值 ID → Grant/关系；领域事务仍为领域锁 → 完整 actor/target/owner 集合，取得最终锁强度后再复验；HTTP dependency 不先持共享锁 | S1 管理路径真实 PostgreSQL 并发无锁升级/逆序；对应领域接入时再验证跨项目 actor/owner 循环，不把未迁领域提前宣称通过 |
| 相同值 override 被压缩、拒绝覆盖丢失 | wire 第 2、3、5、8 节及 `same_as_template_override_stays_present`、`explicit_empty_actions_override`、`clear_override_restores_saved_v2_not_latest_v3`：覆盖键存在性独立；clear 回账号保存快照，无快照则物化拒绝且 source=none；sync 才追最新模板；source-only 仍推进 epoch/资源版本 | 模板保存不追改账号；preserve 不删除相等覆盖；删除模板资源在 sync 明确撤销继承值；现有 S0 Grant 导入为显式 override，不猜旧模板 |
| epoch/project.version 不覆盖对象关系撤销 | wire 第 7 节单列领域 authorization_revision，任务和业务请求重查有效关系，不以 epoch 未变证明关系未变 | S1 通用接口/前端不得伪造对象 revision。维保切换阶段必须验证实际 manager/viewer 撤销及工具、文件、任务、历史产物边界 |

复用 `token_version` 作为 epoch、`SysRoleTemplate.version` 作为 metadata+bundle CAS 可成立：账号来源变化也推进 epoch，模板来源变化也推进模板版本，不需要第二个只保护来源的版本列。运行时仍只读规范 Grant；`authorization_profile` 只负责来源与编排。

## 本轮追加核对并已纳入合同的接缝

1. **永久切换不能仅靠环境变量。** 当前 S0 `service.enabled()` 是两个 bool 的 OR；两者都关闭后，销售 API 会重新选择旧路径。wire 第 1 节现要求 `SysAuthorizationCutover`：可信 enable 配置在服务启动独立短事务幂等激活，提交后才能 ready；普通请求只读，缺 marker 但要求启用则失败关闭。有 marker 后改回 legacy/false 必须 suspended 或拒绝启动；跨实例不能永久缓存“无 marker”。这满足显式切换事务，S4 再删除 legacy 代码；不能把当前 S0 env 行为描述为已有永久保证。
2. **切回 control-plane mode 也不能恢复旧写旁路。** S1 建立 profile 后，旧 S0 `PUT /authz/users/{username}` 跨 mode 永久拒绝，避免直接更新 Grant 而不更新来源。已 cutover 资源的旧 bool 写也不能因 legacy_compat 重新开放。mode 选择协议，不能抹除资源的授权权威。
3. **模板生命周期与成员查询也属于控制面。** archive/restore 转实时管理员、模板锁和 expected_version；归档不改变已套账号，但不能继续 apply/sync/copy。unified 的成员查询与 canonical_usage_count 从 profile.template_snapshot 取值，不能继续仅查询旧 template_code。
4. **新模板应用不暗改未迁域。** apply/sync 只更新 canonical 包/来源，保留旧 role/template_* 等列；base_role 在模板 metadata 中只读。角色或销售映射改变必须走另一个有预览的安全命令。统一 UI 读取 canonical summary，不能把旧模板列显示成新授权来源。
5. **文档只保留一套 wire。** 前端已去掉旧 migration/runtime 候选枚举和 staged 执行状态，使用 `planned|implemented|retired` 与 `legacy|enabled|suspended`；S1 没有 staging 写能力。错误使用 detail，兼容数组列表是明确 transport 例外。

## S1 必须运行的具体反例

| 编号 | 操作/安排 | 必须观察到的结果 |
| --- | --- | --- |
| S1-01 | 持已过期 epoch 的管理员 token；管理员随后停用/软删除/降级；或共享 fallback 身份伪造旧 role/perms 调用模块管理 | 实时身份拒绝，无 Grant/profile/关系/审计部分提交；正常业务不能从旧 token 获得新允许 |
| S1-02 | 非管理员拥有旧 account_manage，提交自身 salesperson_name/role、套模板、bulk、sync、他人改密；display_name 混发 `permissions:{}`、`overrides:{}`、`keys:[]`、`role:null` | 安全操作拒绝，混合请求连 display_name 也不写；单独合法 display_name 不重算授权、不推进 epoch |
| S1-03 | 新账号默认 none；显式模块/模板建号中放入 planned 资源、不存在客户或过期模板版本 | 默认无 Grant；非法建号全事务零落库。模板 metadata+包保存任一项失败也不能部分成功；模板保存不改账号 |
| S1-04 | override A 恰等于模板 v2；模板 v3 扩成 B 后 preserve sync；再显式空 actions；最后 clear 与 sync 分别执行 | A 保持独立覆盖；空动作保持拒绝覆盖；clear 回保存快照，sync 才追最新。无快照 clear 生成拒绝 tombstone，不恢复旧 role |
| S1-05 | 初次写 expected_version=0；撤销后再用 0 重建；相同值 replace/clear 或相同值新模板 snapshot 后执行旧 preview | 首次版本只能使用一次；撤销行不删；source-only 变化仍导致旧 epoch/版本/指纹冲突 |
| S1-06 | 预览 Alice、Bob 两目标后只改变 Bob epoch；或改模板 metadata/version；或向模板新增 Charlie；或 Alice 更换 canonical 模板 | 过期预览整批零写入；Charlie 不自动纳入旧目标集合；不是只检查第一目标或只比较有效值 |
| S1-07 | 客户字段省略、显式空数组、无效客户分别与完整资源 patch 同交；套模板同时观察原客户关系 | 省略保持、空数组清空、无效客户全回滚；模板不复制/覆盖关系；一次命令目标 epoch 只推进一次 |
| S1-08 | 两条真实 PG connection+同步屏障：双管理员互改、单改与 bulk、模板保存/archive 与 sync、最后管理员并发停用；另测本人 Grant 写后旧 token | 稳定锁序无死锁/升级循环，CAS 阻止旧写，保住最后管理员；提交结果随响应返回，actor_session_revoked 正确，旧 token 失效。不能用自动重试掩盖锁序错误 |
| S1-09 | canonical 成员与 legacy template_code 故意不同；归档模板后 apply/sync/copy；旧 S0 PUT、旧账号/模板/bulk bool 请求 | 成员列表和 usage 使用 canonical 来源；归档命令 CAS 有效；旧入口拒绝且不破坏 profile↔Grant 一致性 |
| S1-10 | 启用后重启改 false/legacy_compat；另一实例在本实例启动后创建 marker；启用配置存在但 activation 事务失败；planned/retired 请求 enabled | 已切换资源绝不进入旧允许，旧授权写也不重开；无 marker 不偷偷激活；非法配置失败关闭。marker 建立于独立启动事务，普通 /me 和业务 GET 零 marker 写入 |
| S1-11 | 两个异构可执行测试资源 A/B 使用不同 scope/fields；批量真实 CheckResources；注入错资源、缺项、重复 PDP 结果；改用旧 UserContext/dict 作为新入口身份 | 无 scope×fields 并集，结果严格配对，非法结果整体失败关闭；新身份入口只接受可信认证/受控委派。测试定义不把真实 planned 模块偷偷开放 |
| S1-12 | 比较 1 与多个测试资源的 /me SQL/PDP 调用；持 planned、suspended、未知动作和 PDP 断连快照 | Grant/marker 批量读取，PDP 批量调用，不逐域加载项目列表/PlanResources；planned 不可写且新决定 deny，suspended 不回旧允许，错误无部分 allow |
| S1-13 | 前端 A 会话慢成功/慢 401 在 B 登录后到达；目标诊断 Alice→Bob 时旧请求迟到；409 后保留草稿再改目标/模板/override_policy | 操作者 Provider 不接收目标快照；旧会话和旧目标结果丢弃；旧 preview 失效，不自动重放写入；本人会话撤销立即清理 |
| S1-14 | 未知 schema/资源/动作、requires_context、planned、suspended、服务不可用；挂载菜单/路由/面板/按钮与编辑器 | 不默认 allow、不发无权子组件请求、不自动补成本或更大范围；统一模式只消费新 resources，兼容 sales 顶层由相同结果派生 |

对应领域切换时追加、**不冒充 S1 已运行结果**：两个不同项目互换 actor/owner、同账号并发写不同项目；拒绝时回滚预建 state；在账号 epoch 和项目主档 version 均不变时撤销 manager/viewer，确认对象决定、工具、排队导出、原件及历史结果重放都重查当前关系。S1 只需确保通用接缝没有提前持用户锁、伪 revision 或 epoch-only 允许。

## 本次验证及其上限

独立解析五份 JSON 共 **45 cases**，检查 case ID 唯一、全部响应 no-store、错误 committed=false 且不含允许 envelope；检查 **36 个账号资源样例**的 override 存在性/来源一致、version=0 与无配置一致，以及 **3 个资源 diff** 的动作/字段增减与前后配置相等；递归确认响应无 password/password_hash。结果通过。逐例人工核对 clear/sync、模板移除、保存不追改、成员冻结、空客户、源变化、自撤权和旧字段拒绝语义。

这些是 DTO 与静态语义检查；不是数据库状态变更、密码审计脱敏、指纹算法测试向量、真实 PDP 或浏览器行为证据。没有为文档合同运行或更改 S1 app/tests。最终完成标准仍是各项实现反例、三角度独立复审、真实 PostgreSQL/Cerbos/前端检查以及精确 SHA 全量 CI；统一用户点按安排在全部模块迁完之后，生产保持未授权。
