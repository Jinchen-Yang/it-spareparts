# v1.20 补丁发布与 HSTS scoped CAS Runbook

本流程只处理 Issue #153 的发布控制和 IT 站点 HSTS。HSTS 必须在 Issue #178
redirect-only edge 已按
[`edge-v120-scoped-runbook.md`](edge-v120-scoped-runbook.md) 正式提升后执行，
并显式绑定该 edge generation。它不改变任何业务口径，不拆 ingress，不覆盖原
personal assistant，也绝不调用整套 HTTPS ingress 回滚。HSTS 头必须精确为
`max-age=31536000`；禁止加入
`includeSubDomains` 或 `preload`。

## 1. exact-main 与 root authority

在可信 cloudlay 控制机固定目标提交，并按
[`v1.20-release-runbook.md`](v1.20-release-runbook.md) 生成、传输和安装 control
package。新 manifest 必须同时包含并验证：

- `CONTROL_MANIFEST_HASH`
- `TARGET_COMMIT`
- `HSTS_ROOT_SHA256`
- `HSTS_OPERATOR_SHA256`
- `SOURCE_TAR_SHA256`

从 clean shell 开始时先执行以下完整输入块；`PACKAGE_DIR` 必须直接复用主 Runbook
在可信控制机生成的本地目录，不能猜 `/var/tmp`：

```bash
set -Eeuo pipefail
umask 077
TARGET_COMMIT='填写 40 位 merge commit'
PACKAGE_DIR='填写主 Runbook 的可信控制机 PACKAGE_DIR'
EDGE_FINAL='填写已正式 promoted 的 edge generation'
OPERATOR=$(mktemp)
cleanup_hsts_operator() {
  status=$?
  trap - EXIT HUP INT TERM
  rm -f -- "$OPERATOR"
  exit "$status"
}
trap cleanup_hsts_operator EXIT
HSTS_OPERATOR_SHA256=$(
  sed -n 's/^HSTS_OPERATOR_SHA256=//p' "$PACKAGE_DIR/manifest.txt"
)
test "${#TARGET_COMMIT}" -eq 40
test -n "$EDGE_FINAL"
test "${#HSTS_OPERATOR_SHA256}" -eq 64
install -m 500 "$PACKAGE_DIR/hsts-v120-operator.sh" "$OPERATOR"
test "$(sha256sum "$OPERATOR" | cut -d' ' -f1)" \
  = "$HSTS_OPERATOR_SHA256"
```

生产 root state 必须是 `RELEASE_PHASE=observed`。已存在 root authority 时，普通补丁
不是 bootstrap；必须从 root state 读取精确父 `RELEASE_ID`，保存父 state SHA-256，
并严格执行主 Runbook 的 `build_v120.sh <TARGET_COMMIT> --supersedes
<PARENT_RELEASE_ID>` 步骤。这里不复制一段依赖隐式 `tools` 或小写变量的命令，避免
从 clean shell 误用未定义值。

observed 父 release 的新 child 使用父 `TARGET_COMMIT` 与父
`NEW_APP_IMAGE_ID`/`NEW_FRONTEND_IMAGE_ID` 作为旧业务 rollback base，并把
`ROLLBACK_POLICY` 重置为 `old_allowed`。build 必须证明当前 app/frontend CID 的
实际镜像与父 authority 一致。进入 `opening` 时再在同一 release lock 内单向锁存为
`forward_only`。`prepared`、`backup_verified`、`opening` 和 `switched` 都不是合法
supersession 父状态。

先按 v1.20 主 Runbook 完成 exact-SHA 应用发布、备份/恢复校验、迁移、smoke 和应用
0/5/15/30 分钟观察。HSTS 仍保持 300。

## 2. 安装可信 HSTS control

root control 只能从已验证的 hash-addressed
`/var/lib/it-spareparts-release-control/current` 执行。禁止从生产 checkout、`/tmp`
脚本或环境变量覆盖 root 路径。远端 `hsts-v120-root.sh` 每次执行都会调用
`install-v120-control.sh verify` 重验完整 manifest。

在可信控制机从 exact target 解出 operator，并与 manifest 的
`HSTS_OPERATOR_SHA256` 比较：

```bash
git show "$TARGET_COMMIT:.deploy/hsts_v120_operator.sh" > "$OPERATOR"
chmod 500 "$OPERATOR"
test "$(sha256sum "$OPERATOR" | cut -d' ' -f1)" = "$HSTS_OPERATOR_SHA256"
```

原 assistant 的公开 health URL 固定为
`https://118.25.94.90/health`。它不是秘密，但也不得由应用账号、环境变量或操作员输入
覆盖。首次 root `prepare` 会在
`/etc/it-spareparts/assistant-health.url` 不存在时以 no-clobber、原子方式建立
`root:root:600` 文件；已有文件若是链接、权限不安全或内容不精确一致则失败关闭。
manifest 同时绑定该固定内容的 SHA-256。不得手工预建或改写定位文件，也不得输出其他
`.env` 内容。HSTS scoped rollback 保留这个 root-only control metadata，不把它当成
assistant 配置回滚；后续 `inspect` 仍重验其类型、权限和摘要。

## 3. generation snapshot 与回滚演练

generation 名只使用目标 SHA 前缀和 UTC 时间，不使用“最新文件”或目录猜测：

```bash
DRILL_GENERATION="hsts-${TARGET_COMMIT:0:12}-$(date -u +%Y%m%dT%H%M%SZ)-drill"
test -n "$EDGE_FINAL"
/usr/bin/bash "$OPERATOR" prepare it-spareparts-prod \
  "$TARGET_COMMIT" "$DRILL_GENERATION" "$EDGE_FINAL"
```

prepare 会在 root-only generation 下创建 `snapshot.txt`、`manifest.txt`、
`SHA256SUMS` 和原子状态文件。manifest 绑定 exact target、`RELEASE_ID`、
root state generation/SHA-256、HSTS 前后完整 Compose/render 摘要、Caddyfile 摘要、
IT Compose 摘要和固定 assistant health locator 摘要；不复制或记录 `.env` 内容。

在正式提升前完成一次真实 scoped 演练：

```bash
/usr/bin/bash "$OPERATOR" promote it-spareparts-prod \
  "$TARGET_COMMIT" "$DRILL_GENERATION" "$EDGE_FINAL"
reconciled=$(
  /usr/bin/bash "$OPERATOR" reconcile it-spareparts-prod \
    "$TARGET_COMMIT" "$DRILL_GENERATION" "$EDGE_FINAL"
)
test "$reconciled" = 'RECONCILED exact-promoted continue-verification'
# live header 必须精确 max-age=31536000。

/usr/bin/bash "$OPERATOR" rollback it-spareparts-prod \
  "$TARGET_COMMIT" "$DRILL_GENERATION" "$EDGE_FINAL"
reconciled=$(
  /usr/bin/bash "$OPERATOR" reconcile it-spareparts-prod \
    "$TARGET_COMMIT" "$DRILL_GENERATION" "$EDGE_FINAL"
)
test "$reconciled" = 'RECONCILED exact-rolled-back observed'
# live header 必须精确 max-age=300。
```

scoped rollback 只允许把绑定的 `31536000` CAS 回 `300`。任何 Caddyfile、Compose
其他字段、render、应用 Compose 或 root authority 漂移都必须停止，不得覆盖。演练
前后都要证明原 assistant health、Caddy 双网络、internal ingress、frontend ingress、
app/db 隔离以及 `127.0.0.1:8080` 不变量。
HSTS 阶段还必须保持 Issue #178 的 Docker-owned `10.0.0.11:8080` 精确监听；
禁止出现 `0.0.0.0:8080`、`[::]:8080` 或第三个 ingress 成员。

## 4. 正式提升与 0/5/15/30 观察

演练回退后创建新的正式 generation：

```bash
FINAL_GENERATION="hsts-${TARGET_COMMIT:0:12}-$(date -u +%Y%m%dT%H%M%SZ)-final"
/usr/bin/bash "$OPERATOR" prepare it-spareparts-prod \
  "$TARGET_COMMIT" "$FINAL_GENERATION" "$EDGE_FINAL"
/usr/bin/bash "$OPERATOR" promote it-spareparts-prod \
  "$TARGET_COMMIT" "$FINAL_GENERATION" "$EDGE_FINAL"
```

在 0 分钟、5 分钟、15 分钟和 30 分钟，每次都必须重新读取 root generation 并将
CAS 摘要与 live header 对账，不能复用第一次结果：

```bash
reconciled=$(
  /usr/bin/bash "$OPERATOR" reconcile it-spareparts-prod \
    "$TARGET_COMMIT" "$FINAL_GENERATION" "$EDGE_FINAL"
)
test "$reconciled" = 'RECONCILED exact-promoted continue-verification'
```

四次输出都必须是 `exact-promoted`，且每次 live header 都只能有一条
`strict-transport-security: max-age=31536000`。同时复核 HTTPS/TLS、HTTP path/query
308、Issue #178 旧公网 `118.25.94.90:8080` 的 GET/HEAD 308 与 unsafe 405、
零 Cookie/零业务正文、原 assistant、app/db、登录与 RBAC、CSV/XLSX/ZIP CRC 和移动端。

若任一观察点失败，只允许执行：

```bash
/usr/bin/bash "$OPERATOR" rollback it-spareparts-prod \
  "$TARGET_COMMIT" "$FINAL_GENERATION" "$EDGE_FINAL"
```

随后必须得到 `exact-rolled-back` 和精确 `max-age=300`。SSH 非零、超时或断线时，
operator 只执行只读 `inspect`：`exact-pre` 可重试，`exact-promoted` 继续验收，
`exact-rolled-back` 记录已回退，持续不可达或 `divergent-or-unknown` 立即停止人工
处置。不得猜测成功，也不得扩大回滚范围。
