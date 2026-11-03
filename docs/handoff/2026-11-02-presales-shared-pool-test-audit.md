# D-0052 共享池删除测试复核

复核对象是初次交接时的工作树。以 `git show :<路径>`、`MERGE_HEAD` (`fc85ac61`) 和工作树中的 Python 函数逐项核对。原清单有 63 个唯一测试函数，另有一行说明整个测试文件被删除。`origin/main` 包含其中 62 项，第 16 项来自待合并的生产热修。另有 3 个候选查询旧函数改名，不计入这 63 项。

下表中的编号对应文末原函数。右列列出关键回归的函数名，统一省略 `test_` 前缀，同一格内多个名称以 `/` 分隔。运行范围和结果在[共享池交接](2026-11-02-presales-shared-pool.md)中单列。

| 原编号 | 本次处置 | 可检索的回归用例 |
|---|---|---|
| 1、5、57 | 改写字段保护。单条、批量和详情的共享值一致，内部字段仍不输出。 | `shared_offer_fields_match_across_readers_and_omit_internal_fields` |
| 8、9 | 共享报价、备注和自定义业务值对有售前权限者公开。历史个人受众列表仍只回读给字段作者或主管，包括报价修订。 | `shared_values_do_not_disclose_personal_audience_lists` |
| 2、15 | 保留客户权限及搜索隔离。供应商随售前菜单公开。 | `customer_permission_is_required_even_for_owner_or_supervisor` |
| 3、4、6、11、18、39、46、58 | 旧断言依赖整单私密、报价人受众、参与或领取。按 D-0052 开放已提交记录。菜单权限和报价有效性仍分别校验。 | `private_legacy_offer_is_public_and_no_page_permission_is_denied` |
| 7 | 恢复墓碑保护。删除后从未提交的草稿排除在列表、数量、历史和写入口之外。 | `deleted_unsubmitted_draft_does_not_become_visible_to_other_sales` |
| 10 | 保留主动报价的稳定身份、无来源单和无待办。 | `http_direct_quote_creates_no_document_line_or_task_and_updates_same_identity` |
| 12 | 保留建档失败的事务回滚，去除领取前置。 | `binding_creation_without_assignment_remains_transactional` |
| 13、20 | 恢复建档/绑定双权限及失败零写入，去除领取、指派条件。 | `binding_requires_both_permissions` / `specs_do_not_expand_binding_permissions` |
| 14 | 售前菜单控制模板，有菜单的用户可下载，无菜单返回 403。 | `template_download_requires_page_but_not_sales_inquiry_action` |
| 16 | 从合并索引恢复生产热修的交期合同。摘要和详情能读旧字段，列表不读模板，读取不写回。 | `shared_reads_keep_legacy_delivery_without_loading_list_templates_or_writing_back` |
| 17 | 保留已关单报价的更新、撤回和版本保护。 | `direct_update_of_existing_quote_keeps_source_and_closed_state` |
| 19 | 基于私密受众拒绝通知已取消。旧通知目标的菜单权限另有回归。 | `revoked_presales_page_hides_existing_assignment_and_blocks_actions` |
| 21、22 | 恢复分页与构建详情的边界，历史私密已提交记录公开，草稿和已删除行仍被排除。 | `part_history_pages_multiple_inquiries_before_building_details` |
| 23、24 | 共享池公开历史发起人与更新人。改写旧待办摘要、事件和主动报价的身份读出。 | `sales_creator_and_event_labels_are_visible_to_pool_reader` / `legacy_proactive_quote_creator_is_visible_in_detail_and_history` |
| 25 | 沿用售价/成本与历史未声明税口径，仅改单价不覆盖。 | `workbook_price_basis_import_export_and_price_only_patch` |
| 26 | 按精确 PN 要求拆行，未建档可不报。已处理行数和有效报价数分别断言。 | `exact_requirement_splits_and_unknown_pn_can_skip` |
| 27、28 | 沿用复制为本行价格。拒绝跨 PN、已失效的报价，支持历史税口径。 | `reuse_copies_shared_price_and_historical_tax_to_each_source` / `reuse_rejects_wrong_pn_and_ineffective_offers` |
| 29、30、31 | 旧的另报、选已有报价与存草稿合同终止。共享表即时按行应用，保留行身份和失败回滚。 | `same_pn_merges_and_distributes_without_reply` / `late_source_failure_rolls_back_earlier_distribution` |
| 32 | 去除领取前置，保留来源行版本冲突。 | `another_assignees_line_can_export_but_stale_version_conflicts` |
| 33 | 同一 PN 多个来源只改单价时保留各自的字段、税口径及未选列。 | `price_only_patch_preserves_different_source_drafts_and_hidden_columns` |
| 34、38 | 回放以当前权限和首次回执为准，复核文件移交与原草稿删除情形。 | `deleted_own_draft_does_not_block_first_receipt_replay` |
| 35、36、37 | 原先因已发布记录私密而拒绝的条件已取消。未发布私人内容和当前列权限仍需防止通过转交文件读出。 | `forwarded_buyer_draft_cannot_leak_private_purchase_notes` |
| 40 | 删除引用时自动取消报价待办的旧断言。保留不能用失效引用的边界。 | `effective_reference_skips_dispatch_and_withdrawn_is_rejected` |
| 41、42 | 恢复撤回/无货拒绝成交，以及跨单移动报价的稳定关联。 | `close_rejects_linked_offer_that_is_no_longer_usable` / `original_inquiry_adoption_uses_stable_moved_offer_link` |
| 43、44 | 另报独立身份、更新已有价的目标保护及快照由原服务回归覆盖；移除受众隐藏断言。 | `explicit_repeat_update_keeps_identity_and_explicit_new_keeps_other_sources` |
| 45 | 恢复同一原行两个报价分别跨单移动的独立历史和各自最新价。 | `original_line_keeps_each_source_link_after_separate_cross_inquiry_updates` |
| 47、48、49 | 保留引用修订、备注修改的稳定性及每次移动的钉住版本。 | `notes_only_edit_keeps_reference_pin_after_legacy_scope_changes` / `each_previous_line_link_keeps_its_own_version_after_successive_moves` |
| 50、55 | 已发布记录不再分受众。沿用仍需现有可用报价及明确的修订。 | `reuse_rejects_wrong_pn_or_invalid_offer` |
| 51 | 恢复沿用时记录原报价人和时间。 | `reuse_of_legacy_private_offer_pins_original_reporter_and_time` |
| 52 | 恢复成交时不可变的价格、税口径及更新来源。 | `won_offer_snapshot_survives_update_and_record_scope_change` |
| 53、54 | 恢复报价库分组、排序、更多报价与去重。历史私密的已发布来源可以展示。 | `library_groups_latest_then_sorts_tax_buckets_and_filters_more` |
| 56 | 报价文件上传即生效。仍存在的旧草稿答复继续与他人隔离。 | `workbook_line_decisions_are_private_and_do_not_complete_work` |
| 59 | 恢复保存沿用后、整单回复前发生撤回时的事务保护。 | `saved_reuse_withdrawn_before_reply_keeps_all_decisions_in_draft` |
| 60、61 | 恢复汇总导出的成交钉价及只读 schema，下载的销售汇总不能当报价表导入。 | `summary_prices_keep_pinned_adoption_when_the_shared_price_changes` / `summary_schema_supports_column_picker_but_export_cannot_be_imported` |
| 62 | 去掉私密已发布报价的受众拒绝，保留预览的行列定位和错误零写入。 | `preview_collects_supplier_errors_for_every_row_and_rolls_back` |
| 63 | 去掉退组隐藏已提交记录的检查，保留撤销售前菜单权限后的拒绝。 | `revoked_presales_page_hides_existing_assignment_and_blocks_actions` |

## 原函数清单

路径以 `backend/` 为起点。编号只计函数行。

1. `tests/test_presales_api_visibility.py::test_field_visibility_matrix_removes_values_and_identity_bypasses`
2. `tests/test_presales_api_visibility.py::test_default_supplier_private_and_data_group_intersection_even_for_author`
3. `tests/test_presales_api_visibility.py::test_whole_inquiry_private_blocks_detail_reference_library_and_search_badges`
4. `tests/test_presales_api_visibility.py::test_group_membership_changes_take_effect_on_existing_offer`
5. `tests/test_presales_api_visibility.py::test_history_and_timeline_do_not_reveal_now_private_notes_or_reporter`
6. `tests/test_presales_api_visibility.py::test_public_pool_owner_sees_claimant_while_other_sales_cannot`
7. `tests/test_presales_api_visibility.py::test_deleted_unsubmitted_draft_does_not_become_visible_to_other_sales`
8. `tests/test_presales_audience_readback.py::test_http_rules_follow_each_field_author_without_exposing_hidden_values`
9. `tests/test_presales_audience_readback.py::test_revision_rule_changes_are_only_disclosed_to_offer_author_and_bypass`
10. `tests/test_presales_direct_answers.py::test_standalone_quote_preserves_audience_and_supplier_privacy`
11. `tests/test_presales_direct_answers.py::test_current_answer_never_leaks_new_private_price_or_reporter`
12. `tests/test_presales_inquiries.py::test_binding_creation_is_transactional_and_requires_claim`
13. `tests/test_presales_inquiries.py::test_binding_requires_both_permissions`
14. `tests/test_presales_inquiry_workbook.py::test_template_requires_sales_inquiry_permission`
15. `tests/test_presales_list_search.py::test_shared_inquiry_customer_is_null_and_never_a_search_side_channel`
16. `tests/test_presales_load_hotfix.py::test_list_summary_keeps_legacy_delivery_visibility_without_loading_templates`
17. `tests/test_presales_offers.py::test_other_actor_and_stale_version_rejected_but_owner_can_update_closed_quote`
18. `tests/test_presales_offers.py::test_private_inquiry_has_no_library_or_search_weight_side_channel`
19. `tests/test_presales_offers.py::test_proactive_notice_targets_must_be_allowed_to_view_published_inquiry`
20. `tests/test_presales_part_creation.py::test_specs_do_not_expand_binding_permissions`
21. `tests/test_presales_part_history.py::test_part_history_uses_visibility_before_pagination_and_never_leaks_private_rows`
22. `tests/test_presales_part_history.py::test_part_history_pages_multiple_inquiries_before_building_details`
23. `tests/test_presales_part_history.py::test_sales_creator_name_is_returned_for_assigned_buyer_and_work_summary`
24. `tests/test_presales_part_history.py::test_proactive_creator_name_does_not_bypass_hidden_reporter`
25. `tests/test_presales_quote_workbook.py::test_excel_update_cost_quote_preserves_unedited_basis_and_null_tax`
26. `tests/test_presales_quote_workbook.py::test_exact_requirement_splits_and_unknown_pn_can_skip`
27. `tests/test_presales_quote_workbook.py::test_reuse_pins_version_only_when_every_owner_can_view`
28. `tests/test_presales_quote_workbook.py::test_hidden_reuse_fails_and_rolls_back`
29. `tests/test_presales_quote_workbook.py::test_existing_quote_requires_explicit_choice_and_saves_for_reply`
30. `tests/test_presales_quote_workbook.py::test_batch_cannot_save_multiple_updates_to_same_target`
31. `tests/test_presales_quote_workbook.py::test_missing_existing_choice_fails_without_persisting_draft`
32. `tests/test_presales_quote_workbook.py::test_only_own_active_task_exports_and_source_version_conflicts`
33. `tests/test_presales_quote_workbook.py::test_price_only_patch_preserves_different_source_drafts_and_hidden_columns`
34. `tests/test_presales_quote_workbook.py::test_historical_scope_replay_rechecks_task_ownership`
35. `tests/test_presales_quote_workbook.py::test_deleted_error_plan_cannot_replay_newly_private_requirements`
36. `tests/test_presales_quote_workbook.py::test_deleted_error_plan_cannot_replay_newly_hidden_offer_summary`
37. `tests/test_presales_quote_workbook.py::test_forwarded_supervisor_export_cannot_leak_private_requirements`
38. `tests/test_presales_quote_workbook.py::test_deleted_own_draft_does_not_block_first_receipt_replay`
39. `tests/test_presales_r5_owner_visibility.py::test_owner_keeps_handler_identity_when_reporter_is_private`
40. `tests/test_presales_r5_tasks.py::test_adding_reference_cancels_quote_task_without_staling_offer`
41. `tests/test_presales_r6_close.py::test_close_rejects_linked_offer_that_is_no_longer_usable`
42. `tests/test_presales_r6_close.py::test_original_inquiry_adoption_uses_stable_moved_offer_link`
43. `tests/test_presales_r6_existing.py::test_explicit_new_keeps_restricted_quote_out_of_public_views`
44. `tests/test_presales_r6_existing.py::test_explicit_update_moves_authority_and_filters_original_public_views`
45. `tests/test_presales_r6_existing.py::test_original_line_keeps_each_source_link_after_separate_cross_inquiry_updates`
46. `tests/test_presales_r6_responses.py::test_reuse_rejects_invisible_offer_and_reference_list_uses_m8`
47. `tests/test_presales_r7_review.py::test_notes_only_edit_keeps_reference_pin_when_current_scope_is_narrower`
48. `tests/test_presales_r7_review.py::test_each_previous_line_link_keeps_its_own_version_after_successive_moves`
49. `tests/test_presales_r7_versions.py::test_reference_keeps_snapshot_scope_and_hides_latest_when_new_scope_denies_viewer`
50. `tests/test_presales_r7_versions.py::test_reuse_requires_owner_visibility_even_when_purchaser_can_see_private_quote`
51. `tests/test_presales_r7_versions.py::test_reuse_pins_original_reporter_time_and_masks_name_for_owner`
52. `tests/test_presales_r7_versions.py::test_won_offer_snapshot_survives_update_and_record_scope_change`
53. `tests/test_presales_r7_versions.py::test_library_groups_latest_then_sorts_tax_buckets_and_filters_more`
54. `tests/test_presales_r7_versions.py::test_more_items_keep_their_own_m8_field_rules_and_field_groups`
55. `tests/test_presales_r7_versions.py::test_reuse_owner_visibility_matches_library_after_source_record_rule_changes`
56. `tests/test_presales_t19_review.py::test_excel_draft_response_hidden_until_reply`
57. `tests/test_presales_visibility_batch.py::test_batch_and_single_views_share_field_matrix_without_cross_record_rules`
58. `tests/test_presales_visibility_batch.py::test_batch_record_access_and_field_groups_keep_the_strict_intersection`
59. `tests/test_presales_workbook_decisions.py::test_workbook_reuse_checks_owner_visibility_and_reply_validity`
60. `tests/test_presales_workbook_exports.py::test_summary_prices_use_pinned_adoption_and_hide_private_reporter`
61. `tests/test_presales_workbook_exports.py::test_summary_schema_supports_column_picker_but_export_cannot_be_imported`
62. `tests/test_presales_workbook_validation.py::test_invisible_reuse_is_located_before_apply`
63. `tests/test_presales_workflow_access.py::test_notice_group_membership_is_rechecked_before_total_and_pagination`
