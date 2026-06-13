"""标准化改名服务：别名同改 / 旧名召回 / 目标占用转候选 / 前置校验（设计 §5、§8C 验收）。"""
from datetime import date

import pytest
from sqlalchemy import delete, select

from app.etl import loader
from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import ProductMatchCandidate
from app.models.system import SysAuditLog, SysImportBatch
from app.services import part_rename, part_resolver
from tests import factories as f

NEW = "MZ7LH960HAJR-00005"   # 厂商规范写法（目标名）


@pytest.fixture()
def seeded(db):
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="h1")
    db.add(b)
    db.flush()
    orders = {"O1": f.purchase_head("O1", on=date(2026, 1, 10))}
    lines = [f.purchase_line("O1", "L1", "MZ-7LH960", qty="2", price="50", description="三星SSD"),
             f.purchase_line("O1", "L2", "OTHER-PART", qty="1", price="10")]
    loader.load(db, f.purchase_result(orders, lines), b.id, date(2026, 6, 1))
    db.commit()
    return b


def _part(db, pn):
    return db.scalar(select(DimPart).where(DimPart.pn_std == pn))


def test_rename_basic_alias_follow_and_compact(db, seeded):
    pid = _part(db, "MZ-7LH960").id
    # 预置一条已有别名(原值写法),验证改名时复合外键带着别名 pn_std 同改
    db.add(PartAlias(pn_raw="mz 7lh960", pn_std="MZ-7LH960", part_id=pid,
                     source="auto", status="active"))
    db.commit()

    res = part_rename.rename_part(db, pid, NEW, "厂商规范写法", "tester")
    assert res["renamed"] is True and res["old_pn"] == "MZ-7LH960" and res["new_pn"] == NEW

    db.expire_all()
    p = db.get(DimPart, pid)
    assert p.pn_std == NEW
    assert p.pn_compact == "MZ7LH960HAJR00005", "pn_compact 由库自动重算(去非字母数字+大写)"
    assert NEW in (p.search_doc or ""), "search_doc 由库自动重算"

    # 已有别名的 pn_std 跟着改（复合外键未报错 + 值正确即证明同改）
    a = db.scalar(select(PartAlias).where(PartAlias.pn_raw == "mz 7lh960"))
    assert a.pn_std == NEW
    # 旧名→新名别名：已有恒等别名(loader 建,source=auto)跟着改到新名，旧名照常召回到新规范名
    # （若旧名原本没有恒等别名，rename 会补一条 source='rename'；此处恰好已有,故 source 仍为 auto）
    old_alias = db.scalar(select(PartAlias).where(PartAlias.pn_raw == "MZ-7LH960"))
    assert old_alias is not None and old_alias.pn_std == NEW, "旧名经别名指向新规范名"
    # 审计
    log = db.scalar(select(SysAuditLog).where(SysAuditLog.action == "rename"))
    assert log.before_json["pn_std"] == "MZ-7LH960" and log.after_json["pn_std"] == NEW


def test_rename_old_name_resolves_via_alias(db, seeded):
    pid = _part(db, "MZ-7LH960").id
    part_rename.rename_part(db, pid, NEW, None, "t")
    db.expire_all()
    # 旧名经别名召回仍命中改后的规范名（resolver 模糊路）
    hits = {it["pn_std"] for it in part_resolver.resolve(db, "MZ-7LH960")["items"]}
    assert NEW in hits, "旧名应经 rename 别名召回到新规范名"


def test_rename_inserts_alias_when_old_name_had_none(db, seeded):
    pid = _part(db, "MZ-7LH960").id
    db.execute(delete(PartAlias).where(PartAlias.pn_raw == "MZ-7LH960"))  # 模拟旧名无恒等别名
    db.commit()
    res = part_rename.rename_part(db, pid, NEW, None, "t")
    assert res["renamed"] is True and res["rename_alias_inserted"] is True
    a = db.scalar(select(PartAlias).where(PartAlias.pn_raw == "MZ-7LH960"))
    assert a is not None and a.pn_std == NEW and a.source == "rename" and a.status == "active"


def test_rename_target_occupied_converts_to_candidate(db, seeded):
    src = _part(db, "MZ-7LH960")
    tgt = _part(db, "OTHER-PART")
    res = part_rename.rename_part(db, src.id, "OTHER-PART", None, "t")
    assert res["renamed"] is False and res["occupied_by_part_id"] == tgt.id

    cand = db.scalar(select(ProductMatchCandidate)
                     .where(ProductMatchCandidate.source_part_id == src.id))
    assert cand is not None and cand.candidate_part_id == tgt.id and cand.status == "pending"
    db.expire_all()
    assert db.get(DimPart, src.id).pn_std == "MZ-7LH960", "占用时源名不变"


def test_rename_rejections(db, seeded):
    pid = _part(db, "MZ-7LH960").id
    with pytest.raises(part_rename.RenameError):      # 空名
        part_rename.rename_part(db, pid, "   ", None, "t")
    with pytest.raises(part_rename.RenameError):      # 同名
        part_rename.rename_part(db, pid, "MZ-7LH960", None, "t")
    with pytest.raises(part_rename.RenameError):      # 不存在
        part_rename.rename_part(db, 999999, "X", None, "t")
