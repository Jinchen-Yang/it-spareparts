# S3 root 实现与验证记录

状态：S3 仍在实现，尚未提交完整 CI，不能据本记录判定可合并或可生产。S2 精确提交 68e99445c144eb5df8c6c93b2d0980b94b340619 的全 CI run 40022339294 已绿，之后才开始 S3。

## 迁移运行 7 个入口

已接 preview/search/detail/project evidence/manifest/reconcile/approve。维护原业务复核与不激活生产合同，同时加入实际项目集合、当前动作和完整字段许可、稳定账号幂等与三人职责分离、追加型事件内容凭据、当前权限下重放、RR READ ONLY manifest 重建。主应用真实挂载保留 Beta 发布总开关；canonical 权限不再暗借旧页面白名单。

本地真实 PostgreSQL + Cerbos 0.57.0：新权限 9 项 + 旧 API/运行/来源/仓库/旧口径 66 项，共 75 passed。日志 `/tmp/itsp-s3-migration-runtime-combined.log`。覆盖改名创建人仍不得对账、同名新 ID 独立、preview-only/approve-only 不借读权限、未知历史拒绝、篡改拒绝、撤权后重放拒绝、实际等待领域锁时撤销 epoch 后零运行/事件/sidecar 写入。manifest 读取监测到 repeatable read/on，且无 INSERT/UPDATE/DELETE/领域锁 SQL；实际仓库/旧口径/source reader 也在真实只读事务验证。

测试发现并修复：追加型审计表不允许事后 UPDATE 凭据；可选仓库截止日期为 NULL 的 SQL 参数需明确 DATE 类型；锁后 run ORM 必须刷新。不同模型初读未发现新阻断，仍须阶段最终对抗复审及完整 CI。

## 前端需求单与补货 F3

需求页、行编辑/创建/批量创建/批量编辑、WBDD 上传和对账、补货页已接捕获身份的正式上下文请求；页面、按钮、列表、数据字段独立许可；项目/账号/意图切换使旧请求失效。未知网络结果保留原冻结请求。补货历史可以单独打开；已有草稿可以仅凭提交动作提交，不把草稿修改隐式当作提交前提。

9 个本地文件 108 passed（100 旧业务 + 8 新真实 Provider/实际 API adapter 权限反例），日志 `/tmp/itsp-s3-f3-all-first.log`。包括无显示门零请求、实际授权拒绝零事实请求、409 不重放、账号切换与关闭对话框中止迟到写、历史单独权限。提交已保存只读草稿分支尚待独立专项断言。

## 公共边界增量

canonical 字段适配不再经过旧全局 F→K 依赖，依赖由具体资源 catalog/PDP 决定；旧模式保留旧依赖。13 项 core 真实 PDP 通过 `/tmp/itsp-s3-core-field-fix-real.log`。授权视图新增唯一 `service.context_resolution` 组装入口，必须与前端严格 locator/envelope 校验一致；各领域已开始回归。

## 回款前端

CollectionTab/CollectionBatchMaintenance 首轮 32 passed（28 旧行为 + 4 真实 Provider）：独立 plans/actuals/manage 动作、F 字段遮罩、复合 workspace contracts 区块否决不能借其它 allow、账号切换中止迟到批量授权。日志 `/tmp/itsp-s3-collection-final-first.log`。保留逐行结果、网络未知冻结载荷与读回核验，尚待阶段最终复审。

## 行级工作台

5 个 API 入口已接一个完整合同择一、真实项目关系、集合 SQL 范围先于 count/page/export、各合同自身 C 字段与旧 F 行为隔离、同 RR 校验 precondition 和物化 spool。6 新真实权限测试 + 141 旧筛选/导出/回归共 147 passed，`/tmp/itsp-s3-rows-combined.log`。正常与 managed 两种合同不相互借用字段和范围；已切换后 suspended 不回退。主总表 rows 第 6 个入口由另一实施者复用同 query/project_page seam，独立记录。

## 验收报告、附件与清单

AcceptanceTab/MaintenanceAcceptancePanel 前端22项通过（17旧+5真实Provider），日志 `/tmp/itsp-s3-acceptance-combined.log`。清单、报告、附件显示独立；仅预览不能应用；仅上传无需读报告；实际对象拒绝零正式写；账号切换销毁旧确认框。

6个报告/附件API接 actual project / business_file context、同RR读取与完整bytes hash、domain locks之后actor重新验证、稳定owner和命令幂等、共享文件当前读者重授权。真实10项canonical+23旧API共33 passed，`/tmp/itsp-s3-acceptance-backend-combined.log`；含实际锁等待撤权零业务/操作/sidecar/磁盘文件写、download readonly事务和之后独立审计append、主app真实Beta总开关。S3迁移新增稳定owner守卫后16 passed，`/tmp/itsp-s3-migration-owner-extension.log`。仍待阶段最终独立复审及完整CI。

S4仅只读准备，未开始实现。

## 前端完整回归及金融孤立组件

ProjectWorkbookActions/CollectionPlanImportModal/CollectionMilestoneFollowUpModal/CollectionReminderDetail 首轮32项通过后，前端独立审查发现raw DTO仅凭requires_context shell渲染缺口，现需BoundAuthorizationContext与projectId；缺失/错误对象/旧账号/deny皆隐藏。交叉组合36项通过。补库autosave独立input transport修复迟到view允许旧输入写入问题，先红后绿；已保存只读草稿submit-only专项通过。前端完整179文件2081项、tsc/build/generator check均通过，尚非远端精确提交CI。完整证据见s3-frontend-implementation-review.md。

## 补库后端12入口（进行中）

首稿独立facade已接capabilities/catalog/projects、cart读改删提交、application列表/创建/详情/两种revision/整文件export。领域项目/草稿/申请/版本/备件/advisory锁先于actor，旧原语以实际ContextAccessSet产生的事务绑定CommandAuthorization接入并由facade提交；不使用角色假装或Session.info放行。版本单独稳定owner表达实际执行者（允许全范围授权的操作者复提别人的申请），actual immutable version+review+audit绑定sidecar，同RR读取时完整重验后即时导出。历史未知owner不认领。11新PG/PDP与58旧atomic/beta组合69 passed，`/tmp/itsp-s3-replenishment-combined-first.log`。范围SQL、删除precondition、两种revision、他人代复提独立versionowner、实际锁等待撤epoch、同RR readonly详情/导出均有实证。迁移与ORM共18 passed，`/tmp/itsp-s3-owner-final-migration-drift-second.log`。独立复审仍进行中，未计阶段最终完成。

## 补库后端独立复审结果

Astra 补充真实版本提交后异常与历史版本篡改两项均通过，日志 `/tmp/itsp-s3-replenishment-independent-final.log`。静态未发现新增阻断；本地模块通过不等于阶段精确提交 CI。

## AI 辅助解析 3 个入口

实际输入→类型化 proposal→真实目标预览 batch 全链已接。LLM 在无 DB 事务时执行，返回后重新取得当前身份、动作、字段许可；接受另一人的提案由当前接受人承担实际目标批次 owner，不继承原提案人的 grant。提案内容与实际接受回执分别绑定，真实 batch/mapping/file hash 被改则拒绝；单文件不同 mapping 的同 key 不会塌缩成旧结果。14 新真实 PG/PDP + 12 旧回归 = 26 passed，`/tmp/itsp-s3-ai-combined-final.log`；Astra 独立验证原创建人停用撤权后，另一个只有 accept 权限的当前账号仍能按自己的资格接受，随后撤销 accept 则重放被拒绝，`/tmp/itsp-s3-ai-independent.log`。

## 全局成本文件 5 入口与重算

实际来源 SQL 谓词先于逐单导出的 count/query/行输出；未授权 C/K 不进 SELECT。整合同工作簿只接受 all_sources 与其 F/K 字段合同，且仅加载 renderer 真正用到的订单列；往返模板独立 all_contracts + F/K/C 合同。完整 spool 在同一 RR READ ONLY 中生成，释放 DB 后流式输出，首字节前/每块重验授权。7 新 + 111 旧 = 118 passed，`/tmp/itsp-s3-global-read-second.log`。

重算单独完整 all_sources 动作，不借 admin/read；真实 source/assignment/site-issue 全域拓扑及预条件，领域锁和暂存写之后最终 actor/PDP，撤权全部回滚。加入此用例后本包 8 新 passed，`/tmp/itsp-s3-global-recompute-second.log`。剩余第7入口 roundtrip-import 正在作者验证，尚未冻结；整体仍待独立对抗复审与精确提交全 CI。

## 最后全局与批量入口

全局工作簿回填第七入口已完成：真实签名原件、全合同 C/F/K、同事务实际 batch/operation owner、完整输入及原件摘要、逻辑重放、最终域锁后当前授权。作者 8 + 旧 32 =40 通过；独立原样8 +新增3 =11通过（`s3-roundtrip-independent-review.md`）。全局六入口共享下载实现已由另一作者复核并提取到 `authorization_downloads`，消除 service→API 反向依赖。

批量传输五入口实现见 `s3-batch-transfer-implementation-review.md`，最后独立复核与 S3 全包集成进行中。当前 S3 目录精确为228 live human entry、11 retired；不是以目录登记代替每条实际接线。S4继续保持只读准备，等待本阶段精确提交完整 CI。

## S3 集成与传输最终收口

首轮 S3 全包集成 455 passed（`/tmp/itsp-s3-all-integration-first.log`，611.18s）；其后 actual-main/metadata 投影补丁有各自定向回归，不能把首轮结果当最后精确提交全绿。传输最新作者、跨作者、跨模型与原业务组合 118 passed；独立扩展正式 APPLY/replay 投影与真实两连接锁序 4 passed。两项复现红已闭合，详情见本模块实现/独审报告。当前准备 S3 精确提交 CI，S4 实现尚未开始。
