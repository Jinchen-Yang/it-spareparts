# 部署到云服务器(Debian + 腾讯云 + 宝塔面板)

> 适配环境：Debian 11/12、有 sudo、有宝塔面板、2C2G(或更高)。
> 部署完成后，浏览器访问 `http://<服务器公网IP>:8080`，admin 登录。

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

### 方式 A：用 Personal Access Token(最简单)

1. 浏览器登录 GitHub → 右上角头像 → **Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token (classic)**
2. 权限只勾 `repo`, 有效期选 30/90 天
3. 复制生成的 token(只显示一次)

```bash
mkdir -p ~/apps && cd ~/apps
git clone https://<贴你的 token>@github.com/Jinchen-Yang/it-spareparts.git
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

## 八、防火墙 + 腾讯云安全组放行

```bash
# 系统防火墙(若开了 ufw 或 iptables)
# Debian 默认通常没开, 可以跳过
sudo ufw allow 8080/tcp 2>/dev/null || true
```

**腾讯云控制台**:
1. 登录腾讯云 → CVM → 你的实例 → 安全组
2. 添加入站规则: 协议 TCP, 端口 `8080`, 来源 `0.0.0.0/0`(或限定你公司出口 IP)
3. 22(SSH) 和 8888(宝塔) 应已开

---

## 九、访问

浏览器打开:**`http://<服务器公网IP>:8080`**

- 用户名: `admin`
- 密码: 你在 `.env` 里设的 `ADMIN_PASSWORD`

第一次没有数据, 顶部点"数据导入"上传你的 .xlsx 即可。

---

## 十、备份(强烈建议在客户用之前配上)

```bash
# 创建备份目录
sudo mkdir -p /var/backups/spareparts && sudo chown $USER:$USER /var/backups/spareparts

# 写每日备份脚本
cat > ~/apps/it-spareparts/backup.sh <<'EOF'
#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
DATE=$(date +%Y%m%d-%H%M)
sudo docker compose exec -T db pg_dump -U spareparts -Fc spareparts \
  > /var/backups/spareparts/db-$DATE.dump
# 只留最近 14 天
find /var/backups/spareparts -name 'db-*.dump' -mtime +14 -delete
EOF
chmod +x ~/apps/it-spareparts/backup.sh

# 加 cron(每天凌晨 3 点)
(crontab -l 2>/dev/null; echo "0 3 * * * $HOME/apps/it-spareparts/backup.sh >> $HOME/apps/it-spareparts/backup.log 2>&1") | crontab -

# 测试一次
~/apps/it-spareparts/backup.sh
ls -la /var/backups/spareparts/
```

恢复命令(灾难时):
```bash
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

# 升级代码
cd ~/apps/it-spareparts
git pull
sudo docker compose up -d --build
sudo docker compose exec app alembic upgrade head

# 停服
sudo docker compose down

# 完全干净重建(慎用, 不会删 volume)
sudo docker compose down
sudo docker compose up -d --build
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

### 2. 容器启动后访问 8080 没反应

```bash
# 先看容器是否 Up
sudo docker compose ps

# 看 frontend 日志
sudo docker compose logs --tail 50 frontend

# 看 app 日志(看启动安全告警等)
sudo docker compose logs --tail 50 app

# 检查端口监听
sudo ss -tlnp | grep 8080
```

如果显示 `Listen 0.0.0.0:8080` 但浏览器仍打不开，**90% 是腾讯云安全组没开 8080**。

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

- 跑通后, 让客户的 2-3 个用户先试用一周, 观察内存峰值: `free -h`、`sudo docker stats`
- 如果稳定, 加上 **HTTPS** (用宝塔自带 SSL 模块, 把 `http://IP:8080` 反代成 `https://你的域名`)
- 数据增多到 10w+ 后, 严肃考虑升 4G 内存(腾讯云控制台可以热升级, 重启一次即可)
