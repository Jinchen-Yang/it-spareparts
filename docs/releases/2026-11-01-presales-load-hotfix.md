# 售前首屏紧急热修（2026-11-01）

决策: 无（不改变业务口径、权限或数据库结构）

用户本次明确授权：不跑 GitHub CI、不合并 main，完成数据备份与回滚准备后直接发布独立热修。该例外仅针对本次，不修改分支保护。基线为生产 `e6cfd25c51bc19c1f80d0bad23037d71e6d2aa0e`；当前 schema 与候选都必须是 `a3c5e7f9b2d4`。

改动为询价/待办批量准入、列表摘要减少逐单 HTTP、报价页活动任务先显示和历史后台加载。正式发布 SHA、定向回归和独立复审结论以最终收据填写；本文件本身不代表已上线。

## 私有发布目录

目录 `/data/spareparts/releases/presales-load-<40sha>` 必须 root:root 0700，文件 0600。放最终 `source.tar.gz`、`presales_load_hotfix_ops.py`、未修改的 `presales3_ops.py`、`readonly-smoke.py`、上一生产发布的 `import-gate-review.md`，以及 `manifest.json`。

manifest 必需字段：`release`、`old_revision`、`old_schema`、`target_head`、`old_app`、`old_front`、`old_app_id`、`old_front_id`、`old_app_image`、`old_front_image`、`app_image`、`frontend_image`、`additional_writers: []`、`caddyfile: /opt/it-spareparts/Caddyfile`、`port`、不重叠私网 `subnet`、`source_sha256`、`controller_sha256`、`helper_sha256`、`smoke_sha256`、`nginx_sha256`、`nginx_upstream_count`。复制上一生产已审 `import_gate_allowlist` 并回读匹配 review 文件 hash；不得按年龄或 processing 状态宽泛放行。

helper 必须是仓库原始 LF 字节，其固定 SHA256 为 `054e67d143a808e5672eea716573ac23a4ecf47ae6cd9a762a89901acfff5f16`。所有 SHA256 都对实际发送到服务器的字节计算，不能以 Windows CRLF 副本代替。

## 执行

先用最终源码构建前后端，两镜像 OCI revision 必须等于同一最终 40 位 SHA；不得用带未提交内容的目录构建。控制器逐阶段运行：

```sh
python3 presales_load_hotfix_ops.py manifest.json preflight
python3 presales_load_hotfix_ops.py manifest.json images
python3 presales_load_hotfix_ops.py manifest.json freeze
python3 presales_load_hotfix_ops.py manifest.json backup-old
python3 presales_load_hotfix_ops.py manifest.json migrate
python3 presales_load_hotfix_ops.py manifest.json bluegreen
python3 presales_load_hotfix_ops.py manifest.json cutover
python3 presales_load_hotfix_ops.py manifest.json verify
```

`freeze` 开始短暂停写：仅停止精确旧 app。旧 frontend、DB、Caddy、商城及其他容器不停止。候选 app 在旧 app 停止后才启动，避免两套目录投递 worker 并行。备份包含 full custom dump、globals、uploads、运行配置；独占 PG15 在 network none 下实际恢复并校验 head/表计数后清理。`migrate` 执行 `upgrade head` 和 `check`，但必须不改变 schema 和计数。

每步有唯一 intent 与 PASS 收据；失败或 SSH 结果不明先查看实际状态和私有诊断，不重复执行同一步。不得打印 env、manifest 全文、原始业务日志或备份内容。切流同时 CAS 核对 Caddy 源 inode/字节与 active JSON；源适配仅改变主站 upstream，向 admin `/load` 提交只改该 upstream 的原 active JSON，保留其他 runtime 配置。

## 回滚

```sh
python3 presales_load_hotfix_ops.py manifest.json rollback
# freeze 或后续步骤部分失败也可使用 recover；只允许已知旧/新入口状态
python3 presales_load_hotfix_ops.py manifest.json recover
```

回滚停止精确候选 app，启动保留的旧 app，健康通过后恢复原 Caddy 字节和 active JSON。当前 schema 必须仍为原 head；不恢复旧 dump、不删除新产生的业务数据、不回退商城。旧容器和镜像保持可用。完成后记录精确发布 SHA/tag、镜像 ID、备份/实际恢复/定向回归/复审/公开只读冒烟和观察收据。
