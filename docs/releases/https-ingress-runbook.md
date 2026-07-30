# IT 备件系统 HTTPS 入口与 Issue #178 旧 8080 兼容 Runbook

> 本文第 1–8 节记录首次 HTTPS 接入时“公网 8080 不可达”的阶段基线。Issue #178
> 最终态已取代该阶段结论：应用仍只监听 `127.0.0.1:8080`；经单独批准的安全组/
> 防火墙 **TCP 8080** 只转入 Docker-owned `10.0.0.11:8080` Caddy
> redirect-only 站点，GET/HEAD=308、unsafe=405，禁止业务上游、Cookie 与业务正文。
> 最终发布与观察以
> [`edge-v120-scoped-runbook.md`](edge-v120-scoped-runbook.md) 为准。

适用范围：生产使用独立正式域名，现有 Docker Caddy 终止 TLS；IT 备件
`frontend` 只在宿主机 `127.0.0.1:8080` 和隔离的 Docker ingress 网络上提供服务。

## 1. 发布门禁

依赖主干：

`正式域名/DNS → 精确工件 → 配置备份 → 关闭公网旁路 → Caddy 接入 → 外网验收 → 持续监控`

以下任一条件不满足都必须停止：

1. 域名所有者已确认 IT 备件系统的正式 FQDN。
2. FQDN 的 `A` 记录只指向本机公网 IPv4；未确认 IPv6 入口前不添加 `AAAA`。
3. 80/443 的代理归属已确认，变更不会覆盖同机其他站点。
4. #153 的提交已合并、Required CI 全绿，发布人拿到精确的 40 位 commit SHA
   和经独立复核的工件 SHA-256；不得使用浮动分支或服务器上的未知文件。
5. 已安排短维护窗口。关闭 8080 与 Caddy 首次接入之间会有短暂不可用，本流程不承诺
   零停机。
6. 已备份 Caddyfile、Caddy Compose 和 IT 备件 Compose，且备份权限为 `600`。
7. 已准备并演练“保持 8080 回环绑定”的一键回滚。不得把重新开放明文 8080 当成常规回滚。

应用必须部署在独立域名的根路径，例如 `https://<正式域名>/`。当前前端使用
`BrowserRouter`、`/assets`、`/api` 和 `/manual.html` 根路径，不能挂在
`/it-data/` 一类子路径下。

## 2. 目标拓扑和信任边界

```text
公网 80/443
    ↓
personal-ai-assistant-caddy
    ├── 原域名 → 原 personal assistant 网络
    └── IT 正式域名
            ↓（仅 it-spareparts-ingress）
       it-spareparts-frontend:80
            ↓（仅 IT 默认业务网络）
          app:8000 → db:5432
```

`personal-ai-assistant-caddy` 只能加入原 assistant 网络和固定命名的
`it-spareparts-ingress`。后者只连接 Caddy 与 `frontend`，不得使用
`it-spareparts_default`；否则公网代理会横向获得对 `app` 和 `db` 的网络可达性。
原 assistant 目录与 `.env` 继续采用现网信任模型：`ubuntu` 是获批且已有 sudo
权限的部署管理员；目录必须是 `ubuntu:ubuntu 750`，`.env` 必须是
`ubuntu:ubuntu 600`，Caddyfile/Compose 则必须是 `root:root` 且 group/world
不可写。本次不宣称用 root-owned 子文件防御可信部署管理员。

首次 HTTPS 接入阶段，宿主机的 `127.0.0.1:8080` 只用于本机探针和 SSH 隧道，
并保持旧公网 8080 不可达。Issue #178 最终提升后，安全组、防火墙与 Docker
只能按文首的精确 redirect-only 契约开放，不得把应用端口或通配监听发布出去。

## 3. 构建并交付精确工件

发布总控在干净、已验证的仓库中执行。`HTTPS_SOURCE_SHA` 必须是已合并且 CI 全绿的
40 位提交：

```bash
bash <<'BASH'
set -Eeuo pipefail
HTTPS_SOURCE_SHA='<40位提交SHA>'
test "$(printf '%s' "$HTTPS_SOURCE_SHA" | wc -c)" -eq 40
git cat-file -e "$HTTPS_SOURCE_SHA^{commit}"

BUNDLE_DIR=$(mktemp -d)
for artifact_path in \
  docker-compose.yml \
  .deploy/Caddyfile.it-data.example \
  .deploy/docker-compose.https.yml \
  .deploy/rollback_https_ingress.sh \
  .deploy/monitor.sh
do
  git show "$HTTPS_SOURCE_SHA:$artifact_path" \
    > "$BUNDLE_DIR/${artifact_path##*/}"
done
(
  cd "$BUNDLE_DIR"
  sha256sum \
    docker-compose.yml \
    Caddyfile.it-data.example \
    docker-compose.https.yml \
    rollback_https_ingress.sh \
    monitor.sh > SHA256SUMS
  tar -cf "/tmp/itdata-https-$HTTPS_SOURCE_SHA.tar" \
    docker-compose.yml \
    Caddyfile.it-data.example \
    docker-compose.https.yml \
    rollback_https_ingress.sh \
    monitor.sh \
    SHA256SUMS
)
sha256sum "/tmp/itdata-https-$HTTPS_SOURCE_SHA.tar"
BASH
```

把 tar 包传到生产 `/tmp`。外层 SHA-256 必须通过受信渠道单独传给发布人，不能只信
同目录中的校验文件。生产预检和安装：

```bash
test -n "${BASH_VERSION:-}" || {
  echo "请先进入 bash 再执行生产发布块" >&2
  exit 2
}
set -Eeuo pipefail
export HTTPS_SOURCE_SHA='<同一个40位提交SHA>'
export HTTPS_BUNDLE="/tmp/itdata-https-$HTTPS_SOURCE_SHA.tar"
export HTTPS_BUNDLE_SHA256='<总控提供的外层SHA-256>'

test "$(sha256sum "$HTTPS_BUNDLE" | awk '{print $1}')" = \
  "$HTTPS_BUNDLE_SHA256"
test "$(tar -tf "$HTTPS_BUNDLE" | LC_ALL=C sort)" = \
"$(printf '%s\n' \
  Caddyfile.it-data.example \
  SHA256SUMS \
  docker-compose.yml \
  docker-compose.https.yml \
  monitor.sh \
  rollback_https_ingress.sh | LC_ALL=C sort)"

STAGE_DIR=$(mktemp -d)
chmod 700 "$STAGE_DIR"
tar --no-same-owner --no-same-permissions -xf "$HTTPS_BUNDLE" -C "$STAGE_DIR"
(
  cd "$STAGE_DIR"
  sha256sum -c SHA256SUMS
)
for script in "$STAGE_DIR/monitor.sh" "$STAGE_DIR/rollback_https_ingress.sh"
do
  bash -n "$script"
done

sudo install -m 600 "$STAGE_DIR/Caddyfile.it-data.example" \
  /home/ubuntu/apps/it-spareparts/.deploy/Caddyfile.it-data.example
sudo install -m 600 "$STAGE_DIR/docker-compose.https.yml" \
  /home/ubuntu/apps/it-spareparts/.deploy/docker-compose.https.yml
sudo install -m 600 "$STAGE_DIR/docker-compose.yml" \
  /home/ubuntu/apps/it-spareparts/.deploy/docker-compose.secure.yml
sudo install -o root -g root -m 755 "$STAGE_DIR/rollback_https_ingress.sh" \
  /usr/local/sbin/it-spareparts-https-rollback
sudo install -o root -g root -m 755 "$STAGE_DIR/monitor.sh" \
  /usr/local/sbin/it-spareparts-monitor-next

# 回滚进程直接 flock 这个 root-only 目录，不创建固定名称锁文件。若路径已被
# 非 root 用户预占或做成 symlink，必须失败，不能通过 chown/chmod“接管”。
ROLLBACK_LOCK_DIR=/run/lock/it-spareparts-https-rollback
if sudo test -e "$ROLLBACK_LOCK_DIR" || sudo test -L "$ROLLBACK_LOCK_DIR"
then
  sudo test ! -L "$ROLLBACK_LOCK_DIR"
  test "$(sudo stat -c '%U:%G:%a' -- "$ROLLBACK_LOCK_DIR")" = \
    "root:root:700"
else
  sudo mkdir --mode=700 -- "$ROLLBACK_LOCK_DIR"
fi
sudo test ! -L "$ROLLBACK_LOCK_DIR"
test "$(sudo stat -c '%U:%G:%a' -- "$ROLLBACK_LOCK_DIR")" = \
  "root:root:700"

# 固化的回滚入口必须位于无应用账号可写祖先的持久 root-only 目录。
ROLLBACK_CONTROL_DIR=/var/lib/it-spareparts-release-control
if sudo test -e "$ROLLBACK_CONTROL_DIR" ||
  sudo test -L "$ROLLBACK_CONTROL_DIR"
then
  sudo test ! -L "$ROLLBACK_CONTROL_DIR"
  test "$(sudo stat -c '%U:%G:%a' -- "$ROLLBACK_CONTROL_DIR")" = \
    "root:root:700"
else
  sudo mkdir --mode=700 -- "$ROLLBACK_CONTROL_DIR"
fi
sudo test ! -L "$ROLLBACK_CONTROL_DIR"
test "$(sudo stat -c '%U:%G:%a' -- "$ROLLBACK_CONTROL_DIR")" = \
  "root:root:700"

test "$(sudo stat -c '%U:%G:%a' \
  /usr/local/sbin/it-spareparts-https-rollback)" = "root:root:755"
test "$(sudo stat -c '%U:%G:%a' /usr/local/sbin)" = "root:root:755"
test "$(sudo stat -c '%U:%G:%a' \
  /usr/local/sbin/it-spareparts-monitor-next)" = "root:root:755"
```

这一步只安装经过审核的入口工件和待切换的安全主 Compose，不切换业务镜像、不执行
迁移，也不等同于部署整个新业务版本。

## 4. 只读预检

```bash
export IT_DATA_HOST='<正式域名>'
export IT_DATA_IPV4='<生产公网IPv4>'
export ASSISTANT_SMOKE_URL='https://<原personal-assistant域名>/health'

test -n "$IT_DATA_HOST"
test -n "$IT_DATA_IPV4"
[[ "$ASSISTANT_SMOKE_URL" =~ ^https://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?/health$ ]]
test "$(dig +short A "$IT_DATA_HOST" | sort -u)" = "$IT_DATA_IPV4"
test -z "$(dig +short AAAA "$IT_DATA_HOST")"

sudo docker compose \
  -f /home/ubuntu/apps/it-spareparts/docker-compose.yml \
  -f /home/ubuntu/apps/it-spareparts/.deploy/docker-compose.https.yml \
  config --quiet
sudo docker exec personal-ai-assistant-caddy caddy version
test "$(sudo stat -c '%U:%G:%a' /opt/personal-ai-assistant)" = \
  "ubuntu:ubuntu:750"
test "$(sudo stat -c '%U:%G:%a' /opt/personal-ai-assistant/.env)" = \
  "ubuntu:ubuntu:600"
test "$(sudo stat -c '%U:%G:%a' /opt/personal-ai-assistant/Caddyfile)" = \
  "root:root:644"
test "$(sudo stat -c '%U:%G:%a' \
  /opt/personal-ai-assistant/compose.production.yml)" = "root:root:644"
cd /opt/personal-ai-assistant
sudo docker compose --env-file .env -f compose.production.yml config --quiet
sudo docker exec personal-ai-assistant-caddy \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
! sudo grep -Fq 'IT_DATA_HOST' Caddyfile
! sudo grep -Fq 'IT_DATA_HOST' compose.production.yml
curl -fsS http://127.0.0.1:8080/ >/dev/null
curl --proto '=https' --tlsv1.2 -fsS "$ASSISTANT_SMOKE_URL" >/dev/null
```

DNS 未生效时不得申请证书或修改生产入口。预检不得使用 `env`、`set` 或调试回显
输出生产变量。

## 5. 备份边缘配置

```bash
RELEASE_ID="https-$(date +%Y%m%d-%H%M%S)"
EVIDENCE_DIR="/home/ubuntu/apps/it-spareparts/backups/$RELEASE_ID"
sudo install -d -m 700 "$EVIDENCE_DIR"
sudo install -m 600 /opt/personal-ai-assistant/Caddyfile \
  "$EVIDENCE_DIR/Caddyfile.before"
sudo install -m 600 /opt/personal-ai-assistant/compose.production.yml \
  "$EVIDENCE_DIR/assistant-compose.before.yml"
sudo install -m 600 /home/ubuntu/apps/it-spareparts/docker-compose.yml \
  "$EVIDENCE_DIR/it-compose.before.yml"
sudo sha256sum \
  "$EVIDENCE_DIR/Caddyfile.before" \
  "$EVIDENCE_DIR/assistant-compose.before.yml" \
  "$EVIDENCE_DIR/it-compose.before.yml" |
  sudo tee "$EVIDENCE_DIR/SHA256SUMS" >/dev/null
sudo chmod 600 "$EVIDENCE_DIR/SHA256SUMS"
sudo sha256sum -c "$EVIDENCE_DIR/SHA256SUMS"

# 把精确参数固化为 root-only 可执行回滚入口；后续 shell 中断或变量丢失时不靠人工重建。
ROLLBACK_NOW=/var/lib/it-spareparts-release-control/rollback-now.sh
sudo install -o root -g root -m 700 /dev/null "$ROLLBACK_NOW"
{
  printf '#!/bin/bash\nset -Eeuo pipefail\n'
  printf 'exec /usr/local/sbin/it-spareparts-https-rollback %q %q\n' \
    "$EVIDENCE_DIR" "$ASSISTANT_SMOKE_URL"
} | sudo tee "$ROLLBACK_NOW" >/dev/null
test "$(sudo stat -c '%U:%G:%a' -- "$ROLLBACK_NOW")" = "root:root:700"
printf '紧急回滚入口已固化：sudo %s\n' "$ROLLBACK_NOW"

# 保存仅含环境变量“键”的渲染基线；值统一脱敏，不复制 .env 秘密。
cd /opt/personal-ai-assistant
sudo docker compose --env-file .env -f compose.production.yml \
  config --format json |
  python3 -c '
import json
import sys

config = json.load(sys.stdin)
for service in config.get("services", {}).values():
    environment = service.get("environment")
    if isinstance(environment, dict):
        service["environment"] = {key: "<redacted>" for key in environment}
json.dump(config, sys.stdout, sort_keys=True)
' |
  sudo tee "$EVIDENCE_DIR/assistant-compose.before.rendered.json" >/dev/null
sudo chmod 600 "$EVIDENCE_DIR/assistant-compose.before.rendered.json"
```

不要读取、打印或复制两套应用的 `.env` 内容。回滚脚本会校验上述目录、文件权限、
所有权、固定路径和每个 SHA-256，`it-compose.before.yml` 只作审计证据，不会被恢复。

## 6. 先关闭明文旁路并建立隔离网络

这是维护窗口起点。先把已校验的安全主 Compose 固定安装为 root 所有；这样以后普通
`docker compose up` 也不会重新打开公网端口。过渡覆盖文件仅用于安装前预检，不作为
长期安全边界：

```bash
cd /home/ubuntu/apps/it-spareparts
# 覆盖前先在生产环境渲染待安装文件，避免把不可用 Compose 固化为唯一基线。
sudo docker compose -f .deploy/docker-compose.secure.yml \
  config --format json |
  jq -e '
    .services.frontend.ports ==
      [{"mode":"ingress","target":80,"published":"8080",
        "protocol":"tcp","host_ip":"127.0.0.1"}]
    and
    .networks.ingress.name == "it-spareparts-ingress"
    and
    .networks.ingress.internal == true
    and
    (.services.frontend.networks.ingress.aliases ==
      ["it-spareparts-frontend"])
    and
    (.services.app.networks | has("ingress") | not)
    and
    (.services.db.networks | has("ingress") | not)
  ' >/dev/null

sudo install -o root -g root -m 644 \
  .deploy/docker-compose.secure.yml docker-compose.yml
sudo cmp -s .deploy/docker-compose.secure.yml docker-compose.yml
sudo docker compose \
  -f docker-compose.yml \
  config --format json |
  jq -e '
    .services.frontend.ports ==
      [{"mode":"ingress","target":80,"published":"8080",
        "protocol":"tcp","host_ip":"127.0.0.1"}]
    and
    .networks.ingress.name == "it-spareparts-ingress"
    and
    .networks.ingress.internal == true
    and
    (.services.frontend.networks.ingress.aliases ==
      ["it-spareparts-frontend"])
    and
    (.services.app.networks | has("ingress") | not)
    and
    (.services.db.networks | has("ingress") | not)
  ' >/dev/null

sudo docker compose \
  -f docker-compose.yml up -d --no-deps frontend

curl -fsS http://127.0.0.1:8080/ >/dev/null
sudo docker network inspect it-spareparts-ingress --format \
  '{{json .Containers}}' |
  jq -e '
    length == 1
    and
    (to_entries[0].value.Name | contains("frontend"))
  ' >/dev/null
```

确认监听只有回环地址：

```bash
listeners=$(sudo ss -ltnH '( sport = :8080 )')
test -n "$listeners"
test -z "$(printf '%s\n' "$listeners" |
  awk '$4 != "127.0.0.1:8080" {print}')"
```

在首次 HTTPS 接入阶段不得恢复旧公网 8080，即使接入失败也只能执行第 11 节
阶段回滚；Issue #178 的后续正式提升属于独立 generation，不复用本段回滚。

## 7. 持久配置 Caddy

使用 `sudoedit` 修改 `/opt/personal-ai-assistant/compose.production.yml`。保留
`caddy` 的原 environment、`ASSISTANT_HOST`、卷、端口和原 assistant 网络，只追加
下面三个非秘密的固定值。不得写入共享 `.env`：现有 `api` 使用该文件作为
`env_file`，写入会把无关 IT 入口配置泄漏给 API：

```yaml
services:
  caddy:
    environment:
      # 这是增量示意；原键必须原样保留。
      IT_DATA_HOST: <正式域名>
      IT_DATA_UPSTREAM: it-spareparts-frontend:80
      IT_DATA_HSTS_MAX_AGE: "300"
    networks:
      assistant:
        # Caddy 的 ACME/公网默认路由必须继续走原可出网网络。
        gw_priority: 1
      it_data_ingress:
        gw_priority: 0

networks:
  # 原 assistant 网络定义必须原样保留。
  it_data_ingress:
    external: true
    name: it-spareparts-ingress
```

把已安装的 `.deploy/Caddyfile.it-data.example` 站点块追加到现有
`/opt/personal-ai-assistant/Caddyfile`，不得覆盖或改写原站点。首次发布只使用
`max-age=300`，不启用 `includeSubDomains` 或 `preload`。

先验证 Compose 渲染，不打印环境变量值：

```bash
sudo /bin/bash -c '
set -Eeuo pipefail
target=/opt/personal-ai-assistant/Caddyfile
source=/home/ubuntu/apps/it-spareparts/.deploy/Caddyfile.it-data.example
if grep -Fq "https://{\$IT_DATA_HOST}" "$target"; then
  echo "IT 站点块已经存在，拒绝重复追加" >&2
  exit 1
fi
printf "\n" >> "$target"
cat "$source" >> "$target"
'

cd /opt/personal-ai-assistant
# 当前生产原文件没有 ${...} 插值；本增量也只允许固定值。
test -z "$(grep -n '\${' compose.production.yml)"
sudo docker compose --env-file .env -f compose.production.yml \
  config --format json |
  jq -e --arg expected_host "$IT_DATA_HOST" '
    (.services.caddy.environment.ASSISTANT_HOST |
      type == "string" and length > 0)
    and
    (.services.caddy.environment.IT_DATA_HOST == $expected_host)
    and
    (.services.caddy.environment.IT_DATA_UPSTREAM ==
      "it-spareparts-frontend:80")
    and
    (.services.caddy.environment.IT_DATA_HSTS_MAX_AGE == "300")
    and
    (.networks.it_data_ingress.name == "it-spareparts-ingress")
    and
    (.networks.it_data_ingress.external == true)
    and
    (.services.caddy.networks.assistant.gw_priority == 1)
    and
    ((.services.caddy.networks.it_data_ingress.gw_priority // 0) == 0)
  ' >/dev/null

sudo docker compose --env-file .env -f compose.production.yml \
  config --format json |
  python3 -c '
import json
import sys

config = json.load(sys.stdin)
for service in config.get("services", {}).values():
    environment = service.get("environment")
    if isinstance(environment, dict):
        service["environment"] = {key: "<redacted>" for key in environment}
json.dump(config, sys.stdout, sort_keys=True)
' |
  sudo tee "$EVIDENCE_DIR/assistant-compose.candidate.rendered.json" >/dev/null
sudo chmod 600 "$EVIDENCE_DIR/assistant-compose.candidate.rendered.json"

# 除三个 Caddy 环境键、隔离网络和默认网关优先级外，渲染结果必须与备份前完全一致。
sudo python3 - \
  "$EVIDENCE_DIR/assistant-compose.before.rendered.json" \
  "$EVIDENCE_DIR/assistant-compose.candidate.rendered.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    before = json.load(handle)
with open(sys.argv[2], encoding="utf-8") as handle:
    candidate = json.load(handle)

caddy = candidate["services"]["caddy"]
for key in ("IT_DATA_HOST", "IT_DATA_UPSTREAM", "IT_DATA_HSTS_MAX_AGE"):
    assert caddy["environment"].pop(key) == "<redacted>"

ingress_attachment = caddy["networks"].pop("it_data_ingress")
assert ingress_attachment.get("gw_priority", 0) == 0
assistant_attachment = caddy["networks"]["assistant"]
assert assistant_attachment.pop("gw_priority") == 1
assert assistant_attachment == {}
caddy["networks"]["assistant"] = None

ingress_network = candidate["networks"].pop("it_data_ingress")
assert ingress_network["name"] == "it-spareparts-ingress"
assert ingress_network["external"] is True
assert candidate == before
PY

# 对完整候选渲染做规范化摘要：只把计划中的 HSTS 值替换为固定标记，其余值
# （包括不落盘的秘密）全部参与 SHA-256。晚期提升必须与此摘要完全一致。
sudo docker compose --env-file .env -f compose.production.yml \
  config --format json |
  python3 -c '
import json
import sys

config = json.load(sys.stdin)
environment = config["services"]["caddy"]["environment"]
assert environment["IT_DATA_HSTS_MAX_AGE"] == "300"
environment["IT_DATA_HSTS_MAX_AGE"] = "<normalized>"
json.dump(config, sys.stdout, sort_keys=True, separators=(",", ":"))
' |
  sha256sum |
  awk '{print $1}' |
  sudo tee \
    "$EVIDENCE_DIR/assistant-compose.candidate.normalized.sha256" >/dev/null
sudo chmod 600 \
  "$EVIDENCE_DIR/assistant-compose.candidate.normalized.sha256"
sudo grep -Eq '^[0-9a-f]{64}$' \
  "$EVIDENCE_DIR/assistant-compose.candidate.normalized.sha256"

sudo docker exec \
  -e IT_DATA_HOST="$IT_DATA_HOST" \
  -e IT_DATA_UPSTREAM='it-spareparts-frontend:80' \
  -e IT_DATA_HSTS_MAX_AGE='300' \
  personal-ai-assistant-caddy \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

验证通过后才持久重建 Caddy：

```bash
sudo docker compose --env-file .env -f compose.production.yml \
  up -d --no-deps --force-recreate caddy
test "$(sudo docker inspect -f '{{.State.Running}}' \
  personal-ai-assistant-caddy)" = true
sudo docker exec personal-ai-assistant-caddy \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo docker exec personal-ai-assistant-caddy \
  wget -qO- https://acme-v02.api.letsencrypt.org/directory >/dev/null
sudo docker exec personal-ai-assistant-caddy \
  wget -qO- http://it-spareparts-frontend/ >/dev/null
curl --proto '=https' --tlsv1.2 -fsS "$ASSISTANT_SMOKE_URL" >/dev/null
```

原 personal assistant 或新 IT 上游任一失败，立即执行第 11 节，不进行临时在线修补。

## 8. 机器可判定的入口验收

```bash
EXPECTED_HSTS_MAX_AGE=${EXPECTED_HSTS_MAX_AGE:-300}
https_code=$(curl --noproxy '*' --proto '=https' --tlsv1.2 \
  --connect-timeout 5 --max-time 20 -sS -o /dev/null \
  -w '%{http_code}' "https://$IT_DATA_HOST/")
test "$https_code" = 200

redirect_result=$(curl --noproxy '*' --proto '=http' \
  --connect-timeout 5 --max-time 20 --max-redirs 0 \
  -sS -o /dev/null -w '%{http_code} %{redirect_url}' \
  "http://$IT_DATA_HOST/")
read -r redirect_code redirect_url <<EOF
$redirect_result
EOF
case "$redirect_code" in 301|302|307|308) ;; *) false ;; esac
test "$redirect_url" = "https://$IT_DATA_HOST/"

unauth_code=$(curl --noproxy '*' --proto '=https' --tlsv1.2 \
  --connect-timeout 5 --max-time 20 -sS -o /dev/null \
  -w '%{http_code}' \
  "https://$IT_DATA_HOST/api/maintenance/projects")
test "$unauth_code" = 401

headers_file=$(mktemp)
curl --noproxy '*' --proto '=https' --tlsv1.2 -sS \
  -D "$headers_file" -o /dev/null "https://$IT_DATA_HOST/"
normalized_headers=$(tr -d '\r' < "$headers_file")
grep -Eqi \
  "^strict-transport-security: max-age=$EXPECTED_HSTS_MAX_AGE$" \
  <<<"$normalized_headers"
grep -Eqi '^x-content-type-options: nosniff$' <<<"$normalized_headers"
grep -Eqi '^x-frame-options: DENY$' <<<"$normalized_headers"
unset normalized_headers
rm -f "$headers_file"
```

首次 HTTPS 接入阶段还必须从服务器外部网络确认 `$IT_DATA_IPV4:8080` 不可达。
服务器自身的回环检查不能代替外网验收；Issue #178 最终提升后则改为外部验证
GET/HEAD=308、unsafe=405、零 Cookie/零业务正文。

## 9. 业务验收

先选择确认有数据的只读日期范围，用获批的导出账号执行可记录烟测。Token 只经 stdin
交给 curl，不写命令历史或磁盘：

```bash
DATE_FROM='<YYYY-MM-DD>'
DATE_TO='<YYYY-MM-DD>'
SMOKE_DIR=$(mktemp -d)
chmod 700 "$SMOKE_DIR"
cleanup_export_smoke() {
  unset IT_ACCESS_TOKEN || true
  rm -f \
    "$SMOKE_DIR/orders.headers" "$SMOKE_DIR/orders.xlsx" \
    "$SMOKE_DIR/workbooks.headers" "$SMOKE_DIR/workbooks.zip" || true
  rmdir "$SMOKE_DIR" 2>/dev/null || true
}
trap 'status=$?; cleanup_export_smoke; exit "$status"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

read -rsp '临时访问 Token: ' IT_ACCESS_TOKEN
printf '\n'

download_with_token() {
  local url=$1
  local headers=$2
  local output=$3
  printf 'header = "Authorization: Bearer %s"\n' "$IT_ACCESS_TOKEN" |
    curl --config - --noproxy '*' --proto '=https' --tlsv1.2 \
      --connect-timeout 5 --max-time 300 -fsS \
      -D "$headers" -o "$output" "$url"
}
assert_download_headers() {
  local headers=$1
  local normalized
  normalized=$(tr -d '\r' < "$headers")
  grep -Eqi '^cache-control: no-store$' <<<"$normalized"
  grep -Eqi '^x-content-type-options: nosniff$' <<<"$normalized"
  grep -Eqi \
    "^content-disposition: attachment;.*filename=.*filename\\*=UTF-8''" \
    <<<"$normalized"
}

download_with_token \
  "https://$IT_DATA_HOST/api/maintenance/orders/export?date_from=$DATE_FROM&date_to=$DATE_TO" \
  "$SMOKE_DIR/orders.headers" "$SMOKE_DIR/orders.xlsx"
assert_download_headers "$SMOKE_DIR/orders.headers"
unzip -t "$SMOKE_DIR/orders.xlsx"

download_with_token \
  "https://$IT_DATA_HOST/api/maintenance/export-workbooks?date_from=$DATE_FROM&date_to=$DATE_TO" \
  "$SMOKE_DIR/workbooks.headers" "$SMOKE_DIR/workbooks.zip"
assert_download_headers "$SMOKE_DIR/workbooks.headers"
unzip -t "$SMOKE_DIR/workbooks.zip"
test "$(unzip -Z1 "$SMOKE_DIR/workbooks.zip" | wc -l)" -gt 0

cleanup_export_smoke
trap - EXIT INT TERM
```

随后使用获批账号和真实浏览器完成：

1. HTTPS 登录并重新获取 Token；HTTP origin 的旧 `localStorage` 不应迁移。
2. 无 Token 为 `401`，无权限账号为 `403` 或正确脱敏。
3. `/maintenance`、`/maintenance/downloads` 深链接刷新正常。
4. CSV、订单 XLSX、单本工作簿、批量 ZIP、回填模板 ZIP 均可下载。
5. XLSX 可打开；ZIP CRC、成员数量和时间范围正确。
6. 最大现有 ZIP 无 `502/504`、截断或缓存污染。
7. 导入只在隔离验收库执行；生产入口只做只读导出烟测。

## 10. 启用持续监控并观察

监控配置由运行生产 cron 的应用账号创建，不能由 root 创建成该账号不可读的文件：

```bash
cd /home/ubuntu/apps/it-spareparts
test "$(id -un)" = ubuntu
umask 077
printf 'https://%s/\n' "$IT_DATA_HOST" > .https_monitor_url
chmod 600 .https_monitor_url
test "$(stat -c '%U:%G:%a' .https_monitor_url)" = "ubuntu:ubuntu:600"
sudo install -m 755 /usr/local/sbin/it-spareparts-monitor-next \
  .deploy/monitor.sh
.deploy/monitor.sh
grep -q 'ok=Y' monitor.status
test "$(stat -c '%U:%G' monitor.status)" = "ubuntu:ubuntu"
```

`.deploy/monitor.sh` 会同时检查 HTTPS 首页、同域 HTTP→HTTPS 跳转和证书至少还有
7 天有效期；任一失败都进入原 `monitor.log`/告警通道。确认现有 cron 每 5 分钟调用
该脚本，并观察连续两个 cron 周期均为 `ok=Y`。

在 0、5、15、30 分钟检查：

- Caddy、frontend、app、db 运行状态和重启次数。
- Caddy/app 日志中的 TLS、`499/502/504`、异常 `401/413`。
- `/health`、`/health/db`、HTTPS 首页、跳转目标和证书有效期。
- 下载文件 CRC、临时文件回收和卡住的 `processing` 批次。
- 原 personal assistant 路由不受影响。

30 分钟全部通过后仍保持 `IT_DATA_HSTS_MAX_AGE=300`。HSTS 提升不得再直接编辑
Compose，也不得复用本 Runbook 的整套 ingress 回滚。只能按照
[`v1.20 HSTS scoped CAS Runbook`](hsts-v120-scoped-runbook.md) 建立绑定 exact
root authority 的 generation snapshot，完成 scoped rollback/reconciliation 演练后
再提升到 `31536000`。仍不增加 `includeSubDomains` 或 `preload`。

## 11. 一键安全回滚

本节只适用于首次 HTTPS ingress 接入，禁止用于后续 HSTS 提升。首次接入时，任何
入口或原 personal assistant 验收失败，优先执行第 5 节已经打印并固化的
`sudo /var/lib/it-spareparts-release-control/rollback-now.sh`。这个 root-only
文件自带精确证据目录和原 assistant `/health` 参数，且其祖先目录不允许应用账号
替换；即使原 shell 已退出也不需要人工重建变量：

```bash
sudo /var/lib/it-spareparts-release-control/rollback-now.sh

# 与固化入口等价的显式调用，仅用于同一发布 shell 内交叉核对。
sudo /usr/local/sbin/it-spareparts-https-rollback \
  "$EVIDENCE_DIR" "$ASSISTANT_SMOKE_URL"
```

可信脚本位于 root-owned `/usr/local/sbin`，不会从应用账号可替换的目录执行。它会在
恢复前验证：可信 `ubuntu` 运维账号的目录/`.env` 所有权与权限、root 管理的运行
配置、固定三份证据及 SHA-256、持久主 Compose 不会重开公网端口。脚本保存失败现场
的 Caddyfile/Compose，成对恢复、验证并重建原 Caddy；检查原 assistant 内部健康和
外部 HTTPS Host 路由；最后断开 `it-spareparts-ingress`，并确认 IT 前端恰好只在
`127.0.0.1:8080` 可用。旧 `it-compose.before.yml` 永远不会恢复。

DNS 记录由域名所有者另行决定保留或删除，脚本不会擅自改 DNS。应急访问使用：

```bash
ssh -L 18080:127.0.0.1:8080 it-spareparts-prod
```

首次 HTTPS 接入的阶段回滚不恢复公网 8080。Issue #178 经风险负责人单独批准的
最终入口不是业务明文回退，而是固定 308/405 的 Caddy redirect-only 兼容层；任何
超出该契约的变化必须另立变更单。
