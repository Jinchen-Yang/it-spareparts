# 交接：售前询价报价共享池（D-0052）

> 日期：2026-11-02。接手者先读 `AGENTS.md`，再读 [D-0052](../decisions/0032-售前询价报价共享池.md) 和[发布说明](../releases/2026-11-01-presales-shared-pool.md)。

## 1. 接手时的工作树快照

| 项目 | 状态 |
|---|---|
| 提交 | **未提交**。全部改动只在工作树里 |
| 推送 / PR | **未推送，未开 PR**，远端没有这个分支 |
| 生产 | 交接时尚未部署。后续授权与发布门槛见第 6 节 |
| 工作树 | `G:\project\ybwznt\it-spareparts-presales-pool` |
| 分支 | `feat/presales-shared-pool`，基于 `5358b903` |
| Git 状态 | **合并进行中**：已 `merge --no-commit` 生产热修标签 `release-hotfix-presales-load-e2e335bc-20261101`（`MERGE_HEAD` 存在）。第一次提交就是这个合并提交，不要 `merge --abort` |
| 与 main | 已 fetch 核对 `e8c1fdc6` 领先 25 个提交。双方都改了 `backend/app/main.py` 和 `frontend/src/version.ts`。独立集成副本保留两边的路由，候选版本已调为 1.44.0，并完整保留 main 的 1.43.0 更新日志。实际分支同步须在用户确认提交后进行。 |
| 用户已确认的口径 | 见 D-0052 |
| 用户未回复的问题 | 见第 6 节 |

## 2. 本地检查

验证环境为本机 WSL 与 PostgreSQL 16。测试脚本和浏览器回执在 `G:\project\ybwznt\.investigation\presales-pool-20261102\`。以下 431、182 和 2377 是**接手前**的基线，接手后的结果见文末验证记录。

售前后端测试（约 3 分钟，交接前 431 通过、2 跳过）：

```bash
wsl bash /mnt/g/project/ybwznt/.investigation/presales-pool-20261102/backend_test.sh
```

前端类型检查、售前测试和构建（交接前 182 通过）：

```bash
wsl bash /mnt/g/project/ybwznt/.investigation/presales-pool-20261102/frontend_check.sh src/pages/presales src/__tests__/version.test.ts
```

前端全量（交接前完整运行 2377 通过）：

```bash
wsl bash /mnt/g/project/ybwznt/.investigation/presales-pool-20261102/frontend_check.sh
```

接手阶段的后端全仓集成回归。此入口为**当时待合并状态**预备，校验三项 Git 引用后，每次创建新的 `recheck-<UTC>-<PID>` 工作目录，避免覆盖既有回执。普通 WSL 用户运行，使用本地专用测试库、UTC、LibreOffice 和临时隔离的 Docker。正式同步分支后需更新快照输入。

```bash
wsl bash /mnt/g/project/ybwznt/.investigation/presales-pool-20261102/backend_full.sh
```

要针对同一集成副本重跑前端，将下方的 `recheck-...` 替换为上一步输出的实际运行名。

```bash
wsl bash /mnt/g/project/ybwznt/.investigation/presales-pool-20261102/integration_frontend.sh recheck-...
```

接续时已在 WSL 用户服务 `itsp-presales-pool-candidate.service` 保留本地页面。打开 `http://127.0.0.1:5186/presales/pool`，使用本地种子账号。服务已经运行时直接打开页面。需手动启动时使用以下命令。

```bash
wsl bash /mnt/g/project/ybwznt/.investigation/presales-pool-20261102/dev_up.sh
```

仅在本次页面服务与测试均已结束后，使用以下命令停本机专用测试库。

```bash
wsl bash /mnt/g/project/ybwznt/.investigation/presales-pool-20261102/pg_stop.sh
```

### 判读结果

- 交接时有 15 个维保失败。接手后在相同集成代码的七个文件中复现了本机时区下的 15 个失败，测试进程及数据库连接统一为 UTC 后，93 项全部通过。主线 `e8c1fdc6` 的 [CI 41389943327](https://github.com/Jinchen-Yang/it-spareparts/actions/runs/41389943327) 已通过。
- 早期 `backend_full.sh` 忽略了 9 个部署测试文件。现有包装脚本运行完整 `backend/tests`，使用规范 LF 源码的独立 WSL 集成副本。真实 Caddy 检查使用专属本地 Docker 命名空间。
- 初次交接时的全仓运行后只改了 `test_presales_candidates.py` 的断言，当时结果是 7757 通过，19 失败（上述 15 个 + 4 个已修的旧断言）。合并前要再跑一次全仓。
- 测试数据库使用带 `pg_trgm` 的 `~/pg16root`，pytest 各进程有独立库。前端检查脚本复制源码到专属 WSL 临时目录运行，避免移除正在点按的开发服务依赖。运行开发页面时勿暂存 `frontend/node_modules` 软链。

### 手动点按清单

1. 旧链接 `/presales/ask`、`/presales/quote`、`/presales/workbench`、`/presales/tasks` 都跳到 `/presales/pool`。
2. 填单价按回车，立即生效；更新人、菜单角标和筛选数量同步变化。
3. 展开行填交期后保存，再只改单价，交期还在。
4. 已报价行取消勾选“坏件返回”，弹确认；取消不变，确定后回到待报价，历史里能看到旧价和原因。
5. 已报价行改数量后点取消，输入框恢复原值。
6. 行末“关单”，已报价行才有“已成交”；关单后这张单的所有行离开待报价。
7. 两个账号打开同一行，先后保存，后者收到提示，自己的输入保留。
8. 没有 `data_customer` 的账号看不到客户名和客户选择器。
9. 新建询价（单行、粘贴多行、未建档 PN）、不报、绑定型号。接手后已用真实 Chrome 和合成数据库走通，用户验收仍待安排。
10. Excel：根据所选行确定未关单的询价单，导出其中未直接引用旧报价的活动行。真实页面已完成下载、填价、预览、确认及刷新核对。复验中单价从 238.75 改到 239.25，数量 1 与签名值 1.000 不再误报，预览显示 1 改、0 警告、0 错误。

## 3. 改了什么

| 位置 | 内容 |
|---|---|
| `backend/app/services/presales/pool.py` | 共享池读写：列表、数量、报价、不报、改条件、新建、历史。只锁单行，带行版本 |
| `backend/app/services/presales/answers.py` | 当前答复的唯一计算入口，旧写路径也经它同步 |
| `backend/app/api/presales_pool.py` | `/api/presales/pool/*` |
| `backend/alembic/versions/e5a7c9d1f3b6_*` | 新增答复列和 `updated_by`，回填答复，取消进行中的售前待办；可回滚 |
| `backend/app/services/presales/visibility.py` | 取消受众过滤；客户名只看 `data_customer`；草稿仍私有 |
| `offers.py`、`inquiries.py`、`binding.py`、`candidates.py`、`registration.py` | 去掉动作权限和本人限制，接入答复同步 |
| `quote_workbook.py` | Excel 报价改走 `pool.set_price` / `skip`，上传即生效；去掉“已有报价选择”两列 |
| `frontend/src/pages/presales/PoolPage.tsx` 等 | 合并页面。删除 `AskPage`、`QuotePage`、`QuoteWorkbenchPage`、`TasksPage` |
| `frontend/src/nav.tsx`、`version.ts`、`public/manual.html` | 菜单和旧链接跳转、1.44.0 候选更新日志、手册 |

设计取舍（有问题先看这里，不要重复讨论）：

- 旧接口和旧服务代码**有意保留**，只是不再有入口（strangler），便于回滚，后续单独清理。
- `inquiries.submit` 默认仍开待办，接口层传 `open_tasks=False`，这样旧服务测试不用全改。
- 共享池写入不更新询价单头的 `version`，否则同单不同行会互相冲突。
- 答复查询用 `clock_timestamp()`：`now()` 是事务开始时间，会漏掉本事务刚写入的报价。

## 4. 已知问题和风险

**4.1 初次交接时删除的测试及续接复核**

初次交接时按失败清单批量删除了 63 个后端测试函数。接手后逐项对照工作树、正在合并的索引和热修来源，已按旧断言是否仍适用分别恢复、改写或核对等价覆盖。[63 项完整台账](2026-11-02-presales-shared-pool-test-audit.md)包含原函数名、处置理由和现存测试。下面是初次交接点名的八项。

- `test_presales_quote_workbook.py::test_exact_requirement_splits_and_unknown_pn_can_skip`：拆行和未建档 PN 不报均正常。旧断言把已生效的不报当成草稿，并混淆已处理行数与有效报价数。本例已恢复，两种数量分开核对。
- `test_presales_quote_workbook.py::test_price_only_patch_preserves_different_source_drafts_and_hidden_columns`
- `test_presales_part_history.py::test_part_history_pages_multiple_inquiries_before_building_details`：含分页和查询边界，只是总数变了。
- `test_presales_r7_versions.py::test_won_offer_snapshot_survives_update_and_record_scope_change`：成交快照。
- `test_presales_r7_versions.py::test_library_groups_latest_then_sorts_tax_buckets_and_filters_more`
- `test_presales_r6_close.py::test_original_inquiry_adoption_uses_stable_moved_offer_link`
- `test_presales_r7_review.py::test_each_previous_line_link_keeps_its_own_version_after_successive_moves`
- `test_presales_quote_workbook.py::test_deleted_own_draft_does_not_block_first_receipt_replay`

**4.2 产品和数据风险**

- 上线后私密记录对全员公开，**不可收回**。上线前要只读统计私密记录数、进行中待办数和未发布草稿数。
- 未发布的报价草稿不生效，相应行要重填。
- 待报价会只增不减，除非有人关单（见第 6 节）。
- 引用同一报价的多行并发刷新时，PostgreSQL 可能选中一个事务中止。售前 HTTP 写入遇到 SQLSTATE 40P01 时回滚整次写入，返回 409 并提示核对后重试。
- 草稿编辑页的备注和自定义字段仍显示可见性锁图标，但已不生效。
- 旧版下载的 Excel 报价表多两列，需要重新下载；没有实测旧文件上传时的提示。

**4.3 尚未完成的合并门槛**

完整 CI、跨模型独立复审（`AGENTS.md` 要求写进 PR）、用户点按验收。PR 描述必须有 `决策: D-0052`。

## 5. 容量压测（尚未改代码）

目标：100 账号 × 30 次/分 ≈ 50 请求/秒；每天上百单，一年约 11 万行。

合成 36500 单、109500 行，本机 10 核，无 nginx 和公网，压测工具同机；脚本是 `load_seed.sh`、`load_run.py`、`load_all.sh`。**不是生产数据。**

| 部署 | 请求/秒 | 待报价列表 中位 / p95 | 结果 |
|---|---|---|---|
| 1 进程（现状） | 50 | 68ms / 592ms | 到边缘 |
| 4 进程 | 50 | 60ms / 79ms | 稳定 |
| 1 进程 | 100 | 超时 | 崩溃，实际约 6 请求/秒 |
| 4 进程 | 100 | 约 2.8s / 约 41s | 大量 500、503 |

三类核心查询在这个数据量下只要 5–40ms，主要慢在 Python 单进程。4 进程 × 30 连接超过 Postgres 默认的 100 条上限，可能是 500 的来源，修后要复测。

已确认：`frontend/nginx.conf` 和 Caddy 示例都没开压缩（生产 Caddyfile 未核对）；一页 50 行 41KB，gzip 后约 2KB（合成数据重复多，真实会差一些）；前端资源 3.7MB，gzip 后 1.27MB；`docker-compose.yml` 的 Postgres 没有调内存参数；后端 `Dockerfile` 只起一个 uvicorn 进程；缩略图每个型号一个请求。

## 6. 下一步

用户于 2026-11-02 授权完成修复、更新右上角日志、检测、合并并上生产。本次范围为 D-0052 售前共享池，性能扩容和待报价自动归档另议。30 天仍是建议值。

1. 4.1 已逐项复核，第 2 节的第 9、10 项已完成真实浏览器开发验证，用户点按未单独记录。
2. 主线合回生产热修，保留既有路由和 1.43.0 的更新记录；主线 PR 须同步、复审并通过两项 GitHub CI。
3. 独立发布分支自实际生产 `e2e335bc` 精选售前变化。生产保留 1.42.0 历史，在顶端加入 1.44.0 售前日志。生产库从 `a3c5e7f9b2d4` 真正升级到 `e5a7c9d1f3b6`；主线把售前和 Excel 两条迁移汇合，便于之后从任一已发布版本前进。精确 SHA 验证、备份、实恢复、迁移和流量切换分别留回执。生产只读盘点数随私有发布回执保存。
4. 性能第一批：多进程（先核对 `DeliveryWorker` 和进程内状态，每进程连接降到约 15）、开压缩、过载快速拒绝。
5. 性能第二批：缩略图批量、默认每页 20 行、数量统计短缓存、翻页缓存和 ETag。
6. 性能第三批：搜索加 `pg_trgm` 索引、待报价归档、Postgres 调参。每批都用 `load_all.sh` 复测，至少按 150 请求/秒设计。
7. 另开 PR 清理旧接口、旧服务、`ProcessPage` 和可见性锁图标。

## 7. 红线

- 未经用户明确授权，不提交生产、不部署、不写生产数据。
- 不要管理员绕过保护或强推 main。
- 只用本机专用测试库，不要连接或停止他人容器。
- 不要提交 `frontend/node_modules` 软链、`.tmp` 或 `.worktrees`。

## 8. 接手验证记录（2026-11-02，本机）

实测环境与用户验收分开记录。接手时的旧 HEAD 是 `5358b903`。`MERGE_HEAD` 记录注释标签对象 `fc85ac61`，实际待合并的热修提交为 `e2e335bc`。本地开发页使用该工作树的售前源码。完整回归使用 WSL 独立副本，将相同售前代码和测试叠加到 `origin/main=e8c1fdc6`，规范化 LF，预演合并 `backend/app/main.py` 并保留双方的路由。本轮预演当时未更改原分支。后续获得发布授权后，已用正常 merge commit `63c36392` 保留该热修的真实生产祖先。

- **权限与历史。** 删除未提交草稿后的软关单不能从列表、数量、行历史或写接口访问。共享池报价/不报保留他人未发布的决定及草稿；从未发布的报价不进入共享历史。报价、备注等业务值对售前用户公开，历史个人受众的用户名单及组名只向字段作者和主管回读。查重、绑定、新建并绑定保留主数据和绑定双权限，不要求领取待办。
- **价格及回复。** 成交价保留成交时钉住的修订。清除外部引用后按本行可证明的历史版本读价，保留多来源各自的关联，不把源报价后续改价串入旧行。只改备注/交期保留此前生效的不报及沿用答复。升级前缺少更新人字段的真实格式快照可经 HTTP 读取，旧单价原值及精度保留。
- **Excel。** 拆行、未建档不报、按行版本冲突及首次回执已复核。禁止跨 PN 沿用报价；仅改单价保留未声明税口径及各行已有字段。等值金额/数量因 Excel Decimal 标度不同的只读列误报已复现并修正。真实改值仍提示并使用导出基线。逐个已签名文件核验私稿与当前权限，覆盖转交文件、删行预览、撤权后的预览/确认和历史回执。同一行的另一份公开导出可独立使用；缺乏私稿来源证明的旧导出由导出人或主管处理。
- **页面。** 真实 Chrome 已验证第 2 节第 9、10 项。最终本地服务读取 `spareparts_presales_pool_dev` 的合成数据。在 `XJ20261102010` 的第 15 行，Excel 只改价格 239.25 → 239.75，预览 1 改、0 冲突、0 警告、0 错误，操作 5 确认且刷新后同价。回执、运行进程与源码摘要在外部脚本目录的 `excel-recheck-20261102-075522/receipt.json`、`runtime.json`。开发库缺少图库上游，缩略图曾返回 503；这部分未作为通过项。页面服务保留供用户验收。
- **本地独立复核。** `gpt-6-astra` 给出的四项问题均已处理并定向复核，包括回复版本、旧修订兼容、外部报价解绑历史和私稿 Excel 回放。已有数据在本次修复前就丢失全部关联且没有本行快照的，不凭同 PN 反推原归属。三项旧服务测试原先手动把草稿状态改为已提交，却没有提交时间；现走实际 `inquiries.submit(..., open_tasks=False)` 再构造旧待办，保留原业务断言，完整报价测试文件 21 项通过。
- **回归身份。** 最终 WSL 集成副本为 `/home/jinchen/work/presales-pool-integration/accepted-20261102`，源码清单摘要 `1eb8a4ff0a99d6cfa6edfaef8a8f05de3064cbf8787cfdb5ebc39ffdb9531c45`。1916 个文件，92 项本地变更叠加；交接文档会继续补写运行结果。前端 521 个源文件和构建输入与完整测试副本逐项一致，202 个测试文件、2411 项通过，TypeScript 和 Vite 构建通过。迁移单头 `e5a7c9d1f3b6`、空库升级、ORM 零漂移已验证。后端首轮 8530 通过、9 跳过、1 个维保跨上海午夜用例失败并有 teardown error，退出 1；同一维保失败在干净 main 上亦可复现。第二轮在 WSL 重启前进度到 85%，该维保用例已通过，但无整轮退出或正常清理回执。这两轮均不作全仓通过证明。可复核的 Windows 回执位于外部脚本目录 `integration-receipts/`。
