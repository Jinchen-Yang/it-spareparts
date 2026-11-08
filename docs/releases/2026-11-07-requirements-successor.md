# 无迁移接续发布：销采读台 e62cd60

决策: D-0055（`docs/decisions/0035-销采经营读台对称.md`）

本文是控制器和执行前核对说明。它不是生产回执，不表示已经打标签、切流或上线。

## 已核对的应用身份

应用构建只使用合并提交 `e62cd60cdbcc3fef896c5289388250057c4ce513`。

| 项 | 值 |
| --- | --- |
| 提交 | `e62cd60cdbcc3fef896c5289388250057c4ce513` |
| 父提交 | `ffbf783d346f051d91c6474652be8ce1fa323e09`，然后 `d37384cdd276adbef6e5ce0a56263108922e3730` |
| tree | `5b22aee2545bc5ebb31abe0ce19a626516560048`，与 `d37384cd` 的 tree 相同 |
| 合并时间 | GitHub `mergedAt` `2026-11-07T09:51:57Z`，作者时间 `2026-11-07T01:51:57-08:00` |
| 版本 | `frontend/src/version.ts` 的 `APP_VERSION` 为 `1.46.0` |
| 迁移目录 | 与 `ffbf783d` 的 `backend/alembic` tree 同为 `ce574b78358d3e2d02e602171193e2ddbc8b4f74` |

`d37384cd` 是 #515 的分支头，tree 与合并提交相同，但提交身份不同。归档的 pax comment、发布标签和两个镜像的 OCI revision 都必须是合并提交 `e62cd60`，不能改用分支头或第一父提交。后续 main 提交也不能改写这个目标。

本文件所在的控制器提交不进入应用归档。`--check-archive` 拒绝归档里出现 `.deploy/requirements_successor_ops.py` 或 `.deploy/requirements_successor_smoke.py`。

#515 的 GitHub 讨论在本控制器动工前已是解决状态。三个 Codex 线程有杨金臣在 `2026-11-07T09:49:59Z`、`09:50:05Z`、`09:50:10Z` 的回复。另外两个 Cursor Bugbot 线程也标记为已解决；当时取回的线程正文只有原来的 Bugbot 意见。本工作没有合并、关闭或解决任何 PR。

合并提交自己的 CI run [`42361515831`](https://github.com/Jinchen-Yang/it-spareparts/actions/runs/42361515831) 在编写本文时仍是 `in_progress`。不能把 PR 检查或这次观察写成该提交已经通过的发布证据。

## 生产前序

执行前仍要重读现场，不能只抄本文。当前已知前序是：

- 生产提交 `782c858a75d1f38991aa7151e9eb54f0474fb031`
- 目录 `/data/spareparts/releases/requirements-visibility-782c858a75d1f38991aa7151e9eb54f0474fb031`
- schema `98afc0d2157b`
- 容器 `itsp-requirements-782c858a75d1-app` 与 `itsp-requirements-782c858a75d1-frontend`
- Caddy 文件和 Admin API 都指向 `itsp-requirements-782c858a75d1-frontend:80`
- owner `.requirements-visibility-active-owner.json` 的 `release` 与 manifest 摘要必须和该目录的 `manifest.json` 一致

历史控制器 `.deploy/requirements_visibility_ops.py` 仍只接受 `559489e6 → 782c858a`、schema `e5a7c9d1f3b6 → 98afc0d2157b`。本入口不修改它的常量，也不重写或删除它的 owner。它的字节摘要必须保持 `6b2fe7287242ff6ce5dfcd785613877ef49dd80f13fb8ea15c4d244e93d37e9d`。共享池控制器摘要必须保持 `47b141a4127c349c2bb2d785ede3c11645de9d44c4ebd4accc308f9a76e54a39`。

## 控制器

入口是 `.deploy/requirements_successor_ops.py`。**接管修复后的已审字节摘要（LF）是 `2b2aa5ca2ca305b9f42574b50f505199169919323eea9d7c80ed90e3f19e2d1e`**（原候选 `225e4cc5` 时为 `939e7505f102b7900374c306ab651076de30d54c9d4d060baca27727fd6992ff`）。只读 smoke 是 `.deploy/requirements_successor_smoke.py`，摘要 `f44594c3390cc67f4cb56cf9e0d58a54d6d25f399136bcf43c0124a394c6196f`。这两个摘要属于控制器身份；应用镜像身份仍是 `e62cd60`。

发布目录是 `/data/spareparts/releases/requirements-successor-e62cd60cdbcc3fef896c5289388250057c4ce513`，root、0700，制品 0600。新 owner 是 `.requirements-successor-active-owner.json`。新容器前缀是 `itsp-successor-e62cd60cdbcc`。旧容器、旧目录和旧 owner 保留。

部署入口只有 root 可以执行，并且必须是 `MANIFEST PHASE`。它先按既有顺序非阻塞独占 `.catalog-source-handoff.lock`、`.itsp-production-deploy.lock`。失败只写私有日志，`rollback_invoked` 为 false，不自动回滚。

`--check-archive REPO ARCHIVE` 只读本地 Git 对象和归档，不加锁，不连接生产。归档必须由下面的命令从精确提交生成：

```text
git -C REPO -c core.autocrlf=false -c core.eol=lf archive --format=tar.gz e62cd60cdbcc3fef896c5289388250057c4ce513 > source.tar.gz
python3 .deploy/requirements_successor_ops.py --check-archive REPO source.tar.gz
```

该命令核对 pax comment、两个父提交、tree、1940 个 Git 对象的类型、mode 和 blob，并确认控制器文件不在归档内。普通文件、可执行文件和目录必须能由同一个 `tar.umask` 解释；符号链接必须是 120000 且目标字节等于 blob。硬链接、fifo、设备、07777、`..`、绝对路径和 `PaxHeader` 成员都拒绝，而且拒绝发生在读取成员正文之前。对象缺失时用 `git fetch --depth=2 origin e62cd60…`。depth 1 会把合并提交标成浅边界，`rev-parse` 看不到两个父提交；depth 2 才同时拿到该提交、两个父提交、父 tree，以及该提交的全部 blob。fetch 退出码 0 本身不算成功，必须再核对这些对象。不要改跟 `origin/main` 的浅历史。CRLF 或另一个提交的 comment 也会拒绝。服务器阶段再核对归档摘要、comment 和版本行；完整对象对照必须在复制到发布目录之前完成。不要把某次 gzip 字节摘要写成跨 Git 版本的常量。

在本机 Git 2.43.0、Python 3.12.3 上，这次只读对照的结果是 `status=passed`、1940 个文件、`controller_in_archive=false`、归档 sha256 `5f6921c7f40994cdf50757193f19f8ad4076564c8ff931daf50f630dad2e356e`。这是开发机观察，不是生产制品回执。

## manifest

`manifest.json` 固定 `release=e62cd60cdbcc3fef896c5289388250057c4ce513`、`old_revision=782c858a75d1f38991aa7151e9eb54f0474fb031`、`old_schema=target_head=98afc0d2157b`。旧容器名必须是上面的 `itsp-requirements-782c858a75d1` 一对。`additional_writers` 只能是空数组。`source_handoff`、`catalog_sync`、`import_gate_allowlist` 都拒绝。Caddy 路径只接受 `/opt/it-spareparts/Caddyfile`。端口是 1024 与 65536 之间的整数。子网是严格的私有 IPv4 `/24` 到 `/28`。

还要有旧容器 ID、DB 容器 ID、新旧镜像 `sha256:` ID、nginx 全文摘要和上游次数、三个代理的 ID 与镜像，以及 `visibility_sha256`、`pool_sha256`、`smoke_sha256`、`controller_sha256`、`source_sha256`、`evidence_sha256`、`predecessor_owner_sha256`、`helper_sha256`。`helper_sha256` 是**纵深防御的额外固定字段**：历史 hotfix/feedback 控制器在同一个 `presales3_ops.py` 上使用同名字段，本 manifest 携带并锁定同一摘要（`054e67d1…5f16`），便于审计与前序控制器交叉核对；本入口继承链的 `Hotfix.preflight` 不读取本 manifest，因此删除它不改变本入口门禁语义。控制器 `validate()` 会在任何阶段动作前拒绝缺失/错误值。smoke 摘要必须等于上面的已审文件。前序 owner 的原始字节摘要必须对上现场文件，且其 manifest 摘要指向仍为 `782c` / `98af` 的可见性 manifest。

`release-evidence.json` 要绑定同一发布提交、两个父提交、tree、`1.46.0`、旧提交、两端 schema 都是 `98afc0d2157b`、`migration_performed=false`，以及名称里包含该 40 位提交的 `release-` 标签。六项证据都必须是 `passed`，并且引用文字包含该提交：`required_ci`、`independent_review`、`candidate_regression`、`scope_review`、`no_new_migration`、`git_byte_archive`。旧的 `old_app_on_expanded_schema` 不能代替 `no_new_migration`。这些字段只能在对应记录真实完成后填写。

## 阶段

每次只跑一个阶段：`preflight`、`images`、`freeze`、`backup-old`、`migrate`、`backup-new`、`bluegreen`、`cutover`、`verify`。`recover` 是切流前的显式恢复。intent 和回执都独占创建。已有 recover intent 的目录不能再走正常阶段。

1. `preflight` 先核对证据和归档，再沿用可见性控制器的前序检查：精确写者、旧镜像 OCI、Caddy CAS、候选 heads 必须是 `98afc0d2157b (head)`。processing、作业和返件队列必须全空；旧 allowlist 不能写入 manifest。
2. `images` 核对归档摘要，并要求两个候选镜像的 OCI revision 都是 `e62cd60`。
3. `freeze` 沿用原冻结：只停 manifest 里的旧应用 ID，停后本库 TCP 客户端为 0，保存行内容摘要。不停止数据库、Caddy、商城或其他站点。
4. `backup-old` 沿用原备份：全库、globals、上传原件、配置和 Caddy，上传 tar 再与冻结目录对照。恢复容器使用现场 DB 镜像、`--network none`、无发布端口，并检查 PostgreSQL 15。恢复出的 head 必须是 `98afc0d2157b`。
5. `migrate` 不调用 `upgrade` 或 `downgrade`。它要求上一步的无网络恢复、上传对照、写者清空和 TCP 0，然后只运行 `heads`、`current`、`check`。两端 head 都必须仍是 `98afc0d2157b (head)`，行数和冻结列摘要不变。多头或新 head 在 `check` 之前拒绝。
6. `backup-new` 在停写窗口再做一次当前 head 的全库备份和无网络恢复。没有这一次恢复回执不能继续。
7. `bluegreen` 沿用原克隆：保留环境，只换镜像、名称、专属网络、loopback 端口和 nginx 上游字节。
8. `cutover` 先落暴露标记。持久文件在同一 inode 的锁内重新读取后再替换唯一 dial，不能把更早的缓冲区写回去。运行配置不用 dial 标量的 ETag：先读取包含 host match 的整条主站 route，确认身份后用这条 route 自己的 `ETag` 做 `If-Match` `PATCH`。412 不写，也不退回 `POST /load`。回执里的 `active` 是更新后的整份运行配置，供原来的 verify 读取。两把部署锁不证明没有其他 Caddy 写者。暴露之后仍然不能自动回滚。本机 Caddy 2.11.4 只说明这套 API 行为，不表示已经核对现场 Caddy 版本或拓扑。
9. `verify` 可重复。它检查公开静态字节、`1.46.0` 资源、运行镜像 revision 和 schema。按现场节奏记录 0/5/15/30 分钟，不造业务数据。

## 恢复边界

`recover` 在 `exposure-started.json` 不存在时可用——这是唯一暴露判据：`cutover` 先落该标记再改持久文件/运行配置。仅存在 `cutover.intent.json`（切流阶段在写暴露标记之前失败，例如写者/身份/CAS 前置检查未过）时，生产入口尚未改变，`recover` 仍允许，不能把可恢复的失败锁成前向修复；一旦 `exposure-started.json` 存在，只能前向处理。schema 必须仍是 `98afc0d2157b`；出现其他 head 时停止并前向处理。它只停止本候选容器，再启动原来的 `782c` 应用，不执行 downgrade，也不把 dump 覆盖回生产。冻结后的业务列摘要必须不变。

这里没有沿用历史需求复核里 `sys_access_log` 的水位例外。只读 smoke 如果经应用自己的连接追加了访问审计，切流前恢复会失败关闭，现场改为前向修复。暴露一旦开始，控制器不自动回滚。

## 本机验证

开发机未连接生产，没有创建标签，也没有执行部署阶段。

### 原始候选（225e4cc5，作者本机）

```text
Python 3.12.3
git version 2.43.0
ITSP_CADDY_BIN=/tmp/caddy-2.11.4 python3 -m unittest discover -s .deploy/tests -p 'test_requirements_*ops.py'
Ran 40 tests in 9.141s
OK
python3 -m unittest discover -s .deploy/tests -p test_presales_pool_ops.py
Ran 25 tests in 0.026s
OK (skipped=1)
```

共享池仍是 24 项通过。跳过的 1 项是原来的 Caddy 合同镜像测试，本机没有 `ITSP_CADDY_CONTRACT_IMAGE`，不是本次豁免。40 项里有接续合同，也有原来的 5 项可见性合同。本机没有 docker，Caddy 证明不是 `--network none` 容器，也不声称现场 Caddy 版本。CI 仍跑这些测试，没有新增按 PR 号跳过的路径。

### 接管修复后（2026-11-08，Linux + Docker 实测）

接管方（`tech-manager`）修复 CI 确定性失败与独立复审发现后，在 `jinchen-dev`（Linux、Docker 29.9.0、`caddy:2.11.4` 镜像）用 **LF 归档**（`git -c core.autocrlf=false -c core.eol=lf archive`）重跑：

- 共享池合同 `test_presales_pool_ops.py`：**25 项通过 / 1 项按既有条件跳过**（设置 `ITSP_CADDY_CONTRACT_IMAGE=caddy:2.11.4`，与 CI 一致）；
- 可见性合同 `test_requirements_visibility_ops.py`：**5 项通过**；
- 接续合同 `test_requirements_successor_ops.py`（除 `ArchiveTests`/`FetchContractTests`——这两类需要完整 Git 对象与远端网络，由 CI 覆盖）：**46 项通过**；
- Caddy docker 分支真实容器：`CaddyConditionalTests` 通过；运行后 `docker ps -a --filter name=itst-team-caddytest-` 与 `itsp-successor-caddy` **均无残留容器**；
- 控制器字节摘要（LF）：见「控制器」节；smoke 摘要仍为 `f44594c3…196f`；visibility/pool 继承链摘要不变。

本轮修复覆盖：Caddy docker 分支 admin 端口统一（P1，CI run `42375262184` 根因）、测试容器清理、manifest `helper_sha256` 继承链校验（P2-1）、recover 暴露判据与前置门（P2-2 / F5）、切流主站 matcher OR 语义（F1）、**切流前后持久 Caddyfile 的语义再校验（F2）**与对应 hermetic 反例（P2-4）。仍未覆盖：真实 Git 对照（Archive/Fetch 类，CI 覆盖）、真实 Linux 多进程 flock 竞争、真实 Caddy 重载与生产恢复。

## 独立复审与已知边界

接管方在修复前后做了两次独立复审（报告落盘在团队目录、非本仓库，供执行授权方查阅）：

- `team/reports/tech/review/2026-11-08-516独立复审-qa.md`：P0=0、P1=1（Caddy 端口，已修）、P2=4（helper_sha256 / recover 边界 / 容器清理 / 测试覆盖，已按建议修复或补测）、P3=8（文档/措辞）。
- `team/reports/tech/review/2026-11-08-516独立复审-subagent.md`（**同模型 fresh context 第三复审**，被审快照 `753468ae`）：指出 F2 修复的**比较方向错误（R-01，P1：替换前 adapt(current) 与已换新 dial 的 desired 比较，正常切流必错）** 与 R-04/R-06/R-07/R-08 等。处置：R-01 已修正（改为比较即将写入的 updated 字节的 adaptation，补成功路径测试）；R-04（锁/构造失败写私有日志）、R-06（PASS receipt 移至最终守卫后）、R-07/R-08（F2 与 route 身份/形状预检前移到 exposure 之前）均已修；R-05（intent O_EXCL 阻断重试）与 P3 项见下。
- `team/reports/tech/review/2026-11-08-516独立复审-codex.md`（**Codex 后端独立复审**）：P0=0、P1=3、P2=4。已修 **F1**（matcher OR 语义）、**F2**（重读后未语义校验；并按 PR #519 cursor 复审线程改为**同源比较**：stdin bytes→bytes 相对校验 + 切流后 real-path adapt vs live runtime）、**F5**（被拒绝的 recover 污染发布状态）；以下为**已知边界/后续加固项**：
  - **F3（显式接受项）**：advisory `flock` 无法阻止不取锁的其他配置写者，二次读→写仍有理论丢写窗口。本控制器**接受该残余风险**，并以执行前置条件约束：发布窗口必须冻结 Caddy 配置的其他写入者（含人工/其他代理），由操作员在 `freeze` 前用只读方式核对无并发 Admin 写者并留证；该残余风险不因本接受声明而消除，见「执行前人工核验清单」第 3、5 条。
  - **F4**（route CAS 作用域与全配置比较不一致，合法并发修改可能被误判）：遇到 412/异常/回执丢失时按 intent + 现场显式 reconcile（人工），不自动回滚。
  - **F6**（后续阶段未重绑备份制品字节）：启用候选/切流前由操作员复核 dump/uploads 摘要与恢复回执一致；代码加固另立任务。
  - **F8（运行时发现，已修）**：生产 Docker 29.1.3 的 `docker inspect ... .Mounts` 返回顺序为 map 迭代顺序，同一容器连续调用会随机变化；继承自可见性控制器的 `protected()` 直接比较 `{Config,HostConfig,Mounts}` 原始数组，会在**无真实变更**时误报 `protected configuration drift`（首次发布尝试因此在 `images` 阶段 fail-closed）。修复：本入口覆盖 `config()`，仅把 `Mounts` 按 `(Destination,Type,Source)` 排序后再比较（不忽略任何字段；真实配置漂移仍拒绝），并补乱序等价/真实漂移测试。
  - **F11（运行时发现，已修）**：Caddy `/config/` 导出的顶层 route 附带 `"terminal": true`；`route_is_main` 原严格键集 `{match,handle}` 把它判为未知结构 → attempt3 `cutover` 在写 exposure 标记前 fail-closed（`main route is not singular`），随后 pre-exposure `recover` 恢复旧应用。修复：允许已核实的 `terminal` 键（必须为 true），其他未知键仍 fail-closed；补接受/拒绝测试；并以真实快照 dry-run 回放验证（15/15 PASS）。  - **F9（运行时发现，已修）**：`alembic current` 对合并修订会输出 `98afc0d2157b (head) (mergepoint)`（98af 即 mergepoint）；no-op migrate 首版断言精确等于 `98afc0d2157b (head)`，导致 attempt2 在 migrate 阶段 fail-closed（随后 pre-exposure `recover` 恢复旧应用，未暴露）。修复：`current_revision()`/`head_revision()` 只接受两种合法输出（held head 或 held head + `(mergepoint)`），仍要求唯一 head/current 行；其余畸形输出一律拒绝。
  - **F10（审计结论，仅记录）**：继承自 `requirements_visibility_ops.py` 的 `preflight` 内 `alembic heads == "98afc0d2157b (head)"` 为精确匹配，但该文件字节被摘要锁定且实测 `alembic heads` 单 head 输出不含 `mergepoint` 后缀（attempt2 preflight 已通过），保留现状并在审计报告中登记。
  - **F7**（Docker-only 环境下 `FileRestartTests` 缺少启动/查询分支）：测试缺口记录；CI 若未安装 caddy 二进制，该用例只做字节替换断言，不能当作"重启后保持其他站点"的证明。
  - **R-05（已知 P2，接受）**：intent 为独占创建，阶段失败后重跑同一阶段会 `O_EXCL` 失败；不接受自动清理，须由操作员按私有诊断与现场状态决定前向修复/人工清理（runbook 执行前确认项）。
  - **P3 后续加固（记录，不阻断）**：归档目录条目集合校验（当前只比 blob）、部署期归档仅摘要+comment+version（完整 Git 对照以 `--check-archive` 为发布前置）、recover 后候选容器/网络保留供取证、restore 容器名冲突处理、CI checkout `fetch-depth` 加固（Archive/Fetch 网络依赖）。

跨模型复审现状：上述 Codex 后端复审为独立模型通道尝试；本文件不把同模型复审冒充为跨模型复审。最终合并与执行结论仍需 `lead`/用户与执行授权方确认。

这些边界不改变"禁止自动回滚 / 暴露后前向处理"的既有安全方向，但意味着本控制器**合并后仍不是可直接执行生产阶段的授权**：执行前必须 用户授权 + 现场重读 + 上述边界由执行方逐项确认或完成后续加固。

## 发布尝试记录（2026-11-08）

- **尝试 #1（aborted，未 freeze/cutover）**：2026-11-08T07:36:38Z `preflight` PASS；07:37:14Z `images` FAIL（`protected configuration drift: it-shop-prod-backend-1`）。根因 = F8（生产 Docker 29.1.3 `Mounts` 顺序随机；与本轮操作无关）。处置：fail-closed 停止，未执行 freeze/cutover，生产运行态未改变；本版 cleanup 已按授权执行且不回滚（有 before/after 快照）；旧 release 目录改名保留审计，旧 owner 归档。
- **尝试 #2（aborted，已 pre-exposure recover）**：F8 修复后控制器（`4d738730…b43b`）新建目录重跑：preflight/images/freeze/backup-old PASS；08:51:32Z `migrate` FAIL（F9：`alembic current` 带 `(mergepoint)` 后缀被精确断言误拒）。处置：fail-closed 停止；08:52:24Z–08:53:39Z 执行 pre-exposure `recover` PASS（旧 app 恢复、health 200、无 exposure）；旧目录将改名 `.aborted-…-migrate-mergepoint` 归档；授权清理不回滚。
- **尝试 #3（aborted，已 pre-exposure recover）**：F9+F10 修复后控制器（`92c802bc…`）新建目录重跑：preflight/images/freeze/backup-old/migrate/backup-new/bluegreen PASS；10:23:38Z `cutover` FAIL（F11：Caddy 导出 route 含 `terminal:true` 被严格键集拒绝，`main route is not singular`）；10:24:21Z–10:25:38Z 执行 pre-exposure `recover` PASS（旧 app 恢复、health 200、无 exposure）；旧目录将改名 `.aborted-…-cutover-route-terminal` 归档。

- **尝试 #4（当前）**：使用 F11 修复后的控制器（LF sha256 `2b2aa5ca2ca305b9f42574b50f505199169919323eea9d7c80ed90e3f19e2d1e`）新建 release 目录重跑；不复用任何旧 marker/intent。

## 全阶段 dry-run 门禁（freeze 前强制）

- 每次 attempt 在 `freeze` 前，采集当次生产 live 只读快照并用 `team/tools/g7/dry_run.py` 回放全部格式敏感断言（route 形状/terminal、adapt 同源、ETag、Mounts、alembic 输出、回执结构）；**全绿并落私有 evidence 后才允许 freeze**。
- 首次执行结果：15/15 PASS（快照 2026-11-08T10:38Z；详见 `team/reports/tech/2026-11-08-G7-dry-run报告.md`）。

## 执行前人工核验清单（每次生产执行必做，不替代代码门禁）

1. **用户明确授权**：`lead` 转达、精确候选 SHA/tag 意图、发布窗口与写冻结时段；
2. **现场重读**：生产容器/镜像 ID、DB head、Caddy 运行配置与挂载字节、前序 owner 摘要，全部与 manifest 一致（不得只抄文档）；
3. **Caddy 唯一写者（F3 接受项）**：发布窗口内确认无其他 Caddy Admin 写者/人工改配置/并发发布锁占用，并留证；同时只读核对主站 route 的实际 adapt 形状（顶层键、matcher 是否仅 host），形状不符会导致 **pre-exposure 拒绝**（R-08，属预期 fail-closed）。
4. **冻结前置**：旧应用已停、本库 TCP 客户端=0、业务写者为空（freeze/backup 回执核对）；
5. **F3 残余风险记录**：advisory flock 不能检测不取锁的外部写者；操作员按 runbook 冻结其他配置写者并记录——本控制器不声称能替代该人工核验；
6. **备份制品（F6）**：dump/uploads/globals 的路径、权限、大小、SHA256 与恢复回执一致；
7. **恢复演练**：备份在隔离 PG15 无网络实例真实恢复并核对 head/关键表；`.deploy/restore_drill.sh` 的已审核 head 必须先随 schema 发布更新（见 release-prep PR）；
8. **失败处理**：切流前失败按 `recover` 前向修复；`exposure-started.json` 出现后不自动回滚，按 runbook 前向处理。**阶段 intent 为独占创建（R-05）**：失败后重跑同一阶段会 `O_EXCL` 失败，不得自动清理/自动重试，操作员按私有诊断与现场状态决定前向修复或人工清理；
9. **执行后复核**：两域名静态/版本/API/角色/DB 健康/镜像 revision 复核，保存私有不可覆盖回执与 0/5/15/30 分钟验证记录。

## 执行限制

独立审核本控制器之后，才讨论是否执行。执行仍要另有明确授权。生产写入、重启、标签和切流都不在本说明的完成范围内。
