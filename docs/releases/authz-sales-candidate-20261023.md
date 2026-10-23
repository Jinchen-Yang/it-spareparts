# 销售分析模块授权候选：发布、验收与暂停回退

决策: D-0026。代码核对时间：2026-10-23 12:36 UTC。

**S0 历史候选说明：S1 后权限控制面和切换流程已变化，当前候选以 [统一授权运行说明](authz-unified-candidate.md) 为准。**

**当前状态：候选文档，未部署，未完成生产实名验收。** 本文不提供生产写入授权；执行须按 `AGENTS.md`、`docs/BRANCH_POLICY.md` 取得本批明确部署授权，绑定已审查的精确发布 SHA 与 `release-` 标签。前后端、迁移、PDP 政策必须来自同一发布目录，不能用整条 main 的状态代替精选发布内容。

本批只将销售分析切到新权威，采购、库存、维保、附件和 Agent 仍为 legacy。维护客户归属不会改订单成交销售、导入事实或业务单据。不得把此发布表述为全系统权限已迁移。

## 1. 已核实的运行契约

| 项目 | 当前实际值/路径 | 发布含义 |
| --- | --- | --- |
| 首次启用开关 | `AUTHZ_SALES_ANALYSIS_ENABLED`，`backend/app/config.py` 默认 `false` | 未启用时保留旧销售分析链路；管理 API 仍可提前维护新授权。 |
| 暂停开关 | `AUTHZ_SALES_ANALYSIS_SUSPENDED`，默认 `false` | `true` 时快照仍标记新权威 enabled，read/export 为 false，销售 API 返回 503；不回到旧角色门。 |
| opt-in Compose | `compose.authz.yml`，叠加 `docker-compose.yml` | overlay 将 ENABLED **固定写为 true**；它不是读取 `.env` 中的 ENABLED 值。SUSPENDED 才通过环境插值，默认 false。 |
| PDP | `ghcr.io/cerbos/cerbos:0.57.0@sha256:e5e3b150b3cb0090975605254f255164567d67e233634199fb20972e3eaff300` | 精确镜像，不改浮动 tag；没有宿主机端口，只有内部 `authorization` 网络。 |
| PDP 文件 | `deploy/cerbos/config.yaml`、`deploy/cerbos/policies/` | 只读挂载到 `/config/config.yaml`、`/policies`；政策自动热更新关闭，变更须审核并重启 PDP。 |
| app → PDP | `CERBOS_URL=http://cerbos:3592`、`CERBOS_TIMEOUT_SECONDS=2` | 不依赖 Cerbos Hub；网络故障失败关闭，不能启用旧权限 fallback。 |
| 管理与快照 | `/api/authz/me`、`/api/authz/catalog`、`/api/authz/users/{username}`、`/api/authz/customers` | 管理限当前有效实名 admin；共享口令、旧身份标记、普通账号管理能力都不能代替实名管理员。 |
| 页面 | `/accounts` → 账号行「销售分析授权与客户归属」；`/sales/analytics` | 前端 BrowserRouter 路径；验收需记录实际 HTTPS 基址及可点击完整 URL。 |
| 新迁移 | `f1a3c5e7b9d2`，父版本 `d8f6a2c4b9e7` | 文件 `backend/alembic/versions/f1a3c5e7b9d2_authorization_sales_customers.py`；新增 `sys_authorization_grant`、`sys_customer_assignment`。 |
| 存量授权 | 按冻结的旧 `effective_for_user` 页面资格迁移 | admin/boss 默认 all，其他角色 assigned_customers；旧**实际生效** own=true 强制 assigned。admin 的 raw own=true 不等于旧实际生效 own=true。 |
| 新账号/客户归属 | 新账号无 grant；迁移不生成任何客户归属 | 无 grant 拒绝；assigned 空归属允许页面但所有订单范围为空；不猜手机号、姓名或成交销售名。 |
| 字段组合 | 固定六类 `data_*`；profit、reconciliation 均依赖 purchase_cost | 新管理保存拒绝非法组合；迁移删除缺少成本权限时的利润/对账权限，不扩大成本权限。 |

首次启用前可以保持 ENABLED=false；**一旦正式启用，本 runbook 的回退状态必须为 ENABLED=true、SUSPENDED=true**。`service.enabled()` 的 OR 逻辑另有纵深保护，但运维不得依赖将 ENABLED 改 false 来表达暂停。

## 2. 发布前冻结与备份

1. 记录实际生产 release 标签、提交、前后端镜像 ID、数据库 `alembic_version`、Compose project/文件列表、活动蓝绿槽位、入口代理配置与运行环境文件位置。历史发布曾使用 `it-spareparts-app-bg` / `frontend-bg`，不能假定本仓库的逻辑服务 `app` 就是当前活动生产容器。本次不凭历史文档宣布生产版本。
2. 核对本次发布与实际生产的完整 diff，排除未批准功能；精确发布 SHA 的两项必需 CI 通过、独立复审已记录。保留 backend/frontend 构建及 PDP 镜像 digest，确认 frontend 与 backend 属于同一 SHA。
3. 确认至少一个能正常登录的**实名**管理员；管理员角色字符串或共享 admin 口令不够。准备服务销售验收账号、明确客户清单及每个客户的 `DimCustomer.id`/规范名/原始名。业务负责人确认共同归属和历史订单覆盖范围。
4. 在受控备份目录保存完整 PostgreSQL custom dump、角色/权限定义、实际运行配置与镜像/入口回执，文件限制仅运维可读；不把连接串、密码、会话 token 或完整配置输出到普通日志。保留原始文件卷，禁止 `down -v`。

以下命令以 Bash 数组表示**已核验的目标栈**，不是让操作者新建同名生产栈。先将部署适配器的实际 project、受控 env 文件及已有 override 保留到 `AUTHZ_BASE`；若使用独立蓝绿编排，应执行等价命令并记录映射，不能直接覆盖活动槽位。

```bash
# 在精确发布 SHA 对应目录执行；两变量由当前部署回执填写。
: "${AUTHZ_COMPOSE_PROJECT:?填写已核验的目标 Compose project}"
: "${AUTHZ_RUNTIME_ENV:?填写受控运行环境文件绝对路径}"
: "${AUTHZ_BACKUP_DIR:?填写本次独立备份目录绝对路径}"
AUTHZ_BASE=(docker compose --project-name "$AUTHZ_COMPOSE_PROJECT" --env-file "$AUTHZ_RUNTIME_ENV" -f docker-compose.yml)
# 如有现用 override，在上一数组中逐个加入，保留其顺序。
AUTHZ_NEW=("${AUTHZ_BASE[@]}" -f compose.authz.yml)
"${AUTHZ_BASE[@]}" config --quiet
"${AUTHZ_NEW[@]}" config --quiet
umask 077
mkdir -p "$AUTHZ_BACKUP_DIR"
"${AUTHZ_BASE[@]}" exec -T db sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$AUTHZ_BACKUP_DIR/database.dump"
"${AUTHZ_BASE[@]}" exec -T db sh -c 'exec pg_dumpall -U "$POSTGRES_USER" --globals-only' > "$AUTHZ_BACKUP_DIR/globals.sql"
test -s "$AUTHZ_BACKUP_DIR/database.dump"
sha256sum "$AUTHZ_BACKUP_DIR/database.dump" "$AUTHZ_BACKUP_DIR/globals.sql" > "$AUTHZ_BACKUP_DIR/SHA256SUMS"
"${AUTHZ_BASE[@]}" exec -T db pg_restore --list < "$AUTHZ_BACKUP_DIR/database.dump" > "$AUTHZ_BACKUP_DIR/restore-list.txt"
```

每条命令均须检查退出状态；不能只凭 dump 文件存在判定成功。`globals.sql` 可能含密码哈希，按数据库备份同级保护。

5. 将备份恢复到**独立项目、独立数据卷、无生产挂载/连接串**的 PostgreSQL，执行实际 `pg_restore --exit-on-error`；清单解析不算恢复演练。核对用户/客户/销售订单/明细/对账快照计数、关键约束、序列及迁移版本，与备份时记录对齐。角色与权限定义也须核对；使用 `--no-owner/--no-privileges` 的演练不能声称验证了原有 ACL。保存恢复日志与校验结果，不输出业务明细。
6. 在该恢复库上用**同一候选 backend 镜像**演练下节迁移。检查授权数量/范围与旧有效资格一致、迁移前后业务事实计数不变、初次迁移的客户归属为零；检查非法 profit/reconciliation 组合被收紧。恢复及迁移演练失败则不切换生产。

## 3. 首次发布：先迁移与配置，再切换权威

1. 仅限此前从未启用新模块的目标环境：保持新模块默认关闭，构建精确发布的 app/frontend，尚不切换活动入口。备份必须已通过上节恢复演练。
2. 读取当前 migration revision 与唯一 head。若当前已是 `f1a3c5e7b9d2`，不再回退重播 seed；保留已配置 grant/归属。未知 revision、多头、或跨越额外未审迁移均需先重新核定迁移清单。

```bash
"${AUTHZ_BASE[@]}" run --rm --no-deps app alembic current
"${AUTHZ_BASE[@]}" run --rm --no-deps app alembic heads
```

3. 本批父链含 `d8f6a2c4b9e7` 的并发 GIN 索引迁移。历史生产清单曾停在 `c9d4f2a6b8e1` 并排除该索引，因此发布时必须重查：若仍在 c9，先独立执行 `alembic upgrade d8f6a2c4b9e7`，验证 `ix_batch_source_cancel_receipts` 的 `pg_index.indisvalid=true`，再执行新授权迁移。该父迁移有 autocommit 边界；失败可能遗留 INVALID index，按它的迁移说明排查，不能盲目重跑或当作整个链已回滚。其他更旧基线先单独评估中间迁移。

```bash
# 仅在确认当前版本为已审核父版本后执行本批授权迁移。
"${AUTHZ_BASE[@]}" run --rm --no-deps app alembic upgrade f1a3c5e7b9d2
"${AUTHZ_BASE[@]}" run --rm --no-deps app alembic current
```

4. 迁移完成后才启动/切换候选 app/frontend。首次未启用阶段使用核验过的默认关闭配置；不要提前给活动 app 叠加 `compose.authz.yml`。确认 `/health` JSON 的 status=ok、`/health/db` JSON 的 db=reachable；后者错误也可能返回 HTTP 200，不能只查状态码。
5. 实名管理员通过 `/accounts` 为每个拟开通账号维护「销售分析授权与客户归属」：read/export 分开，服务销售范围为 assigned_customers，逐一核对客户 ID、规范名和原始名。方框多选面板的「确定」只是确认当前选择，仍需抽屉「保存授权」落库。同规范名不代表同一个 ID；共同分配必须明确确认。旧职位模板/page/data 勾选不会同步更新此模块。
6. 保存后检查该账号新版本、审计记录、客户归属，以及旧会话失效；目标用户重新正常登录。修改管理员自身时会退出登录。可在未启用状态提前配置，但此时销售页面仍按旧链路运行，不能把它当成新权限验收。
7. 启动固定 PDP，再将候选 app 切为**新权威但暂停**。在受控环境文件持久化 `AUTHZ_SALES_ANALYSIS_SUSPENDED=true`，保持其他生产参数，且确认当前 shell 没有反向覆盖；后续所有 app 重建均保留 `compose.authz.yml`。仅设置 shell 临时变量不足以保证下一次发布仍暂停。

```bash
"${AUTHZ_NEW[@]}" pull cerbos
"${AUTHZ_NEW[@]}" up -d cerbos
"${AUTHZ_NEW[@]}" run --rm --no-deps app python -c 'import urllib.request; r=urllib.request.urlopen("http://cerbos:3592/_cerbos/health", timeout=2); assert r.status == 200'
# 同 SHA 前后端就绪后，按实际蓝绿/入口编排切换；下面为标准逻辑服务示例。
"${AUTHZ_NEW[@]}" up -d --no-deps app frontend
"${AUTHZ_NEW[@]}" exec -T app python -c 'from app.config import get_settings; s=get_settings(); print({"enabled":s.authz_sales_analysis_enabled,"suspended":s.authz_sales_analysis_suspended,"cerbos_url":s.cerbos_url,"timeout":s.cerbos_timeout_seconds}); assert s.authz_sales_analysis_enabled and s.authz_sales_analysis_suspended'
```

Compose 的 `depends_on: service_started` 不证明 PDP 已健康；因此必须保留显式健康检查。上述读回只输出四个非秘密设置，不输出整个 settings/env。

8. 暂停态由实名会话核验 `/api/authz/me`：enabled=true、read=false、export=false、reason 为暂停；销售分析 API 为 503，其他批准保留模块正常。再次确认 grant/客户归属、客户空范围名单和同 SHA 前后端。
9. 仅在切换验收窗口将受控配置的 SUSPENDED 改为 false，继续保留同一 overlay，重建 app 并读回 enabled=true/suspended=false；随后立即进行下节实名浏览器验收。测试环境应先走完同样流程，生产记录独立填写。

## 4. 必须完成的实名浏览器验收

使用正常登录的真实管理员与服务销售账号，分别在独立浏览器会话中点按。脚本、Mock token、CI 与代理开发期浏览器证据不替代用户对候选/生产的实际点按验收。保存实际环境 URL、精确 SHA、脱敏截图、对应 API 状态与业务负责人确认；不要记录密码或 token。

| 场景 | 可核对的预期 |
| --- | --- |
| 管理员打开账号授权抽屉 | 可读取、编辑和保存；目录仅 sales_analysis 标 migrated，其余 legacy。旧权限中心提示此模块独立维护。 |
| 同规范名不同 ID 客户 | 搜索结果和已选项可用原始名/ID区分，只保存明确选择的 ID；跨搜索选择不丢失。重置/取消不会未经保存改库。 |
| 服务销售已分配客户 | 菜单可进入销售分析；同一客户由其他销售成交的历史及后续订单都可见；未分配客户和未匹配 customer_id 的订单不在范围内。 |
| 用户筛选不能扩大范围 | 客户/销售筛选只减少授权范围；输入未分配客户名不出现其订单；未授权订单详情为 404。 |
| 全读取面一致 | 候选、汇总/同期、新老客、分组/明细维度、趋势/无日期桶、直方图、分页详情与导出，均与预先选定客户订单清单对齐。 |
| 无归属或无 grant | assigned 空归属有页面但范围为空；新账号无 grant 不能进入。不能临时改 all 来掩盖空归属。 |
| 字段与导出 | 无客户字段不能按客户筛选/分组；无成本不能金额筛选/直方图，详情数量与金额组合遮蔽；隐藏字段不能通过排序、对账原订单金额或 CSV 重建。仅 read 的账号不能导出；非法 profit/reconciliation 缺成本组合保存为 422。 |
| 修改与会话撤销 | 保存后目标旧会话失效，重新登录只看到新范围；修改自身需重新登录；两个管理员同版本保存后一方收到 409 并重新加载。 |
| 管理权限边界 | 服务销售不能调用管理 GET/PUT/customers/catalog；共享口令身份不能管理。没有共享登录的环境无需为测试开启共享口令。 |
| 销售筛选多选交互 | 单人姓名可见，勾选/搜索/重置/确定可用；取消草稿不触发筛选；移动端面板不溢出。 |

PDP 断网/停止后返回 503、恢复后正常的故障注入，只在隔离候选环境演练；生产不为验证主动中断所有用户的 PDP。生产观察实际 401/403/404/409/503、延迟与错误日志，区分合法拒绝和服务故障。待实名验收通过再记录本批已部署；没有结果的项明确写待验收。

## 5. 启用后的回退：暂停新模块，保留新权威与数据

触发情形包括范围不符、字段越权、PDP 不可用或不能完成验收。立即停止扩围，在受控运行配置持久化：

```dotenv
AUTHZ_SALES_ANALYSIS_ENABLED=true
AUTHZ_SALES_ANALYSIS_SUSPENDED=true
```

继续使用 `docker-compose.yml + 已核验现用 overrides + compose.authz.yml`，重建 **app**，读回这两个值；保留与该后端兼容的同批 frontend。验证 `/api/authz/me` 仍 enabled=true/read=false/export=false，销售 API 为 503，页面显示不可访问/暂停，其他模块健康。保存暂停时刻、实际镜像/配置、错误证据与影响范围。

**禁止以下“回滚”：** 移除 authz overlay、将 ENABLED=false、回退到忽略新开关的旧后端并继续提供销售分析、或执行 `alembic downgrade`。这些动作可能恢复旧的全公司权限或删除新授权/客户归属，违背 D-0026。保留 `f1a3c5e7b9d2`、授权、客户归属、版本、审计和 token_version，不修改旧 permission 快照来模拟新授权。

可在暂停状态修复/更换已核验且仍支持新授权及暂停语义的同代镜像或固定 PDP；没有这样的安全版本时保持暂停并修复，不切旧权威。修复后先在隔离环境复现并验证，再由明确的恢复操作将 SUSPENDED=false，重做受影响的实名验收。整库备份恢复是另行评估的灾难恢复，会影响备份之后的数据，不能当作本批常规回退步骤。

## 6. 发布回执填写项

实际发布时在 `docs/releases/` 另存回执：授权依据、发布标签和完整 SHA、前后端镜像 ID、PDP digest/政策校验值、生产迁移前后 revision、备份路径与哈希、恢复演练结果、实际 Compose/蓝绿映射、四个 authz/PDP 非秘密配置读回、客户归属确认人与授权范围、实名管理员/服务销售验收 URL 和结果、暂停演练结果、观察窗口与最终状态。本候选文件没有预填这些尚不存在的生产结果。
