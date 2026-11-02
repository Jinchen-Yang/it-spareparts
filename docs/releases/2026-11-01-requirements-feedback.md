# 2026-11-01 历史需求复核发布 Runbook

决策: D-0051、D-0050

状态：发布准备，未执行生产变更。Issue [#505](https://github.com/Jinchen-Yang/it-spareparts/issues/505)。用户已明确授权调研、决断、实现、测试、CI、正常合并和上线；该授权不代替实际测试、候选点按验收或部署回执。

## 范围与生产身份

来源为《需求_问题_bug 描述.xlsx》B4:B14。表中 G2 是示例，不代表已修复；逐项处置及真实需求见 D-0051 与 `docs/plans/2026-11-01-requirements-revalidation.md`。本轮包括销售/采购只读分析与费用查看、稳定需求单操作接线、项目归档/恢复、免返原文/地址导出、销售人员筛选、实收核对入口、返还收货独立 Excel 往返和完整筛选集流水导出。各子方案、实现、验证和验收必须逐项映射，不能以页面截图存在推定能力完成。

2026-11-01 只读核查的真实业务主机为 `ybwznt`（hostname `zs0`），域名 `ybwznt.com` / `www.ybwznt.com`。旧应用 `itsp-presales3-e6cfd25c51bc-app`，旧前端 `itsp-presales3-e6cfd25c51bc-frontend`，二者 OCI revision 为 `e6cfd25c51bc19c1f80d0bad23037d71e6d2aa0e`，数据库 head `a3c5e7f9b2d4`。开始发布前重新读取完整容器 ID、image ID、StartedAt、Caddy active/文件/inode、Nginx 字节及上游次数，不复用文档里的技术 ID。

main `18270fa1` 的四批 Excel 源字段保真修复尚未部署。最终发布范围须显式包含这四批及本轮变更，逐项核对生产到最终 SHA 的完整差异；不能仅比较最新 main 与工作分支。本轮业务改动预计无新增迁移，但随尚未上线的 D-0050 将 head 推至 `c7e1a3b5d9f2`。该迁移扩展文本并增加 nullable 原文列，无历史补录。发布不会重放原件、补录历史数据或更改业务审批状态。

## 精确候选放行

1. 分批 PR 指向最新 main；包含 `决策: D-0051`。完成跨模型独立复审并记录实际模型、精确源码身份、发现、修复和结论，不能将未运行/失败的 OMP 记成完成。
2. 独立开发库完成后端回归、前端 Vitest/tsc/build、完整迁移链、空库与生产旧 head 到新 head 升级、ORM 零漂移及新字段保真/拒绝破坏性降级验证；旧应用在新扩展 schema 的读取/启动兼容须有真实合成证据。本次新控制器安全测试与现有受复用保护测试也必须通过。
3. UI 候选报告“待验收”，由用户安排真实点按；浏览器自动化只算开发验证。最终回执分别记录自动回归与实际点按结果，不代造验收。
4. 同步 main，解决全部讨论，必需 CI `后端测试（pytest + 迁移链验证）`、`前端类型检查 + 构建` 全绿后正常 merge commit；不使用 `--admin`，不强推、不修改保护、不删除远端分支。
5. 对最终合并 SHA 再核完整范围和验证身份，创建并回读 `release-` 标签精确指向该 SHA。来源归档及前后端镜像都从该已提交 SHA 构建；两镜像 OCI revision 必须完全相等。不能从脏工作树构建生产制品，也不能将预验 SHA 的 CI 写成合并 SHA 的 CI。

## 控制器与 manifest

新增入口 `.deploy/feedback_release_ops.py`，仅适用于上述固定旧基线到 `c7e1a3b5d9f2`。固定生产目录 `/data/spareparts/releases/feedback-<40位最终SHA>`，root/0700，制品文件0600。控制器通过 `helper_sha256` 先校验 `.deploy/presales3_ops.py` 的实际字节，再执行这些已验字节作为库；不执行其旧入口、不改其全局变量、不伪造 D-0049 handoff、旧迁移头或旧 manifest。禁止传 `source_handoff`、`catalog_sync`、旧 `import_gate_allowlist`。商城配置与凭据保持原样。

复用的是既有真实 PG15 恢复、前后端精确克隆、环境逐项保留、镜像检查、迁移、Caddy CAS 和只读冒烟。新入口独立实现本次 manifest、preflight、写者扫描、导入再审、所有容器配置保护、不可变阶段与应用回退。控制器/复用库的任何字节改变都须重新审核、更新 manifest 哈希；不能在失败的发布目录里覆盖任一已固定文件。

manifest 必填字段：

| 字段 | 实际来源/约束 |
| --- | --- |
| `release` | 已验证并打标签的最终40位 SHA |
| `old_revision`, `old_schema`, `target_head` | 上述固定生产基线与新 head |
| `old_app`, `old_front`, `old_app_id`, `old_front_id` | 现场旧容器名及64位 ID |
| `old_app_image`, `old_front_image`, `app_image`, `frontend_image` | 现场/构建产物 `sha256:<64hex>` image ID |
| `port`, `subnet` | 未占用 loopback 端口与无重叠专用私有 IPv4 /24–/28 |
| `caddyfile` | 现场 bind 的 `/opt/it-spareparts/Caddyfile` 或 `/etc/caddy/Caddyfile` |
| `nginx_sha256`, `nginx_upstream_count` | 旧前端只读 default.conf 全文 hash 及旧 app:8000 出现数 |
| `additional_writers` | 明确数组；每项仅 `{name,id,image}`，无额外实例也写空数组 |
| `controller_sha256`, `helper_sha256` | 本入口与未经改写的复用库实际字节 SHA256 |
| `source_sha256`, `smoke_sha256` | `source.tar.gz`、`readonly-smoke.py` 实际字节 SHA256 |
| `evidence_sha256` | 本次 `release-evidence.json` 实际字节 SHA256 |
| `predecessor_owner_sha256` | 现场 `.presales3-active-owner.json` 原始字节 SHA256，其 release 必须是旧生产 SHA |
| `processing_review` | 默认省略；有遗留 processing 时按下节重新生成 |

`release-evidence.json` 绑定 `release`、`old_revision`、`target_head`、`release_tag`、`release_tag_sha`；`required_ci`、`independent_review`、`candidate_regression`、`scope_review`、`old_app_on_expanded_schema` 均为真实 `passed` 才放行。`references` 对象必须给出对应精确提交的 CI、测试日志、独立复审、范围和验收记录地址/摘要。这些字段是已完成证据的索引，不是可以预填的承诺。旧应用在扩展 schema 的兼容证据用于支持下述有限应用回退。

`readonly-smoke.py` 必须先审阅并绑定 hash。在生产仅使用已有合法身份执行读取，对销售/采购筛选与权限、费用导入可用性、项目目录/归档显示、需求单列表及权限、看板/实收核对、流水导出、既有导出格式做无状态读取。匿名关键 API 应拒绝。禁止调用预览/确认、编辑/归档、导入、测试造数、报价、审批、业务刷新或强制目录同步；即使名为 preview 也可能创建账本。服务内部执行 DB 读取时使用只读事务，HTTP 请求只走已审无状态入口。脚本只输出无业务正文、无凭据的检查结果，末尾 `{"status":"passed",...}`。

## 同库写者与 processing 再审

preflight 比较所有容器和完整运行配置；扫描运行容器的 DATABASE_URL，统一 DB 主机名、Docker alias、DB IP，核对 manifest 声明的全部 writer，并核 `pg_stat_activity` 客户端地址是否属于它们。freeze 前再扫一次；停止后本库 TCP client 必须为零。不得凭容器名字只停当前 Caddy 上游，也不得停止数据库、Caddy、商城或其他站点。任何未知客户端、容器增减、运行配置/网络成员变更均阻断。

默认要求 `sys_import_batch` / `sys_import_job` 没有 processing，返件异步账本没有 queued/processing。上一轮 batch 282/428/464、job137 的豁免不会自动继承。本次如仍存在，须在只读事务中重新获取完整行 hash/关联 batch/进度/StartedAt，并审查当前生产源码中的进程生命周期、无重启恢复路径及无存活 worker；不得按年龄或状态标签自行声称任务已完成。

显式 `processing_review` 仅含 `review_sha256`、`snapshot_sha256`、`reviewed_at`，时间为含时区 ISO8601，最迟在复审后6小时进入 freeze。私有 `processing-review.private.json` 字段为本次 `release`、`old_revision`、`writers`（顺序与控制器一致）、`reviewed_at`、`snapshot_sha256`、真实 `no_resume_path_reviewed`/`no_surviving_worker_reviewed` 布尔、非空 `source_evidence`（准确文件行、代码版本和检查结论）。`snapshot_sha256` 使用控制器 `digest_json` 对其 `import_gate_query(core)` 的完整 JSON 结果计算，包含新增 `has_archives`。文件本身另算 `review_sha256`。

另必填 `legacy_preview_without_archives`，精确列出当前快照中缺少 archives 的预览 ID，没有则为空数组。旧共享 SQL 只识别历史282缺 archives 的形状，新入口仍要求这一特殊形状在本次审查文件中显式列出，并说明原源码兼容依据；不能因为 ID 是282自动豁免，也不泛化接受其他缺字段预览。未知/遗漏/多列出的例外均拒绝。

控制器只接受结构完整的持久维保预览，以及一个文件已成功、进度1/1、无错误、未结束、创建早于所有现存 writer 启动的孤儿 sales 账本。每行/关联批次、worker ID/image/StartedAt、完整快照及审查文件均绑定；未知活动任务、漂移、过期或无重启恢复证据均阻断。`post_import_refresh_completion_unknown` 保持真实未知；不更新任何任务状态、不重放、不触发刷新。preflight 与 freeze 停写前后结果必须完全相等。

## 单系统阶段

每次仅执行一阶段：`python3 feedback_release_ops.py <绝对manifest路径> <phase>`。入口先按固定次序持有现有 `.catalog-source-handoff.lock`、`.itsp-production-deploy.lock`，均为非阻塞独占；持久 `.feedback-active-owner.json` 绑定 release/manifest/controller。旧 owner 只读并比对，不被重写或删除。任何另一发布不得交错操作。

1. `preflight`：核放行证据、旧身份、原配置/所有容器、同库写者、导入门禁、旧 schema、锁、容量（至少20GiB）、端口/网络、Caddy bind/active/inode；保存私有全量基线。
2. `images`：核源归档 hash，前后端完整 image ID 与 OCI revision；保存两镜像归档。
3. `freeze`：再次核所有保护和导入状态，逐个按确切 ID 停止已审 writer，核 TCP0；保存所有 public 业务表计数及旧列投影行内容摘要。旧 frontend 保留，短暂停写窗口内 API 暂不可用。
4. `backup-old`：生产原地备份完整 DB、自定义格式 dump、globals、上传原件、配置和 Caddy；记录 SHA256/大小/TOC。用同一 PG15 image、`--network none`、无发布端口、资源限额的专属容器真实恢复，核旧 head 和所有业务表计数；上传 tar 再逐内容与冻结原目录比对。只按确切 ID/归属清理本次恢复容器，备份及证据保留且不下载业务数据。
5. `migrate`：必须已有 old 备份与真实恢复 PASS，核冻结行摘要；用候选 app image/原环境执行唯一目标 `alembic upgrade c7e1a3b5d9f2`，核 heads/current、ORM check、全部计数与旧列行摘要。锁等待有限，失败不自动重试、不强行终止其他会话。
6. `backup-new`：仍在停写窗口生成新 head 全库备份，并再做真实隔离恢复、head/全部业务表计数核对。备份文件没有真实恢复证据时不能继续。
7. `bluegreen`：从原 inspect 配置克隆 app/frontend，只改 image/发布身份、容器名、专属网络、前端 loopback 端口及 Nginx 旧 app 上游字节。所有 Env（含既有目录同步配置）原样保留；迁移和新头恢复完成后才启动新 app。候选只读冒烟与保护复核 PASS 后保留指纹。
8. `cutover`：再次核候选/Caddy CAS/schema/冒烟。只替换两个正式域名共享路由中恰好一个 frontend 上游；Caddy validate/adapt 必须与期望 JSON 完全一致，其他站点不变。任何外部暴露前 fsync `exposure-started.json`，再原 inode 写文件/reload/CAS。
9. `verify`：0/5/15/30分钟分别调用。两域名只读冒烟、静态资产逐项与 loopback 比对、镜像/容器/启动/OOM/重启、日志5xx/异常、DB锁、全部既有容器配置及商城只读 health/ready。未有自然业务样本时明说等待样本，不造生产记录验证。

除重复观察 `verify` 每次拥有独立名称外，阶段 intent/receipt 均用 exclusive create + fsync，不覆盖、不补写假的 PASS。操作结果未知或 SSH 中断后，先读取 intent、私有日志、实际 Docker/schema/Caddy/备份；不能删 intent 盲目再跑。失败不触发自动回切或恢复 DB。最终发布记录须填真实 SHA/标签、两 image ID、schema、切流时间、备份和两次恢复、只读冒烟与观察、逐项需求状态/限制；本文件当前没有这些完成回执。

## 恢复与回退边界

`recover-freeze` 仅用于 freeze 已有 intent 但没有 PASS，且没有迁移/切流/暴露 intent，旧 schema/Caddy/精确 writer 均未变的局部停止失败；只启动原实例，核健康，整个候选随后视为终止。

`rollback` 是暴露前有限应用恢复：必须已完成 freeze、没有任何 cutover intent/exposure，Caddy 仍是旧基线，DB head 只能为旧 head 或新 head；停止确切本候选实例后要求 TCP0、业务表及其他技术表计数/旧列内容不变，再启动旧 writer。仅 `sys_access_log` 有明确的访问审计例外：认证 HTTP 读取会通过应用自己的独立 Session 提交审计，smoke 进程的 PGOPTIONS 不控制该连接。freeze 记录该表 max(id) 和完整旧行摘要；恢复必须验证水位以内的全部旧行逐字节未变、总数不减少，只保留当前水位后的新增审计，不删改它们。不能将所有 sys 表排除；例如目录投递队列的状态仍严格守恒。该验证证明冻结前审计仍在且当前新增审计被保留，不声称能证明冻结后新审计的完整历史追加链。

始终保留当前 schema，不执行 downgrade，不恢复旧 dump，不截短新文本/清空新增原文。旧应用与新 schema 的兼容必须在放行证据中实测。回退或恢复一旦生成 intent，本目录不能再继续正常发布或重试恢复。

发生切流意图、外部暴露、业务行变化、未知 schema 或旧兼容证据不足时，自动应用回退被拒绝，保留现场并另做精确评估/前向修复。数据库恢复覆盖生产属于单独事故处置；不会作为正常回退步骤。已写长文本或新原文时迁移 downgrade 的拒绝是数据保护，禁止通过删数据绕过。

## 准备验证记录

新增 `.deploy/tests/test_feedback_release_ops.py` 为纯合成安全契约：新旧协议隔离、helper 校验先于执行、精确写者/alias/client、processing 新复审和漂移/生命周期、配置保护、不可变 intent/owner/阶段顺序，以及保留 schema 的应用回退。它不连接生产，也不能代替真实恢复/配置克隆演练或最终 CI。

开发机建议在本任务专属 checkout 运行：

```text
python3 -m unittest discover -s .deploy/tests -p test_feedback_release_ops.py -v
python3 -m unittest discover -s .deploy/tests -p test_presales3_ops.py -v
FEEDBACK_RELEASE_PG_DRILL=1 python3 -m unittest discover -s .deploy/tests -p test_feedback_release_ops.py -v
```

真实 Docker 演练只能创建本任务独占、network-none、无端口的资源，按精确归属清理；不得使用或删除开发机其他任务容器。本次可选演练使用新 Release.counts，覆盖任意名称/空表/带空格表的完整计数、旧新双头真实恢复、错误计数拒绝与失败资源清理，以及真实 PG 访问审计新增后有限回退、旧审计篡改/删除和同计数业务篡改拒绝。后者的编排、旧 app 启动与 Caddy 是合成 seam，审计行由真实 SQL 插入；不冒称已经启动候选镜像或做了真实认证 HTTP 点按。实际退出码、用例数、日志 hash 与最终源码 hash 完成后再记录。

2026-11-01 开发机独立目录 `/home/cloudlay/work/requirements-20261101-01a1976c/release-safety` 完成复审修复后验证，受管进程真实 exit0，59项中57通过、2项旧 SQL drill 显式跳过，35.802秒；本次23项（22合成+1实际PG多场景演练）全部通过。真实PG15先后恢复旧头、新头两份合成全库，错误计数分支实际拒绝，追加审计后恢复门禁实际放行且保留新审计，篡改/删除旧审计和等数业务篡改均拒绝。随后只读 `docker ps -a` 确认本任务 `itsp-feedback-synthetic-` 容器零残留。全程未连接或写生产。

证据日志 `release-safety/review-fixed-final.log` SHA256 `245be7876753b655f5e239c6667583f1298aef20c1af6555645ee94b3a21384a`；控制器 `b6a60c014727500ad207be4c4d94276eca84003240ecb25f2c7969b824146421`；测试 `7001808eda9ab574f755f1b6bee2e7e96289ab26ed9081196d56d3e6efc75b53`；未经改写的复用库 `60419aeb3ada6165c1968c8af8deffb7d4db30224077eebb94b79898193fda2f`。这是控制器开发证据，不替代最终候选镜像、真实认证 HTTP 冒烟、实际点按、精确提交 CI 或生产备份/恢复回执。

## 精确合成候选与兼容验证

2026-11-01 的开发验收候选为 `f84c672250f5dfd7f5816533a0abf63fa4632850`，Git tree `59ca4a0e6621d2fa9b86513ac24007338992d1f6`，1,906 个文件逐一按 Git blob 核对原始字节。应用版本为1.43.0，候选独立库 `spareparts_feedback_dev`，仅有合成数据。测试源码/运行目录与其他测试进程分开，更新候选未重置已登记事实。

- 前端完整回归204个文件、2,436项全部通过；未放宽用例超时，开发机限制4个worker避免共享CPU争用。TypeScript、Vite构建及生产依赖审计通过。首次完整运行发现采购新定位仍保留旧分组，下钻清理修复后定向与完整复验均通过。
- 迁移链 `upgrade head`、ORM零漂移 `check` 和单头检查通过，目标为 `c7e1a3b5d9f2`。业务定向回归与发布安全验证分别留日志；完整后端仍在运行，不能将定向结果写成完整通过。
- Linux归档修复后，部署脚本/监控/边缘控制的124项测试通过、1项按既有条件跳过。早先 Windows 默认 `git archive` 受 `core.autocrlf` 影响引入CRLF，导致shell测试失败；改用 `git -c core.autocrlf=false -c core.eol=lf archive` 并逐字节校验后重跑。该失败属于验证制品准备，未用改变断言或生产脚本规避。
- 精确候选完成24项接口/交付检查；新的发布只读smoke在后端23项、前端22项全部通过。前后核对所有非 `sys_access_log` 表逐行摘要及旧审计水位，均不变，保留32条真实HTTP新增访问审计。最新回执 `release-safety/readonly-smoke-dev-receipt.json` SHA256为 `381a93b72af3cb583ebb1fb84aa5029fc663485b1cbb87b99569ef4ba7849d6c`。
- 原生 `gpt-6-astra` 独立复审覆盖三批业务改动、发布控制器及构建/smoke准备脚本。已修复规范XSDD漏关联、审计阻断回退和构建输入校验后被替换的问题，并逐项二次复核；当前无剩余可操作P1/P2。实际用户点按尚待反馈，代理页面检查只计开发验证。

有限旧应用回退的实测使用精确旧提交 `e6cfd25c51bc19c1f80d0bad23037d71e6d2aa0e`，1,874个文件逐blob一致，归档SHA256 `7207a5237827240a6f3548aeb5bbf22efdc16c79fa3d7faa6cb3ec3b4b2ebeac`。在本任务独立 `spareparts_feedback_oldcompat_gitbytes` 库（由合成候选一致性dump恢复）启动旧应用，schema前后始终为c7；29项真实HTTP读取/权限检查通过，140张业务与状态表摘要不变。认证、访问/失败审计和请求诊断另列，不将真实登录的系统写入称为零写入。旧应用完成lifespan shutdown后受监督SIGTERM退出，验证器实际exit0，独立18501端口已释放。此证据限定旧应用启动、既有读取与权限防护，不承诺所有旧写路径或保留新版本入口。

以上完整日志和回执位于开发机本任务独占目录及控制机 `.tmp/requirements-audit-20261101/oldcompat-gitbytes-evidence`。最终main/标签/镜像、用户验收结果、必需CI与生产恢复收据仍须在实际完成后绑定，当前不宣称生产已变更。

## 姓名筛选修复与生产镜像兼容复验

首批原提交 `2539156fa5e1e3a4d9ffc4fc989c06147dff1a28` 的 CI run `41317699349` 实际通过：后端8504通过、11跳过，前端202文件、2418项通过，迁移与依赖扫描通过。随后修复复审指出的销售人员选项首尾空白与筛选不一致问题，只规范化看板最终姓名投影，不改变主档优先、人工清空或公共活单众数。独立目录两文件29项回归通过，原生gpt-6-astra复审无剩余P1/P2；新首批提交 `d765f49f337a539365a30e7db3edff184b75b3f4` 重新运行必需CI，旧CI不代替新提交验证。

更新后的合成验收候选为 `4843ee8df12bc78c0c5c848129c6eeaf7b7b0a6f`，Git tree `7b36ebaca18bec6c2d55d558446509e75a83f98e`，1906文件逐Git blob一致；源码树SHA256 `ec70b36991c9d13ecc72d68d6f4f005509d113d1faf9a72af3288ac5acdf25a3`。保留原候选账号、种子和所有已操作数据，未seed、迁移或重置。原已审hook重新通过后端23/前端22项，非访问审计表和旧审计水位不变，保留32条新审计；新回执在 `ui-candidate/revision-4843ee8d/readonly-smoke-dev-receipt.json`，SHA256 `7f1939cf229ac045d1a50ea8de2aae0076d5bc1ca112d6a1fa589ea28c53bbb8`。旧含登录的24项仅保留为历史结果，本次不重复登录或改账号状态。用户点按仍待反馈。

旧源码兼容验证的解释器实际为Python3.12.3，另已补测精确生产镜像 `sha256:d5eea20acbf7b9f8f72f834ee4d220fbf1633111705c9ae9c9c6a6f9858522e5`（OCI e6cfd25c51bc19c1f80d0bad23037d71e6d2aa0e，实际Python3.11.15）。复用原独占合成c7库，29项实际HTTP/权限检查通过，140张业务和状态表摘要不变；认证及审计仍按原明确例外记录。容器正常exit0后按精确ID删除、18501释放、本库客户端0、原PG身份与网络未变。首轮功能检查通过但结束时端口探测误报导致整体exit1，原失败收据保留；修正端口释放判据后在独立run-2完整复验，验证器真实exit0。最终回执 `oldcompat-production-image/run-2/compatibility-receipt.json` SHA256 `5fdc8c06e5e04326fc165a5fa472f4418e6de1c82587371e0f2cda92a6f4d793`。这是同一有限读取/启动兼容范围，不扩大到所有旧写路径或暴露后的自动回退。

## 2026-11-02 UTC 集成进度与未完成检查

首批修复提交 `d765f49f337a539365a30e7db3edff184b75b3f4` 的精确CI run `41325377895` 已实际成功：后端8505通过、11跳过，两项必需检查均通过，讨论已解决；PR #506 正常合并到 `5358b90368153871279f936c1ae20f185e4dcf73`。这表示进入main，不表示已上线。

完整本地后端分段回归先完成6244项、跳过6项，在采购订单对照断言处停止。独立ORM参考函数补齐新增产品/关联信息，并增加非空、过期关联和字段权限样例，保留完整结果等价及性能断言；修复提交 `cc38bb53` 的采购等价/反馈两文件27项实际通过，独立复审通过。继续运行后完成569项、跳过1项，随后既有 `test_thirty_thousand_rows_full_walk` 虽完整返回30000行/60页且无重复，仍因约426秒超过180秒预算失败。该文件独立复跑36项通过，规模用例仍约428秒失败；没有修改性能阈值。该失败尚待定位，之后64文件1585项尚未执行，不能宣称后端全量通过。

PR #507 的新增复审修复为 `a81c90ae`：数量/日期单元格同值重输不会误判为业务变更，元信息允许受限空白格式行，仍严格要求五个唯一字符串签名键。普通原样保存本身未复现类型转换，修复针对实际重输/粘贴类型变化；文本、只读字段、基线签名及确认绑定保持严格。新增20项参数化边界用例，原生gpt-6-astra静态复审通过；本提交的数据库回归及必需CI仍待真实结果，旧提交结果不代替。

最终Python3.11应用镜像与Nginx前端的开发验证脚本已独立静态复审通过，待最终main/标签及镜像构建后实际执行。它使用既有合成兼容库和固定只读hook，要求后端23/前端22项及四项项目读取实际通过，前后业务表、旧审计、PG身份/网络守恒并保留新访问审计；未知资源归属或清理失败均阻止通过。静态审查不等于最终镜像验证。用户已选择自行安排候选实际点按，尚未反馈通过；生产仍为旧e6/a3基线，未执行生产变更。
