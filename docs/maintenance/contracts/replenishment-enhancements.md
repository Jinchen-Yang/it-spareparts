# 补库增强契约

## 云端购物车

`GET/PUT/DELETE /api/replenishment-beta/cart-drafts/{project_id}` 与
`POST .../{project_id}/submit`。草稿唯一键为用户+项目，明细唯一键为草稿+`part_id`；PUT 使用 `expected_version`，提交在创建申请和删除草稿的同一事务内完成。

## 自动审核

`replenishment_auto_review_enabled=true` 时：池内 PN 通过；非池 PN 在近 182 天有采购或销售样本即通过；两者都无则打回。`niche_pn` 只作为冻结证据展示。打回行进入 `needs_revision`，响应的 `screening_json.schema_version=2`，最多返回 3 个池内相似 PN。

`POST /api/replenishment-beta/applications/{application_id}/revisions` 要求对全部 rejected `request_line_id` 恰好处理一次，支持 `replace/remove`，至少保留一条有效明细。

## 搜索

补库目录使用 `dim_part.search_doc`、`keyword_groups_or_substr` 和 `col_matches_any`。规格词变体共享查询层口径，左词界阻止 `8t` 命中 `18t/1.8t`。

