# v1.20 技术补丁：旧 8080 收口、HSTS 与最终观察

本 Runbook 只处理 Issue #153 与 Issue #178 的技术发布控制。业务口径、报价、审批和数据均不
在范围内。旧入口只允许 `10.0.0.11:8080` 上的 redirect-only Caddy；应用仍只发布
`127.0.0.1:8080`。任何摘要、root authority、监听、网络成员或 SSH 对账不精确时，
立即失败关闭。

Edge 与 HSTS 对共享 Caddy 的所有读写统一使用持久的 root-owned
`/etc/it-spareparts/shared-caddy.lock`。锁序固定为先获取 v120 锁，再获取 shared-Caddy 锁；
任何人工 writer 或其他 assistant writer 都必须遵循同一锁和同一
顺序，禁止绕过控制脚本直接覆盖 Compose/Caddyfile。

## 1. 固定输入并安装 exact control

下列变量必须由已审核 PR 的 merge commit 和本次 control package 实际输出填写，不能
使用 `latest`、分支名或目录猜测：

```bash
set -Eeuo pipefail
umask 077

TARGET_COMMIT='填写 40 位 merge commit'
CONTROL_MANIFEST_HASH='填写 64 位 control manifest SHA-256'
PACKAGE_DIR='填写 v1.20 主 Runbook 在可信控制机生成的 PACKAGE_DIR'
CONTROL_PACKAGE="$PACKAGE_DIR"
SSH_TARGET=it-spareparts-prod
LOCAL_EVIDENCE_BASE='填写可信控制机上的绝对私有证据目录'
WORK_DIR=$(mktemp -d)
EDGE_OPERATOR="$WORK_DIR/edge-v120-operator.sh"
HSTS_OPERATOR="$WORK_DIR/hsts-v120-operator.sh"
ARTIFACT_VALIDATOR="$WORK_DIR/validate-release-artifacts.py"
MOBILE_RELEASE_PROBE="$WORK_DIR/mobile-release-probe.mjs"
SSH=(timeout --kill-after=5s 30s ssh -o BatchMode=yes
  -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)
MOBILE_PID=

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ -n "$MOBILE_PID" ]; then
    kill "$MOBILE_PID" 2>/dev/null || true
    wait "$MOBILE_PID" 2>/dev/null || true
  fi
  if ! find "$WORK_DIR" -depth -mindepth 1 -delete; then
    [ "$status" -ne 0 ] || status=97
  fi
  if ! rmdir "$WORK_DIR"; then
    [ "$status" -ne 0 ] || status=97
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

test "${#TARGET_COMMIT}" -eq 40
test "${#CONTROL_MANIFEST_HASH}" -eq 64
test -d "$CONTROL_PACKAGE"
[[ "$LOCAL_EVIDENCE_BASE" = /* ]]
[[ "$LOCAL_EVIDENCE_BASE" != *填写* ]]
test -d "$LOCAL_EVIDENCE_BASE"
test ! -L "$LOCAL_EVIDENCE_BASE"
test "$(stat -c '%a' "$LOCAL_EVIDENCE_BASE")" = 700
test "$(sha256sum "$CONTROL_PACKAGE/manifest.txt" | cut -d' ' -f1)" \
  = "$CONTROL_MANIFEST_HASH"
grep -Fx "TARGET_COMMIT=$TARGET_COMMIT" "$CONTROL_PACKAGE/manifest.txt"
test "$(grep -c '^SOURCE_TAR_SHA256=' "$CONTROL_PACKAGE/manifest.txt")" = 1
test "$(sha256sum "$CONTROL_PACKAGE/source.tar" | cut -d' ' -f1)" = "$(
  sed -n 's/^SOURCE_TAR_SHA256=//p' "$CONTROL_PACKAGE/manifest.txt"
)"

install -m 500 "$CONTROL_PACKAGE/edge-v120-operator.sh" "$EDGE_OPERATOR"
install -m 500 "$CONTROL_PACKAGE/hsts-v120-operator.sh" "$HSTS_OPERATOR"
test "$(sha256sum "$EDGE_OPERATOR" | cut -d' ' -f1)" = "$(
  sed -n 's/^EDGE_OPERATOR_SHA256=//p' "$CONTROL_PACKAGE/manifest.txt"
)"
test "$(sha256sum "$HSTS_OPERATOR" | cut -d' ' -f1)" = "$(
  sed -n 's/^HSTS_OPERATOR_SHA256=//p' "$CONTROL_PACKAGE/manifest.txt"
)"
validator_hash=$(
  tar -xOf "$CONTROL_PACKAGE/source.tar" \
    .deploy/validate_release_artifacts.py | sha256sum | cut -d' ' -f1
)
tar -xOf "$CONTROL_PACKAGE/source.tar" \
  .deploy/validate_release_artifacts.py > "$ARTIFACT_VALIDATOR"
chmod 500 "$ARTIFACT_VALIDATOR"
test "$(sha256sum "$ARTIFACT_VALIDATOR" | cut -d' ' -f1)" = "$validator_hash"
test "$(stat -c '%a' "$ARTIFACT_VALIDATOR")" = 500
python3 "$ARTIFACT_VALIDATOR" --self-test
mobile_probe_hash=$(
  tar -xOf "$CONTROL_PACKAGE/source.tar" \
    .deploy/mobile_release_probe.mjs | sha256sum | cut -d' ' -f1
)
tar -xOf "$CONTROL_PACKAGE/source.tar" \
  .deploy/mobile_release_probe.mjs > "$MOBILE_RELEASE_PROBE"
chmod 500 "$MOBILE_RELEASE_PROBE"
test "$(sha256sum "$MOBILE_RELEASE_PROBE" | cut -d' ' -f1)" \
  = "$mobile_probe_hash"
test "$(stat -c '%a' "$MOBILE_RELEASE_PROBE")" = 500
```

以上同一 shell 的只读输入可在任何远端动作前用空环境重验；这是
`runbook-contract`，不会连接生产或读取 secret：

```bash
env -i PATH=/usr/bin:/bin HOME="$HOME" /usr/bin/bash -s -- \
  "$TARGET_COMMIT" "$CONTROL_MANIFEST_HASH" "$PACKAGE_DIR" <<'runbook-contract'
set -Eeuo pipefail
target_commit=$1
manifest_hash=$2
package_dir=$3
test "${#target_commit}" -eq 40
test "${#manifest_hash}" -eq 64
test -d "$package_dir"
test "$(sha256sum "$package_dir/manifest.txt" | cut -d' ' -f1)" \
  = "$manifest_hash"
grep -Fx "TARGET_COMMIT=$target_commit" "$package_dir/manifest.txt" >/dev/null
runbook-contract
```

本地 evidence 的 no-clobber、generation 隔离和批准账号独立性可先用临时 fixture
执行；不会连接生产：

```bash
EVIDENCE_CONTRACT_ROOT=$(mktemp -d)
env -i PATH=/usr/bin:/bin HOME="$HOME" /usr/bin/bash -s -- \
  "$EVIDENCE_CONTRACT_ROOT" "$TARGET_COMMIT" <<'evidence-contract'
set -Eeuo pipefail
root=$1
target=$2
edge=edge-contract-final
hsts=hsts-contract-final
admin="$root/admin.json"
restricted="$root/missing-restricted.json"
chmod 700 "$root"
printf '{}\n' > "$admin"
chmod 600 "$admin"
available() {
  candidate=$1
  [[ "$candidate" = /* ]] && [ -f "$candidate" ] && [ ! -L "$candidate" ] \
    && [ "$(stat -c '%a' "$candidate")" = 600 ]
}
available "$admin"
! available "$restricted"
install -d -m 700 "$root/$target" "$root/$target/$edge"
evidence="$root/$target/$edge/$hsts"
mkdir -m 700 -- "$evidence"
! mkdir -m 700 -- "$evidence" 2>/dev/null
printf 'status=ok\n' > "$evidence/probe.txt"
chmod 600 "$evidence/probe.txt"
test "$(stat -c '%a' "$evidence/probe.txt")" = 600
evidence-contract
find "$EVIDENCE_CONTRACT_ROOT" -depth -mindepth 1 -delete
rmdir "$EVIDENCE_CONTRACT_ROOT"
```

control package 的安装、root authority 镜像和应用补丁发布仍按
[`v1.20-release-runbook.md`](v1.20-release-runbook.md) 执行。应用发布必须先完成：

- exact source、数据库备份和恢复校验；
- migration `upgrade/current/check`；
- app/frontend/db exact image/CID/restart；
- `/health`、`/health/db` 经 HTTPS 返回真实 JSON，而非 SPA HTML；
- `127.0.0.1:8080` 是唯一应用监听。

## 2. Edge scoped rollback 演练与正式 generation

generation 使用 explicit UTC 字符串。先演练 promote/rollback，再创建不同的正式
generation；不得复用已 rolled-back generation：

```bash
EDGE_DRILL="edge-${TARGET_COMMIT:0:12}-$(date -u +%Y%m%dT%H%M%SZ)-drill"
"$EDGE_OPERATOR" prepare "$SSH_TARGET" "$TARGET_COMMIT" "$EDGE_DRILL"
"$EDGE_OPERATOR" promote "$SSH_TARGET" "$TARGET_COMMIT" "$EDGE_DRILL"
test "$("$EDGE_OPERATOR" reconcile "$SSH_TARGET" \
  "$TARGET_COMMIT" "$EDGE_DRILL")" \
  = 'RECONCILED exact-promoted continue-verification'
"$EDGE_OPERATOR" rollback "$SSH_TARGET" "$TARGET_COMMIT" "$EDGE_DRILL"
test "$("$EDGE_OPERATOR" reconcile "$SSH_TARGET" \
  "$TARGET_COMMIT" "$EDGE_DRILL")" \
  = 'RECONCILED exact-rolled-back observed'

EDGE_FINAL="edge-${TARGET_COMMIT:0:12}-$(date -u +%Y%m%dT%H%M%SZ)-final"
"$EDGE_OPERATOR" prepare "$SSH_TARGET" "$TARGET_COMMIT" "$EDGE_FINAL"
"$EDGE_OPERATOR" promote "$SSH_TARGET" "$TARGET_COMMIT" "$EDGE_FINAL"
test "$("$EDGE_OPERATOR" reconcile "$SSH_TARGET" \
  "$TARGET_COMMIT" "$EDGE_FINAL")" \
  = 'RECONCILED exact-promoted continue-verification'
```

此时必须逐项证明：

```bash
"${SSH[@]}" "$SSH_TARGET" -- \
  sudo -n ss -H -ltnp '( sport = :8080 )'
curl --noproxy '*' --max-redirs 0 --connect-timeout 5 --max-time 15 \
  -sS -D - -o /dev/null \
  'http://118.25.94.90:8080/a/b?x=1'
curl --noproxy '*' --max-redirs 0 --connect-timeout 5 --max-time 15 -sS -I \
  'http://118.25.94.90:8080/a/b?x=1'
curl --noproxy '*' --max-redirs 0 --connect-timeout 5 --max-time 15 \
  -sS -X POST -D - -o /dev/null \
  'http://118.25.94.90:8080/a/b?x=1'
```

GET/HEAD 必须单跳 `308` 到 `https://hbzgc.icu/a/b?x=1`；POST 必须 `405`。三者
均不得出现 `Set-Cookie`，不得返回业务 HTML/API。生产主机必须只有
`127.0.0.1:8080` 和 Docker-owned `10.0.0.11:8080`，禁止 `0.0.0.0`、`[::]`
或公网/NAT 地址绑定。

## 3. HSTS 必须绑定正式 Edge digest

HSTS 的每个命令都显式传入 `EDGE_FINAL`。root generation 会绑定该 edge 的
manifest/state/post-Compose/post-Caddyfile 摘要；没有这个 exact promoted edge 就
不能 prepare：

```bash
HSTS_DRILL="hsts-${TARGET_COMMIT:0:12}-$(date -u +%Y%m%dT%H%M%SZ)-drill"
"$HSTS_OPERATOR" prepare "$SSH_TARGET" "$TARGET_COMMIT" \
  "$HSTS_DRILL" "$EDGE_FINAL"
"$HSTS_OPERATOR" promote "$SSH_TARGET" "$TARGET_COMMIT" \
  "$HSTS_DRILL" "$EDGE_FINAL"
"$HSTS_OPERATOR" rollback "$SSH_TARGET" "$TARGET_COMMIT" \
  "$HSTS_DRILL" "$EDGE_FINAL"

HSTS_FINAL="hsts-${TARGET_COMMIT:0:12}-$(date -u +%Y%m%dT%H%M%SZ)-final"
"$HSTS_OPERATOR" prepare "$SSH_TARGET" "$TARGET_COMMIT" \
  "$HSTS_FINAL" "$EDGE_FINAL"
"$HSTS_OPERATOR" promote "$SSH_TARGET" "$TARGET_COMMIT" \
  "$HSTS_FINAL" "$EDGE_FINAL"
test "$("$HSTS_OPERATOR" reconcile "$SSH_TARGET" \
  "$TARGET_COMMIT" "$HSTS_FINAL" "$EDGE_FINAL")" \
  = 'RECONCILED exact-promoted continue-verification'
```

HSTS 只能是单条 `Strict-Transport-Security: max-age=31536000`，禁止
`includeSubDomains` 和 `preload`。

## 4. 0/5/15/30 分钟证据

以下间隔是累计 0、5、15、30 分钟。每轮重新 SSH/HTTP 取证，不复用旧结果：
公网 evidence 必须分别出现 `method=GET`、`method=HEAD`、`method=POST`、
`method=PUT`、`method=PATCH`、`method=DELETE` 六行。

```bash
for value in "$EDGE_FINAL" "$HSTS_FINAL"; do
  [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$ ]]
done
LOCAL_EVIDENCE="$LOCAL_EVIDENCE_BASE/$TARGET_COMMIT/$EDGE_FINAL/$HSTS_FINAL"
install -d -m 700 "$LOCAL_EVIDENCE_BASE/$TARGET_COMMIT"
install -d -m 700 "$LOCAL_EVIDENCE_BASE/$TARGET_COMMIT/$EDGE_FINAL"
mkdir -m 700 -- "$LOCAL_EVIDENCE"
test ! -L "$LOCAL_EVIDENCE"
test "$(stat -c '%a' "$LOCAL_EVIDENCE")" = 700

REMOTE_EVIDENCE="/var/lib/it-spareparts-release-control/final-observation/$TARGET_COMMIT/$EDGE_FINAL/$HSTS_FINAL"
"${SSH[@]}" "$SSH_TARGET" -- sudo -n /usr/bin/bash -s -- \
  "$TARGET_COMMIT" "$EDGE_FINAL" "$HSTS_FINAL" <<'REMOTE-EVIDENCE'
set -Eeuo pipefail
umask 077
target=$1
edge=$2
hsts=$3
[[ "$target" =~ ^[0-9a-f]{40}$ ]]
for value in "$edge" "$hsts"; do
  [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$ ]]
done
base=/var/lib/it-spareparts-release-control/final-observation
install -d -m 700 -o root -g root "$base" "$base/$target" "$base/$target/$edge"
evidence="$base/$target/$edge/$hsts"
test ! -e "$evidence" && test ! -L "$evidence"
mkdir -- "$evidence"
chown root:root "$evidence"
chmod 700 "$evidence"
marker=$(mktemp "$evidence/.binding.XXXXXX")
{
  printf 'TARGET_COMMIT=%s\n' "$target"
  printf 'EDGE_FINAL=%s\n' "$edge"
  printf 'HSTS_FINAL=%s\n' "$hsts"
} > "$marker"
chmod 600 "$marker"
chown root:root "$marker"
sync -f "$marker" || exit $?
mv -T -- "$marker" "$evidence/binding.txt" || exit $?
sync -f "$evidence/binding.txt" || exit $?
sync -d "$evidence" || exit $?
REMOTE-EVIDENCE

elapsed=0
probe_public_edge() {
  method=$1
  minute=$2
  headers=$(mktemp "$WORK_DIR/.public-headers.XXXXXX")
  body=$(mktemp "$WORK_DIR/.public-body.XXXXXX")
  case "$method" in
    HEAD)
      code=$(curl --noproxy '*' --max-redirs 0 --connect-timeout 5 \
        --max-time 15 -sS -I -o "$headers" \
        -w '%{http_code}' 'http://118.25.94.90:8080/a/b?x=1')
      ;;
    GET|POST|PUT|PATCH|DELETE)
      code=$(curl --noproxy '*' --max-redirs 0 --connect-timeout 5 \
        --max-time 15 -sS -X "$method" -D "$headers" -o "$body" \
        -w '%{http_code}' 'http://118.25.94.90:8080/a/b?x=1')
      ;;
    *) return 64 ;;
  esac
  case "$method" in
    GET|HEAD)
      test "$code" = 308
      location=$(tr -d '\r' < "$headers" \
        | sed -n 's/^[Ll]ocation: *//p')
      test "$location" = 'https://hbzgc.icu/a/b?x=1'
      ;;
    POST|PUT|PATCH|DELETE)
      test "$code" = 405
      location=none
      allow=$(tr -d '\r' < "$headers" | sed -n 's/^[Aa]llow: *//p')
      test "$allow" = 'GET, HEAD'
      ;;
    *) return 64 ;;
  esac
  if tr '[:upper:]' '[:lower:]' < "$headers" \
      | grep '^set-cookie:' >/dev/null; then
    return 65
  fi
  test ! -s "$body"
  {
    date -Ins
    if test "$location" = none; then
      printf 'method=%s status=%s Location=%s allow=GET,HEAD\n' \
        "$method" "$code" "$location"
    else
      printf 'method=%s status=%s Location=%s\n' \
        "$method" "$code" "$location"
    fi
  } >> "$LOCAL_EVIDENCE/minute-$minute-public-8080.txt"
  rm -f -- "$headers" "$body"
}
for wait_seconds in 0 300 600 900; do
  sleep "$wait_seconds"
  elapsed=$((elapsed + wait_seconds))
  minute=$((elapsed / 60))
  hsts=$("$HSTS_OPERATOR" reconcile "$SSH_TARGET" \
    "$TARGET_COMMIT" "$HSTS_FINAL" "$EDGE_FINAL")
  test "$hsts" = 'RECONCILED exact-promoted continue-verification'
  "${SSH[@]}" "$SSH_TARGET" -- \
    sudo -n /usr/bin/bash -s -- "$REMOTE_EVIDENCE" "$minute" <<'REMOTE'
set -Eeuo pipefail
evidence=$1
minute=$2
umask 077
probe_json_health() {
  endpoint=$1
  expected_status=$2
  expected_kind=$3
  response=$(curl --noproxy '*' --proto '=https' --tlsv1.2 \
    --connect-timeout 5 --max-time 15 --max-redirs 0 \
    -fsS --write-out $'\n%{content_type}' "$endpoint")
  printf '%s' "$response" | python3 -c '
import json
import sys

raw = sys.stdin.buffer.read()
body, separator, content_type = raw.rpartition(b"\n")
if not separator:
    raise SystemExit(1)
mime = content_type.decode("ascii", "strict").split(";", 1)[0].strip().lower()
if mime != "application/json":
    raise SystemExit(1)
try:
    payload = json.loads(body)
except (json.JSONDecodeError, UnicodeDecodeError):
    raise SystemExit(1)
if not isinstance(payload, dict) or payload.get("status") != sys.argv[1]:
    raise SystemExit(1)
if sys.argv[2] == "db" and payload.get("db") != "reachable":
    raise SystemExit(1)
result = {
    "content_type": mime,
    "endpoint": sys.argv[3],
    "status": payload["status"],
}
if sys.argv[2] == "db":
    result["db"] = payload["db"]
print(json.dumps(result, ensure_ascii=True, sort_keys=True))
' "$expected_status" "$expected_kind" "$endpoint"
}
{
  date -Ins
  docker ps --format '{{.ID}} {{.Names}} {{.Image}} {{.Status}}'
  docker inspect -f '{{.Id}} {{.Image}} {{.RestartCount}}' \
    personal-ai-assistant-caddy
  ss -H -ltnp '( sport = :8080 )'
  probe_json_health https://hbzgc.icu/health ok app
  probe_json_health https://hbzgc.icu/health/db ok db
  curl --noproxy '*' --proto '=https' --tlsv1.2 \
    --connect-timeout 5 --max-time 15 -fsSI \
    https://hbzgc.icu/
  probe_json_health https://118.25.94.90/health ready assistant
} > "$evidence/minute-$minute.txt"
chmod 600 "$evidence/minute-$minute.txt"
REMOTE
  for method in GET HEAD POST PUT PATCH DELETE; do
    probe_public_edge "$method" "$minute"
  done
done
```

无业务 Token 的公开安全分支必须验证匿名 API 为 `401`，并将脱敏结果保存为
`anonymous-api-401.txt`。技术受限身份必须是应用在生产容器内签发的、带显式
`readonly` 失败关闭权限图的 authenticated fallback identity；它不创建数据库账号，
且访问维保页必须精确返回 `403`。无效 Token 的 `401` 不能冒充这项 RBAC 证据。
签发出的 Authorization header 只能短暂存在生产主机 root-owned `0600` 文件中，不得
复制出生产主机、写入证据或出现在进程参数。管理员、CSV、XLSX、ZIP CRC
和真实移动端是最终发布硬门，必须使用甲方批准的真实管理员账号完成；受限销售
只在甲方批准的真实受限账号下验收。成功证据分别使用
`authorized-csv.txt`、`authorized-xlsx.txt`、`authorized-zip-crc.txt` 与
`mobile-browser.txt`，只记录状态、文件类型、CRC/成员数、视口与脱敏截图路径，不
记录 Token、客户或业务金额。仅当未提供真实受限账号时，写入
`external-limitation-restricted-account.txt`，不得伪造 Token 或账号；其他公开及
管理员、下载和真实移动端验收仍须完成，否则不得生成 final manifest。

真实账号分支必须从 `600` Token 文件读取，并覆盖登录成功、受限账号 RBAC、CSV 可
解析、XLSX 可打开、ZIP `unzip -t`/CRC、375px mobile-browser 导航与下载；每项证据
写到上述独立路径。`monitor`/`observer` 每轮都必须重新执行 public NAT 的六种方法，
不能只证明生产主机内 `10.0.0.11:8080` 可达。

公开安全分支示例（只记录状态码）：

```bash
anonymous_code=$(curl --noproxy '*' --connect-timeout 5 --max-time 15 \
  -sS -o /dev/null -w '%{http_code}' \
  https://hbzgc.icu/api/maintenance/board)
test "$anonymous_code" = 401
printf '%s anonymous-api-401 status=%s\n' "$(date -Ins)" "$anonymous_code" \
  > "$LOCAL_EVIDENCE/anonymous-api-401.txt"

technical_code=$("${SSH[@]}" "$SSH_TARGET" -- \
  sudo -n /usr/bin/bash -s -- "$REMOTE_EVIDENCE" \
  "$TARGET_COMMIT" <<'REMOTE'
set -Eeuo pipefail
evidence=$1
expected_target=$2
umask 077
state=/var/lib/it-spareparts-release-control/v120-state.state
control=/var/lib/it-spareparts-release-control/current
[[ "$expected_target" =~ ^[0-9a-f]{40}$ ]]
test -f "$state" && test ! -L "$state"
test "$(stat -c '%a %U:%G %h' "$state")" = "600 root:root 1"
mapfile -t authority < <(python3 - "$state" "$expected_target" <<'PY'
import re
import sys

path, expected_target = sys.argv[1:]
values = {}
for raw in open(path, encoding="ascii"):
    line = raw.rstrip("\n")
    if line.count("=") != 1:
        raise SystemExit(1)
    key, value = line.split("=", 1)
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
        raise SystemExit(1)
    values[key] = value
required = {
    "TARGET_COMMIT",
    "RELEASE_PHASE",
    "NEW_APP_CID",
    "NEW_APP_IMAGE_ID",
    "CONTROL_MANIFEST_HASH",
}
if not required.issubset(values):
    raise SystemExit(1)
if values["TARGET_COMMIT"] != expected_target:
    raise SystemExit(1)
if values["RELEASE_PHASE"] != "observed":
    raise SystemExit(1)
if not re.fullmatch(r"[0-9a-f]{64}", values["NEW_APP_CID"]):
    raise SystemExit(1)
if not re.fullmatch(r"sha256:[0-9a-f]{64}", values["NEW_APP_IMAGE_ID"]):
    raise SystemExit(1)
if not re.fullmatch(r"[0-9a-f]{64}", values["CONTROL_MANIFEST_HASH"]):
    raise SystemExit(1)
print(values["NEW_APP_CID"])
print(values["NEW_APP_IMAGE_ID"])
print(values["CONTROL_MANIFEST_HASH"])
PY
)
test "${#authority[@]}" = 3
app_cid=${authority[0]}
app_image=${authority[1]}
control_hash=${authority[2]}
test -L "$control"
control_version=$(readlink -- "$control")
test "$control_version" = "versions/$control_hash"
manifest="/var/lib/it-spareparts-release-control/$control_version/manifest.txt"
test -f "$manifest" && test ! -L "$manifest"
test "$(stat -c '%a %U:%G %h' "$manifest")" = "600 root:root 1"
test "$(sha256sum "$manifest" | cut -d' ' -f1)" = "$control_hash"
test "$(docker inspect -f '{{.Id}}' "$app_cid")" = "$app_cid"
test "$(docker inspect -f '{{.State.Running}}' "$app_cid")" = true
test "$(docker inspect -f '{{.Image}}' "$app_cid")" = "$app_image"
test "$(docker inspect -f '{{.RestartCount}}' "$app_cid")" = 0
test "$(docker inspect -f \
  '{{index .Config.Labels "com.docker.compose.project"}}' "$app_cid")" \
  = it-spareparts
test "$(docker inspect -f \
  '{{index .Config.Labels "com.docker.compose.service"}}' "$app_cid")" = app
technical_tmp=$(mktemp -d /run/it-spareparts-release.XXXXXX)
technical_header="$technical_tmp/technical.header"
cleanup_technical_identity() {
  rm -f -- "$technical_header"
  rmdir -- "$technical_tmp"
}
trap cleanup_technical_identity EXIT
install -m 600 -o root -g root /dev/null "$technical_header"
docker exec "$app_cid" python -c '
from app import permissions
from app.auth import _make_token
restricted = permissions.runtime_safe(
    permissions.effective("readonly", None)
)
token, _ = _make_token(
    "readonly",
    "technical-release-probe",
    None,
    fallback=True,
    perms=restricted,
)
print(f"Authorization: Bearer {token}")
' > "$technical_header"
chmod 600 "$technical_header"
test "$(stat -c '%U:%G:%a:%h' "$technical_header")" = root:root:600:1
technical_code=$(curl --noproxy '*' --connect-timeout 5 --max-time 15 \
  -sS -o /dev/null -w '%{http_code}' \
  -H @"$technical_header" https://hbzgc.icu/api/maintenance/board)
test "$technical_code" = 403
printf '%s technical-token-403 status=%s\n' "$(date -Ins)" "$technical_code" \
  > "$evidence/technical-token-403.txt"
chmod 600 "$evidence/technical-token-403.txt"
printf '%s\n' "$technical_code"
REMOTE
)
test "$technical_code" = 403
printf '%s technical-token-403 status=%s\n' \
  "$(date -Ins)" "$technical_code" \
  > "$LOCAL_EVIDENCE/technical-token-403.txt"
chmod 600 "$LOCAL_EVIDENCE/technical-token-403.txt"
```

远端 0/5/15/30、binding 与技术 `403` 也必须在同一 generation-scoped 目录生成
no-clobber manifest；不记录 Token 或响应正文：

```bash
"${SSH[@]}" "$SSH_TARGET" -- sudo -n /usr/bin/bash -s -- \
  "$REMOTE_EVIDENCE" "$TARGET_COMMIT" "$EDGE_FINAL" "$HSTS_FINAL" <<'REMOTE'
set -Eeuo pipefail
umask 077
evidence=$1
target=$2
edge=$3
hsts=$4
expected="/var/lib/it-spareparts-release-control/final-observation/$target/$edge/$hsts"
test "$evidence" = "$expected"
test "$(realpath -e "$evidence")" = "$expected"
test "$(stat -c '%a %U:%G' "$evidence")" = "700 root:root"
grep -Fx "TARGET_COMMIT=$target" "$evidence/binding.txt" >/dev/null
grep -Fx "EDGE_FINAL=$edge" "$evidence/binding.txt" >/dev/null
grep -Fx "HSTS_FINAL=$hsts" "$evidence/binding.txt" >/dev/null
manifest="$evidence/evidence-manifest.txt"
test ! -e "$manifest" && test ! -L "$manifest"
temporary=$(mktemp "$evidence/.evidence-manifest.XXXXXX")
cleanup_remote_manifest() {
  status=$?
  trap - EXIT
  if [ -n "$temporary" ] && ! rm -f -- "$temporary"; then
    [ "$status" -ne 0 ] || status=97
  fi
  exit "$status"
}
trap cleanup_remote_manifest EXIT
{
  printf 'EVIDENCE_FORMAT=issue153-remote-v1\n'
  printf 'TARGET_COMMIT=%s\n' "$target"
  printf 'EDGE_FINAL=%s\n' "$edge"
  printf 'HSTS_FINAL=%s\n' "$hsts"
  find "$evidence" -mindepth 1 -maxdepth 1 -type f \
    ! -name '.evidence-manifest.*' -print0 \
    | sort -z \
    | while IFS= read -r -d '' artifact; do
        test "$(stat -c '%a %U:%G' "$artifact")" = "600 root:root"
        printf 'FILE=%s MODE=600 SHA256=%s\n' \
          "$(basename -- "$artifact")" \
          "$(sha256sum "$artifact" | cut -d' ' -f1)"
      done
} > "$temporary"
chmod 600 "$temporary" || exit $?
chown root:root "$temporary" || exit $?
sync -f "$temporary" || exit $?
mv -T -- "$temporary" "$manifest" || exit $?
temporary=
sync -f "$manifest" || exit $?
sync -d "$evidence" || exit $?
trap - EXIT
REMOTE
```

如果生产容器内签发、root `0600` 临时文件或精确 `403` 任一条件无法满足，发布验收
立即失败，且不得生成 final manifest。有批准账号时，Token 同样只能放在 `600`
临时文件并在 trap 中删除，禁止出现在命令行、日志和证据文件。

批准账号可用时，以下只读脚本是真实 CSV/XLSX/ZIP 与 RBAC 验收的可复制入口。
两个 login JSON 由批准人私下创建为本机 `0600` 文件，只含登录请求；脚本不打印
响应、Token、文件内容或业务标识。管理员登录文件缺失或登录失败会立即阻断；只有
真实受限账号缺失可记录一个 external limitation，不得用共享口令或伪造账号补位：

```bash
APPROVED_ADMIN_LOGIN_JSON='填写批准的管理员登录请求 JSON 的绝对 0600 路径'
APPROVED_RESTRICTED_LOGIN_JSON='填写批准的受限账号登录请求 JSON 的绝对 0600 路径'
API_ORIGIN=https://hbzgc.icu
approved_login_available() {
  login_file=$1
  [[ "$login_file" = /* ]] && [ -f "$login_file" ] \
    && [ ! -L "$login_file" ] \
    && [ "$(stat -c '%a' "$login_file")" = 600 ]
}
restricted_available=0
approved_login_available "$APPROVED_ADMIN_LOGIN_JSON"
if approved_login_available "$APPROVED_RESTRICTED_LOGIN_JSON"; then
  restricted_available=1
else
  printf '%s approved restricted account unavailable; technical 403 retained\n' \
    "$(date -Ins)" \
    > "$LOCAL_EVIDENCE/external-limitation-restricted-account.txt"
  chmod 600 "$LOCAL_EVIDENCE/external-limitation-restricted-account.txt"
fi

login_readonly() {
  request=$1
  response=$2
  header=$3
  code=$(curl --noproxy '*' --proto '=https' --tlsv1.2 \
    --connect-timeout 5 --max-time 30 -sS \
    -H 'Content-Type: application/json' --data-binary @"$request" \
    -o "$response" -w '%{http_code}' "$API_ORIGIN/api/auth/login")
  test "$code" = 200
  python3 - "$response" "$header" <<'PY'
import json
import os
import sys

response, header = sys.argv[1:]
with open(response, encoding="utf-8") as source:
    payload = json.load(source)
token = payload.get("token")
if not isinstance(token, str) or not token:
    raise SystemExit(1)
fd = os.open(header, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="ascii") as target:
    target.write(f"Authorization: Bearer {token}\n")
PY
}

download_readonly() {
  url=$1
  output=$2
  headers=$3
  max_bytes=$4
  max_time=$5
  code=$(curl --noproxy '*' --proto '=https' --tlsv1.2 \
    --connect-timeout 5 --max-time "$max_time" --max-filesize "$max_bytes" \
    --max-redirs 0 -sS \
    -H @"$admin_header" -D "$headers" -o "$output" \
    -w '%{http_code}' "$url")
  test "$code" = 200
  normalized_headers=$(mktemp "$WORK_DIR/.download-headers.XXXXXX")
  tr -d '\r' < "$headers" > "$normalized_headers"
  grep -Eiq '^Cache-Control: *no-store$' "$normalized_headers"
  grep -Eiq '^X-Content-Type-Options: *nosniff$' "$normalized_headers"
  grep -Eiq '^Content-Disposition: *attachment;' "$normalized_headers"
  rm -- "$normalized_headers"
}

admin_response="$WORK_DIR/admin-login.json"
admin_header="$WORK_DIR/admin.header"
login_readonly "$APPROVED_ADMIN_LOGIN_JSON" "$admin_response" "$admin_header"
csv_file="$WORK_DIR/maintenance-profit.csv"
csv_headers="$WORK_DIR/maintenance-profit.headers"
xlsx_file="$WORK_DIR/maintenance-orders.xlsx"
xlsx_headers="$WORK_DIR/maintenance-orders.headers"
zip_file="$WORK_DIR/maintenance-workbooks.zip"
zip_headers="$WORK_DIR/maintenance-workbooks.headers"
download_readonly \
  "$API_ORIGIN/api/maintenance/board/export?lifecycle=all" \
  "$csv_file" "$csv_headers" 536870912 60
download_readonly "$API_ORIGIN/api/maintenance/orders/export" \
  "$xlsx_file" "$xlsx_headers" 268435456 120
download_readonly "$API_ORIGIN/api/maintenance/export-workbooks" \
  "$zip_file" "$zip_headers" 536870912 120

csv_validation=$(python3 "$ARTIFACT_VALIDATOR" csv "$csv_file" "$csv_headers")
xlsx_validation=$(
  python3 "$ARTIFACT_VALIDATOR" xlsx "$xlsx_file" "$xlsx_headers"
)
zip_validation=$(python3 "$ARTIFACT_VALIDATOR" zip "$zip_file" "$zip_headers")

{
  printf '%s status=200 mime=text/csv;charset=utf-8 %s\n' \
    "$(date -Ins)" "$csv_validation"
} > "$LOCAL_EVIDENCE/authorized-csv.txt"
{
  printf '%s status=200 mime=application/vnd.openxmlformats-officedocument.spreadsheetml.sheet %s\n' \
    "$(date -Ins)" "$xlsx_validation"
} > "$LOCAL_EVIDENCE/authorized-xlsx.txt"
printf '%s status=200 mime=application/zip crc=ok %s\n' \
  "$(date -Ins)" "$zip_validation" \
  > "$LOCAL_EVIDENCE/authorized-zip-crc.txt"
if [ "$restricted_available" = 1 ]; then
  restricted_response="$WORK_DIR/restricted-login.json"
  restricted_header="$WORK_DIR/restricted.header"
  login_readonly "$APPROVED_RESTRICTED_LOGIN_JSON" \
    "$restricted_response" "$restricted_header"
  restricted_code=$(curl --noproxy '*' --proto '=https' --tlsv1.2 \
    --connect-timeout 5 --max-time 30 -sS -H @"$restricted_header" \
    -o /dev/null -w '%{http_code}' \
    "$API_ORIGIN/api/maintenance/board/export?lifecycle=all")
  test "$restricted_code" = 403
  printf '%s restricted-rbac status=%s\n' \
    "$(date -Ins)" "$restricted_code" \
    > "$LOCAL_EVIDENCE/authorized-rbac.txt"
fi
chmod 600 "$LOCAL_EVIDENCE"/authorized-*.txt
```

真实 375px 浏览器验收必须使用同一个已批准管理员登录请求，且通过受控 CDP 脚本把
登录响应只写入临时 Chrome profile；脚本验证 `/maintenance`、`/maintenance/downloads`
与 `/maintenance/reminders` 可达、页面没有“加载失败”、viewport 为 375px，并把业务
区域模糊后截图。执行机缺少固定版本 Chrome/Node、管理员账号未批准或任一浏览器
断言失败都会阻断发布，且不得生成 final manifest。不得上传 storage state，也不得把
profile 或登录响应移出 `WORK_DIR`；截图只能写入本次私有 evidence 目录。

```bash
approved_login_available "$APPROVED_ADMIN_LOGIN_JSON"
CHROME_LAUNCHER=/opt/google/chrome/google-chrome
CHROME_REAL_BIN=/opt/google/chrome/chrome
NODE_BIN=/usr/bin/node
CHROME_VERSION_EXPECTED='Google Chrome 151.0.7922.71'
NODE_VERSION_EXPECTED='v24.18.1'
CHROME_LAUNCHER_SHA256_EXPECTED=aea09d69ce7f24d5901f6bfb15dd44d0c856e793e0a498f8d8393ec7d2c308ec
CHROME_REAL_SHA256_EXPECTED=4cf210c4a0aeee3e69a73639260918a7448626d6b99892ec61e20750bc7c7079
NODE_SHA256_EXPECTED=f3432a45b03b2da0d270095fdd8813dc34cbea73f5fc8b18c7a384b7cf9b333a

assert_secure_release_parent() {
  local parent=$1 mode
  test "$(readlink -e -- "$parent")" = "$parent"
  test ! -L "$parent"
  test "$(LC_ALL=C stat -c '%F|%u|%g' -- "$parent")" = 'directory|0|0'
  mode=$(stat -c '%a' -- "$parent")
  test $((8#$mode & 8#22)) -eq 0
}
assert_exact_release_file() {
  local candidate=$1
  test "$(readlink -e -- "$candidate")" = "$candidate"
  test ! -L "$candidate"
  test "$(LC_ALL=C stat -c '%F|%u|%g|%a|%h' -- "$candidate")" \
    = 'regular file|0|0|755|1'
}
release_file_identity() {
  LC_ALL=C stat -c '%d|%i|%s|%Y|%Z|%u|%g|%a|%h' -- "$1"
}
for release_parent in \
  / /opt /opt/google /opt/google/chrome /usr /usr/bin; do
  assert_secure_release_parent "$release_parent"
done
assert_exact_release_file "$CHROME_LAUNCHER"
assert_exact_release_file "$CHROME_REAL_BIN"
assert_exact_release_file "$NODE_BIN"
CHROME_LAUNCHER_IDENTITY_BEFORE=$(release_file_identity "$CHROME_LAUNCHER")
CHROME_REAL_IDENTITY_BEFORE=$(release_file_identity "$CHROME_REAL_BIN")
NODE_IDENTITY_BEFORE=$(release_file_identity "$NODE_BIN")
test "$(od -An -tx1 -N4 "$CHROME_REAL_BIN" | tr -d ' \n')" = 7f454c46
test "$(od -An -tx1 -N4 "$NODE_BIN" | tr -d ' \n')" = 7f454c46
chrome_version=$(
  "$CHROME_REAL_BIN" --version | tr -d '\r\n' | sed 's/[[:space:]]*$//'
)
node_version=$(
  "$NODE_BIN" --version | tr -d '\r\n' | sed 's/[[:space:]]*$//'
)
test "$chrome_version" = "$CHROME_VERSION_EXPECTED"
test "$node_version" = "$NODE_VERSION_EXPECTED"
test "$(sha256sum "$CHROME_LAUNCHER" | cut -d' ' -f1)" \
  = "$CHROME_LAUNCHER_SHA256_EXPECTED"
test "$(sha256sum "$CHROME_REAL_BIN" | cut -d' ' -f1)" \
  = "$CHROME_REAL_SHA256_EXPECTED"
test "$(sha256sum "$NODE_BIN" | cut -d' ' -f1)" = "$NODE_SHA256_EXPECTED"
test "$(release_file_identity "$CHROME_LAUNCHER")" \
  = "$CHROME_LAUNCHER_IDENTITY_BEFORE"
test "$(release_file_identity "$CHROME_REAL_BIN")" \
  = "$CHROME_REAL_IDENTITY_BEFORE"
test "$(release_file_identity "$NODE_BIN")" = "$NODE_IDENTITY_BEFORE"
mobile_script="$WORK_DIR/mobile-probe.mjs"
# 实际执行的是 source.tar 中已绑定哈希的脚本；它对每项 `expectedRoute`
# 做 location.pathname 精确比较，并分别检查“详细盈亏”“下载中心”“项目提醒”；
# launcher 保留 Google Chrome 的启动语义，但不能代替真实 ELF 的版本、magic 与哈希
# 固定；Node 通过 `--remote-debugging-pipe` 启动 launcher，pipe framing、
# 每条 CDP command、Chrome 退出和整体 Node 都带 deadline；浏览器内下载使用
# `AbortSignal.timeout`。登录响应与 0700 profile 在成功和失败路径都会被删除。
install -m 500 "$MOBILE_RELEASE_PROBE" "$mobile_script"
mobile_evidence_tmp="$WORK_DIR/mobile-browser.txt"
mobile_screenshot_tmp="$WORK_DIR/mobile-browser-redacted.png"
mobile_listeners_before=$(ss -H -ltnp)
env -u MOBILE_PROBE_TEST_ORIGIN -u MOBILE_PROBE_TEST_MODE \
  -u MOBILE_PROBE_TEST_COMMAND_TIMEOUT_MS \
  -u MOBILE_PROBE_TEST_NAVIGATION_TIMEOUT_MS \
  -u MOBILE_PROBE_TEST_OVERALL_TIMEOUT_MS \
  -u MOBILE_PROBE_TEST_PROFILE_RM_FAILURES \
  -u MOBILE_PROBE_TEST_CLEANUP_LOG \
  timeout --kill-after=5s 180s "$NODE_BIN" \
  "$mobile_script" "$CHROME_LAUNCHER" "$admin_response" \
  "$mobile_evidence_tmp" "$mobile_screenshot_tmp" "$WORK_DIR"
mobile_listeners_after=$(ss -H -ltnp)
test "$mobile_listeners_after" = "$mobile_listeners_before"
test ! -e "$admin_response" && test ! -L "$admin_response"
test -s "$mobile_evidence_tmp"
test -s "$mobile_screenshot_tmp"
grep -F 'origin=https://hbzgc.icu' \
  "$mobile_evidence_tmp" >/dev/null
install -m 600 "$mobile_evidence_tmp" \
  "$LOCAL_EVIDENCE/mobile-browser.txt"
install -m 600 "$mobile_screenshot_tmp" \
  "$LOCAL_EVIDENCE/mobile-browser-redacted.png"
rm -- "$mobile_evidence_tmp" "$mobile_screenshot_tmp"
```

全部可安全完成的观察与验收结束后，最后生成 no-clobber manifest。它只收录持久证据，
登录响应、Authorization header、下载原文件、Chrome profile 和临时 body 必须仍只在
`WORK_DIR`，不得出现在 evidence：

```bash
for required in anonymous-api-401.txt technical-token-403.txt \
  authorized-csv.txt authorized-xlsx.txt authorized-zip-crc.txt \
  mobile-browser.txt mobile-browser-redacted.png; do
  test -s "$LOCAL_EVIDENCE/$required"
done
test -z "$(find "$LOCAL_EVIDENCE" -mindepth 1 -maxdepth 1 \
  -name 'external-limitation-*.txt' \
  ! -name 'external-limitation-restricted-account.txt' -print)"
test "$(find "$LOCAL_EVIDENCE" -mindepth 1 -maxdepth 1 \
  -name 'external-limitation-restricted-account.txt' -print | wc -l)" -le 1
test -z "$(find "$LOCAL_EVIDENCE" -mindepth 1 -maxdepth 1 \
  \( -name '*login*' -o -name '*.header' -o -name '*.xlsx' \
     -o -name '*.zip' -o -name '*.csv' -o -name '*profile*' \) -print)"
evidence_manifest="$LOCAL_EVIDENCE/evidence-manifest.txt"
test ! -e "$evidence_manifest" && test ! -L "$evidence_manifest"
manifest_tmp=$(mktemp "$WORK_DIR/.evidence-manifest.XXXXXX")
{
  printf 'EVIDENCE_FORMAT=issue153-final-v1\n'
  printf 'TARGET_COMMIT=%s\n' "$TARGET_COMMIT"
  printf 'EDGE_FINAL=%s\n' "$EDGE_FINAL"
  printf 'HSTS_FINAL=%s\n' "$HSTS_FINAL"
  find "$LOCAL_EVIDENCE" -mindepth 1 -maxdepth 1 -type f -print0 \
    | sort -z \
    | while IFS= read -r -d '' artifact; do
        test "$(stat -c '%a' "$artifact")" = 600
        printf 'FILE=%s MODE=600 SHA256=%s\n' \
          "$(basename -- "$artifact")" \
          "$(sha256sum "$artifact" | cut -d' ' -f1)"
      done
} > "$manifest_tmp"
chmod 600 "$manifest_tmp" || exit $?
sync -f "$manifest_tmp" || exit $?
mv -T -- "$manifest_tmp" "$evidence_manifest" || exit $?
sync -f "$evidence_manifest" || exit $?
sync -d "$LOCAL_EVIDENCE" || exit $?
```

## 5. 回滚顺序

如果 HSTS 已提升，必须先 scoped 回到 300，再移除 Edge；不可调用历史整套 ingress
rollback：

```bash
"$HSTS_OPERATOR" rollback "$SSH_TARGET" "$TARGET_COMMIT" \
  "$HSTS_FINAL" "$EDGE_FINAL"
"$EDGE_OPERATOR" rollback "$SSH_TARGET" "$TARGET_COMMIT" "$EDGE_FINAL"
```

随后重新验证 HSTS=300、旧公网 8080 不可达、`127.0.0.1:8080` 正常、原 assistant
健康、容器重启/OOM 不变和 root generation 对账。任何 SSH 非零或
`divergent-or-unknown` 都只允许 read-only reconcile 与人工停止，不能猜测成功或
扩大回滚。
