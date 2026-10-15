# 商城图库只读契约 v1

- 版本：v1.0-draft（`schema_version=1`）。
- 日期：2026-10-15。
- 状态：**拟定，尚未实现**；本文不是当前可调用的 OpenAPI 或验收证据。
- 决策：D-36；提供方及规范 owner 为 P6（商城），消费方为 P5（备件系统）。两仓同路径保存逐字节相同副本，规范变更先同步文档，再改实现。
- 来源：批次 0 计划；《02-边界-P6-目录同步》§3.3–3.4、《02-边界-P5-目录发布》§3.4、《合并-目录同步-调研》§3.5–3.6；均为 2026-10-15 调研材料。

## 1. 权威与用途

图库、原图、缩略图、图片状态、来源、人工主图、顺序及删除意图由商城拥有。备件系统以 `(source_namespace, part_id)` 只读关联图库，可在主数据库详情和型号查询中查看图片、跳转商城管理。这里只传元数据和短期访问链接，浏览器直接从 OSS 读取图字节，应用后端不转发图片。

默认只返回正式、未删除、非瑕疵的通用图片；候选图、处理中图片、已删除图均不外发。瑕疵图须另有明确用途和契约扩展，本版本不提供切换参数。接口不上传、删图、排序，不写库存或主数据库资料，也不触发搜索、AI、建品或重新抓图。

## 2. 请求、鉴权与分页

```text
GET /api/integrations/gallery/v1/parts/{part_id}?source_namespace=itsp
Authorization: Bearer <独立 gallery:read 服务凭据>
```

服务凭据只由备件系统后端持有，不送浏览器，不复用 `catalog:submit` 或商城登录凭据。`gallery:read` 只授权图库元数据和签名读取，不能取得商城写接口权限。跳转 `management_url` 后仍需商城会话与其原有操作权限。

| 输入 | 类型和规则 |
|---|---|
| `part_id` | 主数据库稳定正整数 ID，不能用 PN 文本、本地 PN UUID 或展示号替代 |
| `source_namespace` | 必传，当前仅支持 `itsp`；与路径 ID 共同定位来源身份 |
| `gallery_id` | 可选，已知图库的稳定 ID；若与当前映射不一致返回 409，不能静默改查别的图库 |
| `cursor` | 可选，不透明分页游标，绑定来源、part_id、gallery_id、身份/成员/图库版本和页大小；首次不传 |
| `limit` | 拟定默认 100，范围 1–100；不允许无界全量，超限明确 422 |
| `If-None-Match` | 可选，条件读取；仅在身份、成员、图库版本和该页仍有效的访问链接均可复用时返回 304，否则 200 刷新链接 |

首次响应固定完整版本元组 `(identity_revision, member_revision, gallery_id, gallery_revision)`；后续页必须基于同一元组。无法保留固定页时返回版本冲突，客户端丢弃未完成轮次并从首屏重新读取，不能把不同版本的页拼成一套图。不透明游标失效返回 410。排序按 `sort_order` 升序，同值按 `image_id` 字符串升序，不能用临时 URL 排序。

`ETag` 为不透明值，仅用于同请求页的条件读取；稳定资源版本和 URL 有效期分别判断。客户端缓存的链接过期后须重新 GET，即使 `gallery_revision` 未变，也不能无限以 304 复用过期签名。响应使用私有缓存或禁止共享缓存，不能把带签名链接的响应放公共缓存。

## 3. 响应字段

| 字段 | 类型、含义 |
|---|---|
| `schema_version` | 整数 1；未知主版本停止解析，不能当空图库 |
| `source_namespace`, `part_id` | 回显请求的稳定来源身份 |
| `canonical_part_id` | 当前规范身份的正整数 ID；请求 merged 源时仍保留请求 part_id |
| `identity_revision`, `member_revision` | 非负整数；分别表示权威身份和本地图组成员版本；任何一个变化都使旧轮次失效 |
| `gallery_id`, `gallery_revision` | 稳定图库 ID 字符串和非负整数版本；成员、排序、主图、抑制变化须反映到版本 |
| `updated_at` | UTC RFC3339，图库有效内容或编排最后变化时间；签名刷新不伪造资料更新时间 |
| `management_url` | 受信商城站内商品管理 HTTPS 地址；固定允许的商城域名，不能承载任意外站跳转 |
| `items` | 本页图片数组；图库已绑定但无可公开通用图时为 `[]` |
| `next_cursor` | 下一页不透明字符串；末页为 null；空图库必须为 null |

每个 `items[]` 含以下字段。除带 `?` 字段外均必传；可选字段缺失或 null 均表示无该项，不代表删除其他图片。

| 字段 | 类型、含义 |
|---|---|
| `image_id`, `origin_pn_id` | 稳定图片 ID 和原归属商城 PN UUID；原归属不能因合并而改写 |
| `sha256` | 原图内容的 64 位小写十六进制摘要，用于去重，不以 URL 判断相同图 |
| `purpose` | 本版本固定 `general`（通用图），不把瑕疵/候选图伪装为标准图片 |
| `width`, `height`, `bytes` | 正整数像素和字节数，表示原图；不传原图二进制或 OSS 写凭据 |
| `content_type` | 经过商城图片校验的 MIME 类型，例如 `image/jpeg` |
| `source`, `source_url?` | 已存来源标识及可公开的原始来源地址；没有网址时 null，不能编造。返回来源不等于授予第三方图片再利用权，仍遵守已有许可范围 |
| `sort_order` | 整数，商城有效编排顺序；首项即有效主图，空图集无主图 |
| `url`, `thumb_url?`, `url_expires_at` | HTTPS 原图/缩略图短期读取地址和 UTC 到期时间；缩略图缺失可为 null。到期时间为本项所有链接共同安全可用的最早截止时刻 |

备件系统只持久保存稳定来源身份、gallery_id/image_id 和所见版本，不把签名 URL 当永久字段。链接到期重新取元数据；链接失效不能解释为图片被删除。无图、未绑定、接口故障分别显示，图库故障不阻塞主数据库资料编辑或商城库存业务。

## 4. 身份变化与图片保留

P6 先原子应用完整目录批次，再生成对应的图库关联；不根据单 PN 当前查询或半个 merge 批次先行改图组。P5 将响应 `identity_revision` 与本地所见来源身份核对，不以图库回执推进目录 checkpoint。无法确认关系时保留旧稳定引用并提示重读，不能将图挂到同名的另一 part_id。

合并仅聚合成员图库，原 image_id、origin_pn_id、OSS key 及来源保留；相同 SHA 展示一个有效代表并保留所有来源关联，不复制原图。组排序和抑制属于该组成员范围；目标人工删图/隐藏意图不能因成员再次出现而复活。撤销合并按来源恢复原图状态，不将组编排反写来源。分页期间出现 merge、unmerge 或人工调图时须整轮重取。

## 5. 离线 JSON 示例

以下为合成夹具，域名 `.invalid`、UUID 与签名占位仅供离线解析，不能调用；示例 SHA 为合成值，不代表已验过图字节。

已绑定且有一张通用图（HTTP 200）：

```json
{
  "schema_version": 1,
  "source_namespace": "itsp",
  "part_id": 101,
  "canonical_part_id": 102,
  "identity_revision": 7,
  "member_revision": 3,
  "gallery_id": "gallery-demo-102",
  "gallery_revision": 12,
  "updated_at": "2026-10-15T08:00:00Z",
  "management_url": "https://shop.example.invalid/pn/00000000-0000-4000-8000-000000000102",
  "items": [
    {
      "image_id": "00000000-0000-4000-8000-000000000201",
      "origin_pn_id": "00000000-0000-4000-8000-000000000101",
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "purpose": "general",
      "width": 1600,
      "height": 1200,
      "bytes": 240000,
      "content_type": "image/jpeg",
      "source": "employee_upload",
      "source_url": null,
      "sort_order": 1,
      "url": "https://images.example.invalid/demo.jpg?signature=offline",
      "thumb_url": "https://images.example.invalid/demo-thumb.jpg?signature=offline",
      "url_expires_at": "2026-10-15T08:15:00Z"
    }
  ],
  "next_cursor": null
}
```

已绑定但无正式通用图（HTTP 200）：

```json
{
  "schema_version": 1,
  "source_namespace": "itsp",
  "part_id": 103,
  "canonical_part_id": 103,
  "identity_revision": 1,
  "member_revision": 1,
  "gallery_id": "gallery-demo-103",
  "gallery_revision": 0,
  "updated_at": "2026-10-15T08:00:00Z",
  "management_url": "https://shop.example.invalid/pn/00000000-0000-4000-8000-000000000103",
  "items": [],
  "next_cursor": null
}
```

未绑定的来源身份（HTTP 404）；这与 `items=[]` 不同：

```json
{
  "error": {
    "code": "gallery_not_found",
    "message": "该来源身份尚未关联商城图库",
    "retryable": false,
    "request_id": "gallery-read-demo-1",
    "details": {"source_namespace": "itsp", "part_id": 104}
  }
}
```

## 6. 错误和恢复

统一错误结构为上例 `error{code,message,retryable,request_id,details}`；日志隐藏服务凭据、签名 URL 查询参数和业务隐私。

| HTTP | code | 消费方处理 |
|---|---|---|
| 401 | `invalid_service_credential` | 停止该凭据重试，配置修复后再读；不退回匿名图库 |
| 403 | `scope_denied` | 不扩大凭据作用域，不通过写接口取图 |
| 404 | `integration_disabled`, `gallery_not_found` | 区分未启用和来源尚未绑定；均不能显示为已确认无图 |
| 409 | `identity_conflict`, `group_revision_conflict`, `gallery_revision_conflict` | 保留稳定引用，丢弃本轮未完成分页，刷新目录或首屏；不混版本 |
| 410 | `gallery_cursor_expired` | 从首屏重新开始，不续接旧页 |
| 413 | `payload_too_large` | 明确上限，不截断图片元数据或签名 URL；必要时减小页大小 |
| 422 | `invalid_field`, `invalid_cursor`, `unsupported_schema` | 修正输入/客户端，不将失败解释为图库清空 |
| 429 | `rate_limited` | 按 Retry-After 有界重试，保留最后成功信息 |
| 503 | `gallery_unavailable` | 显示暂不可用，保留旧稳定引用，编辑及库存继续可用；过期 URL 不继续显示为有效 |

## 7. 兼容矩阵与实施验收

| 提供方 / 消费方 | 兼容结论 |
|---|---|
| 当前商城 / 新 P5 客户端 | 接口尚未实现；功能关闭或明确不可用，不能声称已关联成功 |
| gallery v1 / 支持 schema_version=1 的 P5 | 依本契约读正式通用图，独立服务凭据；未知可选字段可忽略 |
| gallery v1 / 既有商城图库与上传 | 原有会话、写权限、直传和票据协议继续使用，读令牌无写权 |
| 未知主版本 / v1 客户端 | 停解析并提示不兼容；改必需字段/语义须升主版本，两仓先更新规范 |
| catalog v2 / gallery v1 | 分别版本化；目录水位不受图库响应或故障推进，身份不匹配时重新核对 |

以下为 P6/P5 后续必须实现的契约场景，本次仅校验文档与 JSON 语法，未运行接口或真实 OSS：

| 初始条件 / 角色 | 动作 | 预期 |
|---|---|---|
| P5 服务只有 gallery:read；绑定 PN 有正式/候选/瑕疵/删除图 | GET 后尝试用同凭据写图库 | 只返回正式通用图，写操作拒绝 |
| 绑定空图集、未绑定 part_id、图库服务故障各一例 | 逐一 GET | 分别为 200 空数组、404、503，页面提示不同 |
| 多页固定图集，另一员工在翻页时调序或合并成员 | 继续旧 cursor | 固定版本或显式冲突，绝不混页；首屏重取可恢复 |
| 已读图集，签名 URL 已过期而图库版本未变 | 带条件重新 GET | 200 刷新可用链接，不能永久 304 返回旧链接 |
| A/B 合并且 SHA 相同，组内人工隐藏后重放目录，再撤销合并 | 读取各阶段图集 | 不复制原图、不复活隐藏图、来源保留，撤销按来源恢复 |

文档修订通过新提交同步两仓。实现后的回退优先关闭跨系统图库读取，保留原图、关联、抑制和审计；不能用删除图库或回滚数量账恢复旧显示。
