"""短词检索：汉字保留子串召回，ASCII 有意收窄为 token 前缀。"""

import re

import pytest
from sqlalchemy import text

from app.models.dimensions import DimPart, PartAlias
from app.services.part_search import search
from app.services.part_search_normalize import normalize


def _part(db, pn, description):
    part = DimPart(pn_std=pn, description=description)
    db.add(part)
    db.flush()
    return part


@pytest.fixture()
def short_catalog(db):
    return [
        _part(db, "Q9000", "机械硬盘 SAS M1-12345"),
        _part(db, "Q9001", "服务器内存 HPE 1TB"),
        _part(db, "Q9002", "固态硬盘 ABS XM1 HPX A1TB"),
        _part(db, "Q9003", "硬件 盘片 ＳＡＴＡ Ｍ２ １２ＧＢ"),
        _part(db, "Q9004", "存储 闪存 É A硬 㐀 𠀀𠀁 𰀀"),
    ]


def _ids(db, query, **kwargs):
    return {item["part_id"] for item in search(db, query, limit=30, **kwargs)}


@pytest.mark.parametrize("query", ["硬", "盘", "硬盘", "内存", "存", "㐀", "𠀀", "𠀀𠀁", "𰀀"])
def test_short_cjk_recall_equals_original_ilike_for_every_row(db, short_catalog, query):
    expected = set(db.scalars(text(
        "SELECT part_id FROM part_search_text WHERE doc ILIKE :pattern"
    ), {"pattern": "%" + query + "%"}))
    actual = _ids(db, query)
    for part in short_catalog:
        assert (part.id in actual) == (part.id in expected), (query, part.pn_std)


@pytest.mark.parametrize("query, expected_indexes, removed_indexes", [
    ("S", {0, 3}, {2}),
    ("M", {0, 3}, {2}),
    ("M1", {0}, {2}),
    ("HP", {1, 2}, set()),
    ("1T", {1}, {2}),
    ("12", {0, 3}, set()),
])
def test_short_ascii_prefix_difference_from_ilike_is_explicit(
    db, short_catalog, query, expected_indexes, removed_indexes,
):
    old_ids = set(db.scalars(text(
        "SELECT part_id FROM part_search_text WHERE doc ILIKE :pattern"
    ), {"pattern": "%" + query + "%"}))
    actual = _ids(db, query)
    expected = {short_catalog[i].id for i in expected_indexes}
    removed = {short_catalog[i].id for i in removed_indexes}
    assert actual == expected
    assert old_ids - actual == removed
    assert actual <= old_ids
    docs = db.execute(text("SELECT part_id, doc FROM part_search_text")).all()
    for part_id, doc in docs:
        token_prefix_match = any(token.startswith(query) for token in re.findall("[A-Z0-9]+", doc))
        assert (part_id in actual) == token_prefix_match


@pytest.mark.parametrize("query, equivalent", [("ｓ", "S"), ("ｈｐ", "HP"), ("１ｔ", "1T")])
def test_fullwidth_short_queries_share_normalization(db, short_catalog, query, equivalent):
    assert _ids(db, query) == _ids(db, equivalent)
    assert all(item["matched_terms"] == [normalize(equivalent)] for item in search(db, query))


def test_short_ascii_does_not_match_across_token_boundaries(db):
    _part(db, "Q9100", "A-B")
    target = _part(db, "Q9101", "ABCD")
    assert _ids(db, "AB") == {target.id}


@pytest.mark.parametrize("query", ["*S", "?S", "%", "_", "\\", "É", "A硬"])
def test_short_terms_without_index_keys_keep_previous_literal_and_wildcard_path(db, query):
    from app.services.part_search import _pattern

    _part(db, "Q9200", "ABS % _ \\ É A硬")
    _part(db, "Q9201", "SATA 普通资料")
    pattern = "%" + _pattern(normalize(query)) + "%"
    compact_pattern = _pattern(normalize(query), compacted=True)
    sql = "SELECT part_id FROM part_search_text WHERE doc LIKE :pattern ESCAPE E'\\\\'"
    params = {"pattern": pattern}
    if compact_pattern:
        sql += " OR doc_compact LIKE :compact_pattern ESCAPE E'\\\\'"
        params["compact_pattern"] = "%" + compact_pattern + "%"
    assert _ids(db, query) == set(db.scalars(text(sql), params))


def test_multi_term_query_keeps_short_prefix_requirement(db, short_catalog):
    assert _ids(db, "S 硬盘") == {short_catalog[0].id}
    assert _ids(db, "S 机械*") == {short_catalog[0].id}


@pytest.mark.parametrize("query", ["S", "HP", "硬盘"])
@pytest.mark.parametrize("alias", [False, True])
def test_short_exact_identity_precedes_field_matches_and_survives_cap(db, monkeypatch, query, alias):
    from app.services.part_search import get_settings

    monkeypatch.setattr(get_settings(), "part_search_candidate_limit", 1)
    for i in range(12):
        _part(db, f"A930{i:02}", query)
    target = _part(db, "Z9400" if alias else query, "精确目标")
    if alias:
        db.add(PartAlias(pn_raw=query, pn_std=target.pn_std, part_id=target.id, status="active"))
        db.flush()
    items = search(db, query)
    assert items[0]["part_id"] == target.id
    assert items[0]["match_type"] == ("exact_alias" if alias else "exact_pn")


def test_short_recall_applies_filters(db, short_catalog):
    assert _ids(db, "硬盘", filters={"part_ids": [short_catalog[0].id]}) == {short_catalog[0].id}
    assert _ids(db, "S", filters={"part_ids": [short_catalog[2].id]}) == set()


@pytest.mark.parametrize("query", ["HP", "硬盘"])
def test_short_tier_lanes_keep_pn_and_boost_outside_document_cap(db, monkeypatch, query):
    from app.services.part_search import get_settings

    monkeypatch.setattr(get_settings(), "part_search_candidate_limit", 1)
    field = _part(db, "A-FIELD", query)
    boosted = _part(db, "B-BOOST", query)
    substring = _part(db, f"X-{query}-SUBSTRING", "")
    prefix = _part(db, f"{query}-PREFIX", "")
    exact = _part(db, query, "")
    items = search(db, query, limit=5, filters={"boost_part_ids": {boosted.id}})
    assert [item["part_id"] for item in items] == [exact.id, prefix.id, substring.id, boosted.id, field.id]
    assert [item["match_type"] for item in items] == ["exact_pn", "prefix", "substring", "boost", "field"]


def test_short_unicode_pn_substring_keeps_original_ilike_locale_semantics(db, monkeypatch):
    from app.services.part_search import get_settings

    monkeypatch.setattr(get_settings(), "part_search_candidate_limit", 1)
    _part(db, "A-DOC", "INTEL")
    target = _part(db, "Xİ", "INTEL")
    expected = db.scalar(text(
        "SELECT part_search_normalize(:pn) ILIKE '%I%'"
    ), {"pn": target.pn_std})
    items = search(db, "I", limit=30)
    actual = {item["part_id"]: item for item in items}
    assert (target.id in actual) == expected
    if expected:
        assert actual[target.id]["match_type"] == "substring"
