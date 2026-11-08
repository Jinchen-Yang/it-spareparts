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

入口是 `.deploy/requirements_successor_ops.py`。本次审核时的字节摘要是 `939e7505f102b7900374c306ab651076de30d54c9d4d060baca27727fd6992ff`。只读 smoke 是 `.deploy/requirements_successor_smoke.py`，摘要 `f44594c3390cc67f4cb56cf9e0d58a54d6d25f399136bcf43c0124a394c6196f`。这两个摘要属于控制器身份；应用镜像身份仍是 `e62cd60`。

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

还要有旧容器 ID、DB 容器 ID、新旧镜像 `sha256:` ID、nginx 全文摘要和上游次数、三个代理的 ID 与镜像，以及 `visibility_sha256`、`pool_sha256`、`smoke_sha256`、`controller_sha256`、`source_sha256`、`evidence_sha256`、`predecessor_owner_sha256`、`helper_sha256`。`helper_sha256` 必须等于历史继承链 `feedback_release_ops.Hotfix.preflight` 使用的 helper（`.deploy/presales3_ops.py`）的已审字节摘要 `054e67d143a808e5672eea716573ac23a4ecf47ae6cd9a762a89901acfff5f16`；控制器 `validate()` 会在任何阶段动作前拒绝缺失/错误值，避免继承链在 preflight 才 `KeyError`。smoke 摘要必须等于上面的已审文件。前序 owner 的原始字节摘要必须对上现场文件，且其 manifest 摘要指向仍为 `782c` / `98af` 的可见性 manifest。

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

共享池仍是 24 项通过。跳过的 1 项是原来的 Caddy 合同镜像测试，本机没有 `ITSP_CADDY_CONTRACT_IMAGE`，不是本次豁免。

40 项里有接续合同，也有原来的 5 项可见性合同。本轮持久反例包括：缺 `active` 的切流回执在真实 `Hotfix.verify` 上 `KeyError`，补上后贯穿 verify；同 inode 上先改其他站点再提交，重启 Caddy 2.11.4 后修改仍在，删掉重新读取则丢失；标量 dial ETag 在交换 host 后 `PATCH 200` 改错站点，route ETag 不改错站点；全新 bare 仓库 depth 1 看不到父提交，depth 2 后 1940 个 blob 与归档对照通过；删除 `execute` / `cas_file` / `rollback` 里的守卫后错误操作成功，原方法仍拒绝。本机没有 docker，Caddy 证明不是 `--network none` 容器，也不声称现场 Caddy 版本。CI 仍跑这些测试，没有新增按 PR 号跳过的路径。

这些测试覆盖：历史常量和 Caddy / 写者 / 冻结 / 备份 / 切流函数身份不变；只接受 `e62cd60` 而拒绝两个父提交和 `782c`；禁止 upgrade、downgrade 和旧 import allowlist；无网络 PG15 恢复标志缺失时不调用 alembic；暴露或 schema 变化时不调用 Docker；失败路径不回滚且不改前序 owner；两把锁跨进程互斥；真实 Git blob 对照和 CRLF 拒绝。它们不代替现场备份、真实恢复、镜像构建、认证冒烟或用户点按。

## 执行限制

独立审核本控制器之后，才讨论是否执行。执行仍要另有明确授权。生产写入、重启、标签和切流都不在本说明的完成范围内。
