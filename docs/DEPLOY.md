# 部署到云服务器（Debian + 腾讯云 + HTTPS）

> 适配环境：Debian 11/12、有 sudo、有宝塔面板、2C2G(或更高)。
> Docker 前端只监听 `127.0.0.1:8080`。生产用户必须通过独立正式域名的
> HTTPS 入口登录；完整代理与回滚步骤见
> [`docs/releases/https-ingress-runbook.md`](releases/https-ingress-runbook.md)。

---

## 一、前置检查

```bash
# 确认系统
cat /etc/os-release

# 确认架构(x86_64 还是 aarch64)
uname -m

# 确认能 sudo
sudo whoami    # 输出 root 即可

# 检查 Docker 是否已装(宝塔有时会装过)
docker --version 2>&1
docker compose version 2>&1
```

如果两行 `docker` 命令都报"command not found"，跳到二；否则跳到三。

---

## 二、安装 Docker(Debian 官方源)

```bash
# 1. 准备依赖
sudo apt update
sudo apt install -y ca-certificates curl gnupg lsb-release

# 2. 加 Docker 官方 GPG key + 源
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# 3. 装 Docker
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 4. 起服务 + 开机自启
sudo systemctl enable --now docker

# 5. 验证
sudo docker run --rm hello-world
```

> 国内服务器拉镜像慢的话, 可以配 Docker 国内镜像源(腾讯云内网通常直接走腾讯云镜像加速):
> ```
> sudo mkdir -p /etc/docker
> sudo tee /etc/docker/daemon.json <<EOF
> {"registry-mirrors": ["https://mirror.ccs.tencentyun.com"]}
> EOF
> sudo systemctl restart docker
> ```

---

## 三、加 Swap(2G 内存机器**必做**, 否则重算大表会 OOM)

```bash
# 检查现有 swap
free -h | grep -i swap

# 没有的话, 加 3GB swap
sudo fallocate -l 3G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# 验证
free -h
```

---

## 四、拉代码

仓库 `Jinchen-Yang/it-spareparts` 是私有的。两种拉法,二选一:

### 方式 A：HTTPS + Personal Access Token

1. 浏览器登录 GitHub → 右上角头像 → **Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token (classic)**
2. 权限只勾 `repo`, 有效期选 30/90 天
3. 复制生成的 token(只显示一次)

```bash
mkdir -p ~/apps && cd ~/apps
git clone https://github.com/Jinchen-Yang/it-spareparts.git
# Git 提示 Password 时再粘贴 token；不要把 token 写进命令、远端 URL 或 shell 历史。
cd it-spareparts
```

### 方式 B：用 Deploy Key(更长期)

```bash
# 在服务器上生成专用 SSH key
ssh-keygen -t ed25519 -C "deploy@server" -f ~/.ssh/github_deploy -N ""

# 把公钥贴到 GitHub 仓库 Settings → Deploy keys → Add deploy key (只勾 Read)
cat ~/.ssh/github_deploy.pub

# 配 SSH 别名
cat >> ~/.ssh/config <<'EOF'
Host github-deploy
  HostName github.com
  User git
  IdentityFile ~/.ssh/github_deploy
EOF
chmod 600 ~/.ssh/config

# 拉代码(用别名)
cd ~/apps
git clone git@github-deploy:Jinchen-Yang/it-spareparts.git
cd it-spareparts
```

---

## 五、配置 .env

```bash
cp .env.example .env

# 生成随机密钥/密码
openssl rand -hex 16    # 复制输出, 当 POSTGRES_PASSWORD
openssl rand -hex 16    # 复制输出, 当 ADMIN_PASSWORD
openssl rand -hex 32    # 复制输出, 当 SECRET_KEY

# 编辑 .env, 把上面三个值填进去, ENVIRONMENT 保持 prod
vi .env
```

最终 `.env` 大致是这样(用你自己生成的随机值):
```
POSTGRES_PASSWORD=ab12cd34ef56...
ADMIN_PASSWORD=xy78pq90mn12...
SECRET_KEY=long-random-32-byte-hex...
ENVIRONMENT=prod
FRONTEND_PORT=8080
APP_PORT=8000
```

> ⚠️ **千万记住 ADMIN_PASSWORD**，这是你以后登录用的密码(用户名 `admin`)。
> `FRONTEND_PORT` 只绑定宿主机回环地址，不是公网访问端口。

---

## 六、起服务(首次)

```bash
# 在 it-spareparts 目录下
sudo docker compose up -d --build

# 这一步会:
# 1) 拉 postgres:15 / node:20 / nginx 镜像 (~300MB)
# 2) 装前端依赖 + tsc + vite build (1-3 分钟)
# 3) 装后端依赖 + 构建 backend 镜像 (1-2 分钟)
# 4) 起三个容器: db, app, frontend
# 总耗时 5-10 分钟, 2G 内存 + swap 应该够

# 看启动状态
sudo docker compose ps

# 应该看到 3 个 Up:
#   it-spareparts-db-1        Up (healthy)
#   it-spareparts-app-1       Up
#   it-spareparts-frontend-1  Up
```

> ⚠️ 如果 build 中途因为内存被 kill, 见底部"故障排查"。

---

## 七、初始化数据库

```bash
sudo docker compose exec app alembic upgrade head

# 应该看到一连串 "INFO [alembic.runtime.migration] Running upgrade ..."
# 最后没有 ERROR 就 OK
```

---

## 八、正式域名、HTTPS 与安全组

1. 由域名所有者确认独立正式 FQDN，并将其 `A` 记录指向服务器公网 IPv4。
2. 80/443 只由获批的 Caddy/Nginx 入口监听；不要覆盖同机已有站点。
3. 腾讯云安全组只向公网放行 80/443；**不得放行 8080**。
4. 按
   [`HTTPS 入口 Runbook`](releases/https-ingress-runbook.md)
   完成证书、HTTP→HTTPS、响应头、回滚和外网验收。

容器启动后应看到：

```bash
sudo ss -ltnp '( sport = :8080 )'
# 只能出现 127.0.0.1:8080；不得出现 0.0.0.0:8080 或 [::]:8080。
```

---

## 九、访问

浏览器打开：**`https://<正式域名>/`**

- 用户名: `admin`
- 密码: 你在 `.env` 里设的 `ADMIN_PASSWORD`

第一次没有数据, 顶部点"数据导入"上传你的 .xlsx 即可。

---

## 十、备份(强烈建议在客户用之前配上)

```bash
cd ~/apps/it-spareparts || exit 1
APP_DIR=$(pwd -P)
BACKUP_DIR=/var/backups/spareparts

# 拒绝把既有 symlink 当成备份目录，避免 install/chown 跟随到意外目标。
test ! -L "$BACKUP_DIR" || { echo "$BACKUP_DIR 不能是符号链接"; exit 1; }
# 新建和既有真实目录都收紧为仅当前运维用户可访问。
sudo install -d -m 700 -o "$(id -un)" -g "$(id -gn)" "$BACKUP_DIR"

# 使用仓库内受测试的脚本，不再维护容易漂移的内联副本。
# 脚本需复制到应用根目录，才能从该目录读取 docker-compose.yml。
install -m 700 "$APP_DIR/.deploy/backup.sh" "$APP_DIR/backup.sh"

# 幂等安装 cron（每天凌晨 3 点）：清理本应用新旧路径的重复项，保留其他任务。
# BACKUP_CRON_INSTALL_BEGIN
BACKUP_SCRIPT="$APP_DIR/backup.sh"
REPO_BACKUP_SCRIPT="$APP_DIR/.deploy/backup.sh"
BACKUP_LOG="$APP_DIR/backup.log"
test -x "$BACKUP_SCRIPT" || { echo "$BACKUP_SCRIPT 不可执行"; exit 1; }
touch "$BACKUP_LOG"
chmod 600 "$BACKUP_LOG"
(
  set -eu
  CRON_CURRENT=
  CRON_FILTERED=
  CRON_ERROR=
  cleanup_cron_install() {
    [ -z "$CRON_CURRENT" ] || rm -f -- "$CRON_CURRENT"
    [ -z "$CRON_FILTERED" ] || rm -f -- "$CRON_FILTERED"
    [ -z "$CRON_ERROR" ] || rm -f -- "$CRON_ERROR"
  }
  trap cleanup_cron_install EXIT
  CRON_CURRENT=$(mktemp)
  CRON_FILTERED=$(mktemp)
  CRON_ERROR=$(mktemp)

  if LC_ALL=C crontab -l > "$CRON_CURRENT" 2> "$CRON_ERROR"; then
    :
  else
    CRON_READ_STATUS=$?
    EXPECTED_EMPTY="no crontab for $(id -un)"
    if [ "$CRON_READ_STATUS" -ne 1 ] \
        || [ "$(cat "$CRON_ERROR")" != "$EXPECTED_EMPTY" ] \
        || [ -s "$CRON_CURRENT" ]; then
      cat "$CRON_ERROR" >&2
      exit "$CRON_READ_STATUS"
    fi
    : > "$CRON_CURRENT"
  fi

  if grep -Fv -e "$REPO_BACKUP_SCRIPT" -e "$BACKUP_SCRIPT" \
      "$CRON_CURRENT" > "$CRON_FILTERED"; then
    :
  else
    CRON_FILTER_STATUS=$?
    [ "$CRON_FILTER_STATUS" -eq 1 ] || exit "$CRON_FILTER_STATUS"
  fi
  printf '0 3 * * * umask 077; %s >> %s 2>&1\n' \
    "$BACKUP_SCRIPT" "$BACKUP_LOG" >> "$CRON_FILTERED"
  crontab "$CRON_FILTERED"
)
# BACKUP_CRON_INSTALL_END

# 测试一次；本次运行也会把历史 dump/sha256 的权限统一修复为 600。
"$APP_DIR/backup.sh"
test "$(stat -c '%a' "$BACKUP_DIR")" = 700
find "$BACKUP_DIR" -maxdepth 1 -type f \
  \( -name 'db-*.dump' -o -name 'db-*.dump.sha256' \) \
  -printf '%m %p\n'
```

验收口径：备份 cron 必须恰好一条，`backup.log`、dump 与 `.sha256` 必须是
`600`，备份目录必须是 `700`；出现其他结果即视为部署失败。脚本使用稳定
`flock` 拒绝重叠执行，先把 dump 写入同目录临时文件，完成 TOC 与 checksum
校验后再原子发布；失败或同分钟重跑都不会截断既有恢复点。

恢复命令(灾难时):
```bash
sha256sum -c /var/backups/spareparts/db-<日期>.dump.sha256
sudo docker compose exec -T db pg_restore -U spareparts -d spareparts --clean --if-exists \
  < /var/backups/spareparts/db-<日期>.dump
```

---

## 十一、日常运维命令

```bash
# 看日志
sudo docker compose logs -f --tail 100 app
sudo docker compose logs -f frontend

# 重启
sudo docker compose restart app

# 升级必须按 docs/releases/ 中对应版本 Runbook，从精确 commit SHA 的 git archive
# 构建并留存 checksum/镜像 ID/回滚证据；不要在生产工作区直接 git pull 或现场构建。
# HTTPS 启用后 docker-compose.yml 由 root 固定管理，直接 git pull 也会破坏安全门禁。

# 停服
sudo docker compose down

# 完全干净重建(慎用, 不会删 volume)
sudo docker compose down
sudo docker compose up -d --build
```

---

## 十二、监控 / 告警 / 日志

**日志轮转**：docker-compose 已统一 `json-file` 单文件 10MB×5（每服务最多 50MB），不会撑满磁盘。

**健康巡检**（`.deploy/monitor.sh`，cron 每 5 分钟）：检查容器、DB、正式
HTTPS、同域 HTTP→HTTPS 跳转、证书 7 天续期余量、磁盘，以及最新备份的
新鲜度与 checksum 完整性；正常静默（只刷 `monitor.status` 心跳），异常追加
`monitor.log` 并（可选）发钉钉。
```bash
# 首次安装或升级巡检脚本（幂等；会清理本项目旧路径，不影响其他项目 cron）
cd ~/apps/it-spareparts || exit 1
APP_DIR=$(pwd -P)
# MONITOR_CRON_INSTALL_BEGIN
LEGACY_MONITOR="$APP_DIR/monitor.sh"
MONITOR_SCRIPT="$APP_DIR/.deploy/monitor.sh"
test -f "$MONITOR_SCRIPT" || { echo "缺少 $MONITOR_SCRIPT"; exit 1; }
test -x "$MONITOR_SCRIPT" || { echo "$MONITOR_SCRIPT 不可执行"; exit 1; }
(
  set -eu
  MONITOR_CRON_CURRENT=
  MONITOR_CRON_FILTERED=
  MONITOR_CRON_ERROR=
  cleanup_monitor_cron_install() {
    [ -z "$MONITOR_CRON_CURRENT" ] \
      || rm -f -- "$MONITOR_CRON_CURRENT"
    [ -z "$MONITOR_CRON_FILTERED" ] \
      || rm -f -- "$MONITOR_CRON_FILTERED"
    [ -z "$MONITOR_CRON_ERROR" ] \
      || rm -f -- "$MONITOR_CRON_ERROR"
  }
  trap cleanup_monitor_cron_install EXIT
  MONITOR_CRON_CURRENT=$(mktemp)
  MONITOR_CRON_FILTERED=$(mktemp)
  MONITOR_CRON_ERROR=$(mktemp)

  if LC_ALL=C crontab -l \
      > "$MONITOR_CRON_CURRENT" 2> "$MONITOR_CRON_ERROR"; then
    :
  else
    MONITOR_CRON_READ_STATUS=$?
    MONITOR_EXPECTED_EMPTY="no crontab for $(id -un)"
    if [ "$MONITOR_CRON_READ_STATUS" -ne 1 ] \
        || [ "$(cat "$MONITOR_CRON_ERROR")" != "$MONITOR_EXPECTED_EMPTY" ] \
        || [ -s "$MONITOR_CRON_CURRENT" ]; then
      cat "$MONITOR_CRON_ERROR" >&2
      exit "$MONITOR_CRON_READ_STATUS"
    fi
    : > "$MONITOR_CRON_CURRENT"
  fi

  if grep -Fv -e "$LEGACY_MONITOR" -e "$MONITOR_SCRIPT" \
      "$MONITOR_CRON_CURRENT" > "$MONITOR_CRON_FILTERED"; then
    :
  else
    MONITOR_CRON_FILTER_STATUS=$?
    [ "$MONITOR_CRON_FILTER_STATUS" -eq 1 ] \
      || exit "$MONITOR_CRON_FILTER_STATUS"
  fi
  printf '*/5 * * * * %s\n' "$MONITOR_SCRIPT" >> "$MONITOR_CRON_FILTERED"
  crontab "$MONITOR_CRON_FILTERED"
)
# MONITOR_CRON_INSTALL_END
# 启用钉钉告警：把钉钉群机器人 webhook URL 写进下面文件即可（不填则只记 monitor.log）
umask 077
printf '%s\n' 'https://oapi.dingtalk.com/robot/send?access_token=xxx' > "$APP_DIR/.alert_webhook"
chmod 600 "$APP_DIR/.alert_webhook"
# HTTPS 发布完成后必须启用边缘探针；该文件由运行 cron 的应用账号创建，不要用 root。
IT_DATA_HOST='<正式域名>'
printf 'https://%s/\n' "$IT_DATA_HOST" > "$APP_DIR/.https_monitor_url"
chmod 600 "$APP_DIR/.https_monitor_url"
cat "$APP_DIR/monitor.status"   # 看最近一次巡检结果
```

没有安全的 `.https_monitor_url` 时，脚本会继续做内部巡检，但不会探测证书和公网入口；
这种状态不满足生产 HTTPS 验收。

**本次生产修复 / 以后升级的验收**：

1. 先执行上面的幂等安装块，再确认 `test -x "$MONITOR_SCRIPT"` 成功，且
   `crontab -l | grep -F "$MONITOR_SCRIPT"` 只返回一条、绝对路径以
   `"$APP_DIR/.deploy/monitor.sh"` 结尾。
2. 确认 `.https_monitor_url` 是当前正式根域名、权限为 `600`；手工执行一次
   `"$MONITOR_SCRIPT"`。如返回非零，先检查 `monitor.status` / `monitor.log`，
   不得把异常结果当作通过。记录当前状态时间戳与 `monitor.log` 行数。
3. 连续两个 cron 周期观察（至少 11 分钟）：两次 `monitor.status` 时间戳都应向前推进，
   且状态均为 `ok=Y`；`monitor.log` 不得新增异常。
4. 用 `sudo -n journalctl -u cron --since '15 minutes ago' --no-pager` 核对这两个周期，
   不得出现脚本不存在、权限拒绝、sudo 交互或超时堆积。

**Postgres 慢查询日志**（免重启、写进数据卷持久化，零风险）：
```bash
sudo docker compose exec -T db psql -U spareparts -d spareparts \
  -c "ALTER SYSTEM SET log_min_duration_statement = 1000;" -c "SELECT pg_reload_conf();"
# 之后 >1s 的慢查询会进 db 容器日志：sudo docker compose logs db | grep duration
```

---

## 故障排查

### 1. `docker compose build` 因为内存被 OOM Kill

2G 机器 + 宝塔，build 前端时 Node + vite 内存峰值可能撑爆。两种解法:

**A. 临时停掉宝塔再 build**:
```bash
sudo systemctl stop bt
sudo docker compose build
sudo docker compose up -d
sudo systemctl start bt
```

**B. 本地 build 好镜像再传上去**(更稳, 见仓库 `docs/DEPLOY_PREBUILT.md` —— 暂未写, 需要时告诉我补)。

### 2. HTTPS 入口打不开

```bash
# 先看容器是否 Up
sudo docker compose ps

# 看 frontend 日志
sudo docker compose logs --tail 50 frontend

# 看 app 日志(看启动安全告警等)
sudo docker compose logs --tail 50 app

# 检查回环入口
sudo ss -tlnp | grep 8080
curl -fsS http://127.0.0.1:8080/ >/dev/null

# 检查边缘代理和证书
sudo docker logs --tail 100 personal-ai-assistant-caddy
curl -fsSI https://<正式域名>/
```

若回环入口正常而 HTTPS 失败，按域名 DNS、80/443 安全组、Caddy 配置、
证书签发和上游网络逐层检查。不要通过重新开放公网 8080 绕过故障。

### 3. 数据库连接失败

```bash
sudo docker compose exec db psql -U spareparts -d spareparts -c "select 1"
```
如果报 "password authentication failed", 说明 `.env` 改了 `POSTGRES_PASSWORD` 但 db 容器是旧密码起的。解决:
```bash
sudo docker compose down
sudo docker volume rm it-spareparts_postgres_data   # ⚠️ 会清空数据库, 仅在还没导入真实数据时用
sudo docker compose up -d --build
```

### 4. app 容器报"安全护栏拒启"

说明 `ENVIRONMENT=prod` 但密码/密钥还是默认值。检查 `.env` 是不是把 `POSTGRES_PASSWORD` / `ADMIN_PASSWORD` / `SECRET_KEY` 都改了。

---

## 接下来

- HTTPS、权限与导入导出验收通过后，让客户的 2-3 个用户先试用一周，
  观察内存峰值：`free -h`、`sudo docker stats`
- 数据增多到 10w+ 后, 严肃考虑升 4G 内存(腾讯云控制台可以热升级, 重启一次即可)
