"""统一搜索（unified-search-v2）——"精确即唯一"短路 + part_id 贯通 + 统一结构。

甲方复现 bug：型号查询搜精确 PN `02311DYQ` 返回 20 个相似型号（trigram 对同前缀
系列打高分），采购查询却能精确返回。规则：PN/别名精确命中 → 只返回唯一标准型号
（exact=True，前端直开全景），相似候选降级到 similar_items 独立区域；无精确命中
才走 前缀/包含/描述/模糊 回退。每条结果带统一结构：
part_id / pn_std / description / brand / category / match_type / matched_text /
score / pool_group_id / pool_name。

移植自旧分支 feat/p1-unified-search(d3f6d4d) 的 test_resolver_exact.py 六用例，
并按新统一结构扩展（池身份 / match_type / similar_items / 权限不泄漏 / 规模上限）。
"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.etl import loader
from app.main import app
from app.models.dimensions import DimPart, PartAlias
from app.models.system import SysImportBatch, SysUser
from app.services import part_overview, part_resolver
from app.services import pool_catalog
from tests import factories as f

# 统一结构必备键（三、统一结构）：所有 resolver 结果条目必须带齐
UNIFIED_KEYS = {"part_id", "pn_std", "description", "brand", "category",
                "match_type", "matched_text", "score", "pool_group_id", "pool_name"}


@pytest.fixture()
def huawei_family(db):
    """华为 02311 系列：同前缀高 trigram 相似——正是 02311DYQ 案的形状。"""
    pns = ["02311DYQ", "02311DYA", "02311DYB", "02311DYC", "02311DXQ",
           "02311DEQ", "02311AYQ", "02312DYQ"]
    parts = {}
    for pn in pns:
        p = DimPart(pn_std=pn, brand="华为", description=f"华为部件 {pn}")
        db.add(p)
        parts[pn] = p
    db.flush()
    db.commit()
    return parts


def _mk_client(db, username, role, permissions=None, password="pw123456"):
    db.add(SysUser(username=username, role=role, permissions=permissions,
                   password_hash=hash_password(password)))
    db.commit()
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200
    c.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    return c


# ---------------------------------------------------------------- 精确即唯一

def test_exact_pn_returns_single_item(db, huawei_family):
    """精确 PN 只返回唯一型号——不再拖出一屏相似候选。"""
    r = part_resolver.resolve(db, "02311DYQ", limit=20)
    assert r["exact"] is True
    assert len(r["items"]) == 1, f"精确命中应只有 1 条，实得 {len(r['items'])}"
    it = r["items"][0]
    assert it["pn_std"] == "02311DYQ"
    assert it["part_id"] == huawei_family["02311DYQ"].id
    assert it["match_type"] == "exact_pn"
    assert it["matched_text"] == "02311DYQ"
    assert it["match_reason"] == "PN精确匹配"
    assert UNIFIED_KEYS <= set(it.keys())
    assert r["low_confidence"] is False and r["ambiguous"] is False


def test_exact_case_hyphen_space_insensitive(db, huawei_family):
    """大小写/连字符/首尾空格不敏感（compact 归一后精确）。"""
    for variant in ("02311-dyq", "  02311dyq  ", "02311 DYQ", "02311_Dyq"):
        r = part_resolver.resolve(db, variant, limit=20)
        assert r["exact"] is True and len(r["items"]) == 1, f"变体 {variant!r} 应精确命中"
        assert r["items"][0]["pn_std"] == "02311DYQ"


def test_exact_via_alias_redirects(db, huawei_family):
    """别名精确命中 → 折叠到目标标准型号，match_type=exact_alias、matched_text=别名原值。"""
    db.add(PartAlias(pn_raw="HW-02311DYQ-OLD", pn_std="02311DYA",
                     part_id=huawei_family["02311DYA"].id, status="active"))
    db.commit()
    r = part_resolver.resolve(db, "HW-02311DYQ-OLD", limit=20)
    assert r["exact"] is True and len(r["items"]) == 1
    it = r["items"][0]
    assert it["pn_std"] == "02311DYA"
    assert it["match_type"] == "exact_alias"
    assert it["matched_text"] == "HW-02311DYQ-OLD"
    assert "别名命中" in it["match_reason"]


def test_similar_not_preempting_exact(db, huawei_family):
    """相似 PN 不抢占精确结果：include_similar=True 时相似候选只出现在 similar_items，
    主结果仍是唯一精确命中。"""
    r = part_resolver.resolve(db, "02311DYQ", limit=20, include_similar=True)
    assert r["exact"] is True and len(r["items"]) == 1
    assert r["items"][0]["pn_std"] == "02311DYQ"
    sim_pns = [it["pn_std"] for it in r["similar_items"]]
    assert "02311DYQ" not in sim_pns, "精确命中不得重复出现在相似区"
    assert sim_pns, "同前缀家族应产出相似候选"
    assert len(sim_pns) <= part_resolver._SIMILAR_LIMIT
    for it in r["similar_items"]:
        assert UNIFIED_KEYS <= set(it.keys())
        assert it["match_type"] != "exact_pn"


def test_internal_callers_get_no_similar_by_default(db, huawei_family):
    """内部消费方（AI 助手/查重）默认不取相似区：零额外召回开销。"""
    r = part_resolver.resolve(db, "02311DYQ", limit=20)
    assert r["similar_items"] == []


def test_no_exact_falls_back_with_part_id(db, huawei_family):
    """无精确命中 → 回退相似排序；每条结果都带统一 part_id 与 match_type。"""
    r = part_resolver.resolve(db, "02311DY", limit=20, log_miss=False)
    assert r["exact"] is False
    assert len(r["items"]) > 1                     # 前缀族群召回多条
    for it in r["items"]:
        assert it["part_id"] is not None           # part_id 贯通（统一身份）
        assert UNIFIED_KEYS <= set(it.keys())
        assert it["match_type"] in {"fuzzy_pn", "alias", "description", "weak"}
    pns = [it["pn_std"] for it in r["items"]]
    assert "02311DYQ" in pns


# ---------------------------------------------------------------- 歧义

def test_duplicate_compact_no_shortcut_marks_ambiguous(db):
    """脏数据：两个未合并型号 compact 相同 → 不短路，走排序并标 ambiguous 要求消歧。"""
    db.add_all([DimPart(pn_std="AB-100", description="变体一"),
                DimPart(pn_std="AB100", description="变体二")])
    db.commit()
    r = part_resolver.resolve(db, "AB100", limit=10)
    assert r["exact"] is False
    assert r["ambiguous"] is True
    exact_pns = {it["pn_std"] for it in r["items"] if it["match_type"] == "exact_pn"}
    assert exact_pns == {"AB-100", "AB100"}


def test_multi_alias_same_compact_marks_ambiguous(db):
    """多个别名同写法指向不同型号 → 明确 ambiguous 响应，两个目标都在场。"""
    p1 = DimPart(pn_std="TARGET-ONE", description="目标一")
    p2 = DimPart(pn_std="TARGET-TWO", description="目标二")
    db.add_all([p1, p2]); db.flush()
    db.add_all([
        PartAlias(pn_raw="XY-999-V1", pn_std="TARGET-ONE", part_id=p1.id, status="active"),
        PartAlias(pn_raw="XY999V1", pn_std="TARGET-TWO", part_id=p2.id, status="active"),
    ])
    db.commit()
    r = part_resolver.resolve(db, "XY-999-V1", limit=10, log_miss=False)
    assert r["exact"] is False
    assert r["ambiguous"] is True
    pns = {it["pn_std"] for it in r["items"]}
    assert {"TARGET-ONE", "TARGET-TWO"} <= pns


def test_merged_tombstone_not_exact_target(db, huawei_family):
    """已合并墓碑不作为精确目标；其别名重定向到存活型号。"""
    ghost = DimPart(pn_std="02311GHOST", status="merged",
                    merged_into_id=huawei_family["02311DYQ"].id)
    db.add(ghost); db.flush()
    db.add(PartAlias(pn_raw="02311GHOST", pn_std="02311DYQ",
                     part_id=huawei_family["02311DYQ"].id, status="active"))
    db.commit()
    r = part_resolver.resolve(db, "02311GHOST", limit=10)
    assert r["exact"] is True and len(r["items"]) == 1
    assert r["items"][0]["pn_std"] == "02311DYQ"   # 重定向到存活目标，不返回墓碑


# ---------------------------------------------------------------- 池身份

def test_pool_identity_attached(db, huawei_family):
    """结果带池 ID 和池名；非成员为 null；归档池不算。"""
    ids = [huawei_family["02311DYQ"].id, huawei_family["02311DYA"].id]
    created = pool_catalog.create_pool(db, name="华为互通池", member_part_ids=ids,
                                       operated_by="test")
    gid = created["group_id"]
    r = part_resolver.resolve(db, "02311DYQ", limit=5, include_similar=True)
    assert r["items"][0]["pool_group_id"] == gid
    assert r["items"][0]["pool_name"] == "华为互通池"
    by_pn = {it["pn_std"]: it for it in r["similar_items"]}
    if "02311DYA" in by_pn:
        assert by_pn["02311DYA"]["pool_group_id"] == gid
    # 非成员：无池身份
    r2 = part_resolver.resolve(db, "02312DYQ", limit=5)
    assert r2["items"][0]["pool_group_id"] is None
    assert r2["items"][0]["pool_name"] is None
    # 归档后不再挂池身份
    pool_catalog.archive_pool(db, group_id=gid, version=created.get("version", 1),
                              operated_by="test")
    r3 = part_resolver.resolve(db, "02311DYQ", limit=5)
    assert r3["items"][0]["pool_group_id"] is None


# ---------------------------------------------------------------- 规模上限

def test_large_similar_family_bounded(db):
    """大量相似 PN：返回数量受 limit/_SIMILAR_LIMIT 约束，不随族群规模膨胀。"""
    for i in range(120):
        db.add(DimPart(pn_std=f"ST8000NM{i:04d}", brand="Seagate",
                       description=f"8TB 企业盘 变体{i:04d}"))
    db.add(DimPart(pn_std="ST8000NM0001X", brand="Seagate", description="8TB 企业盘 X"))
    db.commit()
    r = part_resolver.resolve(db, "ST8000NM0001X", limit=20, include_similar=True)
    assert r["exact"] is True and len(r["items"]) == 1
    assert len(r["similar_items"]) <= part_resolver._SIMILAR_LIMIT
    r2 = part_resolver.resolve(db, "ST8000NM", limit=20, log_miss=False)
    assert len(r2["items"]) <= 20


# ---------------------------------------------------------------- API 层

def test_search_api_exact_shape_and_no_price_leak(db, huawei_family):
    """/parts/search：exact 透传 + 统一结构 + 搜索结果不携带任何价格/成本/约束价字段
    （无权限字段不因搜索接口泄漏——搜索只出身份与匹配证据，价格一律走各自权限门的接口）。"""
    ids = [huawei_family["02311DYQ"].id, huawei_family["02311DYA"].id]
    pool_catalog.create_pool(db, name="华为互通池", member_part_ids=ids, operated_by="test")
    forbidden = {"unit_price", "unit_cost", "avg_cost_moving", "avg_cost_fifo",
                 "purchase_ceiling_ex_tax", "sale_floor_ex_tax", "profit_amount",
                 "profit_rate", "supplier", "customer"}
    for role in ("sales", "purchaser", "boss", "readonly"):
        c = _mk_client(db, f"s_{role}", role)
        r = c.get("/api/parts/search", params={"q": "02311DYQ"})
        assert r.status_code == 200
        data = r.json()
        assert data["exact"] is True and len(data["items"]) == 1
        it = data["items"][0]
        assert UNIFIED_KEYS <= set(it.keys())
        assert it["pool_name"] == "华为互通池"
        for row in data["items"] + data["similar_items"]:
            assert not (set(row.keys()) & forbidden), f"{role} 搜索结果泄漏价格字段"


def test_search_api_structured_branch_unified_keys(db, huawei_family):
    """browse/结构化分支同样带 part_id 与池身份（同一搜索规则的另一入口）。"""
    c = _mk_client(db, "s_browse", "admin")
    r = c.get("/api/parts/search", params={"q": "02311", "browse": "true"})
    assert r.status_code == 200
    for it in r.json()["items"]:
        assert it["part_id"] == it["id"]
        assert "pool_group_id" in it and "pool_name" in it


def test_purchase_history_and_parts_same_exact_rule(db):
    """采购历史与型号查询同规则：同一个精确 PN（大小写不同写法）在两个入口都命中同一型号。"""
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hunified")
    db.add(b); db.flush()
    porders = {"P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True)}
    plines = [f.purchase_line("P1", "PL1", "02311DYQ", qty="2", price="113")]
    loader.load(db, f.purchase_result(porders, plines), b.id, date(2026, 6, 1))
    db.commit()
    c = _mk_client(db, "s_both", "admin")
    pr = c.get("/api/parts/search", params={"q": "02311-dyq"})
    assert pr.json()["exact"] is True
    part_pn = pr.json()["items"][0]["pn_std"]
    assert part_pn == "02311DYQ"
    rr = c.get("/api/purchases/recent", params={"q": "02311DYQ", "days": 3660})
    pns = {row["pn_std"] for row in rr.json()["items"]}
    assert pns == {"02311DYQ"}, "采购历史应命中同一型号的行"


# ---------------------------------------------------------------- part_id 深链

def test_overview_by_part_id(db, huawei_family):
    """GET /parts/overview?part_id= 与 pn_std 同构；part.id 供前端回写稳定深链。"""
    pid = huawei_family["02311DYQ"].id
    c = _mk_client(db, "s_ov", "admin")
    r = c.get("/api/parts/overview", params={"part_id": pid})
    assert r.status_code == 200
    assert r.json()["part"]["pn_std"] == "02311DYQ"
    assert r.json()["part"]["id"] == pid
    r2 = c.get("/api/parts/overview", params={"pn_std": "02311DYQ"})
    assert r2.json()["part"]["id"] == pid
    assert c.get("/api/parts/overview", params={"part_id": 999999}).status_code == 404
    assert c.get("/api/parts/overview").status_code == 422


def test_overview_by_part_id_follows_merge(db, huawei_family):
    """墓碑 part_id 深链沿 merged_into 链重定向到存活档案（旧收藏链接不 404）。"""
    ghost = DimPart(pn_std="02311GHOST", status="merged",
                    merged_into_id=huawei_family["02311DYQ"].id)
    db.add(ghost); db.flush(); db.commit()
    part, redirected = part_overview.resolve_part_by_id(db, ghost.id)
    assert part is not None and part.pn_std == "02311DYQ"
    assert redirected == "02311GHOST"
    c = _mk_client(db, "s_merge", "admin")
    r = c.get("/api/parts/overview", params={"part_id": ghost.id})
    assert r.status_code == 200
    assert r.json()["part"]["pn_std"] == "02311DYQ"
    assert r.json()["part"]["redirected_from"] == "02311GHOST"
