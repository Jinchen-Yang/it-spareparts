# 机型备件池双向只读接口 v1

itsp 维护整机与备件的多对多归属，Shop 提供每个稳定备件身份的好坏件在手数。两边前端使用各自当前登录会话，后端保管专属服务凭据。目录、库存分别读取并附各自版本或读取时间。

## 接口与配置

| 调用方 | 服务端接口 | 配置在调用方 | 配置在服务端 |
| --- | --- | --- | --- |
| Shop 后端 | itsp `GET /api/integrations/model-parts/v1/catalog` | `MODEL_PARTS_ITSP_CATALOG_URL` 为完整地址，`MODEL_PARTS_ITSP_CATALOG_TOKEN` 为目录只读令牌 | itsp 的 `MODEL_PARTS_CATALOG_TOKEN_SHA256` |
| itsp 后端 | Shop `POST /api/integrations/model-parts/v1/stock` | `MODEL_PARTS_SHOP_STOCK_URL` 为完整地址，`MODEL_PARTS_SHOP_STOCK_TOKEN` 为库存只读令牌 | Shop 的 `MODEL_PARTS_STOCK_TOKEN_SHA256` |
| Shop 浏览器 | Shop `GET /api/model-parts/catalog` | 当前员工会话 | 可选 `MODEL_PARTS_ITSP_WEB_ORIGIN` 指定用户浏览器可访问的 itsp 网站 origin，维护链接使用站内 `/model-parts` |

两个服务调用各用独立的高熵 Bearer 令牌，调用方保存原文，服务端保存 SHA-256 小写十六进制摘要。部署传输使用 HTTPS，本机测试可用回环 HTTP。成功返回 `Cache-Control: no-store`。目录凭据只能读机型关系。库存凭据仅能按身份查询数量。

## 机型目录

itsp 服务端返回 `version: 1`、目录变更号 `revision`、机型数组 `models`、备件数组 `parts`、成员数组 `members` 和待核数组 `pending`。机型、备件和待核身份均使用字符串，备件身份为稳定的正整数 `part_id` 的十进制表示。成员引用机型和备件，保留部位、适用配置、来源和核定过的录入原号。每条成员关系必须带 itsp 签发的 `membershipVersion`（64 位小写十六进制字符串）。Shop 按原值透传。用户在 itsp 移出成员时，页面以 `expectedMembershipVersion` 提交所见版本，供 itsp 核对要移出的归属。目录快照里的 `good`、`bad` 由读端取 Shop 数量后填充。Shop 对主库读取有 5 秒整请求时限，接收未压缩响应上限 16 MiB，未知字段、重复身份、悬空关系或中途失败会返回 503。服务不将一次失败当成机型空清单。

两个宿主页均读取 `{catalog, canMaintain, maintenanceUrl, stockStatus, stockAsOf}`。Shop 的 `canMaintain` 为 `false`。配置了维护入口的员工可在 itsp 以本人账号打开同一页面。itsp 原生维护权限由 itsp 会话实时决定。

## 库存批量读取

请求示例

```json
{"partIds":["17","24"]}
```

示例响应

```json
{"items":[{"partId":"17","good":4,"bad":0},{"partId":"24","good":null,"bad":null}],"asOf":"2026-11-26T03:30:00Z"}
```

每批至多 1000 个正整数身份，接受带引号的十进制字符串以保留 64 位精度。请求按首次出现的顺序去重返回，两个等级分别为非负整数或 `null`。Shop 按活动的 `catalog_part_link(source_namespace="itsp", part_id)` 关联有效本地 PN 与好坏 SKU，在一次数据库查询内读取。缺少一个等级只将那个等级标为未知。零库存要求相应 SKU 确实存在且账面数量为 0。已合并、停用或无映射的身份返回未知。

itsp 按 1000 个身份分批读取，8 秒整次时限覆盖全部批次，`stockAsOf` 取各批最早的带时区读取时间。每批必须对齐请求身份，收到超限、重定向、不可解析或不完整响应时，本次目录保留，全部库存置为未知，`stockStatus` 为 `unavailable`、`stockAsOf` 为 `null`。Shop 原生读库存失败时使用相同状态。查询不会写库存流水或备件归属。

## 原生页面与维护

两套应用都在 `/model-parts` 挂载同一个 React 组件。Shop 在「更多」打开，itsp 在销售管理的「型号查询」旁打开。itsp 要求目录读取权限，增改还要求有效实名账号和备件主数据维护权限。Shop 的「在 itsp 维护」只带选择的机型、查询词、类别、库存条件和清单页签。itsp 使用自己的登录会话重新判权。

itsp 的 `POST /api/model-parts/candidates` 按搜索词选档，或按粘贴的原号批量取核对候选。精确查询的 `matches` 按统一宽窄、去除两端空白、转小写后的每个输入原号给出当前匹配的档案 ID 数组。空数组表示当前没有匹配。员工可以为待核的现场原号选择已存在的档案并填写依据。

`POST /api/model-parts/commands` 接受新建机型、确认清单、核定待核行及移出归属。命令携带预览时的版本号和 UUID 请求号，同一身份重试同一命令可取回原回执。服务端确认当前有效的 PN 对应关系，事务提交审计及归属。过期预览返回冲突，要求重看后确认。

共享组件源码在 Shop 的 `frontend/prototypes/model-parts`。Shop 直接编译源码，itsp 使用同源构建的本地 npm 包和锁文件。发布下一版组件时，先递增 `pack.mjs` 里的包版本。在 Shop 的 `frontend` 下执行 `node prototypes/model-parts/pack.mjs <itsp 的 frontend/vendor 目录>`，再从 itsp 的 `frontend` 安装新版本的 tarball 并一起提交包和锁文件。两个前端各自构建，页面读取各自的同源 API。

## 候选验收

1. 在 itsp 用具有型号查询和备件主数据维护权限的实名账号新建一个机型，添加已有 PN，经预览后保存。
2. 用独立浏览器登录 Shop 员工账号。在「更多 → 机型备件池」刷新并搜索同一 PN，对照机型、部位、依据和好坏件数量。
3. 在 Shop 选择机型、输入查询词，再打开「在 itsp 维护」。itsp 登录到有权账号后返回刚才的机型和查询。只读账号可以查看归属。
4. 把同一 PN 加入另一个机型，跨机型搜索只显示一行 PN。移出其中一项，另一机型和库存仍可查。分别使用明确零库存和无商城映射的 PN 核对状态。

候选在 itsp 上新增 `d76c5d2b9e04` 迁移，Shop 复用目录映射与 SKU。配对环境的发布顺序是先备份 itsp 并迁移，更新两端后端与前端，再配置两套独立凭据。暂停两侧只读集成并回退应用可保留已录入的归属；实际生产发布另行安排。

### 2026-11-26 隔离联调记录

在两个独立 PostgreSQL 库，以不同账号从各自的菜单进入页面。维护员在 H3C UniServer R6900 G3 预览并加入测试网卡，Shop 员工刷新后查到了同一 PN、安装位置、归属依据及好件 0／坏件 1。移出网卡后，Shop 同步查不到这条归属。两款机型共用的示例电源各自仍能反查到两个机型，两端显示好件 4、坏件 1。

浏览器复验覆盖 Shop 320 像素和 itsp 390 像素宽度，分别验证零库存、未映射库存和带筛选跳转维护。itsp 迁移在隔离库升降往返，迁移后 ORM 检查通过。清单和库存均为验收样例；用户点按和生产发布另行记录。
