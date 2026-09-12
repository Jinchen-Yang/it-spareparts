"""业务类型的写入侧：建项时落库 + 未标注可人工补录（2026-09-08）。

生产取证：648 个项目里 647 个 `maintenance_project.business_type` 是 NULL。根因是这个
字段在 app/ 里只有台账导入会写，而台账尚未进生产；另外三条建项路径全部留空。其中最刺眼
的是 XSDD 销售订单建项——D-05 认定的**唯一正规建项来源**：业务类型已经从源表解析出来、
也放进了建项元数据，但调 `catalog.create_project` 时**没传**，值就地丢弃。

于是卡墙的业务类型筛选在生产上一个项目都筛不出来。补两处：

1. **建项落库**：`create_project` 接受 business_type，XSDD 导入把已解析出来的值传进去。
   此后新建的项目自带业务类型，筛选器才不会永远只有「未标注」一档可用。
2. **人工补录**：`update_project` 的可改字段白名单加上 business_type。否则 647 个存量
   项目在界面上**无法自救**——用户看到「未标注 647 个」也无从下手。

口径：不猜。源表没填就是 None，不按项目名或业务类型关键字倒推（「业务类型只作分类、
维保业务=是 才是建项依据」，2026-09-03 拍板 / D-05）。
"""

import uuid
from datetime import date

import pytest

from app.services import maintenance_project_catalog as catalog
from app.services.maintenance_boss_board import business_type_code


def _create(db, **overrides) -> dict:
    payload = {
        "project_code": f"BT-{uuid.uuid4().hex[:8]}",
        "display_name": f"业务类型写入测试-{uuid.uuid4().hex[:6]}",
        "project_manager_id": None,
        "reason": "契约测试建项",
        "operated_by": "business-type-test",
    }
    payload.update(overrides)
    return catalog.create_project(db, **payload)


# ---------- 建项落库 ----------

def test_create_project_records_business_type(db):
    created = _create(db, business_type="整体维保")
    db.commit()

    from app.models.maintenance_project import MaintenanceProject

    assert created["business_type"] == "整体维保"
    project = db.get(MaintenanceProject, created["project_id"])
    assert project.business_type == "整体维保"
    assert business_type_code(project.business_type) == "overall"


def test_create_project_without_business_type_stays_unlabeled(db):
    """不猜：源表没填就是空，落进「未标注」那一档，不按项目名倒推。"""

    created = _create(db, display_name=f"某某整体维保项目-{uuid.uuid4().hex[:6]}")
    db.commit()

    assert created["business_type"] is None
    assert business_type_code(created["business_type"]) == "unlabeled"


def test_create_project_rejects_overlong_business_type(db):
    """business_type 是 String(16)，超长必须在应用层拒绝而不是撞数据库。"""

    with pytest.raises(catalog.MaintenanceProjectCatalogError):
        _create(db, business_type="整" * 20)


# ---------- 人工补录 ----------

def test_update_project_can_fill_in_a_missing_business_type(db):
    """647 个存量项目的自救通道。"""

    created = _create(db)
    db.commit()
    assert created["business_type"] is None

    updated = catalog.update_project(
        db,
        project_id=created["project_id"],
        version=created["version"],
        updates={"business_type": "算力运维"},
        reason="补录业务类型",
        operated_by="business-type-test",
    )
    db.commit()

    assert updated["business_type"] == "算力运维"
    assert updated["version"] == created["version"] + 1


def test_update_project_can_clear_business_type(db):
    """填错了要能改回未标注，不能只进不出。"""

    created = _create(db, business_type="整体维保")
    db.commit()
    updated = catalog.update_project(
        db,
        project_id=created["project_id"],
        version=created["version"],
        updates={"business_type": ""},
        reason="填错了，清空",
        operated_by="business-type-test",
    )
    db.commit()
    assert updated["business_type"] is None


def test_update_project_business_type_is_audited(db):
    """改经营口径要留痕：审计里能看到改前改后。"""

    from sqlalchemy import select

    from app.models.maintenance_project import MaintenanceProjectAuditLog

    created = _create(db)
    db.commit()
    catalog.update_project(
        db,
        project_id=created["project_id"],
        version=created["version"],
        updates={"business_type": "备件维保"},
        reason="补录业务类型",
        operated_by="business-type-test",
    )
    db.commit()

    audit = db.scalars(
        select(MaintenanceProjectAuditLog)
        .where(
            MaintenanceProjectAuditLog.entity_id == created["project_id"],
            MaintenanceProjectAuditLog.action == "update",
        )
        .order_by(MaintenanceProjectAuditLog.id.desc())
    ).first()
    assert audit is not None
    assert (audit.after_json or {}).get("business_type") == "备件维保"
    assert (audit.before_json or {}).get("business_type") is None


def test_update_project_still_rejects_unknown_fields(db):
    """白名单没被顺手放开。"""

    created = _create(db)
    db.commit()
    with pytest.raises(catalog.MaintenanceProjectCatalogError):
        catalog.update_project(
            db,
            project_id=created["project_id"],
            version=created["version"],
            updates={"lifecycle_status": "ongoing"},
            reason="试图改不该改的",
            operated_by="business-type-test",
        )


# ---------- XSDD 建项把解析出来的值传下去 ----------

def test_sales_order_import_creates_project_with_business_type(db):
    """D-05 唯一正规建项来源：源表业务类型必须落到项目主档上。"""

    from io import BytesIO

    from openpyxl import Workbook
    from sqlalchemy import select

    from app.models.maintenance_project import MaintenanceProject
    from app.services import maintenance_bulk_import as bulk

    workbook = Workbook()
    sheet = workbook.active
    sheet.append([
        "SeqNo", "ObjectId", "Status", "F0000118", "F0000059",
        "F0000119", "F0000134", "F0000131", "F0000132", "F0000021", "F0000053",
        "F0000054", "F0000055", "F0000056",
    ])
    sheet.append([
        "订单编号(必填)", "数据ID(不可修改)", "数据状态", "维保业务", "业务类型#",
        "项目名称(必填)", "项目经理(必填)", "维保起始日期(必填)", "维保终止日期(必填)",
        "订单金额", "是否含税(必填)", "税率(必填)", "税金", "不含税金额",
    ])
    project_name = f"业务类型建项测试-{uuid.uuid4().hex[:6]}"
    sheet.append([
        "XSDD-20260908-0001", f"raw-{uuid.uuid4()}", "已生效", "是", "算力运维",
        project_name, "张三", date(2026, 1, 1), date(2026, 12, 31),
        "113000", "含税", "0.13", "13000", "100000",
    ])
    buffer = BytesIO()
    workbook.save(buffer)

    preview = bulk.preview_transfer(
        db, [("sales.xlsx", buffer.getvalue())], operated_by="business-type-test")
    row_keys = [row["row_key"] for row in preview["rows"] if row["row_status"] == "ready"]
    assert row_keys, preview["rows"]
    bulk.apply_transfer(
        db,
        preview_token=preview["preview_token"],
        payload_hash=preview["payload_hash"],
        data_version=preview["data_version"],
        row_keys=row_keys,
        operated_by="business-type-test",
        real_operator=True,
    )
    db.flush()

    project = db.scalars(
        select(MaintenanceProject)
        .where(MaintenanceProject.display_name == project_name)
    ).first()
    assert project is not None, "没建出项目，夹具或建项条件不对"
    assert project.business_type == "算力运维", (
        f"源表业务类型没落到项目上：{project.business_type!r}")
