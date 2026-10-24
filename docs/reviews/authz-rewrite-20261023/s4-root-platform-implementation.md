# S4 平台与公共上下文实施记录

这是阶段内开发与审查记录。S3 精确提交 c0396fc08f6d5084358ebf352ffedad717980274 全量 CI 通过后才开始本包；尚未声明 S4 全量 CI、统一浏览器验收、可合并或可生产。生产没有变更。

## 已实现的执行链

- 普通资料：`account_directory.read/read_activity` 与 `account_profile.update` 分离；每资源尚未切换时保留实时旧能力，持久切换后只能使用当前 Grant/PDP，管理员无旁路。`/me` 直接复用同批 PDP，不再从旧布尔图推导已切换模块的 capability；快照版本包含 capability。
- 管理恢复：`/authz/accounts` 由实时实名管理员访问，只查询真实定位列及 canonical source 摘要。单个 profile 损坏仍返回定位信息与 `summary=null/status=invalid`，其具体模块诊断仍报 409。普通目录使用相同目标列投影，身份验证本身仍按既有实名主体流程。
- 活动：只有独立 `read_activity` 可读；SQL 不选择访问日志 resource/detail/user_agent、审计 reason/before/after。输出相应字段为 null。资料更新只改 display_name，在独立短读预检结束后锁定排序后的完整用户集合，再执行当前 PDP 和管理员目标保护；不推进授权 epoch。
- 控制面：规范模式下旧 `_meta` 退役，模板目录改为实名管理权限和封闭 DTO。角色/客户归属/授权包/模板/口令/生命周期继续由 S1 原子管理事务执行。
- 平台目录：十个边界与员工模块互斥，46 个 literal method/path 记录真实适配器和分支。共享 PUT 的 profile 分支属于普通模块，security_change 分支属于管理边界。机器服务只接受独立 SHA256 凭据和当前服务闭合动作配置；空配置暂停，未知/重复/跨服务动作拒绝。
- 价格证据：只 SELECT 固定合同允许列，维持单价/税率/数量语义和 keyset 分页；不物化整个订单/明细 ORM 后再裁剪。
- 公共上下文：跨域 dispatch、selection、文档与历史来源复验、RR 证明、最终用户锁移入单一 `domain_contexts`。维保 re-export 相同函数/类/registry；只对冻结的 81 入口、93 reference、48 query family 显式注册项目行为。新领域不能凭 project/collection 类型获得隐式维保 fallback。注册冲突先全量校验再修改，避免半次登记。

## 开发验证

使用独立本机 PostgreSQL 和真实 Cerbos，未 mock allow：

- 平台 14 项新增反例通过：普通目录/活动/资料独立、管理员无普通 Grant 的恢复路径、损坏单个 source 的定位、SQL 敏感列非物化、持久 marker 后撤配置/撤模式/暂停、PDP 故障、同批检查、双向资料编辑无锁升级死锁、保存前撤权/目标晋升拒绝且零写。
- 公共上下文与机器接口加 S3 foundation/artifact 回归组合 65 项通过。机器反例涵盖全部七入口的另一服务凭据/JWT/停动作时业务 SQL 为零、非法配置、价格参考单独停用、实际价格 SQL 投影。
- 新增两项销售/采购价格内容及翻页测试通过；最初测试把项目 Qty 的三位小数写成四位，按既有 Numeric(14,3) 修正期望，生产实现未改数量精度。
- 旧账号与商城回归组合中 151 项通过；先发现四项错误文案兼容回归，已恢复原 PN/返件错误文案并在该组合通过。S1 两项迁移测试原样经 backend owner 复跑通过，最初失败发生在其新 S4 空库边界调整前，未放宽降级保护。

日志仅是本次工作机临时证据：`/tmp/itsp-s4-platform-second.log`、`/tmp/itsp-s4-core-machine.log`、`/tmp/itsp-s4-machine-compat.log`。最后一份含新增数量测试修正前的两项失败，应与单独两项绿色复跑一起看；它不等价完整 CI。

## 交叉审查与阻断闭合

Root 对 Agent 提出执行输入深不可变反例，已复用公共 `_freeze` 并先红后绿；list/preview 的实际正式 HTTP 缺 authorization_context 也已补齐并由前端原样消费真实 golden。Root 对导入提出四项阻断：typed source permit、每文件/最终提交完整计划复核、共享归档错误补偿删除、本次失败回执误取历史 hash。四项均已修复；复审又要求每次 loader/派生调用重算实际计划摘要、失败事务不能复用 permit，并统一 global advisory → job/排序 items → 领域 → 最终用户锁顺序。最终新增 32 导入加 13 迁移测试由 root 独立复跑 45 项通过，日志 `/tmp/itsp-s4-root-import-final.log`。

Astra 使用真实合法 batch 构造跨入口/跨显式分支的错误 resolver，两项毒针先红；root 在公共 dispatch 与历史 reference 复验入口加入 batch observation、精确 entry 和显式 branch 核对。reference 仍使用持久依赖自己的 producer entry，不能换成外层 consumer。原样毒针和独立新增 reference 反例通过，S3 正例未破坏。backend owner 对 root 另增八项真实 HTTP 反例：逐资源未切换兼容、boundary 注入 422 且零审计/epoch、损坏 source 不猜空。

最终本机 `backend/tests/test_authz_s4_*.py` 由 root 独立完整运行 **151 passed**，100.79 秒；日志 `/tmp/itsp-s4-all-root.log`。这些包含作者测试与独立对抗文件，不能与其它组合相加成独立用例数。该结果是 S4 定向验证，仍需最终候选完整 CI 和统一验收。

前端跨包独审发现实际入口启用 React.StrictMode，而导入 live ref 仅 cleanup 置 false，导致 busy 状态不能解除；已用真实 Provider 先红后绿修复 setup。完整前端首轮出现 6 文件/18 测试失败，主要来自 api → domainTransport → AuthzProvider → api 模块循环及旧测试异步可见性。抽出不依赖 React 的 domainApi 后保留原断言闭合；最终完整前端 187 文件 / 2,150 tests 通过，TypeScript、构建、目录生成检查通过。Astra 独立 5 文件 / 53 tests 通过，含切换/删除会话时迟到上传与实际后端 golden 消费。日志 `/tmp/itsp-s4-frontend-full-third.log` 与 `/tmp/itsp-s4-frontend-cross-review-frozen.log`。

## 明确的可用性边界

PDP 整体故障时 `/me` 仍返回 503，受控前端不可用；独立实名管理 API 可以工作。本轮没有第二份权限缓存或故障专用前端，不能称为界面故障自恢复。真实源校验、文件来源、未知历史对象拒绝会让部分旧产物需要重建；这属于迁移验收内容，不能以放开旧权限或仅改前端隐藏掩盖。

## 最后后台入口对账

扫描实际进程内任务、线程及线程池启动点，将 36 个位置归入 14 个语义适配器；不能用目录里 3 个新增 TOOL/WORKER 合同声称覆盖全部异步执行。真实旧任务排队/运行期间 cutover 反例发现授权检查与事务提交间的窗口。公共 runtime 增加按资源共享事务锁，首次切换持同键独占锁；旧执行必须在领域/用户锁前入门，且每次事务重开都重新检查。未知来源保守检查五源，RR 历史快照拒绝使用此 guard。

Root 的返还导入多事务与全局 roundtrip 保护组合 82 passed（`/tmp/itsp-s4-root-worker-cutover-final.log`）；独立作者原样新增反例及 S3 组合 13 passed。返还 public wrapper 在授权前仅取 ID/owner/protocol，解析后和失败发布均重新入门。纯 FakeSession 清理单元显式 stub SQL guard，原 rollback/close/临时文件断言保留；真实 PostgreSQL 反例承担切换保护证明。项目/月度按清理前后两次 guard 保留原独立事务与锁序；真实窗口反例及既有 canonical/旧 API 组合 61 passed。Chat 的全局 authority 接缝以共享短事务屏障串行首次资源或 artifact-protocol marker，LLM 不持锁；独立非 Agent 首 marker 及文件碰撞/清理反例原样修复，独审组合 28 passed（`/tmp/itsp-s4-agent-global-barrier-independent.log`）。最终证据仍以冻结候选 CI 与统一验收报告为准。
