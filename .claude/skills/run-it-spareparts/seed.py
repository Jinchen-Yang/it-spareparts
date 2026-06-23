"""Seed the dev DB with an admin + sample data so the app has something to drive.

Idempotent: skips if already seeded. Run via the backend's venv so app/tests import:
    cd backend && DATABASE_URL=... uv run python ../.claude/skills/run-it-spareparts/seed.py
(dev-up.sh does this for you.) Creates admin / admin888 + 6 parts with purchases,
sales, inventory (long descriptions/suppliers so the tables have realistic content).
"""
import os
import sys
from pathlib import Path

# make `app` and `tests` importable regardless of cwd
_BACKEND = Path(__file__).resolve().parents[3] / "backend"
sys.path.insert(0, str(_BACKEND))
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://spareparts:spareparts@127.0.0.1:5433/spareparts_dev",
)

from datetime import date, timedelta  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.system import SysImportBatch, SysUser  # noqa: E402
from app.etl import loader  # noqa: E402
from app.services import profit  # noqa: E402
from tests import factories as f  # noqa: E402

PARTS = [
    ("MZ7LH960HAJR-00005", "三星 PM883 960GB 企业级 SATA 2.5寸 固态硬盘 读密集型 V-NAND", "Samsung"),
    ("ST8000NM000A-2KE101", "希捷 Exos 7E8 8TB 7200转 SATA 3.5寸 企业级机械硬盘 垂直磁记录 256MB缓存", "Seagate"),
    ("AOM-S3108L-H8iR", "超微 8口 SAS3 12Gbps RAID 卡 含 2GB 缓存与超级电容 LSI3108 芯片组", "Supermicro"),
    ("UCSC-C240-M5SX", "思科 UCS C240 M5 2U 机架式服务器准系统 24 盘位 SFF 双路可扩展", "Cisco"),
    ("DDR4-2933-32G-RDIMM", "三星 32GB DDR4 2933MHz ECC RDIMM 服务器内存条 双列 RECC", "Samsung"),
    ("PWR-2KW-AC-V2", "戴尔 2000W 铂金级冗余热插拔交流电源模块 适用于 R740/R840", "Dell"),
]
SUPPLIERS = [
    "深圳市鼎芯伟业科技有限公司（华强北一手货源·支持验货）",
    "北京中关村联想阳光雨露信息技术服务有限责任公司",
    "上海浦东张江高科电子元器件批发（个体经营·王经理）",
]


def main() -> None:
    db = SessionLocal()
    if not db.query(SysUser).filter_by(username="admin").first():
        db.add(SysUser(username="admin", role="admin", display_name="管理员",
                       password_hash=hash_password("admin888")))
        db.commit()
        print("  + admin / admin888")

    today = date.today()
    def d(n: int) -> date:
        return today - timedelta(days=n)

    if db.query(SysImportBatch).filter_by(file_hash="seedp").first() is None:
        bp = SysImportBatch(filename="seed_purchase.xlsx", file_type="purchase", file_hash="seedp")
        db.add(bp); db.flush()
        porders, plines = {}, []
        for i, (pn, desc, brand) in enumerate(PARTS):
            for j in range(3):
                oid = f"PO{i}{j}"
                h = f.purchase_head(oid, on=d(2 + i * 3 + j), source_type="采购订单")
                sup = SUPPLIERS[(i + j) % len(SUPPLIERS)]
                h["supplier_name_raw"] = sup
                h["supplier_name_normalized"] = sup
                h["purchaser"] = "刘青青"
                porders[oid] = h
                plines.append(f.purchase_line(oid, f"PL{i}{j}", pn, qty=str(5 + j * 3),
                                              price=str(800 + i * 150 + j * 20),
                                              description=desc, brand=brand))
        loader.load(db, f.purchase_result(porders, plines), bp.id, d(0))

        bs = SysImportBatch(filename="seed_sales.xlsx", file_type="sales", file_hash="seeds")
        db.add(bs); db.flush()
        sorders, slines = {}, []
        for i, (pn, desc, brand) in enumerate(PARTS):
            oid = f"SO{i}"
            sorders[oid] = f.sales_head(oid, on=d(1 + i))
            slines.append(f.sales_line(oid, f"SL{i}", pn, qty=str(2 + i),
                                       price=str(1200 + i * 220), description=desc, brand=brand))
        loader.load(db, f.sales_result(sorders, slines), bs.id, d(0))

        bi = SysImportBatch(filename="seed_inv.xlsx", file_type="inventory", file_hash="seedi")
        db.add(bi); db.flush()
        invrows = [f.inventory_row(f"INV{i}", pn, qty=str(10 + i * 5), description=desc, brand=brand)
                   for i, (pn, desc, brand) in enumerate(PARTS)]
        loader.load(db, f.inventory_result(invrows), bi.id, d(0))
        db.commit()
        print(f"  + {len(PARTS)} parts · 18 purchases · 6 sales · 6 inventory")

    stats = profit.recompute(db)
    db.close()
    print(f"  seed done (sales_lines={stats['sales_lines']})")


if __name__ == "__main__":
    main()
