from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import maintenance_migration_warehouse as warehouse


def _movement(**overrides):
    row = {
        "movement_id": "document-1:line-1",
        "document_id": "document-1",
        "line_id": "line-1",
        "document_no": "FH-001",
        "document_date": "2026-08-02",
        "movement_type": "delivery",
        "source": "maintenance_warehouse_v1",
        "source_document_type": "shipment",
        "source_status": "confirmed",
        "formal_available": False,
        "project_id": "project-1",
        "part_id": 1,
        "balance_key": "project-1:1",
        "pn": "PN-001",
        "quantity": "2",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"movement_id": "invented"}, "document_id:line_id"),
        ({"project_id": "project-2"}, "其他项目"),
        ({"part_id": 2}, "balance_key"),
        ({"source": "legacy"}, "confirmed canonical warehouse"),
        ({"source_status": "pending"}, "confirmed canonical warehouse"),
        ({"source_document_type": "receipt"}, "confirmed canonical warehouse"),
        ({"formal_available": True}, "正式可用标记"),
        ({"document_date": "2026-07-31"}, "早于切换日"),
    ],
)
def test_warehouse_contract_rejects_invented_or_mismatched_movements(changes, message):
    with pytest.raises(warehouse.MaintenanceMigrationWarehouseError, match=message):
        warehouse.validate_cutover_inventory_movements(
            [_movement(**changes)],
            cutover_date=date(2026, 8, 1),
            project_id="project-1",
        )


def test_warehouse_contract_accepts_only_exact_document_type_mapping():
    movements = [
        _movement(),
        _movement(
            movement_id="document-2:line-2",
            document_id="document-2",
            line_id="line-2",
            movement_type="available_receipt",
            source_document_type="receipt",
            formal_available=True,
        ),
        _movement(
            movement_id="document-3:line-3",
            document_id="document-3",
            line_id="line-3",
            movement_type="return_registration",
            source_document_type="return",
        ),
    ]

    assert warehouse.validate_cutover_inventory_movements(
        movements,
        cutover_date=date(2026, 8, 1),
        project_id="project-1",
    ) == tuple(movements)


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self.rows)


class _WarehouseSession:
    def __init__(
        self,
        *,
        open_ambiguities=(),
        bad_return_status="warehouse_confirmed",
    ):
        self.open_ambiguities = list(open_ambiguities)
        self.bad_return_status = bad_return_status
        self.documents = [
            {
                "document_id": f"document-{index}",
                "document_type": document_type,
                "document_no": f"WH-{index}",
                "document_date": date(2026, 8, index + 1),
                "normalized_status": "confirmed",
                "first_import_id": "import-1",
                "source_file_hash": "a" * 64,
                "adapter_version": "warehouse-v1",
                "header_signature": "b" * 64,
                "version_state": "known",
                "batch_status": "applied",
            }
            for index, document_type in enumerate(
                ("shipment", "receipt", "return"), start=1
            )
        ]
        self.lines = [
            {
                "line_id": f"line-{index}",
                "document_id": f"document-{index}",
                "pn": "PN-001",
                "sn": None,
                "quantity": Decimal(str(index)),
            }
            for index in range(1, 4)
        ]
        self.links = []
        for index in range(1, 4):
            document_id = f"document-{index}"
            self.links.extend(
                [
                    {
                        "link_id": f"order-link-{index}",
                        "document_id": document_id,
                        "line_id": None,
                        "link_kind": "maintenance_order",
                        "target_type": "maintenance_order",
                        "target_id": "order-1",
                        "status": "active",
                        "version": 1,
                    },
                    {
                        "link_id": f"project-link-{index}",
                        "document_id": document_id,
                        "line_id": None,
                        "link_kind": "project",
                        "target_type": "maintenance_project",
                        "target_id": "project-1",
                        "status": "active",
                        "version": 1,
                    },
                    {
                        "link_id": f"part-link-{index}",
                        "document_id": document_id,
                        "line_id": f"line-{index}",
                        "link_kind": "part",
                        "target_type": "dim_part",
                        "target_id": "1",
                        "status": "active",
                        "version": 1,
                    },
                ]
            )
            if index in {2, 3}:
                self.links.append(
                    {
                        "link_id": f"return-link-{index}",
                        "document_id": document_id,
                        "line_id": None,
                        "link_kind": "bad_return",
                        "target_type": "maintenance_bad_return",
                        "target_id": "bad-return-1",
                        "status": "active",
                        "version": 1,
                    }
                )

    def scalar(self, statement, params=None):
        sql = str(statement)
        if "SELECT project_id FROM maintenance_project" in sql:
            return "project-1"
        raise AssertionError(f"unexpected scalar query: {sql}")

    def scalars(self, statement, params=None):
        sql = str(statement)
        if "SELECT DISTINCT document.document_id" in sql:
            return _Result([row["document_id"] for row in self.documents])
        if "maintenance_warehouse_ambiguity" in sql:
            return _Result(self.open_ambiguities)
        raise AssertionError(f"unexpected scalars query: {sql}")

    def execute(self, statement, params=None):
        sql = str(statement)
        if "FROM maintenance_warehouse_document AS document" in sql:
            return _Result(self.documents)
        if "FROM maintenance_warehouse_document_line" in sql:
            return _Result(self.lines)
        if "FROM maintenance_warehouse_document_link" in sql:
            return _Result(self.links)
        if "FROM maintenance_source_order_assignment" in sql:
            return _Result(
                [
                    {
                        "assignment_id": "assignment-1",
                        "source_order_id": "order-1",
                        "project_id": "project-1",
                        "version": 4,
                    }
                ]
            )
        if "FROM dim_part" in sql:
            return _Result([SimpleNamespace(id=1, pn_std="PN-001")])
        if "FROM maintenance_bad_return" in sql:
            return _Result(
                [
                    {
                        "return_id": "bad-return-1",
                        "project_id": "project-1",
                        "status": self.bad_return_status,
                        "version": 3,
                    }
                ]
            )
        raise AssertionError(f"unexpected execute query: {sql}")


def test_formal_bridge_maps_201_and_209_evidence_without_inference(monkeypatch):
    db = _WarehouseSession()
    monkeypatch.setattr(warehouse, "_contracts_available", lambda _db: True)
    monkeypatch.setattr(warehouse, "_lock_warehouse_snapshot", lambda _db: None)

    movements, ready = warehouse.load_project_inventory_movements(
        db, "project-1", date(2026, 8, 1)
    )

    assert ready is True
    assert [row["movement_type"] for row in movements] == [
        "delivery",
        "available_receipt",
        "return_registration",
    ]
    assert all(row["balance_key"] == "project-1:1" for row in movements)
    assert all(row["source_assignment_version"] == 4 for row in movements)
    assert movements[1]["bad_return_id"] == "bad-return-1"
    assert movements[1]["bad_return_status"] == "warehouse_confirmed"
    assert movements[1]["bad_return_version"] == 3
    assert movements[1]["formal_available"] is True
    assert movements[2]["formal_available"] is False


def test_formal_bridge_fails_closed_on_open_warehouse_ambiguity(monkeypatch):
    db = _WarehouseSession(open_ambiguities=["document-1"])
    monkeypatch.setattr(warehouse, "_contracts_available", lambda _db: True)
    monkeypatch.setattr(warehouse, "_lock_warehouse_snapshot", lambda _db: None)

    movements, ready = warehouse.load_project_inventory_movements(
        db, "project-1", date(2026, 8, 1)
    )

    assert movements == ()
    assert ready is False


def test_receipt_is_not_available_before_bad_return_is_warehouse_confirmed(
    monkeypatch,
):
    db = _WarehouseSession(bad_return_status="in_transit")
    monkeypatch.setattr(warehouse, "_contracts_available", lambda _db: True)
    monkeypatch.setattr(warehouse, "_lock_warehouse_snapshot", lambda _db: None)

    movements, ready = warehouse.load_project_inventory_movements(
        db, "project-1", date(2026, 8, 1)
    )

    assert movements == ()
    assert ready is False
