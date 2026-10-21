# 交接：catalog-v2 生产者 D1（itsp 分支 feat/catalog-v2-publish，lane D）

> 暂停说明：金臣（产品负责人）2026-10-21 18:45（北京时间）下令全部开发暂停、准备重构。本文件由暂停收尾执行者于 2026-10-21 2026-10-21 19:10（北京时间）写入，只记录现状，不补完、不修测试。收尾期间**未跑任何测试**，下文测试结果均摘自原执行日志，标明来源。
> 本 PR 带标签 `paused-refactor`，标题前缀「[暂停-待重构]」。不合并、不部署；未删除任何分支、worktree、容器、stash 或数据库。

## 目标与编号
- lane D / D1：itsp catalog-v2 生产者（规范编码/hash、PG 触发器捕获、发布者、读接口 snapshots/list/changes/parts/digests、分片传输、catalog:read 凭据）。决策 D-36（docs/decisions/0014-主数据库与商城目录互通.md）；交接 ITSP-7；契约 docs/integrations/catalog-v2.md。
- 计划：~/workspaces/reviews/lanes-20261020/laneD-plan.md §2；完整提示 lanes-20261020/laneD/D1-full.md。

## 已完成 / 未完成
- 已完成（本地提交）：828c112「实现目录 v2 规范编码与固定摘要」（canonical.py + test_catalog_v2_canonical.py，Codex 自报 66 项定向测试通过）。
- 半成品（本次原样提交 2aa313c）：迁移 d4e8f2a6b0c1_catalog_v2_capture.py、models/catalog_v2.py、api/catalog_v2.py、services/catalog_v2/publisher.py、transport.py、cli/catalog_v2.py、config/main/master_data/dimensions 改动、测试 test_catalog_v2_{api,capture,publisher}.py、docs/releases/catalog-v2-d1.md、docs/integrations/catalog-v2-d1-validation.md。
- 未完成（据 Codex 最后消息）：发布者“分片清单随发布事务保存”刚接入待复验；子表触发器加父行锁后与别名审核/合并可能锁序相反；合法多跳重定向变化时后代节点 identity 版本需推进——均未修。D2–D6 未开工。
- Codex 会话 2026-10-20 03:05 启动，约 05:51 因 workspace routing/模型刷新超时中断，之后无人续做。

## 当前状态
- 收尾时代码 head：`2aa313c`（父 828c112，基线 origin/main 319e25f6）；本交接文档提交在其后，最终 head 见 PR。分支此前从未推送、无 PR，本次新开 Draft PR。
- 测试：收尾未跑。Codex 自述：规范编码 66 项定向通过；捕获与既有写路径回归 34 项通过；发布者首轮 11 过 1 失败（分片清单未持久化）——未经复核。
- CI：新推送触发，结果未知。基线已落后 main 很多（319e25f → 现 main），alembic 迁移 down_revision 需按新 head 重定，预计迁移单头检查会红。

## 已知问题 / 坑
- 未提交（保留在 worktree）：backend/spareparts_backend.egg-info/*（构建产物）、空日志 .catalog-v2-targeted.log。
- 临时 PG 容器 laneD-itsp-pg（127.0.0.1:55510，tmpfs）若还在，保留。
- 硬约束（来自计划）：新能力默认关闭；不碰生产、66 条 open catalog_import_conflict、CATALOG_IMPORT_ENABLED、PR-5 回填、参考价文件。

## 未决业务问题
- 见 laneD-plan.md §8 开放问题（v1→v2 切换节奏 Q3、actor 映射管理 UI Q4 等），金臣未裁定。

## 下一步怎么接
1. 重构方案若保留 catalog-v2：merge main、重定迁移 down_revision；2. 修上述三处；3. 跑 golden 与 PG 集成测试；4. 再做 D2/D3。

## 涉及文件与模块
- backend/app/services/catalog_v2/{canonical,publisher,transport}.py、api/catalog_v2.py、models/catalog_v2.py、cli/catalog_v2.py、alembic/versions/d4e8f2a6b0c1_catalog_v2_capture.py、config.py、main.py、models/dimensions.py、services/master_data.py、tests/conftest.py 及 test_catalog_v2_*.py

## 代码作者（按模型）
- 828c112 与 2aa313c 内全部改动：Codex（lane D d1-r1，gpt-6-astra，日志 lanes-20261020/laneD/logs/d1-r1.*）。
