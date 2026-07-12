"""第②块统一搜索——"精确即唯一"短路 + part_id 贯通。

甲方复现 bug：型号查询搜精确 PN `02311DYQ` 返回 20 个相似型号（trigram 对同前缀
系列打高分），采购查询却能精确返回。规则：PN/别名精确命中 → 只返回唯一标准型号
（exact=True，前端直开全景）；无精确命中才走 前缀/包含/描述/模糊 回退。
"""
from datetime import date  # noqa: F401  (与其它 resolver 测试保持一致的导入习惯)

import pytest

from app.models.dimensions import DimPart, PartAlias
from app.services import part_resolver


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


def test_exact_pn_returns_single_item(db, huawei_family):
    """精确 PN 只返回唯一型号——不再拖出一屏相似候选。"""
    r = part_resolver.resolve(db, "02311DYQ", limit=20)
    assert r["exact"] is True
    assert len(r["items"]) == 1, f"精确命中应只有 1 条，实得 {len(r['items'])}"
    it = r["items"][0]
    assert it["pn_std"] == "02311DYQ"
    assert it["part_id"] == huawei_family["02311DYQ"].id
    assert it["match_reason"] == "PN精确匹配"
    assert r["low_confidence"] is False and r["ambiguous"] is False


def test_exact_case_and_hyphen_insensitive(db, huawei_family):
    """大小写/连字符不敏感（compact 归一后精确）。"""
    r = part_resolver.resolve(db, "02311-dyq", limit=20)
    assert r["exact"] is True and len(r["items"]) == 1
    assert r["items"][0]["pn_std"] == "02311DYQ"


def test_exact_via_alias_redirects(db, huawei_family):
    """别名精确命中 → 折叠到目标标准型号，理由带别名证据。"""
    db.add(PartAlias(pn_raw="HW-02311DYQ-OLD", pn_std="02311DYA",
                     part_id=huawei_family["02311DYA"].id, status="active"))
    db.commit()
    r = part_resolver.resolve(db, "HW-02311DYQ-OLD", limit=20)
    assert r["exact"] is True and len(r["items"]) == 1
    assert r["items"][0]["pn_std"] == "02311DYA"
    assert "别名命中" in r["items"][0]["match_reason"]


def test_no_exact_falls_back_with_part_id(db, huawei_family):
    """无精确命中 → 回退相似排序；每条结果都带统一 part_id。"""
    r = part_resolver.resolve(db, "02311DY", limit=20, log_miss=False)
    assert r["exact"] is False
    assert len(r["items"]) > 1                     # 前缀族群召回多条
    for it in r["items"]:
        assert it["part_id"] is not None           # part_id 贯通（第②块统一身份）
    pns = [it["pn_std"] for it in r["items"]]
    assert "02311DYQ" in pns


def test_duplicate_compact_no_shortcut_marks_ambiguous(db):
    """脏数据：两个未合并型号 compact 相同 → 不短路，走排序并标 ambiguous 要求消歧。"""
    db.add_all([DimPart(pn_std="AB-100", description="变体一"),
                DimPart(pn_std="AB100", description="变体二")])
    db.commit()
    r = part_resolver.resolve(db, "AB100", limit=10)
    assert r["exact"] is False
    assert r["ambiguous"] is True
    exact_pns = {it["pn_std"] for it in r["items"] if "PN精确匹配" in it["match_reason"]}
    assert exact_pns == {"AB-100", "AB100"}


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
