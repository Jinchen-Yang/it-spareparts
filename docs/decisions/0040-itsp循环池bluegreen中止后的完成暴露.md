# D-0060 itsp 循环池 bluegreen 中止后的完成暴露

- 日期：2026-11-15（Asia/Shanghai）。
- 编号：D-0060。前一号为 D-0059（`0039-商城在线发布与未执行迁移恢复.md`）。
- 来源：2026-11-13 的 itsp 循环池发布（`ccefa17cc644`）在 backup-new PASS 后，bluegreen 克隆 frontend 时
  于 `nginx.conf` 写入撞 `O_EXCL` 中止（该文件是更早门禁按 manifest `nginx_sha256` 钉住的基线，克隆前未换名，
  属控制器自碰撞缺陷）；随后 recover 因 schema 已前进拒绝（正确行为），旧 app 当晚被人工重启恢复服务，
  preflight 容器指纹（`started_at`）永久漂移，原控制器按设计不可原地续跑。
- 状态：完成暴露脚本的实施依据。脚本为 `.deploy/cycle_pool_expose.py`，一次性使用，不改变各阶段控制器。

## 1. 边界

1. 沿用前次发布的 PASS 回执（preflight/images/freeze/backup-old/migrate/backup-new）作为门禁；
   脚本不复核业务数据、不改数据库、不生成或轮换 token、不打开 `SHOP_CYCLE_SELL_ENABLED`。
2. 只做剩余机械步骤：克隆候选 frontend（nginx 上游换成候选 app，挂载换名后的 `nginx-cycle.conf`，
   端口 18105）、健康探测、Caddy 主站上游条件替换（先 `caddy validate`，先落 `exposure-started.json`
   marker，inode 保持写、reload 不用 `POST /load`）、停旧 app、公网验证。
3. 回执全部 `O_EXCL`：`expose-prepare.json`、`exposure-started.json`、`expose-cutover.json`、
   `verify-*-expose.json`（命名满足原控制器 enable/disable-outbound 的 `verify-*.json` 门禁）。
   旧 frontend 容器保留运行作为即时回退参照；回退 = 把上游换回旧 frontend 并启动旧 app，不动数据库。
4. 事后的 `enable-outbound` / `disable-outbound` 仍由原 `cycle_pool_ops.py` 执行（`recreate_app`
   只快照当前容器状态，不依赖 preflight 指纹）。
5. 失败现场证据（failure 日志、原 intent、操作日志）全部保留在发布目录，不删除、不改写、不重算摘要。

## 2. 与 D-0059 的关系

同为「控制器中止后不原地续跑、另立经审查步骤完成」的先例延伸：D-0059 面向商城在线重发，
本决定面向 itsp 单次完成暴露；两者都不放松 forward-only、不降级、不改写回执的边界。
