"""PN 近似解析器单测（对本地真库跑，临时数据进出有清理）。

核心验收用例（用户指定）：库内只有 "4089-RT"，搜 "super 4089RT-x 准系统" 必须 top1 命中。
"""
import pytest

from app import config
from app.db import SessionLocal
from app.models.dimensions import DimPart, PartAlias
from app.services import part_resolver

_PN = "4089-RT"
_ALIAS_RAW = "TEST-ALIAS-4089X"


@pytest.fixture(scope="module")
def db():
    s = SessionLocal()
    # 造数：超微准系统 + 一条人工别名（teardown 清理；若已存在则不动）
    created_part = created_alias = False
    if s.query(DimPart).filter_by(pn_std=_PN).first() is None:
        s.add(DimPart(pn_std=_PN, description="超微准系统 Supermicro 4U barebone",
                      brand="超微", needs_review=False))
        created_part = True
    if s.query(PartAlias).filter_by(pn_raw=_ALIAS_RAW).first() is None:
        s.add(PartAlias(pn_raw=_ALIAS_RAW, pn_std=_PN, source="manual"))
        created_alias = True
    s.commit()
    yield s
    if created_alias:
        s.query(PartAlias).filter_by(pn_raw=_ALIAS_RAW).delete()
    if created_part:
        s.query(DimPart).filter_by(pn_std=_PN).delete()
    s.commit()
    s.close()


def _top1(db, q):
    r = part_resolver.resolve(db, q, limit=5)
    assert r["items"], f"查询 {q!r} 无结果"
    return r


def test_4089_acceptance(db):
    """验收用例：连字符位置不同 + 多余后缀 + 品牌词 + 中文品类词。"""
    r = _top1(db, "super 4089RT-x 准系统")
    assert r["items"][0]["pn_std"] == _PN
    assert not r["low_confidence"]
    reason = r["items"][0]["match_reason"]
    assert "PN" in reason  # 相似/包含至少一种证据


def test_hyphen_and_case(db):
    """紧凑化：去连字符、大小写不敏感，精确命中恒第一。"""
    r = _top1(db, "4089rt")
    assert r["items"][0]["pn_std"] == _PN
    assert "PN精确匹配" in r["items"][0]["match_reason"]


def test_vcode_strip(db):
    """氚云 V 码尾巴截断后再匹配（复用导入侧 cleaner 逻辑）。"""
    r = _top1(db, "4089-RT V0230LD000000000")
    assert r["items"][0]["pn_std"] == _PN


def test_cjk_only(db):
    """纯中文词走 search_doc（description）命中。"""
    r = part_resolver.resolve(db, "准系统", limit=20)
    assert any(it["pn_std"] == _PN for it in r["items"])


def test_alias_hit(db):
    """人工别名即刻生效：搜别名原值折叠到标准 PN。"""
    r = _top1(db, _ALIAS_RAW)
    assert r["items"][0]["pn_std"] == _PN
    assert "别名命中" in r["items"][0]["match_reason"]


def test_gibberish_low_confidence(db, monkeypatch):
    """无关查询：低置信标记，不污染真库审计表。"""
    monkeypatch.setattr(config, "SEARCH_MISS_LOG", False)
    r = part_resolver.resolve(db, "ZZQX99完全不存在的型号WWK", limit=5)
    assert r["low_confidence"]


def test_exact_beats_variant(db):
    """真库回归：完整 PN 查询时，精确命中必须排在包含变体之前。"""
    r = part_resolver.resolve(db, "ST8000NM000A", limit=5)
    if r["items"]:  # 依赖真库已导数据；空库跳过
        assert r["items"][0]["pn_std"] == "ST8000NM000A"


def test_ambiguous_family(db):
    """口头型号对应多规格变体（V100 16G/32G/PCIE…）→ ambiguous=True 要求消歧。"""
    r = part_resolver.resolve(db, "V100显卡", limit=10)
    if len(r["items"]) >= 2:  # 依赖真库 V100 系列
        assert r["ambiguous"] is True


def test_exact_not_ambiguous(db):
    """敲了完整 PN 精确命中 → 不算歧义，不该反问用户。"""
    r = part_resolver.resolve(db, "ST8000NM000A", limit=5)
    if r["items"] and "PN精确匹配" in r["items"][0]["match_reason"]:
        assert r["ambiguous"] is False
