# IT_data 开发交接（2026-08-10）

## 交接结论

- 仓库：`Jinchen-Yang/it-spareparts`。
- 交接分支：`codex/maintenance-manager-combined`；以 GitHub 上该分支最新 HEAD 为唯一代码基线。
- 稳定版与 Beta 仍共用数据库基础设施，但业务表、API、页面、权限和总闸隔离；默认关闭 Beta，不得把集成候选称为已上线。
- 本次没有部署、没有改生产数据库、没有写入真实业务数据，也没有开启成本/库存切换闸门。

## 已开发并在集成候选中的能力

| 模块 | 已实现的边界 | 生产状态 |
|---|---|---|
| 项目工作台与四表 | 项目内合同、回款、领用、报销、提醒可视化及四表导出；缺字段仍展示 | #200 已进入 `main`，现网 SHA 仍需发布时核验 |
| 项目主档与归属 | 稳定项目、合同、项目经理、来源单显式关联 | 集成候选；未做真实数据映射 |
| 维保需求单删除 | 搜索、跨页选择、全量复核、理由、两次确认、服务端 7 秒闸门、逻辑删除审计 | 集成候选；未开生产权限 |
| 项目经理工作台 | 只看本人负责项目的系统待办；月度全量工作簿下载、预览和安全回填；验收报告流程 | 集成候选；未用真实账号和月报样板验收 |
| 现场领用与成本 | 已确认领用形成实际消耗；按稳定采购、前后 7 天采购加权、销售加权、留空的取价链冻结证据 | 集成候选；新成本口径未切生产 |
| 坏件返还 | 领用生成应返义务，标准品类硬盘自动免返，返还记录不动库存/成本 | 集成候选；仓库确认比例只叫“试算”，官方返还率分子未定 |
| 仓库事实与歧义 | 文件零写预览、稳定事实入库、歧义实名裁决、发货到领用候选桥接 | 集成候选；未导入真实生产文件 |
| 成本/库存迁移控制 | dry-run、证据核对、职责分离审批、签名 manifest | 控制台代码存在；生产切换总闸必须保持关闭 |
| 销售经理补库购物车 Beta | PN 搜索、所属池、近半年采购/销售价量、购物车、版本冻结、审核回传、打回复提、Excel/WBDD 字段子集导出 | 集成候选；Beta 总闸默认关闭 |
| Beta 发布控制 | reader 必须有未停用项目和 active `primary_manager`，creator 必须是精确 `sales`；manifest、实时摘要、smoke、TOCTOU 都失败关闭 | 未发布；需真实白名单与生产验收 |

## 明确尚未开发或未完成验收的部分

1. 外部审核 Agent 的审核规则、身份、签名和自动回传实现；系统目前只提供受控回传边界。
2. 真实项目、合同、销售订单、费用、领用、仓库单据的稳定映射与历史迁移。
3. 现场收货确认的权威单据/状态、前置库库存闭环和正式返库入库闭环。
4. 官方坏件返还率的正式分子、责任边界与审批口径；现有仓库确认率不得升级为 KPI。
5. 报销正式金额列、项目关联键、归属日期等真实样板契约。
6. 成本/库存生产切换、双真值逐行对账、服务端来源工件、完整水位证明和性能锁演练。
7. 真实试用账号、精确 canary、生产备份恢复、production-copy 演练和 0/5/15/30 分钟观察。
8. WBDD 直接导入氚云的源 ID、F 字段码、单号与回滚契约；现有导出只能作为录入辅助。

## 最近验证事实

- 后端全量：`2754 passed, 5 skipped`，隔离 PostgreSQL，耗时 46 分钟；仅有既有第三方弃用和 Alembic 方言 warning。
- 前端全量：`61` 个测试文件、`789` 条通过；生产构建通过，只有既有 chunk-size warning。
- 发布静态自检：`v1.21 Beta release-control static self-test passed (25 migrations)`。
- Ruffle、ShellCheck、Bash 语法、Python 编译和 `git diff --check` 均通过。
- 独立规格审查和工程/发布规范审查均为 `P0=0 / P1=0`；仍不能替代真实生产验收。

## 新电脑最短接手路径

```bash
git clone https://github.com/Jinchen-Yang/it-spareparts.git
cd it-spareparts
git checkout codex/maintenance-manager-combined
./scripts/bootstrap-dev.sh --with-db
cp .env.example .env
```

把 `.env` 改成仅用于本机的 `ENVIRONMENT=dev` 和本地随机/测试值，再按需启动：

```bash
docker compose up -d
docker compose exec app alembic upgrade head
cd frontend && npm run test
```

不要复制生产 `.env`、数据库卷、上传文件、SSH 私钥、GitHub token 或任何 API key 到开发机。完整依赖和安全配置见 [密钥与服务器交接](./SECRETS-AND-SERVERS.md)。

## 下一步建议顺序

1. 在真实脱敏样本上完成项目/合同/来源单映射和双人签认。
2. 锁定现场收货、报销金额、回款表和官方返还率的业务口径。
3. 创建真实试用 reader/creator、独立 canary 和白名单，但保持所有总闸关闭。
4. 完成 production-copy 迁移演练、备份恢复、双路径验收后，才讨论 Beta 发布。

更多细节以 [维保文档入口](../maintenance/README.md)、[业务运行手册](../maintenance/business-handbook.md) 和 GitHub 交接 Issue 为准。
