# 返还台账与领用改造：接续工作记录

更新时间：2026-09-14。本文件替代前一会话未经验证的完成宣称；测试和远端状态以本文件最新记录与实际命令为准。

## 接手入口和授权

- 仓库：`/home/cloudlay/IT-data-over/it-spareparts`。
- 计划：`docs/superpowers/plans/2026-09-11-site-issue-display-and-return-ledger.md`；领域：`CONTEXT.md`；规则：`docs/decisions/0001-维保整改八条口径.md` D-17。
- 用户授权继续完成整个已确认计划、自行查修缺陷、隔离验证真实样表并负责合并；生产部署、生产数据导入仍需另行授权，本轮均未执行。
- 项目操作以 `CLAUDE.md`、`.ai/AI_WORKFLOW.md` 与当前代码为准。工程上下文实际采用根 `CONTEXT.md`、`docs/decisions/`、`docs/api/`；旧工作流列出的六个 `.ai` 上下文文件并不存在，不据此另造冲突副本。
- 工作前先 `git status`，保留当前未提交修改。多代理共用工作树，不可 reset/覆盖。

## 已确认业务口径

- 项目必选；WBDD 可选且必须有效、归属当前项目；PN 必选但无需匹配领用 PN/SN；手工数量严格正整数。
- 登记即实收，可改、转移、作废；操作人来自真实登录身份，前后值留审计。项目总数恒等于需求单汇总加未关联量，免返不抹除收货事实。
- 只以第二份 `入库单_2026-09-11-20-37-34-424.xlsx` 为标准；维保拆旧返件、旧库退返与全部件况纳入。
- 整机主项固定 1 台，附属明细仅展示；源小数不取整、仍计数量并标待审。页面明确改为整数后解除待审且保留来源证据。
- 卡墙 `receipt_return_rate` = 有效收货数量 / 有效 WBDD 备件数量之和；不按行数、不扣免返、不按 PN 匹配。分母不完整不显示百分比，超过100%如实显示。
- 旧坏品消费者仍采用原本就包含上述两类的坏品口径，不计手工来源或其他件况；显式更正和作废会更新其事实结果，不能声称历史数值永远冻结。
- 工作簿新增领用没有 delivery ID；其返还要求采用行事实与独立事件契约，禁止伪造发货身份或逐行分配收货。已有原生领用继续读取旧义务冻结快照并同步其既有投影。

## Git 真实状态

- 本轮交付分支 `integrate/2026-09-12`（接续时以 `git branch --show-current` 为准），接手 HEAD 为 `52278b2`（当时领先 main 19 笔），最新业务代码提交为 `489fa67`。本轮已提交：`c175bb8` 测试夹具与发布回归环境、`0bb182e` 后端台账/导入/并发审计、`e84fcaa` 前端台账/导入/领用同步、`b3de27c` 两项迁移祖先关系测试；接口与规则文档已提交 `e98ba71`，集成分支已推送到远端；最终验收结果随后以文档提交回填，最新提交号以 `git log -1` 为准。
- 完整交付PR：[#323](https://github.com/Jinchen-Yang/it-spareparts/pull/323)。此后接手续先读该PR的实时检查、审批和合并状态，并核对远端main；不要将本文件记录时的状态外推为当前结果。PR创建时main=`e0c80b02220e287530025086e9c26b7ad351b828`，返还历史分支=`80c1db52e20d15ff6a52b7ac77cfb2b0665d967b`。
- 已 fetch 实时 PR321 head；它与本地 `ee31fa6` 的 tree 都为 `766277a91c5caf2080a08879847fb6b749ef5a52`。复核时 PR321 仍 open，不等于远端合并。
- 历史“已推 main”“作者自合并证明保护已关闭”均已在 CHANGELOG 纠正。不得直接推 main、绕过 CI/审批或回滚不明远端变更。
- 交付调整为**完整 integration PR，按上述逻辑提交分组审阅**，集成分支已推送，PR #323已创建并触发远端CI；最终状态以PR为准。保留已有合流历史，不重建或强行拆分用户工作树；只有最终验证树可作为发布候选。`136c9c2` 是已包含前置八笔和 #321 等价内容的本地合流树，但没有本轮独立完整验收，不能把它单独包装成“已验证”的前置发布版本。
- 本次 GitHub 实时核查 main `protected=true`，要求“后端测试（pytest + 迁移链验证）”和“前端类型检查 + 构建”。详细保护读取权限为 403，不能据此假定原批准要求已取消。按仓库流程经 PR/CI/审批后 squash 合并，保留集成分支或归档引用以追溯原始历史。
- 新迁移线为 `c9e5a1b7d3f8 → d2f8b4e6c9a1 → e3a7b9c2d4f6`。

## 接续实现

- 台账全终态校验、明确 null 清空、真实行锁/CAS、同键锁与原始命令重放校验、跨项目范围和历史防泄漏；失败审计覆盖依赖拒绝、Pydantic 与服务异常，独立事务持久化且不记录正文/令牌/异常原文。
- 并发范围缺口已修复：手工写入在权限检查前排序锁定原/目标项目，再锁台账并刷新归属；导入按统一顺序锁定身份、工作簿状态、项目和事实后重新计算权限。真实双事务验证撤权先提交时写入拒绝、写入先获授权时双方撤权等待提交；项目归档、手工疑似重复和备件合并也参与最终锁内校验。
- 修复 d2 数据库默认值多重引号：有旧 RKD 记录时原版升级会在 CHECK 处失败。e3 同时修复已安装旧 d2 的默认值。两迁移有不可保留事实时明确拒绝降级，不删除收货。
- 新异步导入复用 doc batch + SysRawFile 原件归档，稳定原始主/明细身份、完整 CRC/安全扫描、分类预览、原件下载、显式更正、stale 零写入、worker generation/租期、取消与原件重试；旧通道不得绕过 token 或跨来源接管。
- 整机组成件更正纳入来源差异，台账指向最新 raw head，旧证据保留；分页查询批量附属明细只读字段，组件不入数量汇总。
- 领用展示当前主数据描述/品牌/分类/单位、SN、备注及返还要求；Excel 是/否/清空双向同步、版本审计和旧原生义务兼容。附带实际收货只读快照，含层级与待审说明；签名验证阻止编辑统计，原样旧快照允许下载后新增收货。
- 卡墙新收货率批量聚合，分母覆盖没有有效明细的需求单缺失情况。
- 正式领用查询/作废、需求候选、项目 workspace 和返还导入随 boss 总闸，保留原有动作与项目范围。旧草稿/确认/原生坏返仍受 Beta 闸与白名单限制。
- 前端支持清空、完整分页候选、转移、稳定重试键、请求代次防串页、审计和中文导入差异、小数与其他源件况的备注单独修改、整机附属明细、原件下载。版本升为 1.30.0。
- 导入取消与轮询竞争已修复并完成延迟响应专项；轮询不会夺走取消操作的请求代次，取消后 busy 能正常退出。

## PR #323 审查修复（2026-09-13）

- `489fa67` 修复 `business_type_hidden` 与回款生命周期叠加时计数错误：之前回款成员集合已被业务类型缩窄，导致“回款完成”隐藏数少算、其他期限桶多算。现在仅从回款候选中移除业务类型条件，保留项目范围、搜索与生命周期，共用一次批量回款计算；列表和隐藏查询仍保留其各自全部筛选。
- 新增回归先复现8项失败（`/tmp/pr323-business-type-red.log`），修复后相关专项 **92 passed / 0 failed，53.90秒**（`/tmp/pr323-business-type-green.log`）。覆盖四生命周期桶、多选/分页/空页、范围/搜索/活动/归档条件，无财务权限不推导回款身份，以及项目数/页大小增长时 SELECT 次数恒定。
- 该修复在本地4482项全量之后发现；最终发布判断以 [PR #323](https://github.com/Jinchen-Yang/it-spareparts/pull/323) 最新提交的检查及合并状态为准，旧提交 CI 不替代新提交 CI。
- 全依赖审计的两个 moderate 条目对应开发依赖 Vitest 3.2.7 的同一公告 GHSA-82fw-gwwq-j7x9；当前仅 jsdom + vitest run，未启用 browser/API/UI 或受影响插件，生产 Nginx 镜像不含该开发服务器。生产依赖审计为0已知漏洞；全依赖不能记为0。升级至修复版本4.1.11或更新版本可另行兼容验证，详情与官方来源记录在PR说明。

## 验证证据（不要把多次重复专项相加）

- 历史集成全量曾为 **4373 passed / 12 failed / 7 skipped**。已定位真实多合同测试夹具非确定性、umask 文件权限和 shellcheck PATH 问题；没有用跳过或放宽生产校验掩盖失败。
- 领用/工作簿扩展回归：173 passed，`/tmp/return-ledger-workbook-expanded.log`。
- 正式 main 权限初轮：142 passed，`/tmp/receipt-stable-release-tests.log`。
- 最新迁移/原生快照/旧消费者/权限复核：48 passed，`/tmp/receipt-final-review-tests.log`。
- 核心最终相关专项：44 passed，`/tmp/receipt-scope-final-tests.log`；补充转移先授权后原/目标项目撤权的真实并发 2 passed，`/tmp/receipt-transfer-scope-tests.log`。组件/导入/台账前轮为31 passed、1个可选样表 skip（`/tmp/receipt-components-tests.log`），真实样表最终运行见下一条。
- **最终导入完整专项：54 passed、0 skipped**，`/tmp/return-receipt-import-complete.log`，包含真实标准样表、最终权限/项目/备件合并并发修复与原件一致性。完整解析 **12,986 主单、107,651 明细、315 张拆旧+13 张旧库有效单**。按源名称各建隔离项目时有 **2 条同 WBDD 不同项目标签冲突，整批0写入**；明确统一隔离参考项目后，保持原件不变、原件重试应用得到 **501 条实收、数量501**。506条有效源明细中，整机6组成件替换为父1，故不重复。该参考归属由测试构建，不能据此宣称生产归属已治理。
- 卡墙及导出最终专项：11 passed，`/tmp/return-ledger-final-card-export-tests.log`。迁移祖先/单头专项：7 passed，`/tmp/return-ledger-migration-head-regression.log`。
- 全新独立库执行 `upgrade head → alembic check → heads`：1 passed，`/tmp/receipt-alembic-check.log`；输出 `No new upgrade operations detected.`，唯一 head 为 `e3a7b9c2d4f6`。数据库默认值另由带旧数据迁移往返与原生 SQL 插入测试覆盖。
- **最终前端全量：78 文件817项通过、exit0、无未捕获异常**，`/tmp/return-ledger-frontend-full-final.log`；最终 `tsc && vite build` exit0，`/tmp/return-ledger-frontend-build-final.log`。
- 浏览器四个脚本 `flow.cjs`、`workbook-flow.cjs`、`import-flow.cjs`、`machine-flow.cjs` 全成功，分别记录于 `/tmp/return-ledger-browser/` 下同名 `.log`。独立 QA 为 `beta=false / boss=true / V2=true`，所有脚本 `errors=[]`。登记5→改3/清空备注→审计→作废后，原有收货总量仍为7；手机 viewport/document 均390宽，表格内部横滚。工作簿“是→否”上传后网页和再次下载一致，收货仍7；导入1.250并修改备注/重传原件后总量8.250；整机明细展开、原件一致和转移后总量9.250，整机仅计1台。
- 后端生产依赖 pip-audit：无已知漏洞，`/tmp/return-ledger-backend-audit.log`。
- **前端 npm 在线依赖审计已通过**：用户在明确获知发送目的地和依赖包名称/版本后指示“你解决一下”，已按授权执行 `npm run audit:prod`，exit0，输出 `PRODUCTION_AUDIT_OK: no known runtime vulnerabilities`；日志 `/tmp/return-ledger-frontend-audit-online.log`。此前自动审批拒绝已解决，离线跳过没有作为通过证据。
- **接续首轮后端全量已结束**：session6649，`/tmp/return-ledger-backend-full-final.log`，真实结果为 **4 failed / 4457 passed / 8 skipped，2248.92秒**。4个失败均为该进程载入旧模块后已定位、修复并通过专项的问题，不能因此把这次运行改记为全绿。
- **审查修复前固定代码的第二轮全量已通过**：session63974已结束、exit0，`/tmp/return-ledger-backend-frozen-full.log`，结果为 **4482 passed / 7 skipped，0 failed，2427.75秒（40分27秒），exit0**。该轮业务代码固定在 `b3de27c`；随后 PR 审查修复 `489fa67` 改动了卡墙隐藏计数，须以最新 PR head 的远端 CI 完成最终全量验证，不能把本轮结果当作新代码全量结果。最终4489节点与进度符号逐位核对，机器可读结果 `/tmp/return-ledger-final-validation.json`。前一会话挂起十小时的孤儿pytest已退出，未清公共库。
- 7个跳过节点已用最终4489节点收集结果逐位核对：型号夹具缺失的overview回读、发布主机二进制 opt-in、旧收款提醒样表、生产规模性能基准、FK阻止构造孤儿数据的防御路径、可选返还原件、真实Chrome发布主机管道。只有可选返还原件为本次新增，它已在54项零跳过专项中实际运行；其余6个跳过条件原已存在main，不能把未执行的性能/防御路径称为已验证。
- Nginx故障日志检查按CI条件单独实际运行 **1 passed，76.65秒，exit0**，`/tmp/return-ledger-nginx-runtime-check.log`；测试按固定digest拉取镜像后，最终全量中的同项也实际执行通过，未再跳过。

## 交付流程与接续检查

1. 本地实现、前端完整回归、构建、迁移、真实样表专项、浏览器验收与前后端生产依赖在线审计均已完成。后端全量必须按上文各轮对应的代码版本理解；最新业务代码 `489fa67` 的最终完整验证读取PR最新head的CI。npm元数据发送授权已取得且审计通过，不再是阻塞。
2. 完整PR说明已发布至 [#323](https://github.com/Jinchen-Yang/it-spareparts/pull/323)，按既有授权执行 PR → 远端CI → 审批 → squash合并。接续时先核查该PR：已合并则直接核对远端main与合并SHA，不重做交付；尚未合并则处理剩余检查/审批。不得绕过必需检查或批准要求。生产部署和生产数据导入仍需另行授权。

## 演示、隔离验证与本地工具

- 用户演示：`http://home.cloudlay.cn:5176`；backend `127.0.0.1:8000` 已刷新加载审查修复，记录时PID3853749（操作前必须重新核实，记录文件 `/tmp/return-ledger-demo-backup/new-backend.pid`）；Vite5176。库为本机5433 `spareparts_dev`，容器 `it-spareparts-import-test-pg`。
- 演示项目 `9ddd613f-2ffb-4cd2-9b39-a3e3396636f4`；不清库、不重播种子。日志 `/tmp/opencode/backend-demo.log`、`/tmp/opencode/frontend-demo.log`。
- 已只读备份演示库至 `/tmp/return-ledger-demo-backup/spareparts_dev.before-return-ledger.dump`（权限600，601615字节），pg_restore --list 通过；尚未声称完整恢复演练。
- 演示已完成 `d2f8b4e6c9a1 → e3a7b9c2d4f6` 加法升级并启用 V2，其余功能开关未改。原有5条收货保留，8张相关表原有字段与行数逐表 hash 不变；证据 `/tmp/return-ledger-demo-backup/upgrade-result.log`、`facts-preserved.json`。没有清库或重新播种。
- 演示只读浏览器验收通过，`/tmp/return-ledger-browser/demo-check.log`：4条有效/5条全部记录，总量7、未关联4，两份已关联需求单分别为2和1；UI 为1.30.0，`errors=[]`。该核查没有导入或修改用户演示收货。
- 审查修复后再次刷新（当时HEAD=`796dd8f`）及只读验收通过：8表全部字段hash不变、5条收货保留、24组卡墙筛选隐藏数量恒等式通过、浏览器 `errors=[]`。证据 `/tmp/return-ledger-demo-backup/review-refresh-result.json`、`/tmp/return-ledger-browser/demo-review-filter-check.log`、`/tmp/return-ledger-browser/demo-review-refresh-check.log`；不执行新迁移或业务写入。
- 演示实际下载工作簿已核对包含 `06_领用返还`、`返还收货台账（只读）`、`98_字段说明`、`00_使用说明`、`99_元数据`。
- 本轮 QA 曾使用8001/5177和独立随机测试库；完成后已核对精确 cmdline 停止进程4175383/3868295，QA uv session39119 exit0并自清私库，正式演示8000/5176仍正常。需要再次验收时使用 `/tmp/return-ledger-browser/qa_server.py` 新建自己的隔离库；登录信息位于权限600的 qa.json，不输出或提交，截图目录同上。
- shellcheck 安装在 `/tmp/return-ledger-tools/usr/bin`。完整后端用 `PATH=/tmp/return-ledger-tools/usr/bin:$PATH ~/.local/bin/uv run --extra dev pytest -q`。
- Playwright 位于 `/tmp/return-ledger-browser/node_modules`，Chromium需 `LD_LIBRARY_PATH=/tmp/return-ledger-tools/usr/lib/x86_64-linux-gnu`。本地已装 CJK 字体修正截图缺字。
- 真实样表只存 `.opencode-inbox/`；不提交原件、客户行、凭据、令牌或数据库备份。

## 2026-09-14 本地演示域名修复

- 用户通过 `http://home.cloudlay.cn:5176` 访问时实际遇到 Vite Host 403。此前使用本机地址的浏览器验收未覆盖这个入口，不能把本机200外推成用户域名已可用。
- `8cc866b` 在 `frontend/vite.config.ts` 的 `server.allowedHosts` 中加入准确域名 `home.cloudlay.cn`；Vite已热重载。实际域名/本机均200，陌生Host仍403，构建通过。日志 `/tmp/return-ledger-browser/allowed-host-root-check.json`、`/tmp/return-ledger-allowed-host-build.log`。
- 此前 `04635f5` 的 [CI #674](https://github.com/Jinchen-Yang/it-spareparts/actions/runs/34765106251) 已前后端全绿（后端4494通过/7跳过/0失败，前端817通过，迁移及生产依赖审计通过），但普通合并被GitHub以405拒绝：至少需要一位具备写权限的审查者批准。后续配置修复提交的最新CI和审批状态，以 [PR #323](https://github.com/Jinchen-Yang/it-spareparts/pull/323) 为准；未关闭分支保护，未合并main，未部署生产。
- 真实DNS域名浏览器验收通过，未映射hosts：首页和入口模块200，登录后返还台账正常；页面/API/资源/请求错误为空，仅既有antd弃用警告。证据 `/tmp/return-ledger-browser/allowed-host-smoke.json`、`allowed-host-smoke.log`，没有导入或修改演示业务数据。
