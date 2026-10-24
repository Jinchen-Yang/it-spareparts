# S3 Project / read / finance 实施证据

逐入口真值见 `s3-project-read-finance-implementation-coverage.json`，由同目录 `update_s3_project_coverage.py` 生成。原 94 个入口全部已有实际接线与语义测试：本作者 87 个；root 独占的 7 个全局文件/重算入口也已按独立证据闭合。仅登记 registry 不计完成。另两个负责人命令及经理目录共享 facade 已有真实 HTTP/PDP 测试。

已完成的重点边界：project / workspace / analytics / dashboard / boss-board 查询在 count/page/聚合前施加真实 SQL policy；同 resource 的字段控制实际 SQL 投影；六块 workspace 独立 OR 准入和独立字段；所有 canonical 写在完整领域锁后取 actor SHARE 并重验；D-05 canonical 手工建项固定拒绝且零写入。

最近新增证据：

- source-sales 成本与汇总六入口使用自己的 `is_source_sales` 查询族；own_source_sales 不附送同项目其他来源的合同事实。读与 CSV 在同一 RR 内投影，隐藏 cost SQL 列不读取，流式导出发送前及每 chunk 重验 lineage。`/tmp/itsp-s3-source-cost-regressions.log` 76 passed；新增 CSV 与旧导出合同 `/tmp/itsp-s3-source-cost-exports-final.log` 54 passed。
- 合同写预收完整 resync 领域锁再 actor，实际第二连接撤权与原业务回归 `/tmp/itsp-s3-contracts-lock-final.log` 70 passed。
- 业务类型回填完整输入与实际空集准入 `/tmp/itsp-s3-project-backfill-final.log` 14 passed。
- boss 卡片/订单不计算或暴露全库 unlinked count。源是否导入仅为 availability metadata；不明完整性为 `partial`、`unlinked:null`。真实 SQL 捕获、经理目录独立 Grant/精确 authview、旧 health 和查询数回归 `/tmp/itsp-s3-board-health-directory-final.log` 37 passed。
- Master 独立两个反例见 `s3-master-independent-review.md`，不是本包入口完成数。

这些数字对应各日志的实际运行集合，集合有重叠，不相加为唯一测试数量。当前未宣布 S3 CI 或生产完成。

回款提醒+凭证5入口新旧回归 `/tmp/itsp-s3-reminder-evidence-first.log` **50 passed**：真实角色无隐式allow、不同资源利润不串借、目录SQL scope在count前、幂等重放按当前字段、真实领域锁等待期间撤epoch零写、上传授权视图零写、实际bytes更换409、文件故障不关闭节点/不提交DB。原件只存稳定owner与origin metadata；现只有凭证metadata读，无下载HTTP路径，不新增通用下载旁路。


新增 collection plan 4 入口（70/94）：新9条真实PG/PDP与原36条业务回归共45 passed（`/tmp/itsp-s3-collection-plan-final.log`）。预检零写、actual bytes/key绑定；原始上传件只登记真实batch侧车，不伪造operation_input持久来源。raw独立all_imports+F、sameRR文件SHA/size；缺历史侧车拒绝。应用保留整批，完整当前旧/新项目合同锁后actorSHARE/PDP，真实等待期间撤epoch全回滚。发布侧车失败清理仅本次字节；错误文件仍按原业务保留受控error批次。剩24入口，其中global7已交root，本站点operations/siteissue17继续实现。


### Site issues, global repairs, and original-file publication

- Site issue 15 actual API entries now use `maintenance_site_issue_access`. Eight new real PG/PDP cases plus 159 original business cases passed (167 total, `/tmp/itsp-s3-site-issue-regression.log`). Tests preserve real manual and delivery-backed writes, line/header corrections, replay, exact child-parent checks, independent candidate actions, hidden-cost SQL exclusion, stale input/generation, and a second-connection epoch revocation after pending effects that rolls back issue/lines/receipt.
- Two global repair entries use `maintenance_operation_backfill_access`. Five real cases passed with one additional actual migration case for the optional demand `data_customer` field (`/tmp/itsp-s3-operation-backfills-seed-final.log`, 6 total). Repairs retain whole-command semantics: all actual targets must authorize; restricted grants never authorize unrelated global statistics. Real unparented raw rows remain actual subjects and require a true all-projects PDP result; verified empty commands still require the explicit action.
- Root independently reproduced raw collection-plan bytes being released after audit-time revocation. The reader now returns materialized bounded bytes plus its own live carrier lineage; shared `stream_materialized_bytes` checks before headers and every 64 KiB chunk. Original independent red case plus nine original package cases pass (10, `/tmp/itsp-s3-collection-plan-publication-final2.log`). Existing uploaded-file sidecar and current-reader contract remain distinct from generated producer provenance.
- Whole repair input hashing is conservatively over the closed affected table families. This deliberately invalidates on unrelated same-family changes; it is not a performance claim for production-sized tables.


新增受委派 master 3 入口：global spare-part-lines validate/apply 与 collection-milestones restore。全文件 C/F/K、每个真实上传行（含未变化行）的当前父集合、完整输入 SHA、旧/新领域锁后当前 actor/Grant，以及 restore 实际 milestone+contract 槽位。新 6 项 + 旧 50 项真实回归 56 passed，`/tmp/itsp-s3-master-remaining-regression.log`；属于 workflow 分母，不重复计入原 94。Astra 独立复核另记其报告。

原件发布最终扩展：collection-plan 读取物化后 sidecar 被删除的独立红例已修；当前 reader 的 reference 必须再核实际不可变 binding。collection 11 + global 独立 2 共 13 passed，`/tmp/itsp-s3-publication-independent-final.log`；公共 spool 抽取后的组合 21 passed，见 `s3-global-independent-review.md`。


最终 root 7 入口核对：5 个全局文件读取与 recompute 的作者真实测试位于 `backend/tests/test_authz_s3_global_workbooks.py`，本作者独立测试 `backend/tests/test_authz_s3_global_independent.py` 覆盖 render 后真实来源关系改变、仅 Grant 字段/版本撤回、拒绝发布时 spool 关闭；公共响应层修复后的组合 21 passed，见 `s3-global-independent-review.md`。第 7 个 roundtrip-import 由 frontend 作者独立复核，作者 8 + 独立 3 = 11 passed（`/tmp/itsp-s3-roundtrip-independent.log`）；持久证据 `backend/tests/test_authz_s3_roundtrip_independent.py` 覆盖实际原件 metadata 篡改、第二次发布失败不删首次已提交文件、commit 前仅字段撤权的事实/回执全回滚，见 `s3-roundtrip-independent-review.md`。覆盖 JSON 逐入口包含 facade、SQL/锁边界、真实测试文件及独立报告引用。此处完成指本地实施/独立核对，完整 S3 CI 与用户验收仍独立进行。

真实 main 补充反例闭合：frontend 独立抽检发现 manager-assignment 正式命令在没有 assign Grant 时先触发“当前主负责人相同”业务校验并抛 500。`assignment_command` 现在先在独立只读 RR 做 metadata/current action/precondition 检查，再进入原业务原语；完整领域锁后 final user lock/PDP 保持。target 用户只取身份/映射/epoch 元数据，不取密码或整账户记录；canonical 业务冲突、无效输入与唯一约束分别映射结构 409/422。`test_authz_s3_assignment_commands.py` 新增有权同目标 422、撤权同目标 403、初验后第二连接撤 epoch 导致 final 401 且零关系三项；与原 7 及 frontend 原真实 main 红例共 11 passed（`/tmp/itsp-s3-assignment-preflight-fix.log`）。

上述 assignment 修订的原业务文件 `backend/tests/test_maintenance_project_manager_assignments.py` 另原样通过 20 项（`/tmp/itsp-s3-assignment-legacy-final.log`）；没有放宽 legacy 角色或原并发/CAS 断言。
