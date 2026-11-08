# v1.46.0 发布回执（requirements successor，2026-11-08）

> 决策: D-0055（v1.46.0 范围；应用/镜像/标签 commit `e62cd60cdbcc3fef896c5289388250057c4ce513`，无迁移）
> 执行：tech-manager｜授权：用户经 lead「什么时候修好什么时候上」｜生产：ybwznt.com / www.ybwznt.com

## 结果摘要

- **发布成功**：cutover **2026-11-08T13:08:09Z**（PT 05:08 / 北京 21:08）；verify **13:09:01Z PASS**。
- 应用/镜像/标签 commit：**`e62cd60cdbcc3fef896c5289388250057c4ce513`**（v1.46.0，D-0055）。
- release tag：**`release-requirements-successor-e62cd60cdbcc3fef896c5289388250057c4ce513`**，指向 e62cd60c（已 push；不指向 main）。
- 控制器：`.deploy/requirements_successor_ops.py` LF sha256 **`2b2aa5ca2ca305b9f42574b50f505199169919323eea9d7c80ed90e3f19e2d1e`**（PR #525，merged main `da43dc7a`）。
- 镜像：app `sha256:b85b30e7bb9592986f50324dc5c4dc7283bf5526ff86ef71c033dfafcead74f5`；frontend `sha256:d35178973ec4dc193da44dba139dc6a3917830a16ad9eb1f6def5a6563348e21`（revision=e62cd60c）。
- 容器：`itsp-successor-e62cd60cdbcc-app` / `-frontend` 承接；旧 `782c858a…` app 已停，旧容器/镜像/备份保留。
- schema：`98afc0d2157b`，migration_performed=false（heads/current/check no-op，无 upgrade/downgrade）。
- 公开版本：1.46.0（`/assets/index-DGIORTXx.js` sha256 `9bbd49d1e3b05a9500ae988d150cc362f5263b752701aa95eceff12d9ff102c4`）；双域名 `/`、`/health`、`/health/db` 均 200。

## 阶段回执（私有目录）

`/data/spareparts/releases/requirements-successor-e62cd60cdbcc3fef896c5289388250057c4ce513/`
- preflight（`fb735e39…`）→ **freeze 前 dry-run 门禁 19/19 + 21 负例 ALL_OK**（evidence 已落盘）→ images → freeze → backup-old → **migrate(no-op)** → backup-new → bluegreen → cutover（`exposure-started` → 同 inode 写 Caddyfile → Admin route PATCH，无 POST /load）→ verify；每阶段 PASS 回执齐全。
- 备份：`old-head.dump` / `new-head.dump` + globals/uploads/config；两次 network-none PG15 真实恢复（schema 98af、counts 与 freeze 一致）。

## 边界与回滚

- 暴露后不自动回滚（`exposure-started.json` 存在），仅前向修复；旧版本资源与备份保留。
- 发布前授权清理（3 条历史 processing 预览 + 1 条孤儿 job → 终态）已执行且不回滚（证据在私目录）。
- 历史 attempt1–3 均 fail-closed 并已 recover/未暴露；详见 `team/reports/tech/2026-11-08-G7生产事件与恢复报告.md` 与 `2026-11-08-G7发布回执摘要.md`。

## 未验证/限制

- 跨模型复审为 Codex 后端“独立复审尝试”（provider/model 名称无法确认；不冒充）；dry-run 为离线证据工具，updated realpath adapt/PATCH 并发由阶段实测覆盖。
- 生产验证账号 admin（用户 A1 授权）；口令仅临时文件，未入仓库/报告/提交。