# 售前首屏紧急热修（2026-11-01）

决策: 无（不改变业务口径、权限或数据库结构）

用户本次明确授权：不跑 GitHub CI、不合并 main，完成数据备份与回滚准备后直接发布独立热修。该例外仅针对本次，不修改分支保护。基线为生产 `e6cfd25c51bc19c1f80d0bad23037d71e6d2aa0e`；当前 schema 与候选都必须是 `a3c5e7f9b2d4`。

改动为询价/待办批量准入、列表摘要减少逐单 HTTP、报价页活动任务先显示和历史后台加载。正式发布 SHA、定向回归和独立复审结论以最终收据填写；本文件本身不代表已上线。

## 私有发布目录

目录 `/data/spareparts/releases/presales-load-<40sha>` 必须 root:root 0700，文件 0600。放最终 `source.tar.gz`、`presales_load_hotfix_ops.py`、未修改的 `presales3_ops.py`、`readonly-smoke.py`、上一生产发布的 `import-gate-review.md`，以及 `manifest.json`。

manifest 必需字段：`release`、`old_revision`、`old_schema`、`target_head`、`old_app`、`old_front`、`old_app_id`、`old_front_id`、`old_app_image`、`old_front_image`、`app_image`、`frontend_image`、`additional_writers: []`、`caddyfile: /opt/it-spareparts/Caddyfile`、`port`、不重叠私网 `subnet`、`source_sha256`、`controller_sha256`、`helper_sha256`、`smoke_sha256`、`nginx_sha256`、`nginx_upstream_count`。复制上一生产已审 `import_gate_allowlist` 并回读匹配 review 文件 hash；不得按年龄或 processing 状态宽泛放行。

helper 必须是仓库原始 LF 字节，其固定 SHA256 为 `054e67d143a808e5672eea716573ac23a4ecf47ae6cd9a762a89901acfff5f16`。Windows 上 `git archive` 可能按行尾设置导出 CRLF；本次通过 Python 的 `subprocess.check_output(['git', 'show', '<sha>:.deploy/presales3_ops.py'])` 原样保存 Git blob，传输后再次核对该固定摘要。所有 SHA256 都对实际发送到服务器的字节计算。

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

## 实际发布回执

- 已切流时间：2026-11-02 02:40:24 UTC（北京时间 10:40:24；洛杉矶 2026-11-01 18:40:24）。
- 精确生产源码：`e2e335bc72bf8cb95ce72ffae5e0e8e151b6da5c`；标签 `release-hotfix-presales-load-e2e335bc-20261101`，远端已回读 peeled SHA。
- 独立分支 `hotfix/presales-load-20261101`，未创建 PR、未合并 main；按本次明确授权未触发 GitHub CI，`gh run list --branch ...` 返回空。
- 前后端 OCI revision 均为上述精确 SHA。app 镜像 `sha256:566a63d7d6a80fa8bc08d5c4a2aa6cfefffafbbf16cead455c1d2d3146000a33`；frontend 镜像 `sha256:f9043e52b04c0007f06a98cc074c3919c52a16fe6ee9110faa556f133c2ad21e`。
- 容器 `itsp-presales-hf-e2e335bc72bf-app` / `itsp-presales-hf-e2e335bc72bf-frontend`，loopback `127.0.0.1:18095`，专网 `10.239.231.0/24`；两者 running、restart=0、OOM=false。
- 旧 e6 app 保持停止，旧 frontend、两镜像与运行配置保留。数据库始终 `a3c5e7f9b2d4`；已执行候选 `alembic heads / upgrade head / check`，无 schema 或模型漂移。

### 验证与复审

jinchen-dev 专属源码和容器执行 179 项后端定向用例全部通过，包含新增 14 项热修契约；首轮 shell 包装收尾有转义错误，不能称包装进程 exit 0。新增 14 项另以 Python subprocess 正确取退出码复跑，实际 exit 0。前端 5 文件 72 项通过；随后补齐 4 条测试 fixture 的类型字段，`tsc` 和 Vite build 实际 exit 0。生产前后端镜像构建均实际 exit 0。没有在生产运行测试或写测试业务数据。

独立 gpt-6-astra 对应用补丁静态复审 PASS；最终提交相对受审 `83ce4cd6` 的运行时代码差异为空，仅增加发布控制/文档及补齐测试 fixture。OMP 受管执行 240 秒后 Deadline exceeded、exit 1，没有结果，明确不计通过。控制器独立原生复审的两项 P2（回滚等待后再次 CAS、拒绝旧跨系统配置）已修；补充切流脚本独立复审并加即时 schema 检查。

### 备份和实际恢复

私有根目录 `/data/spareparts/releases/presales-load-e2e335bc72bf8cb95ce72ffae5e0e8e151b6da5c` 为 root:root 0700，配置/备份为 0600；业务备份未下载。

| 制品 | 字节 | SHA256 |
|---|---:|---|
| `old-head.dump` | 84286592 | `3ee78c90ee781c85f02e8ee5f86a4bbc5581644f352ee06bb1ce360308133ac8` |
| `uploads.tar` | 2243389440 | `833ff0ff27c8f336191c7f746e6a7966cbbf479adcc0a571c3a62e404abe63d4` |
| `config.private.tar` | 1822720 | `09daf96162ee0ebfc61a82d4e5393ae567ffde9521777f42702de82628f223be` |

另有 globals 私有备份。PG15 在独占 `network=none`、无发布端口、4GiB/2CPU 限制容器中实际恢复同一 dump，head 和冻结表计数一致，完成后按精确 ID/归属标签清理。`backup-old.json` PASS 摘要 `21bbbdee8558087b3d84feb69f4383423d8bb26ecc132193aa7cff7edd86b523`。

### 切流和上线观察

原 `cutover` 在任何 exposure/source/runtime 写入前失败：Caddy 会自动把输入配置路径放进另一站点 file_server 的 `hide` 数组，临时 baseline/candidate 文件名不同导致严格比较失败。未跳过比较或改写旧 intent。保留原控制器/manifest/失败记录，使用受审 `.deploy/presales_load_hotfix_resume.py` 补充脚本：同一 stdin 路径 validate/adapt，确认唯一差异就是主站 upstream，再做原入口双重 CAS、即时 schema 检查、原 inode 写入、精确 active JSON `/load` 和回读。

补充脚本生产字节 SHA256 `f5c75642e44f92347598966929560c8260760c9576db0c790e0668806ffb7ab4`；`cutover.json` PASS 摘要 `ee85575447e3b03c8a7b6075471620dc5dfb6c85cf829999bbbcb880b7af9dc0`。原控制器和回滚命令保持有效。

公开 `ybwznt.com` / `www.ybwznt.com` 的健康、数据库、售前读接口、匿名拒绝及静态资产身份核验通过；应用无重启/OOM，原容器保护快照未漂移。首轮 `verify` PASS 摘要 `834eca3f536acb685925a7cf0657e42316a085c473c9bf4c84d3e9d2229f4cc3`。

上线后只读抽样：销售账号我的询价前 10 张 391.5ms（可见总数90）；历史最多的报价账号前10张400.8ms（可见总数222），当前 claimed 3项87.8ms、公共池9项198.9ms，历史 done 首100/219项2702.0ms。历史仍后台完整补齐，不阻塞活动首屏。仅为少量受控样本，不冒充自然流量 p95 或浏览器首屏验收。用户实际点按验收仍需刷新后确认。
