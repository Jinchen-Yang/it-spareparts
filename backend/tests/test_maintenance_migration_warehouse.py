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
        active_candidate_projects=("project-1",),
    ):
        self.open_ambiguities = list(open_ambiguities)
        self.ambiguity_rows = [
            {
                "ambiguity_id": f"ambiguity-{index}",
                "import_id": "import-1",
                "document_id": document_id,
                "line_id": f"line-{document_id.rsplit('-', 1)[-1]}",
                "ambiguity_type": "field_conflict",
                "field_code": "maintenance_order",
                "source_row": 3,
                "value_hash": "c" * 64,
                "candidates_json": [
                    {
                        "target_type": "maintenance_project",
                        "target_id": "project-1",
                        "label": "合成候选项目",
                    }
                ],
                "fingerprint": f"{index:064x}",
                "status": "open",
                "version": 1,
                "document_no": f"WH-{document_id.rsplit('-', 1)[-1]}",
                "document_date": date(2026, 8, 2),
            }
            for index, document_id in enumerate(self.open_ambiguities, start=1)
        ]
        self.bad_return_status = bad_return_status
        self.active_candidate_projects = set(active_candidate_projects)
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
        if "WHERE is_active IS TRUE AND project_id IN" in sql:
            return _Result(
                sorted(set(params["project_ids"]) & self.active_candidate_projects)
            )
        if "SELECT DISTINCT document.document_id" in sql:
            return _Result([row["document_id"] for row in self.documents])
        if "maintenance_warehouse_ambiguity" in sql:
            return _Result(
                [
                    row["document_id"]
                    for row in self.ambiguity_rows
                    if row["document_id"] is not None
                ]
            )
        raise AssertionError(f"unexpected scalars query: {sql}")

    def execute(self, statement, params=None):
        sql = str(statement)
        if "FROM maintenance_warehouse_ambiguity AS ambiguity" in sql:
            return _Result(self.ambiguity_rows)
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

    movements, ready, ambiguities = warehouse.load_project_inventory_movements(
        db, "project-1", date(2026, 8, 1), date(2026, 8, 10)
    )

    assert ready is True
    assert ambiguities == ()
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

    movements, ready, ambiguities = warehouse.load_project_inventory_movements(
        db, "project-1", date(2026, 8, 1), date(2026, 8, 10)
    )

    assert movements == ()
    assert ready is False
    assert ambiguities[0]["ambiguity_id"] == "ambiguity-1"
    assert ambiguities[0]["document_id"] == "document-1"
    assert ambiguities[0]["ambiguity_type"] == "field_conflict"
    assert ambiguities[0]["fingerprint"] == f"{1:064x}"


def test_unassigned_open_ambiguity_is_a_global_explainable_blocker(monkeypatch):
    db = _WarehouseSession()
    db.ambiguity_rows = [
        {
            "ambiguity_id": "ambiguity-global",
            "import_id": "import-1",
            "document_id": None,
            "line_id": None,
            "ambiguity_type": "missing_stable_link",
            "field_code": "maintenance_order",
            "source_row": 3,
            "value_hash": "d" * 64,
            "candidates_json": [],
            "fingerprint": "e" * 64,
            "status": "open",
            "version": 1,
            "document_no": None,
            "document_date": None,
        }
    ]
    monkeypatch.setattr(warehouse, "_contracts_available", lambda _db: True)
    monkeypatch.setattr(warehouse, "_lock_warehouse_snapshot", lambda _db: None)

    movements, ready, ambiguities = warehouse.load_project_inventory_movements(
        db, "project-1", date(2026, 8, 1), date(2026, 8, 10)
    )

    assert movements == ()
    assert ready is False
    assert ambiguities == [
        {
            "ambiguity_id": "ambiguity-global",
            "import_id": "import-1",
            "document_id": None,
            "line_id": None,
            "document_no": None,
            "document_date": None,
            "ambiguity_type": "missing_stable_link",
            "field_code": "maintenance_order",
            "source_row": 3,
            "value_hash": "d" * 64,
            "candidates": [],
            "fingerprint": "e" * 64,
            "status": "open",
            "version": 1,
            "scope": "global_unresolved",
            "scope_project_ids": [],
            "scope_reason": "candidate_does_not_prove_unique_active_project",
        }
    ]


@pytest.mark.parametrize(
    ("candidate", "active_projects"),
    [
        ({"target_type": "dim_part", "target_id": "1"}, ("project-1",)),
        (
            {"target_type": "maintenance_bad_return", "target_id": "return-1"},
            ("project-1",),
        ),
        ({"target_type": "future_target", "target_id": "future-1"}, ("project-1",)),
        (
            {"target_type": "maintenance_project", "target_id": "inactive-project"},
            ("project-1",),
        ),
    ],
)
def test_non_project_or_inactive_candidate_remains_globally_blocking(
    monkeypatch, candidate, active_projects
):
    db = _WarehouseSession(active_candidate_projects=active_projects)
    db.ambiguity_rows = [
        {
            "ambiguity_id": "ambiguity-unscoped-candidate",
            "import_id": "import-1",
            "document_id": None,
            "line_id": "line-unassigned",
            "ambiguity_type": "missing_stable_link",
            "field_code": "project",
            "source_row": 3,
            "value_hash": "d" * 64,
            "candidates_json": [candidate],
            "fingerprint": "f" * 64,
            "status": "open",
            "version": 1,
            "document_no": "WH-UNASSIGNED",
            "document_date": date(2026, 8, 2),
        }
    ]
    monkeypatch.setattr(warehouse, "_contracts_available", lambda _db: True)
    monkeypatch.setattr(warehouse, "_lock_warehouse_snapshot", lambda _db: None)

    movements, ready, ambiguities = warehouse.load_project_inventory_movements(
        db, "project-1", date(2026, 8, 1), date(2026, 8, 10)
    )

    assert movements == ()
    assert ready is False
    assert ambiguities[0]["ambiguity_id"] == "ambiguity-unscoped-candidate"
    assert ambiguities[0]["candidates"][0]["target_type"] == candidate["target_type"]
    assert ambiguities[0]["scope"] == "global_unresolved"
    assert ambiguities[0]["scope_reason"] == (
        "candidate_does_not_prove_unique_active_project"
    )


def test_receipt_is_not_available_before_bad_return_is_warehouse_confirmed(
    monkeypatch,
):
    db = _WarehouseSession(bad_return_status="in_transit")
    monkeypatch.setattr(warehouse, "_contracts_available", lambda _db: True)
    monkeypatch.setattr(warehouse, "_lock_warehouse_snapshot", lambda _db: None)

    movements, ready, ambiguities = warehouse.load_project_inventory_movements(
        db, "project-1", date(2026, 8, 1), date(2026, 8, 10)
    )

    assert movements == ()
    assert ready is False
    assert ambiguities == ()


def test_empty_warehouse_relation_requires_explicit_completeness_watermark(
    monkeypatch,
):
    db = _WarehouseSession()
    db.documents = []
    db.lines = []
    db.links = []
    monkeypatch.setattr(warehouse, "_contracts_available", lambda _db: True)
    monkeypatch.setattr(warehouse, "_lock_warehouse_snapshot", lambda _db: None)

    movements, ready, ambiguities = warehouse.load_project_inventory_movements(
        db, "project-1", date(2026, 8, 1)
    )
    confirmed_movements, confirmed_ready, confirmed_ambiguities = (
        warehouse.load_project_inventory_movements(
            db, "project-1", date(2026, 8, 1), date(2026, 8, 10)
        )
    )

    assert movements == ()
    assert ready is False
    assert ambiguities == ()
    assert confirmed_movements == ()
    assert confirmed_ready is True
    assert confirmed_ambiguities == ()
