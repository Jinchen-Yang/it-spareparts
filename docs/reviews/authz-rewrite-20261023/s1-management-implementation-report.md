# S1 管理编排实施与验证记录

范围：`codex/authz-s1-control-plane` 工作树，S0 固定基线 `16c38c3063c2f971d3c0cce65f02924f1250e053`。本文是管理实现者的阶段记录，**不是独立审查结论**。依据为 D-0027、[总契约](../../plans/2026-10-23-authz-full-migration-contract.md)、[冻结 wire](s1-wire-contract.md) 与 [共享接口](s1-shared-interfaces.md)。S2 仍只存在设计草案；没有提交、推送或生产操作。

## 实现边界

模型与单一 Alembic revision `a7c2e9f4b6d1` 增加 `SysUser.authorization_profile`、`SysRoleTemplate.authorization_bundle` 和 `SysAuthorizationCutover`。迁移将实际 S0 `sales_analysis` Grant（包含空 actions 拒绝）保存为个人覆盖，相关账号 epoch 加一；不会把旧模板/角色解释成新的模板授权，不创建切换标记。默认 JSONB 使用带空格的 JSON 文本，避免 SQLAlchemy 将冒号后的数字或 null 当作绑定参数。已有任何 cutover 标记时 downgrade 明确拒绝，不能移除持久权威；无标记的迁移 roundtrip 保留 Grant 与单调推进的 epoch。

`app/authz/management.py` 统一处理模块写、客户归属、模板、批量、建号、资料、安全属性、密码重置和账号生命周期。三个 API 路由复用现有 transport，统一模式不运行旧布尔写门。实名校验、目录闭集与运行态消费根实现的公共接口，没有另造 dict → Principal 转换或第二套可执行资源目录。

管理写在完整锁集合下重验 actor；模板先锁、用户按 ID 排序锁，管理员名单动作额外串行保护。Grant、来源 profile、客户关系、epoch 与审计同一事务；审计故障使根事务回滚。预览没有写副作用，执行重新核对目标、来源、资源版本与 fingerprint，后加入模板的成员不会自动进入旧预览目标。

## 冻结契约的具体落点

| 契约 | 实现行为 |
| --- | --- |
| 单次模块写 | CAS 检查账号 epoch 与资源版本；遗漏资源保持；空 updates 拒绝；传客户空数组清空、遗漏保持；客户非法时整批回滚 |
| 覆盖存在性 | 与模板配置数值相同的个人覆盖仍保留；空 actions 仍是显式覆盖；source-only 变化推进资源版本和账号 epoch |
| clear / sync | clear 回账号保存的模板快照；无快照物化拒绝墓碑且来源 none；追最新模板必须显式 sync |
| 模板移除资源 | 模板保存不立即改变账号；后续 sync 明确产生 revoke diff，保留撤销行而非删除版本 |
| 模板管理 | metadata 与模块包原子保存；create/copy/update/archive/restore 使用统一权限与版本门；归属、usage 来自 canonical profile，不读取旧 template_code 代替 |
| 旧字段不隐式编排 | apply/sync 不改变旧 role、template_code/version、template_perms、perm_overrides；统一 UI 只消费 canonical 来源 |
| 新账号 | 默认 readonly、默认无 Grant；授权与资料一次提交；管理员建号仅由受保护 security_change 意图开启，不能借该分支指定任意旧角色 |
| 安全属性 | 普通 profile 仅 display_name；混入 permissions、overrides、keys、role 或 template_code（含空值）整体拒绝；安全属性另走 live-admin + preview/CAS |
| 凭据 | 他人密码重置只限有效实名管理员且直接 CAS；秘密不进入预览、回显、fingerprint 或审计；自助改密由根维护 `app/auth.py`，不混用管理 DTO |
| 自己的会话 | 命令结果明确 `actor_session_revoked`；包含本人且改权时先返回结果，再由新 epoch 使旧 token 失效 |
| 旧 S0 写入口 | `PUT /authz/users/{username}` 跨 mode 永久 409，不再绕过 canonical 来源直接改 Grant |
| HTTP 失败协议 | 三个管理 router 的专用 `AuthorizationRoute` 统一 parser/query/Pydantic 错误为稳定 detail + no-store，不回显校验输入中的秘密；不改全站 legacy 异常协议 |

普通 display_name 更新不宣传为权限 CAS，不推进 epoch。未迁域仍保留旧业务授权；本阶段没有用一个管理员页面门替代 391 个业务入口的真实范围与字段合同。

## 损坏来源的诊断方式

账号模块读取先验证 profile 结构、保存模板身份与版本、各资源配置、Grant 配置及版本，再比较来源与物化结果，之后才排序/序列化。混合类型 actions/fields、非法 profile 或非法配置返回 `409 AUTHZ_SOURCE_INVALID`；合法来源与物化 Grant 不一致或缺物化行返回 `409 AUTHZ_SOURCE_INCONSISTENT`。不返回混合的半可信结果，不自动修复，也不会因 `sorted([1, "read"])` 变成 500。

这是**管理读取诊断**，不另造运行时允许来源。业务读取仍以实时、有效 Grant + PDP 为唯一允许依据。修复损坏持久数据不是普通授权命令隐含行为。

## 实际验证及证据边界

全部后端测试使用现有 `.venv/bin/pytest`，测试基库为本机 `127.0.0.1:32780/spareparts_test`，conftest 为每个进程创建独立数据库。Cerbos 使用本机固定二进制；数据均为合成数据。没有使用生产连接。

| 批次 | 实际结果 | 证据 |
| --- | --- | --- |
| 管理阶段完整 targeted suite | 65 passed，17.24s | `/tmp/s1-management-final-tests.log` |
| 最后新增 Pydantic 分支及既有 parser/query/no-auth 回归 | 6 passed，60 deselected，4.23s | `/tmp/s1-management-error-tests.log` |
| 既有 accounts 与 Beta 白名单 | 18 passed，8.28s | `/tmp/s1-management-legacy-tests.log` |
| 既有 permission_center 首轮 | 58 例通过；3 个直接调用 handler 的旧签名用例失败 | `/tmp/s1-management-tests3.log`（该组合总结果为 113 passed / 3 failed） |
| permission_center 调用适配后复跑 | 3 passed，58 deselected，8.86s | `/tmp/s1-legacy-roster-tests.log` |

当前管理文件为 66 个用例。前 65 个完整通过后，专用 route 包装新增对 Pydantic `ValidationError` 的统一处理，并补第 66 例；最终改动重跑六个相关错误协议用例。**不将 65 + 6 写成 71 个不同测试，也不声称最终代码单次跑过 66 个。** permission_center 的 61 例证据来自先前 58 例加适配后 3 例，不声称单批 61 全跑。后续整合 CI 应重新执行全部用例。

65 例覆盖的重要反例包括：首次并发写只有一个成功；actor 等锁后已撤权不能提交；审计失败零写；同值覆盖及 clear 仍推进 CAS；保存快照与最新模板不同；模板删除资源显式撤销；客户关系 omit / empty / invalid；未知/planned 资源整批拒绝；只改资料混入旧权限键零写；管理读取 SHARE 锁；存量 S0→S1 真 PG 回填、21 个旧角色/快照投影向量；cutover 后 downgrade 拒绝；六种持久来源损坏返回诊断错误。

三项并发测试既单独运行过，也包含于上述 65 例：

- 两位管理员互改对方模块，结果 `[200, 401]`，仅一个 Grant/epoch 提交，无锁升级死锁。
- 两位管理员互相停用，结果 `[200, 401]`，始终至少保留一个有效管理员；自停用仍拒绝。
- 模板 archive 与账号 sync 共用模板锁。archive 成功；sync 若先取得锁可以先提交，若后取得锁则看见停用并拒绝；最终版本、active 与账号 epoch 对应各自合法顺序。

测试清理加入无外键关联的 `sys_authorization_cutover`。`git diff --check` 对管理所属文件通过。旧 permission_center 测试仅把直接 handler 调用适配为新版 dict body、typed identity 与明确 db 参数，原名单并发断言未放宽。

## 他人证据与剩余阶段门

独立审查者在来源诊断与 downgrade 保护完成后重跑其 36 个公开 HTTP/PG/PDP 反例：36 passed，19.19s，`/tmp/itsp-authz-s1-independent-all-final.log`，对应 [独立实施审查](s1-implementation-independent-review.md)。这是独立审查者提供的证据，不冒充本实现者独立测试。

根实现者的核心 80 passed / 30.13s 日志为 `/tmp/itsp-authz-s1-root-final.log`；它不属于本文管理测试，也不属于本人的第三角独立结论。第三角审查另外落在 [核心业务与切换审查](s1-core-business-cutover-review.md)，该审查发现的目录/cutover 接缝由根修复。

S1 完整后端/前端回归与精确提交远端 CI、后续各阶段迁移及最终用户验收仍是后续门槛。此记录不表示全模块已迁完、可发布或已上线。
