# 坏件循环待修池发布控制器（只准备，不执行）

决策: D-0058

本文登记两支新的生产发布控制器和它们的阶段命令。它不是生产回执，不表示已经打标签、切流、开出站或上线。本变更不合并、不部署、不连接生产写、不生成或轮换 token、不改 `frontend/src/version.ts`、不打开闲鱼/闲管家 live 发布，也不碰 HOLD 的 #520–#522。

启用顺序（控制器守边界，本文件不执行）：先部署商城 v2（0028 然后 0029，token 仍空）→ 保存经办人 → 再配商城 token → 再部署 itsp（迁移 + 1.47.0，出站仍关）→ 授权页键 → 最后单独打开 `SHOP_CYCLE_SELL_ENABLED`。

## 不能复用的旧入口

| 文件 | 为什么不能拿来跑这次 |
| --- | --- |
| `.deploy/requirements_successor_ops.py` | `target_head` 钉死 `98afc0d2157b`，migrate 只做 heads/current/check |
| `.deploy/requirements_visibility_ops.py` | 范围是 `e5a7c9d1f3b6 → 98afc0d2157b` |
| `.deploy/shop_pn_ops.py` | 范围是 `0026_pn_master_follow_merges → 0027_catalog_delivery_receipt` |

三个文件的字节都没有改。itsp 控制器在导入时核对可见性控制器 `6b2fe7287242ff6ce5dfcd785613877ef49dd80f13fb8ea15c4d244e93d37e9d` 和接续控制器 `2b2aa5ca2ca305b9f42574b50f505199169919323eea9d7c80ed90e3f19e2d1e`。商城控制器核对 `shop_pn_ops.py` 的 `beca271f1420981e5dcf21b78c7469710ce084efc75d0247b86712b567361c48`。

itsp 的升级顺序继承可见性控制器：先证明无网络恢复出旧头，再比较冻结时的业务行，然后真正迁移，`alembic check`，再比较那些行。命令是 `alembic upgrade head`，不是接续发布的空迁移。候选镜像的脚本头是 `c3e8a1d94b27`，所以升级前 `alembic current` 是 `98afc0d2157b (mergepoint)`（不再带 `(head)`），停在中间修订时是不带后缀的 `86f7486b9ed5`。`86f7486b9ed5` 会把五个循环页键写成 false，因此 `sys_role_template.permissions` 与 `sys_user` 的 `template_perms` / `perm_overrides` / `permissions` 允许按该迁移的投影变化；其余列仍与冻结摘要一致。接续发布的其余教训留在新文件里：Mounts 按目标/类型/源排序后再比较（0 个或 1 个挂载只有一种顺序，不要求现场容器再加挂载；两个及以上仍要证明顺序不影响比较）、主站 route 接受 `terminal: true`、freeze 前必须有当次 live 快照的 dry-run、`exposure-started.json` 先于改 Caddyfile、切流不用 `POST /load`、暴露后不自动回滚。出站重建使用单独的审计文件名，并在停掉正在跑的 app 之前完成环境与路径检查。

## itsp

- 发布提交：`ccefa17cc6446f613b7237ac0cd2121256969622`（#535 的 squash merge，`APP_VERSION=1.47.0`）。不要用父提交 `b2650202c2adb9c40c8fef4663e3472963ef534a`，也不要用分支头 `7e5f15e5`。
- 现网前序：容器 `itsp-successor-e62cd60cdbcc-app` / `itsp-successor-e62cd60cdbcc-frontend`，owner `.requirements-successor-active-owner.json`。新控制器只读这个 owner，不改写、不删除。
- 新目录：`/data/spareparts/releases/cycle-pool-ccefa17cc6446f613b7237ac0cd2121256969622/`（root、0700）。新 owner：`.cycle-pool-active-owner.json`。新容器前缀：`itsp-cycle-ccefa17cc644`。
- 预期 schema：backup-old 恢复头 `98afc0d2157b`；候选 `alembic heads` 唯一为 `c3e8a1d94b27 (head)`；`alembic upgrade head` 走 `98afc0d2157b → 86f7486b9ed5 → c3e8a1d94b27`。backup-new 恢复头 `c3e8a1d94b27`，`maintenance_project` 与 `maintenance_project_contract` 行数与冻结时一致，四张新循环表为 0 行。
- 候选 app 在创建时写入三个环境变量，不靠 `environment_file` 标签，也不读 `/opt/it-spareparts/.env`：`SHOP_CYCLE_SELL_URL=https://shop.ybwznt.com/api/integrations/cycle/sell-intents`，`SHOP_CYCLE_SELL_TOKEN` 来自发布目录里 root/0600 的 `cycle-sell.token.private`（无换行），`SHOP_CYCLE_SELL_ENABLED=false`。不配置 `CYCLE_SELL_CALLBACK_TOKEN_SHA256`。
- 日备目录按现场使用 `/data/backups/spareparts`。`.deploy/restore_drill.sh` 仍查找 `/var/backups/spareparts`，本变更不改那个脚本，避免影响仍引用旧路径的说明和其他调用；这次门禁用控制器自己的 dump，并记录日备文件名和 sha256。
- 回滚：不 `downgrade`，不改写 `alembic_version`，不把 dump 盖回生产。暴露标记出现之后不能 `recover`。若升级停在 `86f7486b9ed5`，排除原因后重跑 migrate，它会再次执行 `alembic upgrade head`，不会降级。关出站是 `disable-outbound`：只把运行中的 itsp app 重建为 `SHOP_CYCLE_SELL_ENABLED=false`，不动数据库、Caddy 和商城。

入口：

```text
python3 .deploy/cycle_pool_ops.py \
  /data/spareparts/releases/cycle-pool-ccefa17cc6446f613b7237ac0cd2121256969622/manifest.json \
  PHASE
```

`PHASE` 依次为 `preflight`、`images`、`dry-run`、`freeze`、`backup-old`、`migrate`、`backup-new`、`bluegreen`、`cutover`、`verify`。切流前、且 schema 仍是 `98afc0d2157b` 时，显式恢复阶段是 `recover`。`recover.intent.json` 落下之后，正常阶段不再执行；没有 PASS 回执时可以再跑一次 `recover`。出站开关是切流和 verify 之后的独立阶段：`enable-outbound`、`disable-outbound`。只停 manifest 里的旧 app，不停 `it-spareparts-db-1`、Caddy 或商城。禁止 `docker compose down` 项目 `it-spareparts`。

`dry-run` 把当次只读快照写到发布目录，再跑 `.deploy/cycle_pool_dry_run.py` 的 19 项检查和负例。19/19 且负例 `ALL_OK` 才会留下 PASS；`freeze` 核对这份回执和控制器 sha256。现场容器有 0 个或 1 个挂载时，两次 Mounts 样本必须相同；两个及以上才要求看到两种顺序。顺序规范化另外用一对合成双挂载证明，不给现场容器加挂载。也可以离线重放，不连生产：

```text
python3 .deploy/cycle_pool_dry_run.py SNAPSHOT.json .deploy/cycle_pool_ops.py
```

归档核对（只读，不加生产锁）：

```text
git -C REPO archive --format=tar.gz ccefa17cc6446f613b7237ac0cd2121256969622 > source.tar.gz
python3 .deploy/cycle_pool_ops.py --check-archive REPO source.tar.gz
```

manifest 必须带上控制器、smoke、dry-run、可见性、接续、pool、helper 的 sha256。执行时以文件字节为准；上面历史控制器的摘要是固定门禁。

### 已有 preflight / images PASS 时换控制器并重跑 dry-run

2026-11-13 的挂载顺序修正只改 `.deploy/cycle_pool_ops.py` 和 `.deploy/cycle_pool_dry_run.py`。生产 app 只有一个挂载，旧门禁却要求两次 Mounts 样本顺序不同，`dry-run` 在 `capture_snapshot` 报 `mount order sample did not vary`。新文件摘要：

| 字段 | 旧值 | 新值 |
| --- | --- | --- |
| `controller_sha256` | `ba4e6874c76f20c4aa89861c8582c7f8e82581291b0b7bd0af27afa26b1a8fbe` | `3bfa6ecf33d874281c7b28aa5a24dbabb04ae13cbad33de2d4bc5b8e548a31ef` |
| `dry_run_sha256` | `073b33061b73c2fbd30d384d8f30f4357782fbc05f1169505d55f445538f8b6e` | `5dd23aa813980e9c20cf96db7a3d6876c19c94daed11f86847d3b19a2f8ca131` |

其它 manifest 字段不动，包括 `release`（仍是 `ccefa17cc6446f613b7237ac0cd2121256969622`）、镜像、smoke、可见性、接续、pool、helper、证据和 token 摘要。不改现场容器挂载，不跑 `freeze` 及之后的阶段。

`preflight.json` / `images.json` 里的 `manifest_sha256` 必须等于当前 manifest 字节。`h.save` 用 `O_EXCL`，不会覆盖已有回执。owner `.cycle-pool-active-owner.json` 也不会在已存在时改写自己。所以要先把 manifest 里的两个摘要换成上表新值，按新 manifest 字节重写 owner，再把下面这些独占文件移出发布目录，然后按原门禁重跑只读的 `preflight`、`images` 和 `dry-run`。这三步仍是原来的门禁，不是放宽后的检查。

只在还没有 `dry-run.json`、`freeze.intent.json`、`exposure-started.json`、`recover.intent.json` 以及更后阶段文件时做。在 root 下执行。`CTRL` 是上次 preflight 实际使用的控制器目录（runbook 入口里的 `.deploy`）。先把两个新文件装成 root、0600、非符号链接，并核对摘要，再跑脚本：

```bash
install -o root -g root -m 0600 cycle_pool_ops.py "$CTRL/cycle_pool_ops.py"
install -o root -g root -m 0600 cycle_pool_dry_run.py "$CTRL/cycle_pool_dry_run.py"
sha256sum "$CTRL/cycle_pool_ops.py" "$CTRL/cycle_pool_dry_run.py"
```

摘要必须分别是上表的新 `controller_sha256` 和新 `dry_run_sha256`。然后：

```bash
set -euo pipefail
: "${CTRL:?set CTRL to the controller directory}"
REL=/data/spareparts/releases/cycle-pool-ccefa17cc6446f613b7237ac0cd2121256969622
OWNER=/data/spareparts/releases/.cycle-pool-active-owner.json
ARCH=/data/spareparts/releases/cycle-pool-ccefa17-rebind-$(date -u +%Y%m%dT%H%M%SZ)
test "$(id -u)" -eq 0
test ! -e "$ARCH"
mkdir -m 0700 "$ARCH"
python3 - "$CTRL" "$REL" "$OWNER" "$ARCH" <<'PY'
import hashlib, json, os, stat, sys
from pathlib import Path

ctrl, rel, owner_path, archive = map(Path, sys.argv[1:5])
NEW_CONTROLLER = "3bfa6ecf33d874281c7b28aa5a24dbabb04ae13cbad33de2d4bc5b8e548a31ef"
NEW_DRY = "5dd23aa813980e9c20cf96db7a3d6876c19c94daed11f86847d3b19a2f8ca131"
OLD_CONTROLLER = "ba4e6874c76f20c4aa89861c8582c7f8e82581291b0b7bd0af27afa26b1a8fbe"
OLD_DRY = "073b33061b73c2fbd30d384d8f30f4357782fbc05f1169505d55f445538f8b6e"
SMOKE = "8b942cbbd5fcb70517f84647af6ad25de72a621ccc7c7befd3ad55191685d7d5"
VISIBILITY = "6b2fe7287242ff6ce5dfcd785613877ef49dd80f13fb8ea15c4d244e93d37e9d"
SUCCESSOR = "2b2aa5ca2ca305b9f42574b50f505199169919323eea9d7c80ed90e3f19e2d1e"
RELEASE = "ccefa17cc6446f613b7237ac0cd2121256969622"

def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()

def need_private(path):
    meta = path.lstat()
    if stat.S_ISLNK(meta.st_mode) or not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or stat.S_IMODE(meta.st_mode) != 0o600:
        raise SystemExit("not a root-owned 0600 regular file: " + str(path))

def write_all(fd, data):
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise SystemExit("short write")
        view = view[count:]
    os.fsync(fd)

for name, expected in (
    ("cycle_pool_ops.py", NEW_CONTROLLER),
    ("cycle_pool_dry_run.py", NEW_DRY),
    ("cycle_pool_smoke.py", SMOKE),
):
    path = ctrl / name
    need_private(path)
    if digest(path) != expected:
        raise SystemExit("unexpected bytes: " + name)
for name, expected in (
    ("requirements_visibility_ops.py", VISIBILITY),
    ("requirements_successor_ops.py", SUCCESSOR),
):
    path = ctrl / name
    if path.is_symlink() or not path.is_file() or digest(path) != expected:
        raise SystemExit("unexpected bytes: " + name)

blocked = (
    "dry-run.json", "freeze.intent.json", "freeze.json",
    "backup-old.intent.json", "backup-old.json",
    "migrate.intent.json", "migrate.json",
    "backup-new.intent.json", "backup-new.json",
    "bluegreen.intent.json", "bluegreen.json",
    "cutover.intent.json", "cutover.json",
    "exposure-started.json", "recover.intent.json",
    "enable-outbound.intent.json", "disable-outbound.intent.json",
)
for name in blocked:
    if (rel / name).exists():
        raise SystemExit("later phase exists: " + name)
if list(rel.glob("verify*.json")) or list(rel.glob("recover-*.json")) or list(rel.glob("recover-*.intent.json")):
    raise SystemExit("verify or recover file exists")
for name, phase in (("preflight.json", "preflight"), ("images.json", "images")):
    row = json.loads((rel / name).read_text())
    if row.get("status") != "PASS" or row.get("phase") != phase or row.get("release") != RELEASE:
        raise SystemExit(name + " is not a PASS receipt for this release")

manifest = rel / "manifest.json"
need_private(manifest)
need_private(owner_path)
raw = manifest.read_bytes()
if raw.count(OLD_CONTROLLER.encode()) != 1 or raw.count(NEW_CONTROLLER.encode()) != 0:
    raise SystemExit("manifest controller pin is not the single old digest")
if raw.count(OLD_DRY.encode()) != 1 or raw.count(NEW_DRY.encode()) != 0:
    raise SystemExit("manifest dry-run pin is not the single old digest")
before_manifest = archive / "manifest.json.before"
before_owner = archive / "cycle-pool-active-owner.json.before"
before_manifest.write_bytes(raw)
before_owner.write_bytes(owner_path.read_bytes())
os.chmod(before_manifest, 0o600)
os.chmod(before_owner, 0o600)
updated = raw.replace(OLD_CONTROLLER.encode(), NEW_CONTROLLER.encode()).replace(OLD_DRY.encode(), NEW_DRY.encode())
body = json.loads(updated)
if body.get("release") != RELEASE or body.get("controller_sha256") != NEW_CONTROLLER or body.get("dry_run_sha256") != NEW_DRY:
    raise SystemExit("manifest identity did not take the new digests")
fd = os.open(manifest, os.O_WRONLY | os.O_NOFOLLOW)
try:
    os.ftruncate(fd, 0)
    write_all(fd, updated)
finally:
    os.close(fd)
manifest_sha = hashlib.sha256(updated).hexdigest()
owner = {"release": RELEASE, "manifest_sha256": manifest_sha, "controller_sha256": NEW_CONTROLLER}
fd = os.open(owner_path, os.O_WRONLY | os.O_NOFOLLOW)
try:
    os.ftruncate(fd, 0)
    write_all(fd, (json.dumps(owner, ensure_ascii=False, indent=2) + "\n").encode())
finally:
    os.close(fd)
print(manifest_sha)
PY
for name in \
  preflight.intent.json preflight.json \
  images.intent.json images.json \
  dry-run.intent.json dry-run-snapshot.json \
  Caddyfile.baseline all-inspect.private.json \
  itsp-successor-e62cd60cdbcc-app.inspect.private.json \
  itsp-successor-e62cd60cdbcc-frontend.inspect.private.json
do
  if [ -e "$REL/$name" ]; then
    mv "$REL/$name" "$ARCH/$name"
  fi
done
python3 "$CTRL/cycle_pool_ops.py" "$REL/manifest.json" preflight
python3 "$CTRL/cycle_pool_ops.py" "$REL/manifest.json" images
python3 "$CTRL/cycle_pool_ops.py" "$REL/manifest.json" dry-run
```

脚本在改写前把旧 manifest 和 owner 复制到 `$ARCH`。打印出的那一行是新的 `manifest_sha256`。`preflight` 会重新读取现场容器和 Caddyfile，并在发布目录里重建 `Caddyfile.baseline` 与 inspect 文件；`images` 只核对候选镜像 ID；`dry-run` 才跑 19 项检查。三步之间不要重启容器。`dry-run` 打印 `status: PASS` 后停止，不要在这一步进入 `freeze`。

若脚本在打印 `manifest_sha256` 之前失败，用 `$ARCH` 里的 `manifest.json.before` 和 `cycle-pool-active-owner.json.before` 覆盖回去并保持 0600。若失败发生在移走独占文件之后，先把发布目录里未完成的 `*.intent.json` 再移入 `$ARCH`，再把旧文件移回。控制器也换回旧摘要。不要手改回执里的 hash。

## 商城

`SHOP_RELEASE` 还没有。必须从生产 `4b40bb24c6de3cc95e1361e97ca55e3b53475792` 窄移植 #89 和 #92 之后，用那个合并提交的 40 位 SHA。不要部署商城 main（已知发散头前缀 `f5481cf1`），也不要把这条 release 分支合回商城 main。本仓库不包含那次窄移植。

- 目录：`/data/shop/releases/cycle-<40位SHOP_RELEASE>/`。owner：`.shop-cycle-active-owner.json`。不改 `.shop-pn-active-owner.json`。
- `old_head=0027_catalog_delivery_receipt`，`intermediate_head` 必须匹配 `0028_*`，`target_head=0029_cycle_sell_qty`。`alembic upgrade head` 先跑 0028 再跑 0029。
- 迁移只使用这条命令形态。`--env-file` 只作为 compose 参数传入，控制器不读取、不打印 `/root/IT-shop/private/next.env`：

```text
docker compose --env-file /root/IT-shop/private/next.env -p it-shop-next \
  -f /data/shop/releases/cycle-<40位SHOP_RELEASE>/prod.compose.yml \
  --profile migration run --rm --no-deps <migration_service> alembic upgrade head
```

`migration_service` 写在 manifest 里，必须是 PRODUCTION.md 里 `--profile migration` 的服务名（预期 `migration`）。它只能是一段服务名，不能带空格或参数。

入口：

```text
python3 .deploy/shop_cycle_ops.py \
  /data/shop/releases/cycle-<40位SHOP_RELEASE>/manifest.json \
  PHASE
```

`PHASE` 依次为 `preflight`、`freeze`、`backup-old`、`migrate`、`backup-new`、`create`、`activate`、`verify`。freeze 已开始但还没有迁移/创建/暴露时，`recover-freeze` 拉起原来的五个容器，并等到 backend 与 frontend 的 Docker 健康检查以及公网 `/api/ready`、`/healthz` 都通过才写 PASS。失败后只要还没有这份 PASS，就可以再跑 `recover-freeze`。停写范围是 backend、worker、agent、frontend 和 NocoDB bridge。不碰 Noco 界面和 Noco 的两个库，不碰 `it-shop-prod-*`、itsp、Caddy。不 `compose down`，不改 Caddyfile 里的 `shop.ybwznt.com`。`preflight` 执行候选树里的 `deploy/scripts/check-external-networks.sh`；frontend 起来之后执行 `deploy/scripts/rollback-networks.sh` 把 ingress 接回去。两份脚本的 sha256 钉在 manifest 里，脚本里若出现 `docker compose down` 则拒绝。

部署期间 `CYCLE_SELL_TOKEN_SHA256` 必须为空。不改 `CATALOG_*` / `PN_MASTER_*`。不降级。0029 上若已经有名字里带 `cycle` 的表写入行，控制器拒绝再把旧程序指回这个库。

## 非目标

- 不在本 PR 执行任一阶段，不 SSH 改生产，不打 `release-` 标签。
- 不生成、不打印、不提交 token 或 `.env`。
- 不打开 `SHOP_CYCLE_SELL_ENABLED` 的默认值；`enable-outbound` 只是以后受控阶段的代码。
- 不实现闲鱼/闲管家 live 发布，不装配真实发帖客户端。
- 不改 HOLD 的 #520–#522，不改 `.deploy/restore_drill.sh`、`shop_pn_ops.py`、`requirements_successor_ops.py`、`requirements_visibility_ops.py`。
