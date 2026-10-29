# 2026-10-29 商城单 PN 接收端窄发布准备

决策: D-0048（执行授权）；D-36（既有主数据自动目录发布边界）

本文件记录准备和门禁，不表示生产已发布。用户已授权当前主会话完成售前与性能修复并按正式流程上线；本配套仅覆盖 PN 接收端，不顺带发布商城 main 已合并但未上线的其他功能。实际操作由主代理统一完成。

## 已核实的生产基线

2026-10-29 只读核查：主机 `ybwznt`，站点 `shop.ybwznt.com`。四个活动应用为 `it-shop-next-backend`、`it-shop-next-worker`、`it-shop-next-agent`、`it-shop-next-frontend`，Compose 来源均为 `/root/IT-shop/releases/next-4651d1b/deploy/prod.compose.yml`。

- 生产来源：`4651d1bf7e032ed0b7d0f298532b271578485149`。生产 backend 内全部 113 个 `app/**/*.py` 的 Git blob SHA1 与该提交逐项完全相同。旧镜像没有 OCI revision，`SHOP_RELEASE=ae658b2` 是过期环境值，不能当作生产版本证明。
- backend/worker/agent 镜像：`sha256:02f12f18890700f6fff91dab24fdeeaaaabe8a14fe44b41f194286bfce80f417`。
- frontend 镜像：`sha256:3d07cbbab7448bdf76eb1c57a4ad6085ef2f10104f0ac036960ea858f3fa6fbb`。
- DB：`it-shop-next-db`，PostgreSQL 15，`shop_next`，当前单一 head `0026_pn_master_follow_merges`。仅新增 `0027_catalog_delivery_receipt` 技术回执表，不迁移 PN、SKU、库存流水或人工研究状态。
- 同库额外写进程：`it-shop-nocodb-nocodb-bridge-1`，当前镜像 `sha256:5a6f6fb3756a081c1e6221a7ee1e1519ae2e0518db4b0a6551bca88ab53f44fd`。已由主代理确认只将这个 bridge 纳入停写并原样恢复；NocoDB 界面、NocoDB 两库、其他 sandbox、旧商城和 ITSP/Caddy 均保护。
- 主应用私网 `it-shop-next_private`；frontend 同时保持 `it-shop-nocodb-frontend` 和 `it-spareparts-ingress`，现有宿主发布端口 `0.0.0.0:28180`。控制器保留全部网络、别名、端口及原只读 Nginx 配置，不改 Caddy。

## Git 与 CI 路径

商城 main `b016c99` 相对当前生产有 78 个文件、约 3000 行其他变更，包含手机导航与其他界面改动。本次不直接部署该 main 快照。

1. 性能任务先完成其 A/B 复审修正、联合契约和功能 PR，按 main 保护正常合并。当前 main 保护是 strict、管理员受约束、两项必需检查：`后端离线测试与数据库迁移`、`前端类型、构建与接口契约`；不要求 GitHub APPROVED。不绕过、不改保护。
2. 从上述精确生产提交建立固定 release baseline 和候选，窄移植已审接收端提交。人工审阅完整 runtime diff，拒绝带入其他 main UI、识别、均价等变更。迁移父头保持 0026。
3. 允许额外携带纯 CI 运行配置：当前 main 已有的 jinchen-dev 三分片及聚合检查，并增加 `release/**` push 或 `workflow_dispatch` 精确 SHA 触发。生产基线旧 CI 只有 PR/main push 且 hosted 20 分钟，不能直接用于完整发布验证。测试集合、检查名称和保护均不削弱。
4. 候选对固定 release baseline 提 PR，跨模型复审及两个门禁全绿后正常 merge；再等最终 merge SHA 自己的 CI 全绿。任何生产写入之前，先创建并推送 `release-` 标签，回读远端标签解析出的完整提交 SHA 与已验 CI 的 SHA 相同；再从该精确提交 `git archive`、构建双镜像和准备生产制品。最终发布回执必须核对实际运行镜像、OCI revision、数据库 head、入口及健康观察，标签存在本身不是上线证据。
5. 功能已先合 main；发布 backport 的代码历史与来源在 PR/回执中关联，不再反向覆盖 main。分支、PR、merge、tag 均由主代理统一创建，本准备任务未执行。

## 制品与凭据准备

控制器为 `.deploy/shop_pn_ops.py`，生产位置固定 `/data/shop/releases/pn-<40位发布SHA>/`，目录 root 所有且 0700，内部证据 0600。由主代理从精确提交生成 `source.tar.gz`、构建 `app_image` 与 `frontend_image`，均写 OCI revision 等于发布 SHA。

`manifest.json` 固定 `release`、`old_revision`、`old_head`、`target_head`、`source_sha256`、`controller_sha256`、候选两镜像 ID、`catalog_token_sha256`，并在 `old` 中逐项列出上述 4 个应用与 bridge 的当前精确容器 ID/镜像。不得猜 ID；切换前再次现场回读。

新生成一个独立 32–256 字符可打印 ASCII 服务 token，只写入生产私有配置。商城目录中的 `catalog.token.private` 必须 root/0600、无尾随换行，与 manifest SHA256 一致。该原文随后供 ITSP `SHOP_CATALOG_SYNC_TOKEN` 使用；商城运行环境只配置 `CATALOG_SYNC_ENABLED=true` 和 `CATALOG_SYNC_TOKEN_SHA256`。禁止复用图库凭据、商城读主库目录凭据或 PN 主数据写穿凭据。不能打印 token、完整环境、HTTP Authorization 或响应业务正文。

控制器只在 backend/worker/agent 的克隆中更改镜像、release provenance 标签及以上两个新配置键，其余 Env/Config/HostConfig 逐字段相同。frontend 环境保持不变。bridge 使用原容器原镜像恢复。

## 逐阶段操作

调用方式为 `python3 shop_pn_ops.py <绝对manifest路径> <阶段>`。每阶段在写 owner、intent 或执行操作之前都重新校验当前控制器 SHA256 等于已审 manifest；每阶段写 intent 和 PASS 回执。结果不明先读实际状态，不能删除 intent 后盲目重跑。

1. `preflight`：绑定源码、脚本、镜像和既有身份，列全同库活动写进程；冻结所有受保护容器指纹以及 Caddy 文件/active 配置摘要。新镜像 revision 不符、额外 writer、异常数据库、凭据复用、缺外部网络或空间不足均拒绝。
2. `freeze`：先停 frontend/API 入口，再停止本系统 agent、worker、bridge。agent 给足原任务结束预算；记录每个已停 ID。核数据库没有剩余远程连接，记录全部 public 表冻结计数。
3. `backup-old`：完整 `pg_dump -Fc` 留在生产私有目录，校验 TOC/hash；使用专属 PostgreSQL 15、network none、无端口、CPU/内存限制，真实恢复并逐表核对计数与旧 head。只按 ID+归属标签清理本次恢复容器。
4. `migrate`：候选镜像、原环境运行实际 `alembic upgrade 0027_catalog_delivery_receipt`，限制锁等待，核单 head、`alembic check`、旧表计数不变、新回执为零。不运行仅查看 heads/current 的假迁移。
5. `backup-new`：新 head 完整备份，再执行第二次真实隔离恢复。
6. `create`：旧四个容器仍停止，按 ID 将其改名为带精确 release 后缀的留存名；创建原稳定名称的候选四容器，逐项比较 Config/HostConfig/网络/别名，保持停止。bridge 不替换。正常流程保留旧容器，不删除旧卷或旧制品。
7. `activate`：在任何新进程启动之前持久化 exposure marker，随后启候选 API、等待 ready，再启 frontend。公网 health/ready；无认证空请求须 401，合法服务凭据加缺全部必填字段的空请求须 422（请求验证拒绝，不进入目录写函数）。然后恢复 worker/agent 和原 bridge。不会向生产提交合法 PN 投递、建档、库存/采购/销售或研究请求。
8. `verify`：公网健康、匿名门禁、合法凭据验证门禁；核所有新容器、镜像、schema、网络及受保护入口/容器不变。主代理另做 0/5/15/30 分钟自然流量、日志、5xx/重启/OOM、同步积压观察，记录样本范围，不用合成延迟宣称生产改善。
9. 商城接收端完成后，才允许 ITSP 新生产者上线并启用。首次真实成功依据自然新增 PN 的 outbox/商城回执观察；无自然样本时明确记录等待样本，不人为制造业务 PN。

## 失败边界

`recover-freeze` 专门处理 freeze 已开始但未迁移/创建/暴露的部分停服：核旧 head、旧容器 ID/镜像、Caddy 基线，再恢复原来进程。start 返回成功后仍逐容器回读原 ID/镜像/Running，等待原 backend/frontend 的 Docker 健康检查通过，并确认商城公网 `/api/ready` 和 `/healthz` 成功，最后再次核容器状态才写 PASS。它不依赖不存在的 freeze PASS，因此 stop 后后续检查失败仍可恢复；无关服务自然重启不妨碍该窄恢复。

一旦迁移 intent 已出现，控制器保守停止自动回退：先核真实 head、迁移容器及回执，由主代理做保留数据的前滚修复。暴露 marker 后尤其禁止恢复旧 dump、自动删回执或直接换回缺少新镜像保护的旧代码。部分 create/activate 失败必须按容器 ID、归属标签、已落盘配置逐项修复；控制器故意不以重跑覆盖未知结果。运行期遇接收端异常先关闭新生产者，保留出站任务与商城回执，不影响既有批量兜底边界。

## 本轮准备验证

只在 `jinchen-dev` 执行。证据位于 `/home/cloudlay/work/release-evidence-01a188b2/`：

- `shop-ops-unit-final.log`：最终 13 项纯合成单测通过（Python 3.10 语法、受限环境变更、凭据不得复用、部分冻结恢复、启动后退出/健康失败拒绝 PASS、每阶段脚本哈希校验、暴露标记先于 start 等）。
- `shop-ops-restore.log`：两个真实 network-none PostgreSQL 15 的旧/新 head 合成备份恢复与逐表计数，以及实际 Docker API 克隆运行配置检查；源库、恢复库和测试网络均独占且按标签清理。

这只证明控制路径及合成恢复，不代表生产备份已执行或最终候选代码已通过 CI。生产放行仍需最终补丁、跨模型复审、精确 SHA CI 及现场预检。
