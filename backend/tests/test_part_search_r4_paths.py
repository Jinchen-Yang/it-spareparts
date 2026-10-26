"""r4 路径契约：短词按词路由，搜索不污染调用方的规划设置。"""

import re

import pytest
from sqlalchemy import text

from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import ProductSpec
from app.services import part_search


def _part(db, pn, **fields):
    part = DimPart(pn_std=pn, **fields)
    db.add(part)
    db.flush()
    return part


def _planner_settings(db):
    return dict(db.execute(text(
        "SELECT current_setting('plan_cache_mode') AS plan_cache_mode, "
        "current_setting('max_parallel_workers_per_gather') AS parallel_workers, "
        "current_setting('jit') AS jit"
    )).mappings().one())


def _set_caller_planner_settings(db):
    db.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
    db.execute(text("SET LOCAL max_parallel_workers_per_gather = 3"))
    db.execute(text("SET LOCAL jit = on"))
    return {"plan_cache_mode": "force_generic_plan", "parallel_workers": "3", "jit": "off"}


@pytest.fixture()
def planner_part(db):
    part = _part(
        db, "R4-PLAN-12345", brand="AuroraBrand",
        description="机械硬盘 1TB",
    )
    db.add(PartAlias(
        pn_raw="R4-LEGACY-12345", pn_std=part.pn_std, part_id=part.id, status="active",
    ))
    db.add(ProductSpec(part_id=part.id, spec_key="capacity", spec_value="1200GB"))
    db.flush()
    return part


@pytest.mark.parametrize("query", [
    "R4-PLAN-12345", "R4PLAN12345", "R4-LEGACY-12345", "AuroraBrand",
    "机械硬盘", "1200GB", "R4*12345", "R4-PLNA-12345",
])
def test_long_wildcard_and_typo_paths_preserve_caller_planning_during_queries(
    db, planner_part, monkeypatch, query,
):
    expected = _set_caller_planner_settings(db)
    original_execute = db.execute
    observed = []
    projection_sql = []

    def execute(statement, *args, **kwargs):
        sql = str(statement)
        if "FROM part_search_text" in sql or "JOIN part_search_text" in sql:
            observed.append(_planner_settings(db))
            projection_sql.append(sql)
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", execute)
    items = part_search.search(db, query)
    assert planner_part.id in {item["part_id"] for item in items}
    assert observed
    assert all(settings == expected for settings in observed)
    assert _planner_settings(db) == expected
    assert all("part_search_short_key" not in sql for sql in projection_sql)


@pytest.mark.parametrize("short_query", ["1T", "硬盘"])
def test_short_then_long_and_mixed_queries_do_not_leak_planner_settings(
    db, planner_part, monkeypatch, short_query,
):
    expected = _set_caller_planner_settings(db)
    original_execute = db.execute
    projection_sql = []

    def execute(statement, *args, **kwargs):
        sql = str(statement)
        if "FROM part_search_text" in sql or "JOIN part_search_text" in sql:
            projection_sql.append(sql)
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", execute)
    for query in (
        short_query, "AuroraBrand", f"{short_query} AuroraBrand",
        f"AuroraBrand {short_query}", "AuroraBrand",
    ):
        projection_sql.clear()
        items = part_search.search(db, query)
        assert planner_part.id in {item["part_id"] for item in items}, query
        assert _planner_settings(db) == expected, query
        assert projection_sql, query
        assert any("part_search_short_key" in sql for sql in projection_sql) == (
            query != "AuroraBrand"
        ), query


@pytest.mark.parametrize("long_term", ["ALPHABET", "机械硬盘", "1200GB"])
def test_neighboring_short_term_does_not_change_long_term_sql_or_bindings(
    db, monkeypatch, long_term,
):
    _part(db, "R4-TERM-TARGET", description="1TB XALPH-ABETY 机械硬盘 1200GB")
    original_condition = part_search._term_condition
    captured = []

    def term_condition(term, index, params):
        sql = original_condition(term, index, params)
        if term == long_term:
            bindings = {key: params[key] for key in re.findall(r":([a-z]+\d+)", sql)}
            # 只抹去词的位置编号，SQL 结构和绑定值必须完全相同。
            canonical_sql = re.sub(r":([a-z]+)\d+", r":\g<1>0", sql)
            canonical_bindings = {re.sub(r"\d+$", "0", key): value
                                  for key, value in bindings.items()}
            captured.append((canonical_sql, canonical_bindings))
        return sql

    monkeypatch.setattr(part_search, "_term_condition", term_condition)
    for query in (long_term, f"{long_term} 1T", f"1T {long_term}"):
        part_search.search(db, query)
    assert len(captured) == 3
    assert captured[0] == captured[1] == captured[2]
    assert "short_keys" not in captured[0][0]
    assert "part_search_short_key" not in captured[0][0]
    expected_bindings = {"t0": f"%{long_term}%"}
    if long_term != "机械硬盘":
        expected_bindings["c0"] = f"%{long_term}%"
    assert captured[0][1] == expected_bindings


@pytest.mark.parametrize("query", ["1T ALPHABET", "ALPHABET 1T"])
def test_mixed_query_keeps_short_prefix_and_long_compact_substring_recall(db, query):
    target = _part(db, "R4-MIXED-TARGET", description="1TB XALPH-ABETY")
    wrong_short = _part(db, "R4-MIXED-MIDDLE", description="A1TB XALPH-ABETY")
    _part(db, "R4-MIXED-OTHER", description="1TB 完全无关资料")
    items = part_search.search(db, query, limit=30)
    assert {item["part_id"] for item in items if item["match_type"] != "fuzzy"} == {target.id}
    assert wrong_short.id not in {item["part_id"] for item in items}


def test_short_ascii_digit_uses_token_prefix_keys_without_crossing_boundaries(db):
    target = _part(db, "R4-CAPACITY-TARGET", description="1TB")
    _part(db, "R4-CAPACITY-MIDDLE", description="A1TB")
    _part(db, "R4-CAPACITY-SEPARATED", description="1-TB")
    keyed_ids = set(db.scalars(text(
        "SELECT part_id FROM part_search_short_key WHERE keys @> ARRAY['1T']::text[]"
    )))
    assert keyed_ids == {target.id}
    assert {item["part_id"] for item in part_search.search(db, "1T", limit=30)} == keyed_ids


@pytest.mark.parametrize("alias", [False, True])
def test_short_ascii_digit_identity_and_tiers_survive_document_candidate_cap(
    db, monkeypatch, alias,
):
    monkeypatch.setattr(part_search.get_settings(), "part_search_candidate_limit", 1)
    # 资料候选按 PN 截断；让资料行先入选，随后验证独立车道和最终分档排序。
    field = _part(db, "000-R4-FIELD", description="1TB")
    boosted = _part(db, "001-R4-BOOST", description="1TB")
    substring = _part(db, "X-1T-R4-SUBSTRING")
    prefix = _part(db, "1T-R4-PREFIX")
    exact = _part(db, "Z-R4-IDENTITY" if alias else "1T")
    if alias:
        db.add(PartAlias(pn_raw="1T", pn_std=exact.pn_std, part_id=exact.id, status="active"))
        db.flush()
    items = part_search.search(db, "1T", limit=5, filters={"boost_part_ids": {boosted.id}})
    assert [item["part_id"] for item in items] == [exact.id, prefix.id, substring.id, boosted.id, field.id]
    assert [item["match_type"] for item in items] == [
        "exact_alias" if alias else "exact_pn", "prefix", "substring", "boost", "field",
    ]
