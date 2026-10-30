# 2026-10-29 售前与性能发布接管

- 负责人：Codex 主会话 `01a188b2-9638-7023-8759-bd179c584a62`。
- 用户授权：接管交接与 Claude 原始日志需求，保留已拍板口径；未决细节由代理采用推荐保守方案，不再询问；按正常发布流程明早完成生产上线。允许多智能体编码和 OMP 事后复审。
- 决策：D-0046、D-0047、D-0048；跟踪 https://github.com/Jinchen-Yang/it-spareparts/issues/493 。
- 本文件是进行中记录，不是完成或上线回执。

## 已确认的范围差异

旧发布任务书把主动报价不生成 BJ、列表图片/放大、便捷 PN 建档、性能同步留到后续；原始人类消息已要求这些。本轮补齐并分工验证。库存三个库原话指定氚云系统，不更改本系统库存口径。原始日志中“智能体慢”指整个系统页面/刷新，不是 AI 聊天延迟。

## 当前分工

- backend_release：售前后端、真正无单据主动报价、来源可空兼容、行答复与生命周期、迁移和契约回归。
- frontend_release：询价/报价与旧深链兼容、税 null 保留、图片、快速建档、失败恢复与 Vitest。
- audit_supervisor：真实生产基线、发布路径、测试账号原因和独立生产日志/请求耗时复核。
- 原独立监督会话 `01a188a7-01c6-7193-a323-e918f68cb2ab` 下的性能工作区 `G:/project/ybwznt/it-spareparts-perf-refresh-latency` 继续做独立性能 PR，不拥有最终发布权限。直接消息工具因 active writer 未送达，最新授权和职责已追加旧 scratchpad 交接及性能任务书；不能把追加文件当成运行中会话已读证明。
- 主会话负责集成、远端完整回归、独立跨模型复审、PR/CI和生产发布。OMP 两轮真实超时，均不计通过，已改由原生 gpt-6.1-sol 复审。已创建本线程每小时巡检 automation（id=automation），真实完成发布与观察后停用。

## 环境与保护

主工作区 claude/epic-keller-xv29cb（PR #492）；已有五个复审补丁保留继续修复。用户 `.claude/launch.json` 和 `backend/spareparts_backend.egg-info/*` 不得暂存、提交、还原。

本轮专用 jinchen-dev 检出 `/home/cloudlay/work/presales-release-20261029-01a188b2`；证据 `/home/cloudlay/work/release-evidence-01a188b2`。测试只能在 jinchen-dev 或 CI。不得停止、修改或连接他人容器。

生产核查：`it-spareparts-prod` 当前为旧边缘跳板；实际业务主机为 `ybwznt`（zs0）。Caddy 实际反代 `it-spareparts-frontend-r142:80`，前后端镜像 revision `99e8a43ee6a544df2f990e7782bace0749445c7f`，标签 `release-r142-99e8a43-20261028`，数据库 revision `d6e8f0a2b4c6`。当时 main `592b12f1` 相比生产仅有 D-0025 文档变更。部署前必须重新核验，不按旧 checkout 或主机别名猜版本。

## 剩余门禁

补丁完成 → 远端真实触发路径与全量回归/迁移 → 跨模型复审修复 → 两项必需 CI（精确候选且同步 main）→ 正常合并 → 核对合并 SHA/生产范围 → 备份及恢复验证/精确 SHA 镜像 → 迁移/切换 → 只读冒烟与观察 → release 标签及真实回执。

2026-10-29 11:25 PDT 进度：f4379e78 已完成新能力，f03e93c9 修正独立复审发现的四项 P2 并收敛询价分页序列化。前端初轮完整 200 文件/2365 用例及真实 UI 链路通过；最新增量与跨单真实 UI 正在验证。后端首轮 104 定向、迁移升降升通过，完整 8012 用例仍运行；目前唯一失败为开发机缺少 shellcheck，须补齐后单项复跑，不能当作已通过。最终候选 CI 未完成，不得部署。

发布准备：已实证三组旧 app 仍同连正式库，冻结清单包含 r142/presales2/presales/shortkey 四组 app，不能只停实际入口 r142。控制器新增部分冻结失败恢复；真实 Nginx 三处上游按文件 hash 和数量约束替换。控制器 14 个合成安全检查及两轮真实隔离 PG15 恢复已通过，仍须独立复审最终字节。

性能：独立生产日志报告已入库；PN 双 HTTP/TLS 开发验证 20 合成样本端到端 p95 376.66ms，不代表生产已改善。配套商城工作区 `G:/project/ybwznt/spareparts-shop-perf-pn-sync`。当前生产商城镜像 4651d1b、schema 0026；main b016c99 多出 78 文件未发内容，最保守方案从真实生产提交派生仅 PN 接收端补丁，不能顺带带上其他 UI/功能。shop_release 正在准备该最小发布方案；尚未写生产。

禁止强推、管理员绕过或修改保护；禁止测试写生产业务数据。新增独立报价后的数据库降级不得删除业务记录或伪造 BJ，必须遵循迁移保护和发布前制定的回滚边界。
