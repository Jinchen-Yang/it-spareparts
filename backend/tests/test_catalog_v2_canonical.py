"""catalog-v2 §6/§8 的离线固定向量；期望摘要直接来自冻结契约。"""

from copy import deepcopy
from decimal import Decimal

import pytest

from app.services.catalog_v2 import canonical
from app.services.catalog_v2.canonical import canonical_bytes, node_hash


GOLDEN_NODE = {
    "part_id": 1,
    "pn_std": "DEMO-PN-A",
    "status": "active",
    "merged_into_id": None,
    "canonical_part_id": 1,
    "needs_review": True,
    "is_excluded": True,
    "machine_or_part": "备件",
    "description": "合成硬盘",
    "brand": "Demo",
    "category_major": "硬盘",
    "category_minor": "SSD",
    "unit": "块",
    "locked_fields": ["brand"],
    "specs": [{"key": "capacity", "value": "960 GB", "unit": "GB", "numeric_value": "960", "source": "auto"}],
    "aggregate_revision": 7,
    "identity_revision": 2,
    "content_hash": "cd61df703e596d1825651dbb30f2d24b8e2e5b53632d30462e51988cdb3081a7",
}


def test_section_8_1_node_has_literal_hash_and_435_utf8_bytes():
    assert node_hash(GOLDEN_NODE) == "cd61df703e596d1825651dbb30f2d24b8e2e5b53632d30462e51988cdb3081a7"
    raw = deepcopy(GOLDEN_NODE)
    raw.pop("content_hash")
    assert len(canonical_bytes(raw)) == 435


@pytest.mark.parametrize(("field", "unsorted", "expected"), [
    ("specs", [{"key": "speed"}, {"key": "capacity"}], [{"key": "capacity"}, {"key": "speed"}]),
    ("locked_fields", ["unit", "brand", "brand"], ["brand", "unit"]),
    ("nodes", [{"part_id": 10}, {"part_id": 2}], [{"part_id": 2}, {"part_id": 10}]),
    ("aliases", [{"alias_id": 10}, {"alias_id": 2}], [{"alias_id": 2}, {"alias_id": 10}]),
    ("items", [{"kind": "alias", "alias": {"alias_id": 1}}, {"kind": "node", "node": {"part_id": 10}}, {"kind": "node", "node": {"part_id": 2}}], [{"kind": "node", "node": {"part_id": 2}}, {"kind": "node", "node": {"part_id": 10}}, {"kind": "alias", "alias": {"alias_id": 1}}]),
    ("transaction_groups", ["z", "a"], ["a", "z"]),
    ("alias_moves", [{"alias_id": 10}, {"alias_id": 2}], [{"alias_id": 2}, {"alias_id": 10}]),
    ("selected_fields", ["unit", "brand"], ["brand", "unit"]),
    ("field_baselines", [{"field": "unit"}, {"field": "brand"}], [{"field": "brand"}, {"field": "unit"}]),
    ("operations", [{"field": "specs", "key": "speed"}, {"field": "unit"}, {"field": "specs", "key": "capacity"}], [{"field": "specs", "key": "capacity"}, {"field": "specs", "key": "speed"}, {"field": "unit"}]),
    ("evidence_refs", ["z", "a"], ["a", "z"]),
])
def test_semantic_array_order_is_canonical(field, unsorted, expected):
    before = deepcopy(unsorted)
    assert canonical.normalize({field: unsorted}) == {field: expected}
    assert canonical_bytes({field: unsorted}) == canonical_bytes({field: expected})
    assert unsorted == before


@pytest.mark.parametrize(("value", "expected"), [
    ("1.500", "1.5"), ("-0.0", "0"), ("+001.500", "1.5"),
    ("1e3", "1000"), ("1.5e-3", "0.0015"), (Decimal("123.4500"), "123.45"),
    ("123456789012345678901234567890.123450000", "123456789012345678901234567890.12345"),
])
def test_decimal_is_exact_and_does_not_depend_on_decimal_context(value, expected):
    assert canonical.normalize_decimal(value) == expected


def test_section_8_6_numeric_strings_normalize_but_display_text_does_not():
    assert canonical.canonical_hash({"numeric_value": "1.500"}) == canonical.canonical_hash({"numeric_value": "1.5"})
    assert canonical.canonical_hash({"value": "1.500 TB"}) != canonical.canonical_hash({"value": "1.5 TB"})
    assert canonical.canonical_hash({"value": None}) != canonical.canonical_hash({})
    assert canonical.canonical_hash({"deleted": False}) != canonical.canonical_hash({"deleted": True})


def test_exact_unicode_scalar_sorting_and_escaping():
    assert canonical_bytes({"𐀀": "/é", "\ue000": "\"\\\b\t\n\f\r\x00\x1f"}) == (
        '{"\ue000":"\\\"\\\\\\b\\t\\n\\f\\r\\u0000\\u001f","𐀀":"/é"}'
    ).encode("utf-8")
    assert canonical_bytes("é") != canonical_bytes("e\u0301")


@pytest.mark.parametrize("value", [1.0, float("nan"), float("inf"), Decimal("NaN"), "\ud800", {"\udfff": 1}, {1: "bad"}])
def test_non_json_values_and_unpaired_surrogates_are_rejected(value):
    with pytest.raises(canonical.CanonicalError):
        canonical_bytes(value)


@pytest.mark.parametrize("raw", [
    '{"value":1,"value":2}', '{"a":{"x":1,"x":2}}',
    '{"value":1.0}', '{"value":1e3}', 'NaN', 'Infinity', '-Infinity',
    '"\\ud800"', '"\\udfff"', '{"\\ud800":1}', b'"\xff"',
])
def test_strict_json_parser_rejects_duplicate_keys_floats_and_invalid_unicode(raw):
    with pytest.raises(canonical.CanonicalError):
        canonical.strict_loads(raw)


def test_strict_json_parser_accepts_paired_surrogate_escape_as_scalar():
    assert canonical.strict_loads('"\\ud800\\udc00"') == "𐀀"


def test_strict_parser_leaves_schema_validation_and_decimal_expansion_to_caller():
    # A request with unknown fields is rejected by its DTO. Parsing it must not
    # expand an untrusted decimal exponent before that validation can happen.
    raw = '{"numeric_value":"1e999999999999999999999999","items":"not-an-array"}'
    assert canonical.strict_loads(raw) == {
        "numeric_value": "1e999999999999999999999999", "items": "not-an-array",
    }


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1_500", " 1.5", "", 1.5, True])
def test_invalid_decimal_inputs_are_rejected(value):
    with pytest.raises(canonical.CanonicalError):
        canonical.normalize_decimal(value)


@pytest.mark.parametrize(("field", "item"), [
    ("specs", {"key": "capacity"}), ("nodes", {"part_id": 1}),
    ("aliases", {"alias_id": 1}), ("items", {"kind": "node", "node": {"part_id": 1}}),
    ("alias_moves", {"alias_id": 1}), ("selected_fields", "brand"),
    ("field_baselines", {"field": "brand"}), ("operations", {"field": "specs", "key": "capacity"}),
])
def test_duplicate_targets_are_not_hidden_by_sorting(field, item):
    with pytest.raises(canonical.CanonicalError):
        canonical_bytes({field: [item, deepcopy(item)]})


# §8 独立字面夹具，从冻结规范复制；测试运行不读取文档或生成期望摘要。
GOLDEN_MERGE_BATCH = {'schema_version': 2,
 'source_namespace': 'itsp',
 'publication_epoch': '00000000-0000-4000-8000-000000000004',
 'batch_id': '00000000-0000-4000-8000-000000000020',
 'published_sequence': 1,
 'previous_sequence': 0,
 'transaction_groups': ['demo-merge-1'],
 'items': [{'kind': 'node',
            'node': {'part_id': 1,
                     'pn_std': 'DEMO-PN-A',
                     'status': 'merged',
                     'merged_into_id': 2,
                     'canonical_part_id': 2,
                     'needs_review': True,
                     'is_excluded': True,
                     'machine_or_part': '备件',
                     'description': '合成硬盘',
                     'brand': 'Demo',
                     'category_major': '硬盘',
                     'category_minor': 'SSD',
                     'unit': '块',
                     'locked_fields': ['brand'],
                     'specs': [{'key': 'capacity',
                                'value': '960 GB',
                                'unit': 'GB',
                                'numeric_value': '960',
                                'source': 'auto'}],
                     'aggregate_revision': 8,
                     'identity_revision': 3,
                     'content_hash': '141c272be2d7cc27ffccd6e51e365e10e07a4317a0e8929564573cb920c43d5e'}},
           {'kind': 'node',
            'node': {'part_id': 2,
                     'pn_std': 'DEMO-PN-B',
                     'status': 'active',
                     'merged_into_id': None,
                     'canonical_part_id': 2,
                     'needs_review': False,
                     'is_excluded': False,
                     'machine_or_part': '备件',
                     'description': '合成硬盘',
                     'brand': 'Demo',
                     'category_major': '硬盘',
                     'category_minor': 'SSD',
                     'unit': '块',
                     'locked_fields': ['brand'],
                     'specs': [{'key': 'capacity',
                                'value': '960 GB',
                                'unit': 'GB',
                                'numeric_value': '960',
                                'source': 'auto'}],
                     'aggregate_revision': 4,
                     'identity_revision': 2,
                     'content_hash': '954d1c04028c6a3f7384b4cfe10a09819cb80f307a742898fbed6d9973f85ca0'}},
           {'kind': 'alias',
            'alias': {'alias_id': 11,
                      'pn_raw': 'DEMO-OLD',
                      'part_id': 2,
                      'pn_std': 'DEMO-PN-B',
                      'status': 'active',
                      'deleted': False}},
           {'kind': 'alias',
            'alias': {'alias_id': 15,
                      'pn_raw': 'DEMO-DELETED',
                      'part_id': 1,
                      'pn_std': 'DEMO-PN-A',
                      'status': 'active',
                      'deleted': True}}],
 'alias_moves': [{'alias_id': 11, 'old_part_id': 1, 'new_part_id': 2}],
 'batch_hash': '2e98be24ae1f13517c406a12cdd2b522bd54214b8c3e0caca75c218b2a75dd1c',
 'request_id': '00000000-0000-4000-8000-000000000021',
 'correlation_id': '00000000-0000-4000-8000-000000000002',
 'generated_at': '2026-10-15T08:00:45Z',
 'complete': True,
 'next_cursor': None}

GOLDEN_UNMERGE_BATCH = {'schema_version': 2,
 'source_namespace': 'itsp',
 'publication_epoch': '00000000-0000-4000-8000-000000000004',
 'batch_id': '00000000-0000-4000-8000-000000000022',
 'published_sequence': 2,
 'previous_sequence': 1,
 'transaction_groups': ['demo-unmerge-1'],
 'items': [{'kind': 'node',
            'node': {'part_id': 1,
                     'pn_std': 'DEMO-PN-A',
                     'status': 'active',
                     'merged_into_id': None,
                     'canonical_part_id': 1,
                     'needs_review': True,
                     'is_excluded': True,
                     'machine_or_part': '备件',
                     'description': '合成硬盘',
                     'brand': 'Demo',
                     'category_major': '硬盘',
                     'category_minor': 'SSD',
                     'unit': '块',
                     'locked_fields': ['brand'],
                     'specs': [{'key': 'capacity',
                                'value': '960 GB',
                                'unit': 'GB',
                                'numeric_value': '960',
                                'source': 'auto'}],
                     'aggregate_revision': 9,
                     'identity_revision': 4,
                     'content_hash': '0bc6d71cc8d9c8be12799231a598320aaa1dad5361a0db50d5b3e0d1080c0193'}},
           {'kind': 'node',
            'node': {'part_id': 2,
                     'pn_std': 'DEMO-PN-B',
                     'status': 'active',
                     'merged_into_id': None,
                     'canonical_part_id': 2,
                     'needs_review': False,
                     'is_excluded': False,
                     'machine_or_part': '备件',
                     'description': '合成硬盘',
                     'brand': 'Demo',
                     'category_major': '硬盘',
                     'category_minor': 'SSD',
                     'unit': '块',
                     'locked_fields': ['brand'],
                     'specs': [{'key': 'capacity',
                                'value': '960 GB',
                                'unit': 'GB',
                                'numeric_value': '960',
                                'source': 'auto'}],
                     'aggregate_revision': 5,
                     'identity_revision': 3,
                     'content_hash': 'c556a43a1f5b6aa9d5d9af601ddf155e7ca31739aafa88d2f3084cccd39b4387'}},
           {'kind': 'alias',
            'alias': {'alias_id': 11,
                      'pn_raw': 'DEMO-OLD',
                      'part_id': 1,
                      'pn_std': 'DEMO-PN-A',
                      'status': 'active',
                      'deleted': False}}],
 'alias_moves': [{'alias_id': 11, 'old_part_id': 2, 'new_part_id': 1}],
 'batch_hash': 'ad1b352698c46b8dc49faa37c1baa1a69f01db2268ae7e8497f9e3504f272308',
 'request_id': '00000000-0000-4000-8000-000000000023',
 'correlation_id': '00000000-0000-4000-8000-000000000002',
 'generated_at': '2026-10-15T08:00:50Z',
 'complete': True,
 'next_cursor': None}

GOLDEN_COMMAND = {'schema_version': 2,
 'command_type': 'patch',
 'request_id': '00000000-0000-4000-8000-000000000010',
 'correlation_id': '00000000-0000-4000-8000-000000000002',
 'actor_mapping_id': 'demo-mapping-1',
 'source_namespace': 'shop',
 'source_pn_id': '00000000-0000-4000-8000-000000000011',
 'part_id': 1,
 'base_revision': 7,
 'identity_revision': 2,
 'draft_revision': 3,
 'selected_fields': ['brand', 'specs.capacity'],
 'field_baselines': [{'field': 'brand', 'present': True, 'value': 'Demo'},
                     {'field': 'specs.capacity',
                      'present': True,
                      'value': {'key': 'capacity',
                                'value': '960 GB',
                                'unit': 'GB',
                                'numeric_value': '960',
                                'source': 'auto'}}],
 'operations': [{'field': 'brand', 'op': 'clear'}, {'field': 'specs', 'key': 'capacity', 'op': 'remove'}],
 'evidence_refs': [],
 'body_hash': 'd072566a3631835bda58226faf6b9955bf79db88354cb4b5607fb66116000dc4'}

GOLDEN_FRAGMENTS = [{'transfer_mode': 'base64_fragments',
  'publication_epoch': '00000000-0000-4000-8000-000000000004',
  'snapshot_id': '00000000-0000-4000-8000-000000000003',
  'payload_id': '00000000-0000-4000-8000-000000000030',
  'part_no': 1,
  'part_count': 2,
  'part_hash': 'a4c2cd7eba9219afd27d2792d0d626a5bd5d82d7f8072ec4bf884c3ddbf8fb8d',
  'payload_hash': 'b872723b239f8a68f7675839cd5644299408e58e2ba46a69e2257f10633df321',
  'payload_fragment': 'eyJhbGlhc19jb3VudCI6MCwiY29tcGxldGUiOnRydWUsImNvbnRlbnRfaGFzaCI6ImNjNTA0Mzk0ZWFiZGVhODQ0ZWM5YWE1YzY5NjgyNWRmZmQwY2YwODdlMDcxZjdiN2MzNDNkYzA2YmE3YTEwY2QiLCJjb3JyZWxhdGlvbl9pZCI6IjAwMDAwMDAwLTAwMDAtNDAwMC04MDAwLTAwMDAwMDAwMDAwMiIsImdlbmVyYXRlZF9hdCI6IjIwMjYtMTAtMTVUMDg=',
  'complete': False,
  'next_cursor': 'demo-fragment-cursor-2',
  'schema_version': 2,
  'source_namespace': 'itsp',
  'request_id': '00000000-0000-4000-8000-000000000031',
  'correlation_id': '00000000-0000-4000-8000-000000000002',
  'generated_at': '2026-10-15T08:00:00Z'},
 {'transfer_mode': 'base64_fragments',
  'publication_epoch': '00000000-0000-4000-8000-000000000004',
  'snapshot_id': '00000000-0000-4000-8000-000000000003',
  'payload_id': '00000000-0000-4000-8000-000000000030',
  'part_no': 2,
  'part_count': 2,
  'part_hash': '71e97bcb368f6f2e998681a780895406805c1a15921a12df375d42334aa1f1f5',
  'payload_hash': 'b872723b239f8a68f7675839cd5644299408e58e2ba46a69e2257f10633df321',
  'payload_fragment': 'OjAwOjAwWiIsIml0ZW1zIjpbXSwibmV4dF9jdXJzb3IiOm51bGwsIm5vZGVfY291bnQiOjAsInB1YmxpY2F0aW9uX2Vwb2NoIjoiMDAwMDAwMDAtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDAwMDA0IiwicmVxdWVzdF9pZCI6IjAwMDAwMDAwLTAwMDAtNDAwMC04MDAwLTAwMDAwMDAwMDAwMSIsInNjaGVtYV92ZXJzaW9uIjoyLCJzbmFwc2hvdF9pZCI6IjAwMDAwMDAwLTAwMDAtNDAwMC04MDAwLTAwMDAwMDAwMDAwMyIsInNvdXJjZV9uYW1lc3BhY2UiOiJpdHNwIiwid2F0ZXJtYXJrIjowfQ==',
  'complete': True,
  'next_cursor': None,
  'schema_version': 2,
  'source_namespace': 'itsp',
  'request_id': '00000000-0000-4000-8000-000000000032',
  'correlation_id': '00000000-0000-4000-8000-000000000002',
  'generated_at': '2026-10-15T08:00:00Z'}]


def test_section_8_2_and_8_6_empty_snapshot_exact_bytes_and_literal_hash():
    assert canonical.snapshot_hash([], []) == "cc504394eabdea844ec9aa5c696825dffd0cf087e071f7b7c343dc06ba7a10cd"
    assert canonical_bytes({"schema_version": 2, "source_namespace": "itsp", "nodes": [], "aliases": []}) == b'{"aliases":[],"nodes":[],"schema_version":2,"source_namespace":"itsp"}'


@pytest.mark.parametrize(("batch", "expected", "node_hashes"), [
    (GOLDEN_MERGE_BATCH, "2e98be24ae1f13517c406a12cdd2b522bd54214b8c3e0caca75c218b2a75dd1c", [
        "141c272be2d7cc27ffccd6e51e365e10e07a4317a0e8929564573cb920c43d5e",
        "954d1c04028c6a3f7384b4cfe10a09819cb80f307a742898fbed6d9973f85ca0",
    ]),
    (GOLDEN_UNMERGE_BATCH, "ad1b352698c46b8dc49faa37c1baa1a69f01db2268ae7e8497f9e3504f272308", [
        "0bc6d71cc8d9c8be12799231a598320aaa1dad5361a0db50d5b3e0d1080c0193",
        "c556a43a1f5b6aa9d5d9af601ddf155e7ca31739aafa88d2f3084cccd39b4387",
    ]),
])
def test_section_8_3_merge_and_unmerge_two_batch_and_four_node_literal_hashes(batch, expected, node_hashes):
    assert canonical.batch_hash(batch) == expected
    assert [node_hash(item["node"]) for item in batch["items"] if item["kind"] == "node"] == node_hashes
    reordered = deepcopy(batch)
    reordered["items"].reverse()
    reordered["request_id"] = "different-transport-request"
    reordered["generated_at"] = "different-transport-time"
    reordered["extensions"] = {"future_optional_transport_field": True}
    assert canonical.batch_hash(reordered) == expected


def test_section_8_4_command_body_hash_is_literal_and_includes_business_request_id():
    assert canonical.body_hash(GOLDEN_COMMAND) == "d072566a3631835bda58226faf6b9955bf79db88354cb4b5607fb66116000dc4"
    changed = deepcopy(GOLDEN_COMMAND)
    changed["request_id"] = "00000000-0000-4000-8000-000000000099"
    assert canonical.body_hash(changed) != "d072566a3631835bda58226faf6b9955bf79db88354cb4b5607fb66116000dc4"


def test_section_8_5_fragment_raw_byte_hashes_and_reassembled_canonical_page():
    import base64

    parts = [base64.b64decode(item["payload_fragment"], validate=True) for item in GOLDEN_FRAGMENTS]
    assert [canonical.bytes_hash(part) for part in parts] == [
        "a4c2cd7eba9219afd27d2792d0d626a5bd5d82d7f8072ec4bf884c3ddbf8fb8d",
        "71e97bcb368f6f2e998681a780895406805c1a15921a12df375d42334aa1f1f5",
    ]
    payload = b"".join(parts)
    assert canonical.bytes_hash(payload) == "b872723b239f8a68f7675839cd5644299408e58e2ba46a69e2257f10633df321"
    page = canonical.strict_loads(payload)
    assert canonical_bytes(page) == payload
    assert page["items"] == []
    assert page["complete"] is True
    assert page["next_cursor"] is None
    assert page["watermark"] == 0


def test_snapshot_validates_node_hash_and_excludes_deleted_aliases():
    deleted = {"alias_id": 15, "pn_raw": "DEMO-DELETED", "part_id": 1, "pn_std": "DEMO-PN-A", "status": "active", "deleted": True}
    assert canonical.snapshot_hash([GOLDEN_NODE], [deleted]) == canonical.snapshot_hash([GOLDEN_NODE], [])
    damaged = deepcopy(GOLDEN_NODE)
    damaged["description"] = None
    with pytest.raises(canonical.CanonicalError):
        canonical.snapshot_hash([damaged], [])


def test_segment_hash_uses_id_range_and_keeps_empty_segment_arrays():
    active = {"alias_id": 11, "pn_raw": "DEMO-OLD", "part_id": 1, "pn_std": "DEMO-PN-A", "status": "active", "deleted": False}
    assert canonical.segment_hash([GOLDEN_NODE], [active], 1, 1001) == canonical.snapshot_hash([GOLDEN_NODE], [active])
    assert canonical.segment_hash([GOLDEN_NODE], [active], 1001, 2001) == "cc504394eabdea844ec9aa5c696825dffd0cf087e071f7b7c343dc06ba7a10cd"
    assert canonical.segment_hash([GOLDEN_NODE], [active], 2, 1001) == "cc504394eabdea844ec9aa5c696825dffd0cf087e071f7b7c343dc06ba7a10cd"


@pytest.mark.parametrize(("field", "value"), [("specs", []), ("description", None), ("specs", [dict(GOLDEN_NODE["specs"][0], source="manual")])])
def test_empty_specs_null_description_and_manual_source_change_node_hash(field, value):
    changed = dict(GOLDEN_NODE, **{field: value})
    assert node_hash(changed) != "cd61df703e596d1825651dbb30f2d24b8e2e5b53632d30462e51988cdb3081a7"
