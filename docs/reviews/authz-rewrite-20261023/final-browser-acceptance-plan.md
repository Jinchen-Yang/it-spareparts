# S0–S4 统一浏览器验收计划

状态：统一验收方案与数据准备完成，尚未执行最终候选浏览器验收。S3 精确提交 `c0396fc08f6d5084358ebf352ffedad717980274` 的 CI 40051216278 已全绿，S4 本地实现与交叉审查正在收口。执行对象必须为全部阶段完成、独立审查及完整 CI 通过的最终候选 SHA。代理浏览器调试、用户最终点按验收、合并和生产部署分别记录；本计划不授权生产。

## 1. 复用现场与启动前提

| 项目 | 本次核实 | 最终执行前必须补齐 |
| --- | --- | --- |
| 浏览器工具 | Node `v24.21.0`、`npx`、`/usr/bin/google-chrome` 可用；Playwright CLI 缓存版本 `0.1.22`；本会话未发现浏览器 MCP | 复用 `/home/cloudlay/.codex/skills/playwright/scripts/playwright_cli.sh`；包查询卡住时使用已核实缓存 `/home/cloudlay/.npm/_npx/31e32ef8478fbf80/node_modules/@playwright/cli/playwright-cli.js`。先真实启动、snapshot，再按当前元素引用操作；本次只查版本，未启动浏览器 |
| 隔离服务 | UI `127.0.0.1:15176/`、API `127.0.0.1:18920/health`、Cerbos `127.0.0.1:18921/_cerbos/health` 均 HTTP 200；PG 容器 `itsp-authz-migration-ram-pg` 映射 `127.0.0.1:32780` | 这些是旧 S2 常驻进程，健康不证明运行最终代码。确认进程归属后只重启本组服务，记录最终 SHA、schema revision、目录/策略版本、实际启用资源与 feature flags；不要终止其它栈 |
| 旧种子入口 | `/tmp/itsp_s2_browser.py` 使用独立 `authz_s2_browser` 库、`/tmp/itsp-authz-s2-browser-raw`、上述 API/PDP 端口及 `unified_modules` | 旧脚本含本地凭据且执行时会写种子并启动服务，不能直接当最终种子重跑。复用其隔离配置，按最终规范管理 API 配置合成账号/Grant；补齐下表事实。口令只留本地受限文件，不进入报告、截图、HAR 或终端输出 |
| 仓库既有运行工具 | `.claude/skills/run-it-spareparts/{dev-up.sh,seed.py,driver.mjs,shot.mjs}` 均存在 | 复用现有工具，不另建平行测试框架。默认脚本会建库/种旧账号，且不负责完整 Cerbos 配置；不要不带隔离参数启动。`driver.mjs` 只有旧基础读冒烟；`shot.mjs` 注入旧登录存储，不能证明登录、撤权或切账号正确 |
| 已有证据 | `output/playwright/s1-target-saved.png`、`s1-template-applied.png`、`s2-policy-only.png`、`s2-control-no-policy.png`、`s2-trend-only.png` 均存在 | 仅复用操作路径及合成数据思路，最终逐项重做。旧 S1/S2 报告和 `/tmp/itsp-s2-browser-trend-requests.txt` 不算最终候选结果 |

启动前使用最终候选的正常迁移/配置入口，并运行前端目录与后端策略 `--check`。不要导入生产备份或使用生产账号。真实登录后核对 `/me` 的 mode、主体、epoch、目录版本和 runtime 状态；未启用/暂停资源不能伪装为全部迁移成功。S3 Beta 发布开关与授权分开验证；S4 的 Agent、导入、普通账号资料/目录及边界注册表必须已实际接通才可开始最终矩阵。

## 2. 最小合成账号与事实

| 夹具 | 用途与可核对的预期 |
| --- | --- |
| 两个有效实名管理员 `accept-control`、`accept-recovery` | 均无商业 Grant；控制账号测试正常管理与本人撤权，恢复账号始终保留独立会话及安全管理能力。恢复通过正常管理命令，不能靠改 localStorage、伪造 token 或 SQL 绕过。普通目录、资料编辑、活动仍各需自己的 show/data，不由管理员身份附送 |
| 两个销售账号 `accept-sales-a/b` | 都明确负责客户 C1；C1 有跨销售人员、跨日期订单，另有未分配 C2、同名不同 ID 的 C3。授权建立后再通过合成数据正常导入增加 C1 新订单，验证未来新增订单自然纳入，不是只看授权时快照 |
| 最小能力账号 `accept-minimal` | 按矩阵逐次配置 trend-only、policy-only、inventory-only、summary-only、report-only 等整条 Grant；每次改权重新登录并记录配置版本，不能沿用旧 token。保留另一个零 Grant 账号用于拒绝对照 |
| 项目账号 `accept-project` | 实际负责人项目 P1、仅 canonical 销售关系项目 P2、无关系项目 P3；造同名不同 ID 项目与跨项目合同/子对象。为 P1/P2 分别配置完整合同，验证 D-03 不拼字段/范围，D-25 汇总不附送详情 |
| 业务标记与文件 | 使用容易识别的 PN、订单、数量、成本、客户和供应商标记；列出每个标记的允许范围。复用现有测试的合法工作簿/导入文件构造函数与服务入口；另保留未验证来源旧文件、跨 owner 文件、已撤权来源生成物；禁止把测试数据库直接当演示数据库 |
| 导入与聊天夹具 | 五个真实导入源、混合 job、独立报告/错误导出、费用作废输入；两个稳定 owner 的会话/文件及完整来源血缘。正常 SSE 需要可用的本地受控 provider 或明确获准的实际 provider；未配置提示只能证明未配置分支，不能算流式工具/回放成功 |

事实形状优先复用 `backend/tests/test_authz_sales_integration.py`、`test_sales_dashboard_d44.py`、`test_authz_s3_workspace.py`、`test_authz_s3_project_workbooks.py` 与 `test_authz_s2_agent_provenance.py`。pytest fixture 本身会清理测试库，不能直接对浏览器库运行；只复用构造语义及合法输入，最终保留一份不含口令的账号/对象 ID/预期标记清单。现存 S2 种子不足以覆盖这些夹具，这是待办，不是已就绪。

## 3. 连贯验收矩阵

以下全部为待执行。每行同时记录 UI、实际请求及返回投影；“按钮不见”不能替代后端拒绝证据，“200”不能替代实际内容核对。

| 编号 / 阶段 | 实际操作与组合 | 通过标准 |
| --- | --- | --- |
| A01 / S0+S2 销售范围 | A/B 分别登录 `/sales/analytics`；C1 订单跨销售/日期翻页、筛选、分组、趋势、详情、导出；增加 C1 订单后重读 | 两人均获得 C1 全部符合业务筛选的历史及新增订单；不因成交销售不同漏单。C2/C3 不进入候选、计数、图表或文件；筛选只能缩小范围，`all` 必须显式配置 |
| A02 / 原始下拉问题 | 销售分析与采购分析中的“销售人员”筛选、账号“负责客户”依次打开下拉→搜索→勾选多项→换搜索→移除已选→重置→确定；另测 Escape、点外部、键盘 Tab/空格、窄屏 | 复选框/完整已选名称可见；搜索不丢已选。未确定仅改草稿；重置清草稿和搜索，取消后恢复原值，确定仅提交一次。负责客户“确定”还需保存账号；候选分页不删除旧选择。空筛选不表示把账号客户范围扩大到全部 |
| A03 / S1 授权闭环 | 零 Grant → 显式 show+data → 撤 show → 撤 data；单独修改字段/范围；非法依赖保存；刷新与深链 | 无权第一帧没有旧事实或后台查询；仅 data 不冒出 UI，仅 show 不加载事实。依赖说明准确且不自动补权；被拒字段的筛选/排序/建议/导出也拒绝，不靠空字符串伪装 |
| A04 / S1 模板/账号 | 新建默认空账号；保存模板；显式 apply/sync 预览执行；保留/清除个人覆盖、空 actions、无模板清除；客户关系同步 | 模板保存不改既有账号；预览显示真实动作/字段/范围/来源；execute 与预览一致。清除恢复保存快照或撤销模块，不暗追最新模板；客户/项目关系不被模板覆盖；409 保留说明且不自动重放 |
| A05 / S1+S4 恢复与控制面 | 管理员无商业/税口径 Grant 深链 `/accounts`；普通目录/资料/活动分别赋权；普通资料注入 role/template；本人撤权/改密后关闭窗口；用恢复账号修复 | `/me` 挂起时不跳走；管理员只用最小管理目标发现，普通账号按独立 show/data。普通资料不能改安全身份；旧 boolean 写不能假成功。本人撤权成功立即清会话；第二管理员能正常恢复；最后管理员保护明确。口令不出现在预览/审计/截图 |
| A06 / S2 独立显示面 | trend-only；库存/型号数量且无税读取；policy-only 池管理；利润 part-only；公司池仅 summary 或独立交易块；quick pricing 单块；主数据/治理写门 | 独立 Grant 可用，不被无关页面/税口径总门吞掉。复合块按本资源字段判定；无权块没有业务请求或事实。池 locator 不伪装成员数 0，total=null 不画假总页数；显示门不充当后端数据门 |
| A07 / S3 六路由 | `/maintenance`、`/maintenance/analytics`、`/maintenance/dashboard`、`/maintenance/projects/:projectId`、`/maintenance/demands`、`/maintenance/replenishment`；managed rows-only、orders-only、manager-only 深链 | `requires_context` 仅开解析壳；实际 view allow 后才请求事实。summary/cards/analysis 与项目详情独立；workspace 六块允许单块，其余 null/受限而非零。manager 缺 project.read 提示关系不可核对且零写；有 project.read 无 workspace 仍可维护 |
| A08 / S3 工作流/文件 | 回款、费用、验收附件、现场领用、仓库、返还/残值/实收、需求/补库、来源分配、导入/总表；逐项实际授权视图→读/写/下载 | 真实对象与完整父链匹配；preview 零业务写；权限或归属改变后 403/409、无自动重放。更换文件/表单输入或关闭弹窗后迟到 allow 不发写。D-03 选择一个完整合同；保存回执可按当前权限复读，来源失效/未知 owner 文件不可读 |
| A09 / S4 导入 | 五源分别预检/提交；可预检提交但无状态/报告；已知 batch report-only/error-only；mixed job 仅部分来源；费用作废 | 正式 precheck 沿用 v2 content，上传使用自己 entry 的 proof。无状态权不轮询；报告/错误导出不强绑列表。partial 明确 restricted，隐藏顶层总数/状态/note 和 input_index，不把可见项成功称为全任务成功；作废 HMAC 与授权 proof 各自有效 |
| A10 / S4 Chat/SSE | 已知 session 深链只读/attach/cleanup；use+run 开新流；无 use/run 的已授权历史读取；正常 done、断线 attach、缺段、EOF、409 busy、撤权 | ready/连续 seq/terminal 闭集按唯一 wire；attach 清旧缓冲从 1 回放；缺段零内容，EOF 不假成功，任何 409 不自动 resubmit。授权/服务端错误清敏感 partial；清理动作不被已失效内容来源锁死。错误帧注入只算客户端对抗，另列真实服务端响应证据 |
| A11 / S2+S4 来源与文件 | 合法工具产物生成后撤销一个来源；改 actor 名称；另一 owner/admin 尝试 preview/download；切账号时下载迟到 | file Grant 不能替代完整来源授权；稳定 owner 改名不丢合法归属，跨 owner 不借管理员读。预览不偷取原件；迟到 Blob 不触发保存，旧 object URL 清理；未知旧血缘明确拒绝 |
| A12 / 全阶段时序和错误 | 慢请求期间 A→B、项目 P1→P2→P1、开窗→关闭、输入改变、搜索 debounce、权限刷新；PDP 故障后恢复 | 第一帧移除旧事实/候选/抽屉/portal；请求使用捕获 token+snapshot+query key，旧结果不落到新身份/对象。503 显示不可用，不退旧角色或假空数据；手工重试重新解析当前上下文，写与导出不自动重放 |

A02 的当前真实实现为 `components/CheckboxMultiSelect.tsx`，由销售/采购分析的“销售人员”筛选和 `pages/accounts/CustomerAssignmentEditor.tsx` 消费；采购分析的“采购员”仍为普通 AntD Select，不计作该复选框迁移面。已有组件测试提供草稿、搜索、重置、确认、失效反例，但不能代替浏览器的布局、焦点及真实网络验收。

## 4. 全面板覆盖与证据收口

1. 从最终 `catalog_manifest.json`、生成的前端 surface 定义及 boundary registry 对账，不沿用早期源码节点数，也不硬编码阶段候选数量。每个活跃 surface 记录真实路由/父面板、最小 show/data/字段、对象夹具、允许与拒绝操作、证据编号；共享嵌入面从实际宿主进入，不生成额外菜单。休眠/无当前 UI 的 surface 逐项注明原因及相应 API/文件证据，不能写“浏览器通过”。
2. 每个模块至少完成一个独立最小 Grant 的实际路径；每个活跃面板完成允许/拒绝状态核对。读、写、导出、文件、后台任务按真实能力分别覆盖，不能只用一个全权账号巡菜单。上述 A01–A12 是跨模块关键组合，不替代逐 surface 台账。
3. 在同一 Playwright CLI 会话工作流记录登录身份标签、候选 SHA、surface/对象 ID、可见文本、截图和脱敏请求 method/path/status/次数；S3/S4 同时记 view→formal、revision 与输入是否一致。写后从正常读接口验证实际结果，拒绝写核对零变化。不得存口令、Authorization header、完整 token 或含敏感值的原始 HAR。
4. 证据继续放 `output/playwright/`，按最终候选短 SHA/案例/身份命名；响应摘要沿既有 `/tmp/itsp-...` 习惯保存。固定失败日志和修复前后同一操作，避免用重试遮掉首帧问题。故障注入、真实服务端错误、代理点按、用户点按分列。
5. 代理检查通过后给用户一组本地候选入口、合成账号安全取得方式和精简点击顺序：A02 → A01 → A03/A04/A05 → 各业务模块 → A09/A10/A11 → A12。用户最终验收前保留恢复管理员、隔离库及原始合成文件；记录未通过/未执行项，全部闭合后才报告可验收结果。远端用户无法访问本机回环地址时，由 root 安排已有安全隧道/候选入口；不临时开放公网端口。
6. A05 的恢复指撤销普通业务/目录 Grant 后，健康快照下的实名管理入口；PDP 故障导致 `/me` 整体 503 时，UI 仍不可用。独立管理 API 的可用性另列后端证据，不能称为浏览器故障恢复。补跑逐资源兼容：S1 仅 sales 切换时目录/资料 legacy 能力保持；其本身 enabled 使用 show/data，suspended 不恢复旧允许。

当前前提缺口：最终 S4 实现与精确全量 CI、最终栈重启与目录版本核对、上述完整合成夹具、可验证正常 SSE 的 provider、用户访问候选的方式均未在本次完成。本次没有浏览器通过结果；仅验证既有本机工具/服务可达及制定复用计划。
