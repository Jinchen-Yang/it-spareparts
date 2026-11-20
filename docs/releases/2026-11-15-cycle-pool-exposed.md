# 循环池两套生产部署回执（2026-11-15）

决策: D-0058、D-0059、D-0060

本文记录坏件循环系统两套生产的实际部署状态与回执摘要。发布以现场回执文件为准，
本文只是脱敏索引；不含 token、口令或私有地址。

## 商城 spareparts-shop（v2 按件接收）

- 发布提交：`c75cab801402f4c203ce4eec96db1521e74ec6cf`（`release/cycle-sell-qty-0029`，
  基于生产基线 `4b40bb24` 窄移植 #89/#92，PR #93）。
- 发布目录：`/data/shop/releases/cycle-c75cab801402f4c203ce4eec96db1521e74ec6cf/`。
- 2026-11-13 00:12 CST 的旧离线发布失败（迁移命令被误拦），现场证据归档于
  `archive-20261113T0012CST-failed-offline-cycle-…`；按 D-0059 在线控制器重发。
- 阶段回执：preflight → backup-old → migrate（0027 → `0029_cycle_sell_qty`）→ backup-new →
  create → activate 全部 PASS；`verify-20261113T015044` PASS（/api/health、/api/ready、/healthz 200）。
- 切流：`exposure-started.json`（2026-11-13 01:50 UTC）后 Caddy 仅切 `shop.ybwznt.com` 上游；
  新容器 `shop-cycle-c75cab801402-{backend,frontend,agent,worker}` 健康；旧栈保留。
- token：backend 已按发布目录私有文件重建（`backend-retoken.create.redacted.json`）。
- 经办人：`cycle_sell_setting.operator_user_id` 已配置（非空）。

## it-spareparts（1.47.0 待修池）

- 发布提交：`ccefa17cc6446f613b7237ac0cd2121256969622`；生产标签
  `release-cycle-pool-ccefa17cc6446f613b7237ac0cd2121256969622`（2026-11-15 补打并推送）。
- 发布目录：`/data/spareparts/releases/cycle-pool-ccefa17cc6446f613b7237ac0cd2121256969622/`。
- 2026-11-13：dry-run 因挂载顺序门禁失败（#539 修复）→ 换控制器重跑 preflight/images/dry-run
  PASS → freeze → backup-old → migrate（98afc0d2157b → 86f7486b9ed5 → `c3e8a1d94b27`）→
  backup-new 全 PASS → bluegreen 因 `nginx.conf` O_EXCL 自碰撞中止（两次 failure 日志在目录内）；
  旧 app 当晚人工重启恢复服务，preflight 指纹漂移，原控制器不可续跑。
- 2026-11-15：按 D-0060 用 `cycle_pool_expose.py`（分支 `fix/cycle-pool-expose-completion`，
  最终摘要 `8aaeb701…`，历史摘要 `3513cb78…`）完成暴露：
  - `expose-prepare.json` PASS：候选 frontend 克隆（manifest `nginx_sha256` 基线换上游另存
    `nginx-cycle.conf`、端口 18105、双网络）、/health 与 /health/db 200。
  - `expose-cutover.json` PASS：`caddy validate` → `exposure-started.json` 先落 → inode 保持写
    （仅替换主站上游一处）→ reload → 双主机公网探测 200 → 停旧 app（旧 frontend 保留作回退参照）。
  - `verify-*-expose.json` PASS：双主机健康、候选与公网资产逐字节一致、候选日志无错误、
    schema 仍 c3e8、循环表 0 行、`page_cycle_*` 授权用户 0。
- 现网：`ybwznt.com` → `itsp-cycle-ccefa17cc644-frontend` → `itsp-cycle-ccefa17cc644-app`
  （`SHOP_CYCLE_SELL_ENABLED=false`，URL/TOKEN 已配置且经摘要核验）。

## 尚未打开（需另行授权/决定）

1. `page_cycle_repair` / `page_cycle_wip` / `page_cycle_manage` 页键授权：当前 0 人（含 admin）。
2. `enable-outbound`：由原控制器执行
   `python3 cycle_pool_ops.py <manifest> enable-outbound`；D-0058 要求单独授权。

## 回退

- itsp 切流回退：Caddyfile 主站上游换回 `itsp-successor-e62cd60cdbcc-frontend:80` 并
  `docker start itsp-successor-e62cd60cdbcc-app`；不动数据库、不降级。
- 商城回退：按 D-0059 `recover-cutover`（循环表已写入后拒绝回切）。
