"""通用号池重算：已生效双向互替连通分量成池、单向/pending不成池、
稳定 group_id 复用、合并/拆分报告、关系待校准、超限、dry_run 预览。"""
import pytest
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.inventory import PartPool, PartPoolMember, PartSubstitute
from app.services import pool
from app import config


def _part(db, pn):
    p = DimPart(pn_std=pn)
    db.add(p); db.flush()
    return p.id


def _edge(db, a, b, *, status="active", direction="both", stype="same_spec"):
    lo, hi = (a, b) if a < b else (b, a)
    db.add(PartSubstitute(part_id_a=lo, part_id_b=hi, status=status,
                          direction=direction, substitute_type=stype))


@pytest.fixture()
def parts(db):
    ids = {n: _part(db, f"PN-{n}") for n in "ABCDEFGHIJ"}
    db.flush()
    return ids


def test_components_only_active_both(db, parts):
    p = parts
    _edge(db, p["A"], p["B"]); _edge(db, p["B"], p["C"])   # 池1 {A,B,C}
    _edge(db, p["D"], p["E"])                                # 池2 {D,E}
    _edge(db, p["F"], p["G"], direction="a_to_b")           # 单向 → 不成池
    _edge(db, p["H"], p["I"], status="pending")             # 未审核 → 不成池
    db.commit()

    r = pool.rebuild(db)
    assert r["pools"] == 2
    assert r["parts_pooled"] == 5
    members = dict(db.execute(select(PartPoolMember.part_id, PartPoolMember.group_id)).all())
    # A,B,C 同组；D,E 同组；F,G,H,I,J 不在任何池
    assert members[p["A"]] == members[p["B"]] == members[p["C"]]
    assert members[p["D"]] == members[p["E"]]
    assert members[p["A"]] != members[p["D"]]
    for n in "FGHIJ":
        assert p[n] not in members


def test_stable_id_and_merge(db, parts):
    p = parts
    _edge(db, p["A"], p["B"]); _edge(db, p["D"], p["E"])
    db.commit()
    r1 = pool.rebuild(db)
    m1 = dict(db.execute(select(PartPoolMember.part_id, PartPoolMember.group_id)).all())
    gid_ab, gid_de = m1[p["A"]], m1[p["D"]]

    # 加一条 B-D 边 → 两池合并为一个分量
    _edge(db, p["B"], p["D"]); db.commit()
    r2 = pool.rebuild(db)
    assert r2["pools"] == 1
    m2 = dict(db.execute(select(PartPoolMember.part_id, PartPoolMember.group_id)).all())
    # 合并后所有成员同组，且复用了较大/较小旧 ID 之一（稳定），报告 merged
    assert len({m2[p[n]] for n in "ABDE"}) == 1
    assert r2["merged"], "应报告合并"
    into = r2["merged"][0]["into"]
    assert into in (gid_ab, gid_de)
    assert set(r2["merged"][0]["from"]) == {gid_ab, gid_de} - {into}


def test_split_report(db, parts):
    p = parts
    _edge(db, p["A"], p["B"]); _edge(db, p["B"], p["C"])   # {A,B,C}
    db.commit()
    r1 = pool.rebuild(db)
    gid = dict(db.execute(select(PartPoolMember.part_id, PartPoolMember.group_id)).all())[p["A"]]

    # 删 B-C，加 D-E：{A,B} 与 {C 独立不成池}，另出新池 {D,E}
    db.query(PartSubstitute).filter_by(
        part_id_a=min(p["B"], p["C"]), part_id_b=max(p["B"], p["C"])).delete()
    _edge(db, p["D"], p["E"]); db.commit()
    r2 = pool.rebuild(db)
    # {A,B} 保留原 gid（重叠最多），C 掉出池
    m2 = dict(db.execute(select(PartPoolMember.part_id, PartPoolMember.group_id)).all())
    assert m2[p["A"]] == m2[p["B"]] == gid
    assert p["C"] not in m2


def test_needs_calibration_and_oversized(db, parts, monkeypatch):
    p = parts
    _edge(db, p["A"], p["B"], stype=None)   # 缺类型 → 该池待校准
    _edge(db, p["D"], p["E"])
    db.commit()
    monkeypatch.setattr(config, "POOL_OVERSIZE_MEMBERS", 1)  # 阈值调低，两池都超
    r = pool.rebuild(db)
    ab = dict(db.execute(select(PartPoolMember.part_id, PartPoolMember.group_id)).all())[p["A"]]
    assert ab in r["needs_calibration"]
    assert len(r["oversized"]) == 2
    row = db.get(PartPool, ab)
    assert row.needs_calibration is True and row.oversized is True


def test_dry_run_no_write(db, parts):
    p = parts
    _edge(db, p["A"], p["B"]); db.commit()
    r = pool.rebuild(db, dry_run=True)
    assert r["dry_run"] is True and r["pools"] == 1
    assert db.execute(select(PartPoolMember)).first() is None   # 未落库


def test_retired_id_never_reused(db, parts):
    """复审 P0-2：池退役后其 group_id 永不被无关新池复用（持久序列，非 max+1）。"""
    p = parts
    _edge(db, p["A"], p["B"])       # 池①
    _edge(db, p["D"], p["E"])       # 池②
    db.commit()
    pool.rebuild(db)
    m1 = dict(db.execute(select(PartPoolMember.part_id, PartPoolMember.group_id)).all())
    gid_de = m1[p["D"]]             # 池② 的 ID

    # 删掉 D-E 边 → 池② 退役；另加一个完全无关的新池 F-G
    db.query(PartSubstitute).filter_by(
        part_id_a=min(p["D"], p["E"]), part_id_b=max(p["D"], p["E"])).delete()
    _edge(db, p["F"], p["G"]); db.commit()
    pool.rebuild(db)
    m2 = dict(db.execute(select(PartPoolMember.part_id, PartPoolMember.group_id)).all())
    gid_fg = m2[p["F"]]
    assert p["D"] not in m2                 # 池② 已退役
    assert gid_fg != gid_de, "退役 ID 绝不能被无关新池复用"


# 迁移对齐场景（空表→1 / max=40→41 / 高水位保持）的测试在 tests/test_migration_pool_seq.py：
# 走真实 Alembic downgrade/upgrade 并保存/恢复序列状态。此前这里有两个复制迁移 SQL 的
# 手动版（复审五轮：直接 ALTER SEQUENCE/setval 且不恢复 → 污染序列状态），已删除。
