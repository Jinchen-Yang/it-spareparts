"""实时检索服务契约：各字段、查询语义、排序和容错补足。"""

import pytest

from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import ProductSpec
from app.services import part_search


RESULT_KEYS = {
    "part_id", "pn_std", "brand", "manufacturer", "summary", "description",
    "category_major", "category_minor", "key_specs", "match_type", "match_reason", "matched_terms",
}


def _part(db, pn, **fields):
    part = DimPart(pn_std=pn, **fields)
    db.add(part)
    db.flush()
    return part


def _pns(db, query, **kwargs):
    return [item["pn_std"] for item in part_search.search(db, query, **kwargs)]


@pytest.fixture()
def catalog(db):
    part = _part(
        db, "0231-AC4L", pn_raw_sample="RAW-IDENTITY-957",
        brand="AuroraBrand", manufacturer="光联制造商", summary="低能耗热插拔",
        description="适用于海底通信服务器", category_major="硬盘", category_minor="SAS企业级",
    )
    db.add(PartAlias(pn_raw="LEGACY-ZX954", pn_std=part.pn_std, part_id=part.id, status="active"))
    for key, value, unit in [
        ("capacity", "1200GB", "GB"), ("interface", "SAS", None),
        ("rpm", "10K", "RPM"), ("cache", "256MB", "MB"),
    ]:
        db.add(ProductSpec(part_id=part.id, spec_key=key, spec_value=value, spec_unit=unit))
    db.flush()
    return part


@pytest.mark.parametrize("query", [
    "0231-AC4L", "RAW-IDENTITY-957", "LEGACY-ZX954", "aurorabrand", "光联制造商",
    "低能耗热插拔", "海底通信", "硬盘", "SAS企业级", "容量", "1200GB", "接口", "10K", "RPM",
    "０２３１－ａｃ４ｌ", "0231AC4L", "ＡｕｒｏｒａＢｒａｎｄ",
])
def test_all_projected_fields_are_searchable(db, catalog, query):
    items = part_search.search(db, query)
    hit = next((item for item in items if item["part_id"] == catalog.id), None)
    assert hit is not None, query
    assert hit["match_type"] != "fuzzy", query
    assert RESULT_KEYS <= items[0].keys()
    assert items[0]["match_reason"]
    assert items[0]["matched_terms"]


def test_key_specs_are_label_value_pairs_with_maximum_three(db, catalog):
    item = part_search.search(db, catalog.pn_std)[0]
    assert len(item["key_specs"]) == 3
    assert all(set(spec) == {"label", "value"} for spec in item["key_specs"])
    assert all(spec["label"] and spec["value"] for spec in item["key_specs"])


def test_wildcard_field_rank_uses_normalized_character_count(db):
    # ß 归一化为两个 S，不能用原文的单字符匹配误升为品牌档。
    _part(db, "A-NORMALIZED-DESCRIPTION", brand="XßY", description="XAY")
    _part(db, "Z-ACTUAL-BRAND", brand="XAY")
    assert _pns(db, "X?Y", limit=2) == ["Z-ACTUAL-BRAND", "A-NORMALIZED-DESCRIPTION"]


def test_wildcard_pn_rank_uses_normalized_character_count(db):
    _part(db, "XßY", description="XAY")
    _part(db, "XAY")
    items = part_search.search(db, "X?Y", limit=2)
    assert [it["pn_std"] for it in items] == ["XAY", "XßY"]
    assert items[1]["match_type"] == "field"


@pytest.mark.parametrize("query", ["０２３１ ＡＣ４Ｌ", "0231ac4l", "0231-ac4l"])
def test_exact_pn_uses_resolver_compact_rules(db, catalog, query):
    item = part_search.search(db, query)[0]
    assert item["part_id"] == catalog.id
    assert item["match_type"] == "exact_pn"


def test_only_active_aliases_participate(db):
    part = _part(db, "UNRELATED-TARGET")
    for suffix, status in [("ACTIVE", "active"), ("PENDING", "pending"), ("REJECTED", "rejected"), ("NULL", None)]:
        db.add(PartAlias(pn_raw=f"ALIAS-{suffix}-792", pn_std=part.pn_std, part_id=part.id, status=status))
    db.flush()
    assert part_search.search(db, "ALIAS-ACTIVE-792")[0]["match_type"] == "exact_alias"
    for query in ["PENDING", "REJECTED", "NULL"]:
        # 非生效别名不应产生直接命中；容错可补入与其他生效内容相近的结果。
        assert not [it for it in part_search.search(db, f"ALIAS-{query}-792") if it["match_type"] != "fuzzy"]


@pytest.mark.parametrize("query", ["WILD*TAIL", "WILD?MIDTAIL"])
def test_star_and_question_are_wildcards(db, query):
    _part(db, "WILDXMIDTAIL")
    _part(db, "OTHER-TARGET")
    assert _pns(db, query, limit=1) == ["WILDXMIDTAIL"]


@pytest.mark.parametrize("literal", ["%", "_", "\\"])
def test_sql_wildcard_characters_remain_literal(db, literal):
    target = _part(db, "LITERAL-TARGET", summary=f"TOKEN{literal}END")
    _part(db, "LITERAL-DECOY", summary="TOKENXEND")
    _part(db, "LITERAL-COMPACT-DECOY", summary="TOKENEND")
    items = part_search.search(db, f"TOKEN{literal}END")
    direct = [item["part_id"] for item in items if item["match_type"] != "fuzzy"]
    assert target.id in direct
    assert len(direct) == 1


@pytest.mark.parametrize("literal", ["%", "_", "\\"])
def test_literal_symbol_in_pn_has_exact_identity(db, literal):
    pn = f"LIT{literal}PN900"
    target = _part(db, pn)
    _part(db, pn + "-SUFFIX")
    _part(db, "LITXPN900")
    _part(db, "LITPN900")
    items = part_search.search(db, pn.lower())
    assert items[0]["part_id"] == target.id
    assert items[0]["match_type"] == "exact_pn"
    assert {item["pn_std"] for item in items} == {pn, pn + "-SUFFIX"}


@pytest.mark.parametrize("literal", ["%", "_", "\\"])
def test_literal_symbol_in_alias_has_exact_identity(db, literal):
    alias = f"OLD{literal}ALIAS712"
    target = _part(db, "CANONICAL-TARGET712")
    db.add(PartAlias(pn_raw=alias, pn_std=target.pn_std, part_id=target.id, status="active"))
    _part(db, "OLDXALIAS712")
    db.flush()
    items = part_search.search(db, alias.lower())
    assert [item["part_id"] for item in items] == [target.id]
    assert items[0]["match_type"] == "exact_alias"


def test_fullwidth_stored_pn_is_exact_after_nfkc_normalization(db):
    target = _part(db, "ＦＵＬＬ－９００")
    _part(db, "FULL-900-SUFFIX")
    items = part_search.search(db, "full-900")
    assert items[0]["part_id"] == target.id
    assert items[0]["match_type"] == "exact_pn"


def test_multi_term_requires_every_term_across_fields(db, catalog):
    _part(db, "MATCH-ONE-TERM", brand="AuroraBrand", description="普通设备")
    _part(db, "MATCH-OTHER-TERM", description="海底通信服务器")
    items = part_search.search(db, "AuroraBrand  海底通信  容量")
    assert [item["part_id"] for item in items if item["match_type"] != "fuzzy"] == [catalog.id]


def test_missing_multi_term_does_not_return_partial_direct_hits(db, catalog):
    items = part_search.search(db, "AuroraBrand 完全不存在的关键字")
    assert not [item for item in items if item["match_type"] != "fuzzy"]


def test_rank_exact_prefix_substring_then_field_weights(db):
    _part(db, "RANK700")
    alias = _part(db, "ALIAS-TARGET")
    db.add(PartAlias(pn_raw="RANK-700", pn_std=alias.pn_std, part_id=alias.id, status="active"))
    _part(db, "RANK700-PREFIX")
    _part(db, "X-RANK700-SUBSTRING")
    _part(db, "BRAND-HIT", brand="RANK700")
    _part(db, "MANUFACTURER-HIT", manufacturer="RANK700")
    _part(db, "CATEGORY-HIT", category_major="RANK700")
    spec = _part(db, "SPEC-HIT")
    db.add(ProductSpec(part_id=spec.id, spec_key="interface", spec_value="RANK700"))
    _part(db, "DESCRIPTION-HIT", description="RANK700")
    _part(db, "SUMMARY-HIT", summary="RANK700")
    db.flush()
    items = part_search.search(db, "RANK700", limit=30)
    by_pn = {item["pn_std"]: index for index, item in enumerate(items)}
    assert set(by_pn) == {
        "RANK700", "ALIAS-TARGET", "RANK700-PREFIX", "X-RANK700-SUBSTRING", "BRAND-HIT",
        "MANUFACTURER-HIT", "CATEGORY-HIT", "SPEC-HIT", "DESCRIPTION-HIT", "SUMMARY-HIT",
    }
    assert max(by_pn["RANK700"], by_pn["ALIAS-TARGET"]) < by_pn["RANK700-PREFIX"]
    assert by_pn["RANK700-PREFIX"] < by_pn["X-RANK700-SUBSTRING"]
    assert by_pn["X-RANK700-SUBSTRING"] < min(by_pn["BRAND-HIT"], by_pn["MANUFACTURER-HIT"])
    assert max(by_pn["BRAND-HIT"], by_pn["MANUFACTURER-HIT"]) < min(by_pn["CATEGORY-HIT"], by_pn["SPEC-HIT"])
    assert max(by_pn["CATEGORY-HIT"], by_pn["SPEC-HIT"]) < min(by_pn["DESCRIPTION-HIT"], by_pn["SUMMARY-HIT"])


def test_ascii_word_keeps_compact_pn_prefix_and_substring_ranking(db):
    prefix = _part(db, "AB-CD-900")
    substring = _part(db, "X-AB-CD-900")
    field = _part(db, "A-FIELD", brand="ABCD")
    items = part_search.search(db, "abcd", limit=3)
    assert [item["part_id"] for item in items] == [prefix.id, substring.id, field.id]
    assert [item["match_type"] for item in items] == ["prefix", "substring", "field"]


@pytest.mark.parametrize("query, match_type", [
    ("ABC900", "exact_pn"), ("ABC", "prefix"), ("BC9", "substring"),
])
def test_fullwidth_pn_preserves_tier_for_ascii_fast_path(db, query, match_type):
    target = _part(db, "ＡＢＣ９００")
    items = part_search.search(db, query, limit=1)
    assert items[0]["part_id"] == target.id
    assert items[0]["match_type"] == match_type


def test_same_rank_is_stable_by_pn(db):
    for pn in ["ZZZ-ORDER", "AAA-ORDER", "MMM-ORDER"]:
        _part(db, pn, brand="SAME-BRAND")
    assert _pns(db, "SAME-BRAND", limit=3) == ["AAA-ORDER", "MMM-ORDER", "ZZZ-ORDER"]


def test_compact_brand_match_keeps_higher_field_weight(db):
    brand = _part(db, "Z-BRAND", brand="VENDOR-777")
    description = _part(db, "A-DESC", description="VENDOR777")
    items = part_search.search(db, "VENDOR777", limit=2)
    assert [item["part_id"] for item in items] == [brand.id, description.id]


def test_pn_tier_tie_breaker_does_not_use_field_weights(db):
    _part(db, "PREFIX500-A")
    _part(db, "PREFIX500-Z", brand="PREFIX500")
    assert _pns(db, "PREFIX500", limit=2) == ["PREFIX500-A", "PREFIX500-Z"]


@pytest.mark.parametrize("use_callable", [False, True])
def test_boost_hook_follows_pn_evidence_but_precedes_field_weight(db, use_callable):
    prefix = _part(db, "BOOST900-PREFIX")
    brand = _part(db, "BRAND-RESULT", brand="BOOST900")
    boosted = _part(db, "LOW-FIELD-RESULT", description="BOOST900")
    calls = []

    def boost(db_arg, query):
        calls.append((db_arg, query))
        return {boosted.id}

    hook = boost if use_callable else {boosted.id}
    items = part_search.search(db, "BOOST900", limit=3, filters={"boost_part_ids": hook})
    assert [item["part_id"] for item in items] == [prefix.id, boosted.id, brand.id]
    assert items[1]["match_type"] == "boost"
    if use_callable:
        assert calls == [(db, "BOOST900")]


def test_spec_reason_and_matched_terms_support_frontend_highlighting(db, catalog):
    item = part_search.search(db, "容量 1200gb")[0]
    assert item["part_id"] == catalog.id
    assert item["match_reason"] == "规格命中：容量"
    assert item["matched_terms"] == ["容量", "1200GB"]


def test_fuzzy_fills_remaining_slots_and_marks_only_fallback(db):
    _part(db, "ST8000NM000A")
    _part(db, "ST8000NM000B")
    _part(db, "UNRELATED-OTHER")
    items = part_search.search(db, "ST8000NM000A", limit=2)
    assert len(items) == 2
    assert items[0]["pn_std"] == "ST8000NM000A"
    assert items[0]["match_type"] == "exact_pn"
    assert items[1]["pn_std"] == "ST8000NM000B"
    assert items[1]["match_type"] == "fuzzy"
    typo = part_search.search(db, "ST8000NM00OA", limit=2)
    assert typo and all(item["match_type"] == "fuzzy" for item in typo)


def test_compact_pn_typo_uses_fuzzy_fallback(db):
    target = _part(db, "AB-CD-EF-GH-IJ-KL-9012")
    items = part_search.search(db, "ABCDEFGHIJKL9013")
    assert [item["part_id"] for item in items] == [target.id]
    assert items[0]["match_type"] == "fuzzy"


def test_compact_alias_typo_uses_fuzzy_fallback(db):
    target = _part(db, "CANONICAL-NOT-SIMILAR")
    db.add(PartAlias(
        pn_raw="QR-ST-UV-WX-YZ-7812", pn_std=target.pn_std, part_id=target.id, status="active",
    ))
    db.flush()
    items = part_search.search(db, "QRSTUVWXYZ7813")
    assert [item["part_id"] for item in items] == [target.id]
    assert items[0]["match_type"] == "fuzzy"


def test_multi_term_fuzzy_can_match_compact_pn_and_another_field(db):
    target = _part(db, "AB-CD-EF-GH-IJ-KL-9012", brand="PrecisionOptics")
    items = part_search.search(db, "ABCDEFGHIJKL9013 PrecisionOptics")
    assert [item["part_id"] for item in items] == [target.id]
    assert items[0]["match_type"] == "fuzzy"


def test_short_exact_term_does_not_disable_long_term_fuzzy_fallback(db):
    target = _part(db, "SHORT-AND-TYPO", brand="HP", manufacturer="STORAGEVENDOR")
    items = part_search.search(db, "HP STORAGEVENDRO")
    assert [item["part_id"] for item in items] == [target.id]
    assert items[0]["match_type"] == "fuzzy"


def test_short_brand_typo_is_recalled_without_pn_or_alias_similarity(db):
    target = _part(db, "UNRELATED-902", brand="SEAGATE")
    items = part_search.search(db, "SEAGTAE")
    assert [item["part_id"] for item in items] == [target.id]
    assert items[0]["match_type"] == "fuzzy"


@pytest.mark.parametrize("unrelated", ["完全无关的词", "unrelatedzzzzzz"])
def test_multi_term_fuzzy_does_not_ignore_unrelated_term(db, unrelated):
    _part(db, "AB-CD-EF-GH-IJ-KL-9012", brand="PrecisionOptics")
    assert part_search.search(db, f"ABCDEFGHIJKL9013 {unrelated}") == []


def test_tombstone_cannot_be_returned_even_by_exact_or_fuzzy(db):
    survivor = _part(db, "SURVIVOR-818")
    ghost = _part(db, "GHOST-818", status="merged", merged_into_id=survivor.id)
    assert ghost.id not in [item["part_id"] for item in part_search.search(db, "GHOST-818")]


@pytest.mark.parametrize("query", ["", "   ", "\t\n　"])
def test_empty_service_query_does_not_use_session(query):
    class NoDatabase:
        def execute(self, *args, **kwargs):
            pytest.fail("空查询不能执行 SQL")

    assert part_search.search(NoDatabase(), query) == []


def test_single_character_numeric_and_no_match_queries(db):
    _part(db, "X-123456")
    assert _pns(db, "X", limit=1) == ["X-123456"]
    assert _pns(db, "123456", limit=1) == ["X-123456"]
    assert part_search.search(db, "完全无关的罕见词汇") == []


def test_filter_constraints_apply_to_direct_and_fuzzy_candidates(db):
    _part(db, "FILTER-AA100", brand="Allowed", category_major="硬盘", category_minor="SAS")
    _part(db, "FILTER-AA101", brand="Other", category_major="硬盘", category_minor="SAS")
    assert _pns(db, "FILTER-AA100", filters={"brand": "Other"}) == ["FILTER-AA101"]
    assert _pns(db, "FILTER", filters={"category_major": "内存"}) == []
