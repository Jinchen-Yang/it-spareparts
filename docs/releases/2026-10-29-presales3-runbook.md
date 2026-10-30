# 2026-10-29 售前与刷新性能发布 Runbook

决策: D-0048、D-0049

本轮由杨金臣明确授权当前 Codex 主会话完成修复、保守细节裁定、正常合并与生产发布。此文是操作门禁，实际执行结果另记回执；未填入精确提交与证据前不代表可以切流。

## 范围与基线

售前 PR #492 补齐无单据主动报价、报价生命周期、统一当前答复/处理状态、税 null 保留、图片、快速建档与兼容回归；独立性能 PR 必须有实际生产日志独立复核和远端前后证据后才能集成。禁止顺带更改库存范围、氚云配置、测试账号权限或既有询价业务状态。

2026-10-29 只读核查基线：真实业务主机 `ybwznt`（zs0），正式域名 `ybwznt.com`、`www.ybwznt.com`；`it-spareparts-prod` 是旧边缘跳板。实际 Caddy 上游为 `it-spareparts-frontend-r142:80`。前后端 OCI revision `99e8a43ee6a544df2f990e7782bace0749445c7f`，标签 `release-r142-99e8a43-20261028`，数据库 head `d6e8f0a2b4c6`。旧 `/opt/it-spareparts` checkout 与真实运行版本不同，禁止在该目录直接 pull/compose 替换。

生产现有发布目录 `/data/spareparts/releases/r142-99e8a43ee6a544df2f990e7782bace0749445c7f` 中的 `r142-ops.py` 可作为受审蓝绿克隆/Caddy CAS 参考，但原脚本针对无迁移发布；它的备份只做 TOC，没有真实恢复，不能原样用于本次。

## 放行条件

本轮先正常合并性能 PR #494，再将售前 PR #492 同步到 main；尚未部署的 e9 迁移父头改为 c6，形成唯一链 `d6e8f0a2b4c6 → c6e8a1b3d5f7 → e9b3d5f7a1c4 → f1a3c5e7b9d2 → a3c5e7f9b2d4`。不先发布独立售前版本。组合预验 `291cd01c` 在 jinchen-dev 完成全售前与目录投递/解析 564 项通过、2 项可选 LibreOffice 跳过，另有生产 d6 往返与已有独立报价/已提交 outbox 拒降完整性 2 项通过，源码前后摘要一致。后续审查另收口报价库超过 200 个 PN 的分批读取、旧报价更新保留未明确修改的税价标记，以及已完成停写阶段禁止误恢复的边界；这些增量须保留独立复审和专项验证证据。最终集成提交必须核对与预验树的全部差异，并完成其自身完整 CI；预验或早期运行期间收到文件同步的诊断性全量均不能替代最终候选门禁。

1. 最终 main 精确 SHA 的来源仅为本次授权范围；与现生产 diff 逐项核对。
2. jinchen-dev 独立数据库完成售前真实触发路径、后端全量、前端 tsc/Vitest/build；空库升级、唯一 head、ORM 零漂移、降一级再升及拒绝破坏性降级均有证据。
3. 跨模型独立复审无未解决 P0/P1/P2；记录模型、实际完成状态、发现与修复。
4. PR 同步 main、讨论解决、两项必需 CI 全绿，正常 merge；合并后 main 的 CI 与精确部署身份再次核对。禁止 --admin、强推、修改或绕过分支保护。
5. 候选控制脚本、源码归档和前后端镜像都绑定该 SHA；不把未提交工作区打成生产源。生产写入前创建并回读 `release-` 标签，必须精确指向已验证的发布提交；标签存在不能代替实际切流和运行回执。

## 操作顺序

**D-0049 顺序门禁优先于下列单系统步骤。** 最终 head 为 `a3c5e7f9b2d4`，父为 `f1a3c5e7b9d2`，仅新增源版本技术触发器，不回填 PN。按 `ITSP hold-source → Shop preflight/freeze（双方 TCP0 后最终版本门禁）→ Shop backup-old/migrate/backup-new/create/activate/verify/seal-handoff → ITSP preflight/images/freeze/backup-old/migrate/backup-new/bluegreen/cutover/verify` 执行。两个最终 SHA 的正常合并、CI、release 标签和制品身份必须先完成；不能以旧候选预验替代。

`hold-source` 是实际停源阶段，在商城 preflight 前执行。控制器先读取精确四源实例的 ID、image、StartedAt、全容器指纹、旧 schema、Caddy 字节/inode/active 与导入门禁，并把这些与两份最终 manifest hash、两候选 SHA 写入独占、fsync 的 intent，之后才能 stop 四个源 writer。全部停止、源 TCP0、完整保护及导入门禁复核后才写 PASS。旧 frontend、数据库、Caddy 与其他站点保持受保护；源 API 暂不可用属于此停写窗口。

商城完成最后的原保护集合核验并写 `seal-handoff.json` 后，ITSP `preflight` 才可接受该同一 hold 所绑定的四个源进程已停止。它重查四个 ID/image/StartedAt、旧 schema、Caddy、导入门禁及 TCP0，然后采集**包括新商城所有实例的完整新 protected 基线**。后续 `freeze` 不再启动或再次停旧源，只复核门禁并采集冻结计数/行摘要。任何阶段不能泛化忽略跨系统漂移。ITSP 切流后使用这份完整新基线及商城只读健康、匿名 401/合法凭据空请求 422 检查；不再调用商城旧 Caddy 基线的 `verify`。

### D-0049 manifest 与文件契约

先固定商城 `manifest.json`，包含 `source_hold`：`release`（最终 ITSP 40 位 SHA）、`controller_sha256`（ITSP 控制器实际字节）、`gate_sha256`（共享门禁实际字节）、`writers`（有序四项 `{name,id,image,started_at}`，顺序与 ITSP `old_app` 再接 `additional_writers` 一致）。不得填示例 ID；StartedAt 必须现场回读。

再固定 ITSP manifest 的 `source_handoff`，且只含 `{release,manifest_sha256,controller_sha256,gate_sha256}`：分别为最终商城 SHA、刚固定商城 manifest 实际字节 hash、商城控制器实际字节 hash、相同门禁 hash。两份 manifest 不互相静态包含 hash；ITSP `hold-source` 回执再绑定实际 ITSP manifest hash。两个 catalog token hash 必须一致。ITSP `target_head` 必须为 `a3c5e7f9b2d4`。

ITSP 固定目录中放 `presales3_ops.py` 和 `catalog_source_version_gate.py`；商城固定目录中放 `shop_pn_ops.py` 和同字节 `catalog_source_version_gate.py`。另按原契约放源码归档、只读 smoke、导入审查、凭据与 manifest。互载控制器和 helper 均先校验实际字节 hash，再 compile 已验字节，不执行未绑定 import、不写 pyc。两入口均先持有 `/data/spareparts/releases/.catalog-source-handoff.lock`，再取原单系统锁；锁只保护本次控制阶段，不替代实际状态复核。

1. 在可信控制机从精确提交生成源码归档和 SHA256；传到本次唯一发布目录（700），文件600。构建前后端镜像，OCI revision 均等于发布 SHA。构建允许使用现有 Dockerfile 的包索引参数；依赖版本及哈希锁不变。
2. 冻结真实 Caddy 文件字节、inode、active JSON、上游；记录旧应用/前端容器 ID、镜像、网络及完整运行配置（仅生产私有证据）。记录其他站点/DB/商城保护快照，选择空闲 loopback 端口和不冲突的专用网络。
3. 按下节精确导入门禁确认没有真实活动导入、并行部署或迁移；不能把持久预览一概当作后台任务，也不能按类型或年龄泛化豁免。进入短暂停写窗口，完整记录被停止的本系统活动应用；不得停止 DB、Caddy、商城或无关容器。只读核查发现 r142、presales2、presales、shortkey 四个本系统 app 同连正式库，须按 manifest 中明确 ID/镜像逐个停写；停止后核本库 TCP 客户端归零，不按名称宽泛停止其他容器。旧版本实例在成功发布后保持停止。
4. 备份完整生产 DB、上传目录、运行配置和 Caddy 基线；验证大小、SHA256、TOC。真实备份留在生产，不下载业务数据。
5. 用专属 `--network none`、无端口、资源限制的 PostgreSQL 15 容器真实恢复旧 head 备份。逐表核对 `maintenance_project`、`maintenance_project_contract`，以及 dim_part、销售/采购/维保行等冻结计数；检查 head。按确切容器 ID 和归属标签清理本次恢复容器。
6. 用候选镜像、生产原有运行环境和受限锁等待执行 `alembic upgrade head`，验证唯一目标 head 和 ORM 一致性。不得使用旧 r142 的只读 heads/current 代替迁移。
7. 在停写窗口生成第一份新 head 完整备份，再做一次隔离真实恢复并核 head 和逐表计数。旧恢复脚本固定过期 head，不能直接调用。
8. 以原运行配置克隆本次 app/frontend，基础允许差异为镜像/发布标签、容器名、专用网络、loopback端口和前端反代新 app 的只读配置。没有显式 `catalog_sync` 时，旧 Env 完全保留；仅性能候选按下节已审配置启用目录同步时，允许 app 的三个指定环境键改变。不改宿主 .env、数据库容器、系统设置或其他站点。真实 Nginx 有三处旧 app 上游，preflight 绑定完整文件 SHA256 与出现次数，只替换这些目标字节；app 不允许宿主机发布端口。
9. 候选 loopback 只读冒烟：health/db、匿名拒绝、已有合法身份读取售前询价/报价库、当前答复、最新报价查询、前端 ask/quote/parts 页面及资产；核新镜像 revision 和迁移头。禁止生产测试建档、创建询价、报价、催办或强制同步。
10. Caddy 文件及 active 配置 CAS 改动本系统目标上游，validate/reload；确认其他站点不变。公网两域名只读冒烟，核页面资产与候选一致。
11. 0/5/15/30 分钟观察自然流量：5xx/异常、重启/OOM、DB锁、同步积压与健康。汇总性能自然流量时注明样本窗口，不拿开发机合成耗时冒称生产改善。
12. 发布标签绑定精确部署 SHA；回执记录真实切流时间、镜像 ID、head、备份及两次恢复、冒烟与观察。保留旧容器及备份，不清理其他发布的资源。

## 导入停写门禁

默认 manifest 不含 `import_gate_allowlist`，要求 `sys_import_batch` 和 `sys_import_job` 均无 processing。返件异步任务另查 `maintenance_doc_import_batch.report_json` 中 `protocol=return_receipts_v1` 的 queued/processing，任何一条均阻断，不能只看归档表已 success。

本轮只读核查发现三个维保持久预览 batch 282/428/464，和一个孤儿作业账本 job 137。严格例外需显式 `import_gate_allowlist`，字段仅为 `batches`（三个 `{id,row_sha256}`）、`jobs`（一个 `{id:137,row_sha256,batch_id:230,batch_row_sha256}`）、`review_sha256`。发布目录还须提供受审 `import-gate-review.md`，其实际字节 SHA256 与 `review_sha256` 一致。没有这个显式快照和复审证据，不放行。

三个 batch 的协议必须精确为 `maintenance-project-batch-transfer-v1`、file_type 为 maint_bulk、无 import_job_id，并具有 public 对象、plans 数组、row_map 对象和合法 token_hash 形状；只允许旧 batch 282 缺 archives，其余须为数组。任何 result/applied/applied_at/selection_hash/failure/cancelled/cancelled_at/canceled_at 键都会拒绝。这里仅验证 token_hash 形状，不输出其值。每条完整数据库行以 UTC 会话下 `SHA256(to_jsonb(row)::text)` 绑定，预览内容、审计字段、计数或状态变化都会阻断，不重新采样覆盖已审指纹。

job 137 仅作为未完成的孤儿账本保留：其完整行及唯一关联 sales success batch 230 的完整行均须匹配已审摘要；total_files=done_files=1、error_files=0、finished_at 仍为 NULL，且 created_at 早于所有当前实际同库 writer 的 StartedAt（同时核原 ID/镜像）。复审还须证明对应旧源码使用进程内 daemon 且没有重启恢复路径。不能把这些条件解释为作业成功完成；post_import_refresh 是否完成仍未知，保留原账本不改状态、不重放、不触发业务刷新。

`preflight`、`freeze` 停写前及停止全部 writer 后均运行相同只读门禁，后两次结果必须与 preflight 相同。停写后还必须 TCP 数据库客户端归零。任何未知 processing batch/job、已审例外消失/变化、活跃返件任务、writer 身份或时间不符均拒绝；只把技术 ID、整行摘要、检查布尔和例外风险写入私有回执，不输出业务正文或凭据。合成验证在 jinchen-dev 执行，证据 `presales-import-gate-unit.log` 和 `presales-import-gate-sql.log`；后者使用独占、network none、无端口 PostgreSQL 15 验证真实 JSON/整行摘要查询。

## 性能候选的目录同步配置

本轮在 #494 最终性能代码正常集成后，只发布一次 #492 售前与性能的最终组合候选，迁移链为 `d6e8f0a2b4c6 → c6e8a1b3d5f7 → e9b3d5f7a1c4 → f1a3c5e7b9d2 → a3c5e7f9b2d4`；先验证商城 receiver，再显式启用 ITSP `catalog_sync`。本轮 D-0049 交接要求显式绑定相同的 catalog token hash；无配置时保留原 Env 的底层函数继续有单测，但不是本轮可用的发布旁路。只有包含 #494 与 D-0049 最终已审代码的候选才能配置：

```json
"catalog_sync": {
  "enabled": true,
  "url": "https://shop.ybwznt.com/api/integrations/catalog/parts/sync",
  "token_sha256": "<独立服务令牌的64位小写SHA256>"
}
```

对象只接受这三个字段；不接受关闭值、其他 URL 或明文 token。明文只来自本次 root/0700 发布目录内固定的 `catalog.token.private`：必须为 root/0600 普通文件、不可为符号链接、32–256 字节可打印 ASCII、无空格或换行，且与 manifest 哈希一致。该令牌必须与商城接收端配置对应，并独立于图库、既有目录读取、PN 写穿和其他既有凭据；不把明文写入 manifest、命令参数、聊天或报告。

显式启用时仅允许 app 的 `SHOP_CATALOG_SYNC_ENABLED=true`、固定地址的 `SHOP_CATALOG_SYNC_URL`、文件中读出的 `SHOP_CATALOG_SYNC_TOKEN` 三个键改变。其他 Env 条目的顺序与值、frontend Env、其余运行配置继续保持并逐字段核对。明文只进入生产私有的环境探针文件和容器创建 JSON。

在商城封存交接后的 ITSP `preflight`，用精确候选镜像、`--network none`、只读根文件系统和受限资源，只导入 `app.config.Settings` 验证三个真实模型字段存在且接受实际候选值，不导入应用或数据库。探针只输出布尔值，旧售前镜像即使忽略未知环境变量也不能冒充已支持。clone 还要求已完成该探针的 preflight 回执，并再次读取/核验令牌文件哈希。

先按商城窄发布计划发布并验证接收端，再发布和启用 ITSP 生产者。上线后观察真实自然新增 PN 的 outbox 与接收回执；没有自然样本时明确记录等待样本，不为了验证而创建生产 PN、报价、库存流水或其他业务记录。合成投递时长不能当作生产效果。配置控制路径验证见 `jinchen-dev:/home/cloudlay/work/release-evidence-01a188b2/presales-catalog-ops-unit.log`（25 项合成测试通过，包含旧镜像拒绝、默认环境不变、三键范围、令牌权限/哈希/复用及原发布控制回归）。

## 回退边界

迁移 `f1a3c5e7b9d2` 允许报价没有来源单据，并新增独立受众。已有独立报价时 downgrade 明确拒绝，不能删除报价、伪造 BJ 或恢复旧 dump 覆盖新业务数据。

控制器 `.deploy/presales3_ops.py` 的每阶段由 manifest SHA256、控制器 SHA256、意图与完成回执绑定。D-0049 恢复仅走 `recover-hold-source`，且仅限**商城首次可变阶段 intent（最早 freeze）之前**：包括 hold 部分 stop 失败或 hold 已 PASS，但必须原 schema/Caddy/全保护指纹/四源 ID/image/StartedAt/导入门禁仍一致。恢复后逐实例 Running 与健康、主站数据库健康及保护/导入门禁复核通过才写 PASS，不还原数据库。配置 Docker Healthcheck 的实例必须 healthy，不能降级；现场四旧源均没有该配置，仅此类实例在精确 CID 内读取 loopback `/health/db`，要求 HTTP 200、status=ok、db=reachable，并在结束前再次逐实例检查。探针不走代理、不跟随重定向、响应最多4096字节，HTTP限3秒、exec限8秒，重试段最多120秒加在途单次探针；不伪造 Docker Health 字段。恢复 intent 一旦出现，该 hold 不能再被商城接受；不得删除任何 intent/receipt 伪造可重跑状态。

商城 freeze intent 已出现（即使失败、超时或结果不明）、商城任何后续可变阶段已可能发生，或 ITSP 迁移/暴露已可能发生，均禁止自动恢复旧源。ITSP `recover-freeze` 与 `rollback` 同受此限制；保留冻结并受控前滚。商城自身未完成 freeze 的受审窄恢复不授予恢复旧源的权限。

- D-0049 商城可变边界之前：仅上述原身份恢复；边界之后即使 ITSP 尚未对外放行，也保持源冻结、受控前滚，不能恢复缺少单调约束的旧 writer。
- 对外放行之后：默认只允许保留数据的前滚修复。没有证明旧镜像兼容全部新 schema/新记录前，不自动回切旧镜像；不得以数据库快照覆盖线上新写入。需要停写时只影响本系统，并保留调查证据。
- SSH 结果不明时先重新读取实际状态和回执，不盲目重复备份、迁移、切流或清理。

## 已知保守选择

最终前端复审收口两项 P2：整单自动回复前核对同询价的未保存行，只豁免本次已保存的值；保存、回复和既有报价选择期间暂禁行编辑，保留未提交输入，全部保存后仍按原口径自动回复。询价详情及催办响应仅在同一询价、同一仍可见客户且名称字段省略时继承列表名称；显式 null、客户变更及权限裁剪不恢复旧值，不整体合并被裁剪字段。

开发机 `frontend-p2-01a188b2` 以精确17871源码加13项新回归验证旧版7失败/6通过；两文件修复后5个测试文件103项全部通过、tsc及Vite构建通过，受管进程实际exit0，源码前后摘要一致 `91650f7ddb902f7ce6f60e389e462559e5e61f584d2cb0e9d8b9f0796353692c`。测试日志SHA256 `5b85fc3e7a121db229483f1965b584e6134991f46a15f31d186e1760510a51d8`；原生gpt-6.1-sol已绑定四文件字节静态PASS。原17871完整CI8148后端通过/11原跳过、2396前端通过仅为旧候选证据，此增量仍须新精确提交完整CI，不替代发布门禁。

2026-10-29 23:01 UTC 再次只读核验：126个生产容器指纹、四源身份/StartedAt、两库schema、Caddy/NGINX及导入指纹未漂移。四旧源均未配置Docker Healthcheck，修复恢复确认对此旧形态的误判，未更改正常发布路径。jinchen-dev独占 `d49-ops-01a188b2/legacy-health-final` 完整104项全部通过、无skip、118.737秒，受管进程exit0；新增真实官方Python3.10容器无Healthcheck时loopback200接受、503拒绝，network-none且不发布端口，精确归属清理完成。其他真实PG15恢复和部分停源恢复再次通过；独立gpt-6.1-sol静态PASS。仍须最终精确提交CI，不以该演练替代生产门禁。

D-0049 控制实现开发验证：`jinchen-dev:/home/cloudlay/work/d49-ops-01a188b2/tests-final.log`，89 项全部通过、无 skip，受管执行真实退出码0。包括既有15项商城阶段守卫、源/商城交接失败路径、真实PG15元数据只读SQL与两系统旧/新头恢复、实际Docker配置克隆，以及4个独占network-none进程真实部分stop失败/恢复。停源演练中的schema/Caddy/导入/TCP/HTTP为合成seam，未连接生产；SQL和恢复由独立真实PG15用例覆盖。日志SHA256 `6afd2ed5d2e53fbbf7fd04429d911a16c452b95baf096907c3c124534fb7514b`。这不代表生产门禁已执行或最终提交已通过CI；控制器最终字节仍须跨模型独立复审。

精确显示名对应账号缺少独立测试身份标记，64条历史领取均已完成。本轮不依据显示名或 admin 角色撤权、不改派已完成单据。库存三库明确属于氚云。主数据“待补全”筛选及全系统账号标记能力不作为本次修复的依赖，不扩张本轮数据范围。
