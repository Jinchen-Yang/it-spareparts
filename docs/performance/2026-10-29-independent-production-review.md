# 生产独立只读性能复核与发布准备

复核：2026-10-29 17:36–17:44 UTC（10:36–10:44 -07:00）。执行者为当前接管会话的独立子代理。未运行测试、未压测、未执行同步任务、未修改源码/生产配置/业务状态。仅此报告按主代理要求写入仓库外目录。未导出原始日志、查询串、业务行、账号标识或凭据。

## 1. 已证实的生产基线

- `it-spareparts-prod` SSH 别名指向旧边缘主机，当前只有 personal-ai-assistant 服务；它不是 ITSP 业务容器所在主机。旧 `/home/ubuntu/apps/it-spareparts` 已不存在，不得按 v1.20 路径发布。
- 已有受控 SSH 路径：边缘主机使用现有 SSH 身份跳至 zs0。业务主机 `/etc/caddy/Caddyfile`（由 `it-spareparts-caddy-1` 挂载）第 13 行实际反代 `it-spareparts-frontend-r142:80`；正式入口为 `ybwznt.com`、`www.ybwznt.com`。`hbzgc.icu` 仍走边缘隧道，不能据旧入口判断新机工作树。
- 当前前后端 OCI revision 均为 `99e8a43ee6a544df2f990e7782bace0749445c7f`，对应标签 `release-r142-99e8a43-20261028`。
- app：`it-spareparts-app-r142`，镜像 `sha256:a4f89bde409439e94b242ea69fc30776d95c54b0876084145cf42fdf8d0ded57`。
- frontend：`it-spareparts-frontend-r142`，镜像 `sha256:3ff219c973e99ed6df6fe8baf0db9d07abd01e5caed6c271cd920569939d6ba2`，仅 loopback `127.0.0.1:18090`。
- 生产 DB `it-spareparts-db-1`（PostgreSQL 15）当前 Alembic revision 为 `d6e8f0a2b4c6`。
- `/opt/it-spareparts` checkout 停在历史 `c32c6fa16d2f4030db17cd39c443da6a66c12051`；运行源码使用 `/data/spareparts/releases/`。不得对旧 checkout 直接 `git pull` 或 `docker compose up`。
- 本地 `origin/main=592b12f17b2bfea3b8ba7cd5a386ab9f1650295b` 相对生产 revision 仅一个 D-0025 文档提交：`CONTEXT.md` 与新决策文件，共 2 文件 64 行，未夹带额外运行代码。发布前仍须对最终 main 再做一次精确差异复核。
- 最近实际回执目录：`/data/spareparts/releases/r142-99e8a43ee6a544df2f990e7782bace0749445c7f/`。`cutover.json` 记录 2026-10-28 15:24:59 +08:00 PASS；`tag.json` 记录上述 release 标签。

## 2. 实测日志范围与方法

独立读取现在线上 `it-spareparts-frontend-r142` 的既有 Docker 日志，命令只请求现存日志；在服务器内解析后仅带出聚合。Nginx 的实际 `safe_path` format 包含 `$request_method $uri $server_protocol`、`status`、`request_time`、`upstream_time`，不记录查询串与 referrer。格式由 `nginx -T` 实际核实，没有更改日志设置。

- 取日志时请求最近 168 小时，实际本容器仅覆盖 **2026-10-28 07:24:14.988Z—2026-10-29 17:40:04.626Z**，约 34 小时；不能声称完整七天。
- 有耗时的 access 记录 83,790 条，API/health 记录 74,171 条；另有 10 条旧式 access 记录未混入耗时分布。
- API/health 请求中 >=5 秒 130 次，>=10 秒 27 次，>=30 秒 1 次，HTTP 5xx 11 次。
- 分位数采用 nearest-rank，`ceil(n*p)`；数字 ID/UUID/资产路径归一后聚合。Nginx 请求时间为秒；上游多段时间单独解析。
- 这是既有生产流量，可能含历史运维探针，未将其全部归为真实用户。日志不含查询串，无法按过滤条件、页大小、账号或场景进一步归因。

## 3. 已测售前与 PN 请求

| 归一路径 | 次数 | p50 秒 | p95 秒 | 最大秒 | 说明 |
|---|---:|---:|---:|---:|---|
| POST /api/presales/lines/<id>/create-part | 31 | 2.163 | **19.522** | **32.468** | 30 次 200、1 次 504；2 次 >=10 秒 |
| GET /api/presales/inquiries | 1,119 | 1.666 | **5.550** | **26.078** | 11 次 >=10 秒；1109 次 200、2 次 499、8 次 401 |
| GET /api/presales/inquiries/<id> | **13,672** | 1.019 | 1.485 | 4.741 | 13,652 次 200、20 次 499；累计请求时长 **12,458.557 秒** |
| POST /api/presales/lines/<id>/bind-part | 16 | 1.102 | 2.645 | 2.645 | 上游同量级 |
| GET /api/presales/lines/<id>/part-check | 63 | 0.993 | 2.474 | 2.707 | 上游同量级 |
| GET /api/parts/search | 850 | 0.886 | 1.209 | 1.884 | 无 5xx |
| GET /api/parts/suggest | 1,046 | 0.121 | 0.298 | 2.785 | 无 5xx |
| GET /api/presales/library | 4,944 | 0.179 | 0.298 | 1.434 | 无 5xx；总时长 909.558 秒 |
| GET /api/integrations/pn-master/changes | 412 | 0.015 | 0.023 | 4.450 | 无 5xx |
| POST /api/integrations/pn-master/parts/batch-get | 58 | 0.025 | 0.041 | 0.181 | 无 5xx |

主要慢路径的 `request_time` 与 `upstream_time` 的 p95 几乎相同。**已测结论**：后端处理等待占主要部分，不能以 nginx 传输延迟解释这些数字。**尚未证明**：某条 SQL、锁或外部网络调用是建档长尾唯一根因；本次没有在生产执行 EXPLAIN ANALYZE 或带来新业务流量。

其他实测较慢路径：维保 workbook apply 32 次 p95 13.590 秒（max 14.123）；validate 35 次 p95 12.884 秒（max 13.488）；导出 52 次 p95 5.337 秒（max 14.625）；图库写请求 288 次 p95 7.063 秒；商城 resolve 426 次 p95 4.506 秒。这些是后续排查候选，本次不扩大维保高危模块整改范围。

## 4. PN 同步的独立只读聚合

在同业务主机 `it-shop-next-db`，以 `BEGIN READ ONLY`、`statement_timeout=5s` 查询既有状态表及导入运行聚合，不触发同步、不修改商城。

- `catalog_import_state` 最新 full pass：2026-10-29 03:55:40.205Z 开始，10:06:22.384Z 结束，整轮 **22,242.179 秒（6 小时 10 分）**。
- 最近七天 `catalog_import_run`：full completed 441 个批次，单批 p95 1.462 秒、errors=0；incremental completed 1,572 个批次，单批 p95 0.352 秒、errors=0。**单批很快不代表整轮很快**。
- 用户指定 H6Z27A 当前已存在一个已关联目录项，一个商城 PN，snapshot 为 exact、无 snapshot error。目录记录的 upstream_updated_at 为 05:47:50.933Z，商城 PN created_at 为 10:01:22.803Z，观察到约 **4 小时 13 分**间隔。
- `pn_master_sync_state` 最后运行 17:40:04.652Z，last_error 为空。

**可支持的判断**：数小时全量扫描窗口真实存在，足以造成新增 PN 等待；当前 H6Z27A 已恢复可见。**不能支持的判断**：没有原始创建事件与逐次游标证据，不能把上述 upstream timestamp 当作原始业务建档时刻，也不能断言四小时全部由某一任务造成。接下来应在开发机验证新增 PN 增量事件、重试与观测，不要在生产再建测试 PN 或强制同步。

## 5. 资源与观测边界

2026-10-29 17:38Z 快照：主机 load 1.51/2.12/1.83，内存总量约 125.5 GiB、available 约 50.6 GiB，swap 已用约 894 MiB/8 GiB；/data 约 2% 使用、根盘约 10%。app CPU 0.13%、内存约 1.25 GiB；DB 内存约 1.34 GiB。app/frontend/DB restart=0、OOMKilled=false；DB healthy。查询时 DB 41 个连接、Lock waiter=0。没有安装 pg_stat_statements，未启用它。

这些只能说明当前没有明显资源耗尽或锁积压，不能排除过去瞬时拥塞。nginx 请求时长不是浏览器整页等待；详情请求数量很大与客户端串行/并发扇出结合后可能放大等待，应由开发机/源码验证。

## 6. 建议给性能实现者的优先级

1. **建档长尾**：已实测 504 与 32 秒等待。围绕 master_edit 的近似查重/resolver、规格处理和事务路径做开发机分解，保证提交结果未知时不盲目重复创建；不要只调整 nginx timeout。
2. **询价列表与详情扇出**：列表本身 p95 5.55 秒，详情 13,672 次且累计成本最高。优先收敛重复可见性计算/N+1或批量读取，保留完整权限与历史快照语义；不以默认截断待办/页数换“变快”。当前候选 QuotePage 会逐页取待办并按 inquiry 取详情，改动须与售前实现者协调。
3. **PN 即时增量**：缩短等待下一整轮全量导入的窗口；持久化失败重试/可观察性，保留已有定时全量兜底。不能把每批 p95 0.35 秒当“用户同步问题已不存在”。
4. suggest/library 现有 p95 约 0.3 秒，缺少证据支持作为首要性能优化；浏览器端去重请求仍有价值。
5. 同一候选在 jinchen-dev 用合成数据获得前后对比，最后再用上线后的自然流量复核。当前报告没有任何修复后数据，不应写成性能改善已完成。

## 7. 发布与恢复清单（仅建议，均未执行）

1. 锁定最终正常合并后的 main 40 位 SHA；留存两项必需 CI、跨模型复审与迁移测试证据；比较生产 99e8a43 到最终 SHA，逐项对应本轮授权的售前/性能改动。当前 origin/main 相对生产只有文档差异。
2. 从可信控制机的该 SHA 归档源码并校验 checksum，在 `/data/spareparts/releases/` 建独属发布目录；两镜像同 SHA、OCI revision 可核对。不要复用旧 `/opt` checkout 构建，不直接运行旧 v120 脚本。
3. 切流前再次读当前 Caddy 上游、r142 镜像/容器 ID 与运行配置；对 DB、Caddy、商城及其他容器建立保护清单。运行环境与挂载只在生产受控私有证据内比较，不能输出完整 inspect/.env。
4. 保留 r142 两应用容器作为回退目标；仅增加本次候选 app/frontend，选未占用 loopback 端口，保持其余服务和共享网络不变。
5. 备份 DB、上传文件、必要运行配置与 Caddy 基线，目录 700/文件 600，检查大小、sha256、TOC。**必须做实际隔离恢复**，用专属 network-none PostgreSQL 15、资源限制、只删除本次创建的恢复容器，核对恢复库 head 和稳定维保项目两表及约定事实表计数。
6. 迁移前进入受控停写窗口，排除 processing 导入与长事务/锁等待。用本次候选镜像迁移至其真实唯一 head；不能照抄旧任务书的固定 head，不能在旧 r142 仍对新 schema 不兼容时保持写流量。升级后新 head 备份/恢复门禁需按 `docs/DEPLOY.md:374–380` 完成。
7. 候选 loopback 先做只读健康、前端资产、售前与权限冒烟，同时核查商城原只读契约。不得在生产建询价/报价/催办/测试 PN。
8. 对当前实际 Caddy 配置做 compare-and-swap 上游切换并 validate/reload；已有 r142 `r142-ops.py:420–447` 有相应思路，但它是旧发布专用脚本，不能原样执行。
9. 公网 ybwznt.com/www 同源只读冒烟与观察；记录前后端 revision、镜像 ID、DB head、备份恢复结果、Caddy checksum 和真实时间。成功后 release 标签指向精确已部署提交。
10. 回退首先使用保留的 r142 上游；**只有已证明旧代码兼容新 schema才允许这样回切**。新迁移回退必须按实际 downgrade 与已发生新写入评估，不得一键恢复旧 dump 覆盖真实业务。

已发现的历史脚本限制：

- r142 `backup.json` 明确 `actual_restore_performed=false`，只有 TOC PASS；不能把旧回执称作已验证可恢复。
- r142 `migrate.json` 为 from=to=`d6e8f0a2b4c6`、migration_performed=false；其 ops `migrate():301–314` 只核 heads/current，不能用于本次 schema 升级。
- `/opt/it-spareparts/.deploy/restore_drill.sh:25` 仍硬编码 `EXPECTED_DB_HEAD=c6f2a8e9d4b1`，与当前生产不符，不能原样调用。
- v1.20 root authority 仍留在边缘主机且为旧 2026-08-04 发布；不代表当前 zs0 蓝绿状态。

## 8. 测试账号事实与最小防再发建议

只读精确显示名匹配到一个账号：2026-06-15 创建，active、未删除，admin/admin 模板，售前页面与报价动作均有效。历史 claimed_by 聚合为 64 个售前待办，当前均 done。未带出账号 ID/用户名/操作者。

未找到该账号的 sys_user 创建或更新审计、携该显示名的账号审计；用户名不匹配常用 test/demo/sandbox/qa/internal/neice 模式。模型无 test/demo/purpose/environment 标记列。仓库检索除了计划/todo，未发现该精确显示名的测试脚本证据。**因此测试身份未被独立证实**，不能按显示名或 admin 角色自动禁用/撤权。

已证实允许领取的技术路径：`workflow/work_items.py:28–43` 实名/active检查后用有效权限，`:138–167` 的 claim 校验业务页/池成员/版本/暂停，`:419–430` 候选列表按同样有效权限；`presales/registration.py:191–192` 使用 `action_presales_quote` 池。没有测试身份隔离。

最小保守方案交主代理决定：显式 `is_test_account`（默认 false，绝不按名称回填），管理员在账号管理中确认标记并留审计、递增 token_version；对已标记者在 candidates、_target、claim 等新增业务参与入口统一拒绝，保持已有业务单据与只读权限不动。身份未确认前仅交付标记能力/风险记录，不自动设置该账号，不改派64条历史待办，不排除全部管理员。

主代理最终范围裁定：本轮账号身份无独立证据且64条均已完成，仅记录风险与原因，不按显示名撤权，不扩大账号体系；上述标记能力为后续建议，本次不实施。
