"""PN 解析性能优化必须保持的大小写、歧义、状态及批次契约。"""

import pytest
from fastapi import Response
from sqlalchemy import event, func, select

from app.api.resale_catalog_export import ResolveRequest, resolve_parts
from app.models.dimensions import DimPart, PartAlias
from app.services.resale_pn_lookup import find_pn_candidates, find_pn_candidates_batch


def _part(db, pn):
    part = DimPart(pn_std=pn)
    db.add(part)
    db.flush()
    return part


def _alias(db, raw, part, status="active"):
    alias = PartAlias(
        pn_raw=raw, pn_std=part.pn_std, part_id=part.id, status=status,
    )
    db.add(alias)
    db.flush()
    return alias


def _resolve(db, raws):
    body = ResolveRequest(items=[
        {"sourceKey": str(i), "pnRaw": raw} for i, raw in enumerate(raws)
    ])
    response = Response()
    result = resolve_parts(body, response, None, db)
    assert response.headers["cache-control"] == "no-store"
    return result["items"]


def test_case_distinct_alias_rows_keep_both_targets_and_ambiguity(db):
    first = _part(db, "CASE-TARGET-1")
    second = _part(db, "CASE-TARGET-2")
    _alias(db, "Mixed-Alias", first)
    _alias(db, "mIXED-aLIAS", second)
    db.commit()

    matches, pending = find_pn_candidates(db, "mixed-alias")
    assert set(matches) == {first.id, second.id}
    assert {kind for _part_row, kind in matches.values()} == {"alias"}
    assert pending is False
    row = _resolve(db, ["mixed-alias"])[0]
    assert row["status"] == "ambiguous"
    assert {candidate["partId"] for candidate in row["candidates"]} == {first.id, second.id}


def test_exact_match_still_checks_pending_alias_and_does_not_choose_exact(db):
    exact = _part(db, "EXACT-PENDING")
    pending_target = _part(db, "PENDING-TARGET")
    _alias(db, "exact-pending", pending_target, "pending")
    db.commit()

    matches, pending = find_pn_candidates(db, "exact-pending")
    assert set(matches) == {exact.id}
    assert matches[exact.id][1] == "exact"
    assert pending is True
    row = _resolve(db, ["exact-pending"])[0]
    assert row["status"] == "ambiguous"
    assert row["candidates"] == [{"partId": exact.id, "pnStd": exact.pn_std}]


def test_exact_and_active_alias_to_same_identity_keep_exact_precedence(db):
    exact = _part(db, "SAME-IDENTITY")
    _alias(db, "same-identity", exact)
    db.commit()

    row = _resolve(db, ["same-identity"])[0]
    assert row["status"] == "exact"
    assert row["partId"] == exact.id


def test_unbound_pending_alias_still_blocks_resolution(db):
    db.add(PartAlias(pn_raw="unbound-pending", pn_std="UNBOUND", status="pending"))
    db.commit()

    assert find_pn_candidates(db, "UNBOUND-PENDING") == ({}, True)
    row = _resolve(db, ["UNBOUND-PENDING"])[0]
    assert row["status"] == "ambiguous"
    assert row["candidates"] == []


def test_batch_keeps_legacy_zero_alias_target_guard_even_if_exact_loaded(db):
    part = DimPart(id=0, pn_std="ZERO-ID")
    db.add(part)
    db.flush()
    _alias(db, "zero-alias", part)
    db.commit()

    assert find_pn_candidates(db, "zero-alias") == ({}, False)
    result = find_pn_candidates_batch(db, ["ZERO-ID", "zero-alias"])
    assert result["zero-alias"] == ({}, False)
    assert result["ZERO-ID"][0][0][1] == "exact"


@pytest.mark.parametrize("status", ["rejected", None])
def test_rejected_and_legacy_null_aliases_do_not_resolve(db, status):
    part = _part(db, "IGNORED-TARGET")
    _alias(db, "ignored-alias", part, status)
    db.commit()

    assert find_pn_candidates(db, "IGNORED-ALIAS") == ({}, False)
    assert _resolve(db, ["IGNORED-ALIAS"])[0]["status"] == "not_found"


def test_alias_punctuation_is_not_compacted(db):
    part = _part(db, "PUNCTUATION-TARGET")
    _alias(db, "RAW-123", part)
    db.commit()

    assert find_pn_candidates(db, "RAW123") == ({}, False)
    assert _resolve(db, ["raw-123"])[0]["partId"] == part.id


@pytest.mark.parametrize("raw", ["café-备件", "straße-9", "ı-42", "Σ-β"])
def test_unicode_keeps_postgres_upper_compared_with_python_upper(db, raw):
    part = _part(db, "UNICODE-TARGET")
    alias = _alias(db, raw, part)
    db.commit()

    # Deliberately retain the existing two runtimes' casing behavior, including
    # cases such as ß whose result depends on the database locale/provider.
    postgres_upper = db.scalar(select(func.upper(PartAlias.pn_raw)).where(PartAlias.id == alias.id))
    expected = {part.id} if postgres_upper == raw.upper() else set()
    matches, pending = find_pn_candidates(db, raw)
    assert set(matches) == expected
    assert pending is False
    row = _resolve(db, [raw])[0]
    assert row["status"] == ("alias" if expected else "not_found")
    if expected:
        assert row["partId"] == part.id


@pytest.mark.parametrize("count,repeat", [(1, False), (10, False), (100, False), (100, True)])
def test_batch_preserves_source_keys_order_and_repeated_results(db, count, repeat):
    parts = [_part(db, f"BATCH-{i:03d}") for i in range(1 if repeat else count)]
    db.commit()
    inputs = [parts[0].pn_std.lower()] * count if repeat else [p.pn_std.lower() for p in parts]
    rows = _resolve(db, inputs)

    assert [row["sourceKey"] for row in rows] == [str(i) for i in range(count)]
    assert [row["partId"] for row in rows] == (
        [parts[0].id] * count if repeat else [p.id for p in parts]
    )
    assert all(row["status"] == "exact" and row["candidates"] == [] for row in rows)


def test_batch_candidate_results_match_single_item_path_including_order(db):
    first = _part(db, "MIXED-RAW")
    second = _part(db, "OTHER-TARGET")
    unicode_part = _part(db, "UNICODE-TARGET")
    _alias(db, "mixed-raw", second)
    _alias(db, "Mixed-Raw", first)
    _alias(db, "MIXED-raw", second, "pending")
    _alias(db, "café-备件", unicode_part)
    _alias(db, "straße-9", unicode_part)
    _alias(db, "ignore-me", second, "rejected")
    db.commit()
    raws = ["mixed-raw", "MIXED-RAW", "café-备件", "CAFÉ-备件", "straße-9", "STRASSE-9",
            "MIXED-RAW V123", "ignore-me", "missing", ""]

    def signature(found):
        matches, pending = found
        return [(part_id, kind) for part_id, (_part_row, kind) in matches.items()], pending

    expected = {raw: signature(find_pn_candidates(db, raw)) for raw in raws}
    actual = find_pn_candidates_batch(db, raws)
    assert {raw: signature(found) for raw, found in actual.items()} == expected


@pytest.mark.parametrize("alias_only", [False, True])
def test_hundred_distinct_inputs_have_bounded_sql_round_trips(db, alias_only):
    for i in range(100):
        part = _part(db, f"COUNT-{i:03d}")
        if alias_only:
            _alias(db, f"count-alias-{i:03d}", part)
    db.commit()
    raws = [f"count-{'alias-' if alias_only else ''}{i:03d}" for i in range(100)]
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        rows = _resolve(db, raws)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(rows) == 100
    assert all(row["status"] == ("alias" if alias_only else "exact") for row in rows)
    assert len(statements) == (3 if alias_only else 2)
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
