# S-C-negpn 实施与验收方案

基线：`fix/req00007-negative-per-pn`，HEAD / origin/main / 只读远端 main 均为 `d92da159a1ecb738d4436876d2a366cebdfcaee5`。fetch 因 worktree git 元数据只读失败，使用 `git ls-remote origin refs/heads/main` 核对；不提交、不 push、不用 gh，不访问生产。决策：D-37。

## 三路分析与采用结论

- 用户与场景：紧凑图使用当前授权筛选全集，手工截图页脚消费 `pn_overview.has_negative`；多维排名消费 `meta.has_negative`。浏览器不接收原始事实、不从 Top 数组推导全集状态；无需布局改动。前端增加提示、累计线与 KPI 的消费契约测试。
- 业务与产品：原始负行保留事实方向，但同一稳定 PN 汇总后非负即允许 PN 集中度。NULL 数量按 0 不变。不同 PN、无权项目或筛选外正量不可用于抵消。无业务未决项、迁移或跨库写入。
- 技术与数据：单次 SQL 已按 PN × 项目 × 客户 × 销售 × 业务类型形成聚合单元，必须在这些单元之上再按 `_key(row, 'pn')` 合计 Decimal。共享 `_negative` 同时服务 `_rank` 和响应标志，三处需一致。
- 反例与取舍：同 PN 在项目甲 -3、项目乙 +5 时 PN 为 +2，但项目甲仍为负。仅在 PN 排名采用新判定，非 PN 的既有守卫保留；拒绝把 PN 非负直接用于项目分布集中度，以免产生 250% 或负份额。

## 最小实现与测试接口

先在决策文档落盘，再经既有公开服务 `explorer()` 编写失败用例并最小修改。测试接口为现有 explorer 响应与前端组件渲染：覆盖标志、PN份额、累计值、KPI、筛选权限、Top/页外负 PN、零量、NULL 和身份归并；不测试私有 helper。前端只补消费契约，生产组件继续使用服务端标志。

后端相关测试后跑全量 pytest；前端相关测试、全量 Vitest、tsc、Vite build。任何失败先在同 SHA 干净 main 快照复现，未经复现不得称既有。日志及最终报告统一写入 `R/logs/S-C-negpn-*`。独立代理在实现完成后按需求、diff、测试证据复核。状态停在待用户验收，后续 main 仍须管理员对精确候选 SHA 人工 APPROVED review。
