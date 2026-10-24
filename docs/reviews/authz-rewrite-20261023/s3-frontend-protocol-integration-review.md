# S3 实际主应用与前端协议复核

本报告检查“合法新 Grant 能否经实际 app.main 到达前端”，补充领域服务自身的拒绝矩阵；不是生产验收，也不是全部 106 面逐项浏览器验证。

## 已发现并修复的接缝

1. 11 个已迁移 router 仍挂固定旧维保 Beta 白名单；readonly 新 Grant 即使领域 facade 允许，也先被旧 page boolean 拒绝。main 改用显式资源声明的发布门，保留 release flag 与实名限制。门读取当前 exact method/path 的正式合同；全部正式资源必须包含在 router 声明内，任一完整分支全部 authoritative 才退出旧白名单。AND 缺一项不算完成；unknown entry 继续旧门；实际分支、对象、字段与动作仍由领域 facade 唯一解析。operations 两条目录/采购资源漏声明也已补齐。
2. `/auth/beta-features` 和登录响应仍按旧 page_replenishment_beta 计算导航，导致新补库 Grant API 可读但真实 App 无入口。canonical 补库状态下该兼容 flag 只表示实名与服务端发布开关；`/me` 显式 show/data 与对象决定继续决定是否进入页面和请求数据。legacy 白名单及 fallback 账号行为不变。
3. 负责人分配正式命令会先抛“目标已是负责人”的业务异常，撤 Grant 时未先授权，且异常未结构化。后端作者添加独立 RR 元数据初验 require/precondition，保留领域锁后 current actor/PDP 和最终 precondition；合法业务冲突返回 409/422，无权先 403。复审者短读确认没有将初验取代最终复验；原真实 main 红例保持原断言转绿。
4. 批量转项目 options 正式 JSON 曾缺 authorization_context，已由作者修复；view/data 真实上下文相同。其余独立所选目标/OCC/发布后失败复审另见 `s3-batch-transfer-independent-review.md`。

## 真实协议证据

`backend/tests/test_authz_s3_frontend_protocol.py` 使用真实 `app.main`、隔离 PostgreSQL、真实 Cerbos、无旧 page bit 的 readonly 实名账号：

- 32 个 JSON 读取场景覆盖 business date、analytics/dashboard options、导出 options、经理总表 status、WBDD latest、返还分类、AI 列表、审计、费用核对、补库目录/草稿/申请、workspace、行字段、验收、来源取消、负责人目录、催收、迁移、需求、仓库、变卖与项目采购。每例 `/me → authorization view → 相同输入正式读取`，验证 exact branch 全结构、epoch/catalog、Grant version、字段上界和 carrier revision；随后撤掉 Grant 再读必须 403。
- 再覆盖实际 document/shipment 单资源 cutover、ledger 真实文件预览后读取、roundtrip 两种下载授权视图、负责人命令、未知路径旧门和全部条件 Beta 挂载资源声明闭合。
- 实际 HTTP 保存的 32 例加 ledger 共 **33 组**响应直接喂给当前前端 `validateAuthzSnapshot`、`validateAuthorizationResolution`、`validateContextualPayload`，**33 passed**：`/tmp/itsp-s3-actual-frontend-protocol.log`。采样 `/tmp/itsp-s3-actual-protocol-final3-samples.jsonl`；临时 probe 已删除，永久 HTTP 回归保留。不使用手写“等价”前端 validator。
- main/protocol、既有 main mount、旧 Beta 行为首个完整组合 **49 passed**（58.82s）：`/tmp/itsp-s3-main-protocol-final3.log`。此运行早于最后 OR 分支门修订。最终 OR 修订后的同组合加 batch transfer 独立三条：**52 passed**（65.70s），`/tmp/itsp-s3-main-protocol-final4.log`。
- 真实 App + Demand 组合 **14 passed**：`/tmp/itsp-s3-beta-app-navigation.log`。补库 history-only 无旧 page bit 可进入实际路由，只有自身列表两阶段请求；flag 不隐式授 show，关闭发布 flag 仍零业务请求。fixture 明确本资源价格治理字段，未削弱依赖。
- 最终 tsc、生产 build、生成器 `--check` 与 `git diff --check` 通过：`/tmp/itsp-s3-frontend-final-tsc.log`、`/tmp/itsp-s3-frontend-final-build.log`（8.29s，仅既有大 chunk 提示）。前一完整前端 179 文件 / 2084 passed 见主前端报告；最终精确全量由 CI 重新验证。

## 范围限度

静态识别 126 个纯数据合同均有实际 main 路由，未发现窄 response_model 静默裁掉 context；这个检查不等于 126 个入口全部实际执行。33 组跨语言响应验证覆盖高风险复合响应、options、目录和导入 metadata，不替代其它领域测试。新账号没有隐式 show；未注册/休眠页面没有因此开放。S3 阶段 gate、完整远端 CI 与用户验收由根代理统一汇总。

## Bootstrap 事实投影最后核查

按实际调用链检查 demand/warehouse/return/replenishment，未把授权之后的完整 receipt 校验误判成 bootstrap。Demand 的 `_actual` 仅身份/父关系，完整删除回执在 `_read.require` 或最终 actor/PDP 后核验；return 命令同样先完整初验与最终复验再核验 receipt，没有发现同类预授权全文投影。

仓库 existing apply 的 `_resolve → _receipt` 曾在 PDP 前读取 Batch.result_json/Audit.after_json；歧义 `_resolve_plan` 曾读取完整 candidates/evidence；补库 `_resolve → _application` 曾读取完整项目名/销售员快照。三条实际 PostgreSQL/Cerbos 拒绝请求分别返回 403，但 SQL 投影反例均真实红：`/tmp/itsp-s3-bootstrap-metadata-red.log`。随后又发现仓库 preview `_input_plan` 调用业务 PN 候选 helper 全 ORM 取 DimPart 描述/分类，第 4 条真实红：`/tmp/itsp-s3-warehouse-part-metadata-red.log`。

最小修订：仓库 bootstrap 只取真实身份/关系 plan，完整 result 在数据库内计算 opaque digest；歧义候选 membership 由 SQL EXISTS 验证、candidate/evidence 仅返回数据库 hash。正式完整 result/hash 校验仍发生在完整 PDP 后。仓库部件定位只取 active part/alias 的 ID，返还候选只取真实 return/project ID；业务 helper 保持不变。补库 bootstrap `_application/_cart` 使用最小 load_only + raiseload + populate_existing，正式读取仍使用原完整 ORM；version/receipt 校验仍在完整 PDP 后。

永久反例 `backend/tests/test_authz_s3_bootstrap_metadata.py` 四条原样转绿：`/tmp/itsp-s3-bootstrap-metadata-four-green.log`（4 passed，10.97s）。先前三红+作者仓库/补库组合30 passed；旧仓库 API/imports 与补库 Beta/atomic API **74 passed**（50.42s），`/tmp/itsp-s3-bootstrap-metadata-legacy.log`。最后四条加完整作者仓库/补库授权组 **31 passed**（50.23s），`/tmp/itsp-s3-bootstrap-metadata-frozen.log`。未取消任何原语锁或最终 actor/PDP。

跨作者 main OR 分支/发布门复审已由根代理收到 Astra 44 全绿确认；本报告不将其计入作者 52 的新增数量。仓库/补库 metadata 最后独审由 backend agent 执行，作者代码和测试均冻结。
