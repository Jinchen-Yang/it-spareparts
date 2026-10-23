# 研究来源、Agent 规则与设计审查记录

配套 [总体设计](../maintenance-refactor-design.md)。核查日期 2026-10-23 UTC；源码基线为 #451 的 `fa97e24`，当前主线为 `7a9fd20`。来源用于提出方法，是否适合本仓库由真实调用、业务契约及测试决定。

**时效更正**：用户要求最新近半个月口径优先。项目业务/协作规则以[近期口径台账](recent-policy-baseline.md)逐条核验结果为准；旧 AGENTS、记忆、归档、PR 描述和旧测试不自动成为有效要求。外部架构文章与 skill 是方法参考，不能覆盖项目近期更正，也不能给旧业务条款重新授权。

## 外部一手来源与采用范围

### 多方案接口设计与深模块

本机仍提供 `design-an-interface`，本次按该 skill 分派 A/B/C。联网追查原作者仓库后确认，它已被 `codebase-design` 吸收；当前方法文件是 [DESIGN-IT-TWICE.md](https://github.com/mattpocock/skills/blob/5beb35b040e3de97e54fa9e386d7e42d15c295c2/skills/engineering/codebase-design/DESIGN-IT-TWICE.md)，blob `d0990138c35c800bd821289a73115e52680c1620`。其三种约束分别是最小接口、最大扩展性、最常见调用方，和本次分工一致。配套 [improve-codebase-architecture](https://github.com/mattpocock/skills/blob/5beb35b040e3de97e54fa9e386d7e42d15c295c2/skills/engineering/improve-codebase-architecture/SKILL.md)与 [codebase-design](https://github.com/mattpocock/skills/blob/5beb35b040e3de97e54fa9e386d7e42d15c295c2/skills/engineering/codebase-design/SKILL.md)用于识别真实变化边界与接口隐藏复杂性的程度。

本仓库落地：合同读取有五类真实消费者；需求归属校验有 manual/facts 双向依赖；已存在的事实原语和命令入口继续使用。没有据文件行数创造若干空 service 或通用 repository。原作者仓库核查提交为 `5beb35b040e3de97e54fa9e386d7e42d15c295c2`；不能把本机旧 skill 的名称与路径冒充上游当前版本。此处只核查与引用，没有更新本机 skills。

### 渐进迁移

[Martin Fowler：Strangler Fig](https://martinfowler.com/bliki/StranglerFigApplication.html)描述按业务需要逐步替换旧系统；[Branch By Abstraction](https://martinfowler.com/bliki/BranchByAbstraction.html)说明通过稳定接口逐段迁移调用方并替换实现。

本仓库落地：先提取真实实现、保留旧名字，再逐个改 caller；复杂工作簿可先委托 legacy 实现，但这一阶段不能宣称传递依赖消失。只读结果可在隔离种子上差分；写操作分别在隔离数据库验证，不向真实库双执行。临时 facade 必须列退出条件，否则会成为新增永久复杂性。

### Agent 工具设计与评估

[Anthropic：Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)强调按任务设计清楚的工具边界、相关结果与任务评估。它没有要求把所有 service 方法自动变成模型工具。

本仓库落地：ToolCatalog 只负责声明与派发一致；handler 保留服务端范围和脱敏。Agent 只调用已授权查询；新增写命令不自动出现在工具目录。后续 eval 至少覆盖“找到可见项目→读取有限明细→说明来源与缺失”，记录任务成功、越权拒绝、调用次数和输出体量。参数越界、空范围与未知金额都需可重复样例。没有真实模型运行时不报告准确率提升。

### Skills 的职责

[Agent Skills specification](https://agentskills.io/specification)规定 skill 元数据与说明/资源组织，支持按需加载。它不为项目提供数据库权限或事务安全证明。

本仓库落地：AGENTS 保留短入口，详细迁移与测试链接到本目录；现有 CONTEXT/decisions 的优先级高于外部 skill 习惯。每个任务只加载相关规则，不把上万行维保口径复制进 skill。现阶段复用已有工程 skills，没有下载或安装新的第三方技能包。

### 事务和锁的实现约束

[SQLAlchemy 2.0：Transactions and Connection Management](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html)明确 outermost transaction 的 commit 语义及 savepoint 对象管理。项目 `backend/uv.lock` 当前锁定 2.0.49；此次调查没有升级依赖。将会 commit 的 legacy service 包在 `begin_nested()` 中，不能变成安全的整本子事务。

[PostgreSQL 15：Explicit Locking](https://www.postgresql.org/docs/15/explicit-locking.html)解释一致的多对象锁顺序、advisory lock 的应用自律及事务级锁生命周期。项目 compose/CI 使用 15；本设计不把网页、工作簿、返还导入各自的锁前缀强行统一。对相交写集验证共同阻挡关系，才能判断迁移保持了并发保护。

## 开发代理规则应该怎样落地

建议在 R1/R2 实施 PR 中给 #451 候选 AGENTS §8 增加少量链接和以下约束；本设计不直接修改 AGENTS，以免在实现尚不存在时发出错误指引。第一条前置规则为：先核查滚动近 15 天的实质条款变更与替代关系，再判断历史规则可否承接；不能把新提交或新文件名当作全文重确认。

1. 扩展前给出一条完整调用链：入口 → 授权 → 查询/命令 → 事实 → 审计/回执 → 提交者。标出新增功能真正变化的层。
2. 新入口使用公共接口；禁止新代码从 boss/workbook/manual 的私有函数取共享事实。兼容旧出口只向新实现转发，不能反向引用。
3. 显式说明空集合、None、未知金额、当前日与历史窗口；不能以便捷默认值吞掉差异。
4. 更改写链必须列所有竞争 writer、锁模式/顺序、CAS、幂等指纹、原回执和失败回滚。原语不得额外提交或持有更上层锁。
5. 新增 Agent 工具用家族注册；权限仍在服务端。保持业务只读，不因 schema 里有 `readOnly` 或 skill 文字就省略权限测试。
6. 行为测试围绕真实反例；架构测试约束传递路径并报告最短违规链。测试通过才更新模块边界说明和旧别名退役状态。

这些是工程约束，不是新业务决策；若实现会改变业务意义，仍按仓库约定先写相应决策。

## 机器可检查与人工复核的分界

| 规则 | 可执行检查 | 仍需人工/行为证据 |
| --- | --- | --- |
| services 不依赖 API；公共读不依赖显示/Excel模块 | 现有 AST 直接和传递可达规则，覆盖函数内 import | 动态 import、包初始化、外部对象引用不是该图保证范围 |
| 新核心不能扩大既有循环 | 固定基线 SCC 与禁止边预算；失败显示路径 | 不能为了降低数字随便搬文件或隐藏 import |
| 公共查询不业务写入 | 定向 session/SQL 断言及真实数据库结果前后比较 | SQL 记录中不含业务值，验证范围必须明确 |
| 事实原语不提交 | 静态 callsite 提示与事务内故障回滚测试 | 别名调用和下游隐式提交不能只靠文本搜索 |
| 五类快照 writer 共同阻挡 | writer清单与双Session竞争测试 | 新 writer 的发现仍需审查ORM/SQL写路径 |
| Agent 名称/schema/handler一致 | Catalog启动检查、schema快照、runtime闭环 | 工具选择正确率和业务回答质量需真实任务eval |
| 前端旧上下文不落地 | 可控Promise的组件生命周期测试 | 用户点按验收另行记录，不冒充已完成 |

优先扩展 #451 已有架构测试和清单脚本，不为单项规则先安装新框架。检查脚本不是产品依赖；若以后采用 import graph 工具，也须证明能覆盖函数局部导入及兼容名单，而不是只换工具名称。

## 多智能体交叉审查裁定

本次三个代理使用同一模型独立工作，交叉提出问题；不是跨模型复审，也不是生产验收。

| 挑战 | 提出/复核 | 裁定 |
| --- | --- | --- |
| 公共合同函数能否返回一种“安全金额”让所有caller共用？ | A / 根 | 不能。五类消费者和fallback零/负值存在实质差异；先迁事实，保留各自策略 |
| 需求归属guard是否应直接复用“有效需求”查询？ | A / 根 | 不能。旧SQL只检查活跃挂靠；追加状态/墓碑过滤是业务变化 |
| facade是否自动降低耦合？ | C / 根 / A | 不能。仅包旧boss/master时没有传递依赖收益；真实实现迁出后再计量 |
| typed plan与资源声明能否证明正确持锁？ | A / B / 根 | 不能。必须覆盖外部writer、锁后freshness和下游后继锁 |
| 将所有命令改为自持Session是否是必要前置？ | C / 根 | 不是。现有HTTP commit与workbook自提交分别保留；有独立调用需求才整体迁移 |
| 统一compiler能否重新定义manual指纹与null规则？ | C / B / A | 不能。保留旧指纹、默认值和回执命名空间，不用新规范化重写历史协议 |
| 工作簿锁后发现新资源能否自行补锁？ | B / A / C | 不得倒序补早期资源或扩大确认范围；已定义的后继锁继续原协议 |
| `expire_all/populate_existing`是否应成为通用读取习惯？ | 根 / B | 不能。新鲜基线阶段与写阶段分开，防止抹掉未flush修改 |
| V2是否已像网页correction一样全面锁后重读账号资格？ | A终审 / 根实码复核 | 不能这样陈述。保留入口及特定分支检查；撤权竞争窗口列R3待调查，不冒充既有保障 |

综合稿终审由三代理分别复核后端契约、前端/扩展面、Agent/来源。后端提出的两处表述已修正：V2 当前鉴权覆盖不能夸大；仅需要补前序锁或超出确认范围的资源漂移才按该类冲突拒绝，合法三路合并与无关新增行不受影响。原六份设计引用的 21 个现有后端测试文件已按完整文件名去重核对。时效修订后增加近期口径台账，并再次核对七份文档的本地 Markdown 链接与代码块；这只是文档与可执行入口核对，不是本方案测试已运行。

完整提案：[A](design-a-explicit.md)、[B](design-b-registry.md)、[C](design-c-facade.md)。它们是备选探索，接口命名和终态选择可不同；实际实施以总体设计的近期接口及批次为准。特别是新Session所有权、全量registry、全面不可变计划不是本轮默认必须实施的工作。

用户追加近半个月约束后，三个代理分别复核后端决策、前端呈现、Agent/工程规则，并由根代理核对未合分支。结果与替代关系汇入[近期台账](recent-policy-baseline.md)。本轮特别更正：D-36 有近期 AI 业务写红线重申；18 schema 是兼容观察；D-34 Q1/Q3 与完整恢复目标不能因暂停或未合 main 而忽略；主干旧审批门槛已被替代。依据是实质条款差分，未将旧文件的修改日期当成全文确认。
