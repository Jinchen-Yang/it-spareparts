"""库存成本回填与正式采购税口径保持同一规则。"""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.etl import loader
from app.models.inventory import Inventory
from app.models.system import SysImportBatch
from app.services import inventory
from tests import factories as f


def test_backfill_costs_only_explicit_true_is_tax_inclusive(db):
    purchase_batch = SysImportBatch(
        filename="inventory-cost-purchase.xlsx",
        file_type="purchase",
        file_hash="inventory-cost-purchase",
    )
    inventory_batch = SysImportBatch(
        filename="inventory-cost-stock.xlsx",
        file_type="inventory",
        file_hash="inventory-cost-stock",
    )
    db.add_all([purchase_batch, inventory_batch])
    db.flush()

    loader.load(
        db,
        f.purchase_result(
            {
                "P-TRUE": f.purchase_head(
                    "P-TRUE",
                    is_tax_inclusive=True,
                    tax_rate=Decimal("0.06"),
                ),
                "P-FALSE": f.purchase_head(
                    "P-FALSE",
                    is_tax_inclusive=False,
                    tax_rate=Decimal("0.06"),
                ),
                "P-NONE": f.purchase_head(
                    "P-NONE",
                    is_tax_inclusive=None,
                    tax_rate=Decimal("0.99"),
                ),
            },
            [
                f.purchase_line("P-TRUE", "PL-TRUE", "PN-COST-TRUE", price="113"),
                f.purchase_line("P-FALSE", "PL-FALSE", "PN-COST-FALSE", price="113"),
                f.purchase_line("P-NONE", "PL-NONE", "PN-COST-NONE", price="113"),
            ],
        ),
        purchase_batch.id,
        date(2026, 7, 29),
    )
    loader.load(
        db,
        f.inventory_result(
            [
                f.inventory_row("INV-TRUE", "PN-COST-TRUE", qty="2"),
                f.inventory_row("INV-FALSE", "PN-COST-FALSE", qty="2"),
                f.inventory_row("INV-NONE", "PN-COST-NONE", qty="2"),
            ]
        ),
        inventory_batch.id,
        date(2026, 7, 29),
    )
    db.commit()

    inventory.backfill_costs(db)

    rows = {
        row.pn_std: row
        for row in db.scalars(
            select(Inventory).where(
                Inventory.pn_std.in_(
                    ["PN-COST-TRUE", "PN-COST-FALSE", "PN-COST-NONE"]
                )
            )
        )
    }
    assert rows["PN-COST-TRUE"].unit_cost == Decimal("100.00")
    assert rows["PN-COST-TRUE"].inventory_value == Decimal("200.00")
    assert rows["PN-COST-FALSE"].unit_cost == Decimal("113.00")
    assert rows["PN-COST-FALSE"].inventory_value == Decimal("226.00")
    assert rows["PN-COST-NONE"].unit_cost == Decimal("113.00")
    assert rows["PN-COST-NONE"].inventory_value == Decimal("226.00")
