# Changelog

## 2026-09-13 — v1.30.0 返还收货台账完整接续

**Agent:** Codex（主任务与 core / frontend / import 并行子任务）
**依据:** 已批准实施计划 `docs/superpowers/plans/2026-09-11-site-issue-display-and-return-ledger.md`、D-17；接手 HEAD `52278b2`。

**Before:** 台账和界面只有初版，刚改的事务代码未验证；工作簿返还要求未完整同步，标准样表异步导入未完成；全量实际有业务与环境夹具失败。旧日志误称已推 main、CAS/失败审计完成及所有失败都是环境原因。

**After:**

- 后端按真实实收事实提供登记、清空、更正、转移、作废、项目/需求单汇总与审计；全终态校验、行锁/CAS、原始幂等命令重放与锁后项目范围复核，失败审计覆盖依赖/入参拒绝并独立持久化。
- 标准入库单走专用异步 doc job，完整原件归档、CRC与来源身份核验、分类预览、显式更正、失效令牌拒绝、整批回滚、取消/租期/原件重试；同项目/PN/数量/业务日期的手工疑似重复须独立确认，绝不凭相似接管原记录。
- 整机父固定1台、组成明细可追溯且不计数；小数保留源值、标待审且仍计数。旧坏品类别/件况边界保留，其有效收货更正/作废仍影响事实结果。
- 领用查询增加当前主数据、现场备注和服务端返还要求；V2 是/否/清空与网页、事件、旧原生义务一致。V2 导出附带签名保护的实际收货只读快照，明确层级和待审状态；修改免返不改变实收。
- 卡墙新增实际收货/有效WBDD备件数量比例，完整性不足不发百分比，超过100%保留真实值；未归属汇总行明确为空，不破坏返回字段一致性。
- 正式需求候选、workspace、领用查/废及返还导入随 boss 总闸，旧草稿/确认等流程仍受 Beta 守卫；无新增账号授权。
- d2 修正重复引号数据库默认值，e3兼容已安装旧d2；带旧事实升级及全链零漂移已验证。存在不可保留的登记/来源证据时降级明确拒绝，不删除用户事实。
- 前端完整登记/转移/审计/中文预览与独立确认；修复慢响应、取消轮询及重试丢响应时的确认残留。版本升为1.30.0。

**Commits:**

- `c175bb8`：固定多合同夹具、测试文件权限与 ShellCheck 可执行路径。
- `0bb182e`：后端数据、迁移、导入、权限、汇总与真实并发测试。
- `e84fcaa`：前端台账、导入、领用同步与1.30.0版本。
- `b3de27c`：历史迁移测试验证单头祖先关系，允许正常线性追加。
- `e98ba71`：交接、接口和已确认业务规则文档；集成分支已推至远端，PR尚未创建。

**验证:**

- 当前前端最终全量 **78文件817项通过、exit0**；`tsc && vite build`通过。日志 `/tmp/return-ledger-frontend-full-final.log`、`/tmp/return-ledger-frontend-build-final.log`。
- 当前导入最终专项 **54 passed / 0 skipped**，含原件及四类真实并发、库存/成本/WBDD/前置库全行快照不变；日志 `/tmp/return-receipt-import-complete.log`。
- 真实原件12986主单/107651明细：隔离参考归属初始2条标签冲突→整批0写；明确治理隔离参考项目且不改原件后→501条/数量501。该归属由测试构造，不证明生产归属已治理；原件与归档hash一致。
- 核心权限/事务44项及反向转移撤权2项通过；迁移/原生快照/旧消费者48项通过；全新独立库升级→alembic check无漂移→唯一e3头通过。
- 浏览器真实点按：手工登记5→改3/清空→历史→作废；小数导入1.250→改备注→重传仍1.250；整机父1/组成明细/原件一致/跨项目转移及WBDD汇总均通过、0脚本/API错误。工作簿只改是→否，网页与重下载一致，7件实收保持不变。
- 首轮完整后端：**4 failed / 4457 passed / 8 skipped，2248.92秒**，`/tmp/return-ledger-backend-full-final.log`。4项卡墙未归属行同构字段和历史迁移固定链头问题已修，并分别11项、7项复验通过；不改写这轮失败结果。固定 `b3de27c` 代码后的第二轮完整后端最终为 **4482 passed / 7 skipped，0 failed，2427.75秒（40分27秒），exit0**，`/tmp/return-ledger-backend-frozen-full.log`。
- 7个跳过节点已用最终4489节点收集结果逐位核对：型号夹具缺失的overview回读、发布主机二进制 opt-in、旧收款提醒样表、生产规模性能基准、FK阻止构造孤儿数据的防御路径、可选返还原件、真实Chrome发布主机管道。只有可选返还原件为本次新增，它已在54项零跳过专项中实际运行；其余6个跳过条件原已存在main，不能把未执行的性能/防御路径称为已验证。
- Nginx故障日志检查按CI条件单独实际运行 **1 passed，76.65秒，exit0**，`/tmp/return-ledger-nginx-runtime-check.log`；测试按固定digest拉取镜像后，最终全量中的同项也实际执行通过，未再跳过。
- 后端生产依赖审计无已知漏洞。前端联网依赖审计被自动审批拒绝，已单独向用户请求该元数据发送授权；离线audit经本机npm源码核实会直接跳过，不能作为通过证据。

**交付与环境:**

- 本轮保留接手时19笔既有集成历史，新增按后端/前端/回归分组的提交；计划原分阶段PR调整为完整integration PR，避免把未独立验证的旧前置树当作已验收版本。只以最终树作为交付候选。
- GitHub实时main仍为e0c80b0且protected=true，两个必需CI检查存在；详细保护端点无读取权，不能推断审批要求已取消。PR321仍open，等价tree已核实，未宣称已远端合并。
- 本地演示库备份后仅升级d2→e3，8张业务事实表原有全部字段哈希逐一相同，保留5条收货；指定旧backend PID已优雅停止，新进程17688，启用V2工作簿供演示。未部署或导入生产。

---

## 2026-09-12 — 卡墙实际返还率（历史局部记录，已纳入下述接续提交）

- Before：项目卡未展示统一收货台账与需求备件数的返还比例。
- After：新增 `receipt_return_rate`，按项目批量汇总有效收货与全生命周期有效 WBDD 数量；卡片显示“坏件返还率”和实际分子/分母，明确包括所有件况。
- 原因：用户要求“已返还（好件/坏件/其他）÷维保需求单备件数”，无需 PN 匹配；分母不完整不展示百分比。
- 验证：新增后端 2 项、原前端卡片 17 项通过，tsc 通过；演示 API 返回 7/42=16.7%。尚未合并或部署生产。

> 记录每次 AI Agent 或开发者的代码变更。格式：日期 + Agent + 任务 + 变更文件 + 测试 + 备注。

---

## 2026-09-12（历史记录，2026-09-13 纠正：三方合流仅在本地）

**Agent:** OpenCode (cloudlay-3080)
**Session:** 计划 §0 三方内容合流至本地 `integrate/2026-09-12`；此前“推 main”宣称不实。2026-09-13 实时远端 main 仍为 `e0c80b0`，后续交付以本文最新条目为准。

**Changed:**
- **merge 1**：`cd3abfe` 八笔（业务类型筛选三件套 / 03e8b8f 04-06 rebase 值修复 / 5f63077 收款单块铺开 / 9c32987 连坐 / 501d01c 报销跨页冲突）——干净合入，无冲突
- **merge 2**：PR #321 内容（现场领用自动编号 / Excel 数值日期；本地等价分支 `ee31fa6`，tree 与远端 `cccf342` 完全一致，见 memory/site-import-pr321-release-pending.md）——三处冲突手工解：
  - `frontend/src/version.ts` + `version.test.ts`：APP_VERSION 取 1.29.0，CHANGELOG 双条目并存（1.29.0 前 1.28.1 后），1.28.1 测试改按版本号查找
  - `maintenance_project_master_workbook.py`：**同机撞车和解**——双方都在 merged_row 后重解析基线字段；保留 03e8b8f 的作废优先注释块 + 采用 #321 的 `epoch=ws.parent.epoch`（空值守卫冗余，_v2_date/_v2_decimal 自身 None 安全）
- **merge 3**：`feat/return-receipt-ledger`（step-2 台账）——链头断言测试冲突解：统一 `d2f8b4e6c9a1`；receipt_ledger 测试采用 #321 引入的动态 `get_current_head()`（以后无需再钉常量）
- **迁移重挂**：`d2f8b4e6c9a1.down_revision` `b7d3f9a1c5e2 → c9e5a1b7d3f8`；**100 条迁移从零重放通过、alembic check 零漂移、单头 d2f8b4e6c9a1**
- 期间发现并修复：早前测试库版本戳与 schema 错位（d2f8 曾在旧 parent 下应用）→ 重建 `spareparts_test` 库全链重放验证

**Tests:**
- 前端：`780 passed`（74 文件）+ `tsc && vite build` 绿
- 后端全量：见本条目 SHA 回填处（跑完回填结果）
- 迁移：100 条零重放 + 零漂移 + 单头 + downgrade 往返

**Notes:**
- 纠正：author=merged_by 不能证明分支保护已关闭；未执行已验证的 main 推送，不据此绕过 PR、CI 或审批，也不自动回滚他人远端变更。
- 纠正：复核时 PR #321 仍 open。仅验证本地 `ee31fa6` 与实时 `refs/pull/321/head` 的 tree 等价，并未证明远端已合并或关闭。
- 部署不在本次授权范围——合流后停下等用户单独发令。

**Agent:** OpenCode (cloudlay-3080)
**Session:** 返还收货台账 step-2（数据与服务层）——计划 `docs/superpowers/plans/2026-09-11-site-issue-display-and-return-ledger.md` §4.3 第 2 步
**Commit:** `4767e88`（分支 `feat/return-receipt-ledger`，自 origin/main `e0c80b0` 切出）

**Changed:**
- `backend/alembic/versions/d2f8b4e6c9a1_return_receipt_ledger.py` — **迁移（单头，parent b7d3f9a1c5e2）**：`maintenance_rkd_return_line` 放开 batch/head NOT NULL（容纳手工行）；新增 `source_order_id`(FK f_maintenance_order.raw_order_id)、`source`、`line_status`、`version`、`description/note/evidence_ref`、`created_by/updated_by/updated_at/voided_by/voided_at/void_reason`；6 个 CHECK（source 枚举/状态枚举/版本/source 形状互斥/void 形状互斥）+ 活跃行 (project, demand) 部分索引。原实现 downgrade 会先删手工行再恢复 NOT NULL；2026-09-13 已改为存在不可保留事实时明确拒绝降级，禁止删除收货记录
- `backend/app/models/maintenance_doc_import.py` — `MaintenanceRkdReturnLine` 扩展为统一台账模型（与迁移同构），docstring 记录口径
- `backend/app/services/maintenance_return_receipts.py` — **新服务**：登记（项目必选/wbdd 可选且校验 active assignment 归属一致/PN 必填/正整数/件况枚举 成品·坏品·废品 或留空）、修改（版本 CAS + 前后值审计 + 项目转移双写审计）、作废（必填原因、不物理删除、退出有效统计）、检索、汇总（Σ需求单+未关联=项目总量 **不变式断言**）、审计读取；`legacy_bad_return_filter()` 共享旧口径冻结过滤器
- `backend/app/api/maintenance_return_receipts.py` — **新 API（非 Beta，生产可达）**：`GET/POST /maintenance/projects/stable/{id}/return-receipts`、`GET .../return-receipt-summary`、`PATCH /maintenance/return-receipts/{id}`、`POST .../void`、`GET .../audit`；写走 `action_maintenance_bad_return_manage`；原实现仅部分 handler 失败调用 record_access_log，不能证明请求校验和依赖拒绝已持久化；2026-09-13 补独立事务 SysAuditLog
- `backend/app/main.py` — 注册新 router（无 Beta 闸）
- `backend/app/services/maintenance_bad_returns.py` / `maintenance_boss_facts.py` / `maintenance_analytics.py` / `maintenance_recovery.py` — **旧坏件口径冻结（先并存后切换）**：四个消费方统一加 `source='rkd_import' AND test_result∈RKD_RETURN_TEST_RESULTS AND line_status='active'` 过滤器，当前行为零变化，防后续「件况全收」导入与手工行漂移旧分子
- `backend/tests/test_maintenance_return_receipts_api.py` — **12 个新测试**：未关联登记/补选不重复计数/跨项目 wbdd 拒绝/入参校验/CAS+审计前后值/作废+复作废/幂等重放/权限 403+读放行/件况不影响总量/旧口径冻结/审计端点/PN 解析 part_id
- `backend/tests/test_maintenance_receipt_ledger_import.py` / `test_maintenance_salesperson_override_migration.py` / `test_maintenance_wbdd_display_columns_migration.py` — 链头断言更新 `b7d3f9a1c5e2 → d2f8b4e6c9a1`（线性追加惯例，同 #321 修法）
- `.gitignore` — 加 `.opencode-inbox/`（真实样表永不入 Git）
- `docs/superpowers/plans/2026-09-11-site-issue-display-and-return-ledger.md` — 完整计划（口径已全部拍板：项目必选/需求单可选/PN 不匹配原领用/按数量统计/可改可废/审计；§5.2 三细节已定）

**Tests:**
- 新增 12 + 迁移断言 15 = `27 passed`（含迁移升级/降级/单头/零漂移：upgrade ✅ check ✅ heads ✅ downgrade→upgrade 往返 ✅）
- 历史后端全量：`4294 passed, 14 failed, 7 skipped`。此前“全部环境固有、与改动零关联、CI 不受影响”的归因没有充分证据，撤回。接续集成实际为 `4373 passed / 12 failed / 7 skipped`；后续逐项定位了多合同夹具不确定选择、测试文件 umask 及 shellcheck 路径问题，以最新完整回归为准。
- 前端未改动（本 PR 纯后端，页面在 step-3）

**Notes:**
- 口径依据：2026-09-11 拍板（计划 §2/§5.2）+ D-14 分子口径（RKD 返件类∈{维保拆旧返件,旧库退返}）背书
- ⚠️ 迁移 parent 与 PR #321 的 `c9e5a1b7d3f8` 同为 `b7d3f9a1c5e2`：若 #321 先合并，本分支需重定基并重挂迁移 parent（已知修法，见 memory/site-import-pr321-release-pending.md）
- §0 合流门槛（cd3abfe 八笔 + #321）由用户执行：本分支与其零文件冲突（cd3abfe 无迁移、不动 doc_import 域）


**Agent:** Claude Code (macOS，本地会话)
**Session:** v1.23 维保展示板生产发布（PR #254 → main `bd867a7`）＋ 发布阻断修复 ＋ 卡墙 R5 回归修复

**Changed:**
- `docker-compose.yml` — **发布阻断修复**：app 服务补透传 `MAINTENANCE_BOSS_DASHBOARD_ENABLED`（此前容器内恒 unset，发布 deploy 闸门必 FATAL、总闸永远翻不开；生产容器实测确认）。同步登记 `.env.example` / `backend/.env.example`
- `backend/tests/test_v120_release_control.py` — uv 供应链闸门测试兼容自托管 runner（`8fc3b9c`/`159efd5` 迁移后 setup-uv action 已移除，版本锁由 runner 侧二进制校验承担；main 上长期红）
- `.deploy/v122_collection_reminders_static_test.py` — 迁移头断言改为「单头＋v1.22 区间端点在链」（原写死 `c8e2a4f6b1d3`，被 v1.23 推进到 `d6e1f4a8c3b5` 后必失败）
- `.deploy/v123_maintenance_boss_release.sh` — 去掉 `compose run --no-build`（生产机 Ubuntu 打包版 compose 2.40.3+ds1 的 `run` 无此参数；发布时以 `unknown flag` 安全失败后打补丁放行）
- `docs/releases/v1.23-deploy-plan.md` — §0.1 发布前复核发现（6 项）＋ §3 补「构建新镜像」步骤 ＋ §4 全程执行结果回填
- `frontend/src/pages/maintenance/MaintenanceHomePage.tsx` + `ProjectCard.tsx` — **R5 回归修复**：卡墙筛选器补「期限缺失」档＋卡片「期限缺失」标签＋空态指引。生产 415 个项目 lifecycle 全 missing（台账未导入），原筛选器只有进行中/已结束两档 → 整面卡墙无声全空。默认仍「进行中」（#37 业务口径不动）
- `backend/app/services/maintenance_ledger.py` + 迁移 `e8b2c6f4d1a7` — **#50 项目周期从名称提取**（业务指示 08-17）：新增 `_period_from_display_name`（8 位日起止为主、支持 `-`/`~`/空格连接符与 6 位年月段）＋`_resolve_lifecycle`（台账权威、名称兜底）；迁移对存量 missing 项目一次性回填（纯数据 UPDATE 零 DDL，自包含解析副本）。生产名称干跑：373/415 回填（152 ongoing＋221 ended），42 保持 missing（真无周期/笔误）。两副本对全部 415 名称输出逐一验证一致
- `backend/app/etl/transform.py` — **报销导入头级字段块内继承**（生产批次 #168 实锤：97 行只进 14 行）：真实氚云导出是「头行＋明细延续行」结构（头行带单号/日期/人员/事由，同单明细行头级格留空「同上」），原「行行独立」口径把 83 行延续行全判 missing_date。现延续行继承头行的日期/单号/人员/事由/销售订单/流程状态；在途单（流程状态「进行中」、日期未生成）整块软标记「可忽略」不计硬错误（同 empty_pn_inactive 惯例），审批完成重新导出上传自然计入；文件开头孤行与「有单号没日期的已生效块首行」仍硬错误，绝不跨块继承。真实文件验证：71 行入库＋26 行在途软标记＝97 行全有归宿、0 硬错误
- **#51 接回 v1.17 老版数据链**（业务指示 08-17：现有 XSDD/WBDD 数据即可确定名称/期限/合同额，不必等台账；参照老版 ProjectCostPage/projects_aggregate 口径）：①迁移 `f3b5d7c9e2a4` 给 `maintenance_project` 加 `period_from/to`（纯加法），WBDD 挂靠聚合回填（覆盖 411/415）→名称解析兜底，lifecycle 按期限重算；②boss-board 合同额两层取数——台账合同优先，缺位回退 XSDD 聚合（生产覆盖 402/415），带 `contract_shared`/`contract_incomplete` 诚实标注；③面板显示维保期限并支持编辑（#39 落地，PATCH 校验起止、按新期限重算 lifecycle、乐观锁）；④修复面板编辑表单三处断头（cmo_name/salesperson 后端不收、response shape 错配、RangePicker 无回填）；⑤台账导入侧 `_resolve_period`（台账权威，缺位不清空回填值）。浏览器端到端验证：期限显示/编辑/重算/回读全链路 200

**生产发布（2026-08-17 上午，relay-vps）：**
- 生产代码 `ab42005` → `bd867a7`（一次性追平 main，跨 143,665 行）；DB `c8e2a4f6b1d3` → `d6e1f4a8c3b5`（15 条迁移 2.4s）
- 七阶段闸门全过：preflight → backup（39MB，`pg_restore --list` 988 条目验证可恢复）→ migrate → deploy（停机约 3s，总闸读回 false）→ canary（读回 true，展示板端点 404→401）→ observe 30 分钟（6 次采样全绿）→ commit-release
- `.env`：`MAINTENANCE_BETA_ENABLED` → false；新增 `MAINTENANCE_BOSS_DASHBOARD_ENABLED`（canary 后 true）；`REPLENISHMENT_BETA_ENABLED`／`MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED` 保持 true（后者与审计 §2 2b 的偏离已记录，业务拍板保持）
- 权限：91 账号＋7 角色模板两个新键全部 fail-closed 回填 false（生产读回核验）

**验证：**
- 后端 CI `3529 passed`（70 分钟；一次 `test_pytest_run_isolation` 偶发＝main 既有、重跑过；一次 runner 断网）；前端 tsc/build/vitest 绿
- R5 修复：相关 12 文件 98 tests 绿 + tsc 干净

**Notes:**
- 存档 issue：#255（凭证落盘顺序 P1，beta 闸后不可达）、#256（对账分页 P2，同）、#257（`LLM_MAPPING_EXTERNAL_ENABLED`/`ENABLE_AGENT` 未透传）
- 演练脚本用 `docker run -e` 绕开 compose，因此 compose 层缺陷（透传缺失、参数不兼容）演练测不到——后续发布前置检查清单见 deploy-plan §4.3
- 台账首次导入生产仍未完成（plan M2-4 验收项）：完成前卡墙默认「进行中」为空属预期，项目在「期限缺失」档可见

---

## 2026-08-13

**Agent:** Claude Code (WSL Ubuntu 26.04)
**Session:** 维保业务工作台重构 → 生产审查修复（分支 `codex/maint-workbench-refactor`，基线 `origin/main@caf4a973`）

**Changed（11 commits，58 files，+8751/−259）:**
- 导航重构：两个同名"维保管理"拆为"维保项目（旧版）/ 维保工作台 / 维保数据维护"三组，业务化标签；betaFeature 白名单门控原样保留
- 需求单项目范围隔离（PR2）：复用 main 的 `owned_project_ids`/`MaintenanceSourceOrderAssignment`，搜索 + 删除意图双重范围校验，越界 403
- 业务文案（PR3/PR6）：maintenanceLanguage 模块 + 卡片/进度条/成本回填/迁移页面业务化；`dry-run→预检`、`manifest→技术依据`、`回填→补录`
- 采购链只读面板（PR4）：稳定归属表优先、唯一名称兜底、ACTIVE_STATUS 过滤、定点金额序列化
- 氚云项目导入（PR5A/5B）：真实脱敏样表字段契约 + preview/apply API + 前端四步向导；全量行存储、幂等 apply、409 冲突、流式 10MB 上限
- 独立审查 23 项发现全部修复：router 未注册、xlrd 生产依赖、迁移 revision 冲突（f9b2d4e7c1a6）、预览截断、apply 幂等、display_name 泄露、无界读取、fb token、N+1、作废单过滤
- `.deploy/v122_release.sh` + `v122_manifest.py`：发布/回滚控制（FROM d9f1a3c7e5b2 → TO f9b2d4e7c1a6），rollback-app 保留回滚排练门

**验证：**
- 前端 790/790 tests 绿、tsc + vite build 绿
- 后端 pytest 全量（等结果）+ 新增 11 个专项测试（范围隔离 7 + 导入契约 4）
- 迁移单 head：f9b2d4e7c1a6

**Notes:**
- 生产发布门：合并后需以 exact-SHA 建发布候选，执行 v122 preflight → backup-restore → migrate → deploy → observe，回滚走 rollback-app（旧镜像需先在 f9b2 上排练）
- 遗留：#128 待办页/验收矩阵完整版（PR6 余项）随真实账号验收推进

---

## 2026-08-12

**Agent:** Claude Code (WSL Ubuntu 26.04)
**Session:** Plan-First 协议 + .ai/ 体系补全

**Changed:**
- `.claude/skills/plan-first/SKILL.md` — 新建 Plan-First 开发协议 skill（Claude Code 版）：问题/目标/路径/验收/风险 五段式计划模板 + 审批门禁 + 留痕闭环
- `.opencode/PLAN_FIRST.md` — OpenCode 版 Plan-First 协议
- `.codex/PLAN_FIRST.md` — Codex 版 Plan-First 协议
- `CLAUDE.md` — 新增 Plan-First Protocol 强制门禁段，引用 skill 文件
- `.ai/AI_WORKFLOW.md` — Phase 0 Step 4 改为五段式计划模板，引用 plan-first skill
- `docs/维保管理字段业务化改造方案.html` — 全量字段业务化改造方案（5 页面 + 全局术语）
- `.ai/CHANGELOG.md` — 本次变更记录

**Notes:**
- 三个 skill 文件为三平台独立副本，核心逻辑相同，格式适配各平台
- Plan-First 协议触发条件：>1 文件、新功能、Bug 修复、重构；例外：单行 typo、格式化、用户说"直接做"
- 同步完成架构文档审计：现有 .ai/ARCHITECTURE.md + DATABASE_DESIGN.md 中所有数字均与实际代码不符，需后续全量修复

---

## 2026-08-11

**Agent:** OpenCode (WSL Ubuntu 26.04)
**Session:** 网络恢复 + #227 找回 + #228 P1 修复

**Changed:**
- `~/.bashrc` + git 全局配置 — 持久化 mihomo 7897 代理（GitHub DNS 污染/直连超时，经代理恢复 gh/curl/git）
- 推送 3 个服务器独有分支到 GitHub：`codex/replenishment-cart`（`bce7deb`）、`codex/issue201-formal-review`（`eef2933`）、`codex/fix-maintenance-return-rate-lock`（`895637c`）
- **#227 找回** `8fd395a`：经 `cloudlay@ddns.cloudlay.cn` SSH 旧服务器，在 `/tmp/it-spareparts-artifact.m1cbgs` worktree 找到该 commit，bundle 传回并恢复分支；验证 68 tests + Ruff(0.14.6) + format + py_compile + diff check 全绿；推送 origin 后 PR #239 head = `8fd395a`，CI 全绿
- `backend/app/agent/workbook_cleaning/models.py` — **#228 P1-2**：新增 `ProposedValueSnapshot`；Assessment 容器（field_diffs/risk_flags/manual_review_reasons）镜像上限 + 唯一性；binding 模型加 max_length/唯一性；`change_count` 与 field_diffs 长度一致性校验
- `backend/app/agent/workbook_cleaning/kernel.py` — **#228 P1-1**：kernel 强制 `proposed_after` 与可信快照值一致（`proposed_value_mismatch`），拒绝未绑定/多余快照（`unbound_proposed_value_ref`/`extra_proposed_value_snapshot`）
- `backend/app/agent/workbook_cleaning/__init__.py` — 导出 `ProposedValueSnapshot`、`SourceEvidenceBinding`
- `backend/tests/test_workbook_cleaning_proposal.py` — +3 回归测试（快照绑定、快照覆盖、Assessment 容器上限）
- 创建 Draft PR #242（#228 修复），CI 前后端全绿

**Tests:** 33 focused tests 全绿（#228）；68 focused tests 全绿（#227）；PR #242/#239 GitHub CI 前后端全绿

**Notes:**
- ruff 0.16.2 对 #227 报 6 个 I001 为版本差异（服务器当时 0.14.6 通过），不改代码保持精确 SHA
- 旧服务器 tailscaled socket 在 `/tmp/tailscale.sock`；SSH 可用 `cloudlay@ddns.cloudlay.cn`

---

## 2026-08-11

**Agent:** OpenCode (WSL Ubuntu 26.04)
**Session:** 制定开发留痕协议（Traceability 硬性要求）

**Changed:**
- `.ai/DEVELOPMENT_RULES.md` — 新增第 0 章"开发留痕协议"：4 类痕迹（任务/变更/提交/架构）、可回溯判定标准、0.3 收尾自答检查（原有状态/变成什么/为什么/是否架构变动/影响面）、禁止项清单
- `.ai/AI_WORKFLOW.md` — Phase 0 Step 4 增加"原有状态登记"；Phase 2 更新文档步骤改为硬性留痕（CHANGELOG 必须含 before/after/原因/验证/commit SHA）；Handoff 协议增加"留痕缺失先补齐"条款
- `.ai/AI_REVIEW_CHECKLIST.md` — "Documentation" 拆分为 "Documentation & Traceability"（7 项硬性检查）；Git 部分增加 commit SHA 回填检查
- `CLAUDE.md` — 新增 "Traceability (hard requirement)" 章节（5 条），所有 AI Agent 在 After Coding 前必读

**Tests:** 无代码变更，纯规范文档

**Notes:** 规范生效后，任何无 CHANGELOG/ADR 记录的变更视为未完成，Code Review 打回。commit `4e5ae474`

---

## 2026-08-10

**Agent:** Claude Code (VSCode Extension, Windows)
**Session:** 开发环境迁移

**Changed:**
- 创建 `.ai/` 项目管理系统（14 个文件）
- 创建 `CLAUDE.md`（AI Coding Rules）
- 创建 `.env`（dev 环境配置）
- 更新 `backend/spareparts_backend.egg-info/SOURCES.txt`（uv sync 自动生成）
- 更新 `frontend/package-lock.json`（npm install 自动生成）

**Tests:** 后端 2375 passed / 5 skipped；前端构建通过

**Notes:**
- 基线分支 `codex/maintenance-manager-combined` 已推送 GitHub（HEAD `3dbc9dc`）
- GitHub `workflow` scope 已授权
- 从旧服务器同步了 4 个 codex 独有分支 + worktree refs + .codex AI 资产
- 安装 Claude Code 2.1.226 + OpenCode 1.18.16
- 迁移 43 skills + 38 memory 文件到 WSL
- 旧的交接包 zip SHA256 验证通过（`d5b21765...c2f06`）

---

## 2026-08-09

**Agent:** Claude Code (cloudlay-ubuntu server)
**Task:** 开发交接打包 (#240, #241)

**Changed:**
- `docs/handoff/2026-08-10-development-handoff.md` — 开发交接文档
- `docs/handoff/SECRETS-AND-SERVERS.md` — 安全交接说明
- `3b4af2a` docs: record GitHub handoff status
- `44458dc` docs: add development handoff package

**Tests:** 完整通过（2754 passed, 5 skipped, 登录构建绿）

---

## 2026-07-30 — 2026-08-09

**Agent:** Claude Code (cloudlay-ubuntu server, multiple sessions)
**Task:** 维保 Beta 集成组合 (#204 #205 #206 #207 #208 #209 + replenishment + migration)

**Changed:**
- 107 commits on `codex/maintenance-manager-combined`
- 维保项目工作台、工作簿 v3、验收、现场领用、坏件返还、WBDD 删除、成本迁移、补库购物车
- Beta 白名单、总闸控制、v1.21 发布控制
- 7 个 merge migration 保持单 head

**Tests:** 维护阶段全量绿

---

## 2026-07-15 — 2026-07-26

**Agent:** Claude Code (macOS + cloudlay-ubuntu)
**Task:** AI 助手 P0-P1 + 项目合同基础 (#196 #198 #200)

**Changed:**
- `backend/app/agent/` — AI Chat 引擎（6 文件）
- 10 个 Agent 工具 + 4 个 Skill Playbook
- Vision OCR 文件识别
- Chat 持久化（PostgreSQL）
- 维保项目主档与多合同聚合
- 项目操作工作台

**Tests:** Agent 5 个测试文件 + 维保核心测试

---

## Template (for future use)

```markdown
## YYYY-MM-DD

**Agent:** [Claude Code / OpenCode / Cursor / Human]
**Task:** [简短描述]

**Changed:**
- `path/to/file` — 变更说明

**Tests:** [通过/失败/X passed Y skipped]

**Notes:** [注意事项]
```
