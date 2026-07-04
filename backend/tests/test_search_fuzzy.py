"""搜索大小写模糊 + 库存匹配率排序/命中率（对齐型号查询/采购的 keyword_term_groups 口径）。"""
from datetime import date
from decimal import Decimal

from app.models.dimensions import DimPart
from app.models.inventory import Inventory
from app.services import inventory as inv_svc
from app.services import part_overview


def _part(db, pn, desc=None, brand=None):
    p = DimPart(pn_std=pn, description=desc, brand=brand)
    db.add(p)
    db.flush()
    return p


def _inv(db, part, qty="5", wh="北京成品仓", desc=None):
    db.add(Inventory(raw_inventory_id=f"INV-{part.pn_std}-{wh}", part_id=part.id,
                     pn_std=part.pn_std, warehouse=wh, source_qty=Decimal(qty),
                     description=desc if desc is not None else part.description,
                     snapshot_date=date(2026, 6, 1)))


def _seed_hdds(db):
    a = _part(db, "PN-A", "Samsung 8TB 7.2K SATA HDD", "Samsung")   # 全命中 3/3
    b = _part(db, "PN-B", "Samsung 8TB SSD", "Samsung")             # 缺 SATA → 2/3
    c = _part(db, "PN-C", "WD 8TB SATA HDD", "WD")                  # 缺 Samsung → 2/3
    d = _part(db, "PN-D", "Intel Xeon CPU", "Intel")               # 0 命中，不召回
    for p in (a, b, c, d):
        _inv(db, p)
    db.commit()
    return a, b, c, d


# ---------- 库存动态：匹配率排序 + 命中率 ----------
def test_dynamic_partial_match_ranking_and_hits(db):
    _seed_hdds(db)
    out = inv_svc.list_dynamic(db, "Samsung 8TB SATA", 1, 20)
    assert out["match_terms"] == 3
    pns = [it["pn_std"] for it in out["items"]]
    assert "PN-D" not in pns                      # 零命中不召回
    assert pns[0] == "PN-A"                        # 3/3 排最前
    assert set(pns) == {"PN-A", "PN-B", "PN-C"}
    top = out["items"][0]
    assert top["match_hits"] == 3 and top["match_terms"] == 3
    # 部分命中项 2/3
    for it in out["items"][1:]:
        assert it["match_hits"] == 2 and it["match_terms"] == 3


def test_dynamic_case_insensitive(db):
    _seed_hdds(db)
    lower = {it["pn_std"] for it in inv_svc.list_dynamic(db, "samsung 8tb sata", 1, 20)["items"]}
    upper = {it["pn_std"] for it in inv_svc.list_dynamic(db, "SAMSUNG 8TB SATA", 1, 20)["items"]}
    assert lower == upper == {"PN-A", "PN-B", "PN-C"}


def test_dynamic_spec_variant(db):
    _seed_hdds(db)
    # 8T 应命中 8TB（规格变体归一）
    pns = {it["pn_std"] for it in inv_svc.list_dynamic(db, "8T", 1, 20)["items"]}
    assert {"PN-A", "PN-B", "PN-C"} <= pns and "PN-D" not in pns


def test_dynamic_no_query_no_match_fields(db):
    _seed_hdds(db)
    out = inv_svc.list_dynamic(db, None, 1, 20)
    assert out["match_terms"] is None
    assert all(it["match_hits"] is None for it in out["items"])


# ---------- 库存静态列表：分词模糊（词序无关、大小写不敏感）----------
def test_list_inventory_tokenized_fuzzy(db):
    a, *_ = _seed_hdds(db)
    # 词序颠倒 + 小写 + 跨词：旧整串匹配搜不到，分词后能命中
    rows = inv_svc.list_inventory(db, None, "sata samsung", 1, 20)
    assert any(r["pn_std"] == "PN-A" for r in rows["items"])
    # 完全不相关词不召回
    assert all(r["pn_std"] != "PN-D" for r in rows["items"])


# ---------- 单字符/CJK 查询不得退化为全表返回（审计 P1）----------
def test_dynamic_single_cjk_char_filters(db):
    cn = _part(db, "PN-CN", "三星企业级固态盘", "三星")
    en = _part(db, "PN-EN", "Intel Xeon CPU", "Intel")
    for p in (cn, en):
        _inv(db, p)
    db.commit()
    # 搜单个中文字"三"（三星）：只召回含"三"的型号，绝不全表返回（审计 P1 回归）
    pns = {it["pn_std"] for it in inv_svc.list_dynamic(db, "三", 1, 20)["items"]}
    assert pns == {"PN-CN"}


def test_dynamic_single_latin_char_still_filters(db):
    _seed_hdds(db)
    # 单个字母/数字被分词丢弃 → 回退整串子串，仍过滤（不全表返回）
    out_all = inv_svc.list_dynamic(db, None, 1, 20)["total"]
    out_w = inv_svc.list_dynamic(db, "W", 1, 20)                # 只 WD 的 PN-C 描述含 W
    assert out_w["total"] < out_all and out_w["total"] >= 1
    assert all("W" in (it["description"] or "").upper() or "W" in it["pn_std"].upper()
               for it in out_w["items"])


def test_list_inventory_single_char_filters(db):
    _seed_hdds(db)
    all_n = inv_svc.list_inventory(db, None, None, 1, 50)["total"]
    one = inv_svc.list_inventory(db, None, "W", 1, 50)
    assert one["total"] < all_n               # 不再整表返回


# ---------- resolve_part 大小写容错 ----------
def test_resolve_part_case_insensitive(db):
    _part(db, "ABC123XYZ", "some part")
    db.commit()
    exact, _ = part_overview.resolve_part(db, "ABC123XYZ")
    lower, _ = part_overview.resolve_part(db, "abc123xyz")
    spaced, _ = part_overview.resolve_part(db, "  abc123XYZ  ")
    assert exact is not None
    assert lower is not None and lower.id == exact.id
    assert spaced is not None and spaced.id == exact.id
    assert part_overview.resolve_part(db, "no-such-pn")[0] is None
