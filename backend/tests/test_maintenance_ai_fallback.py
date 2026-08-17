"""AI 兜底列映射测试（C3）：单步提案 + 封闭目录校验 + 确定性执行。"""

import io

import pytest
from openpyxl import Workbook
from sqlalchemy import text

from app.services import maintenance_ai_fallback as ai
from app.services import maintenance_doc_import as docs


def _variant_return_workbook() -> bytes:
    """列名漂移的返库单：真实表头被改写（AI 兜底的目标场景）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["F0000001"])
    ws.append(
        ["返库类别", "退回日期", "项目", "销售合同号", "需求单", "状态",
         "自贴码", "料号", "SN", "检测结果", "库位", "数量", "行号"]
    )
    ws.append(
        ["其他返库", "2026-08-03", "AI项目", "XSDD-20260203-0029",
         "WBDD-20260203-0026", "已生效", "", "02311AYV", "", "成品", "", "2", "L-1"]
    )
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _cleanup_proposals(db):
    yield
    db.execute(text("DELETE FROM maintenance_ai_mapping_proposal"))
    db.commit()


def _fake_llm(prompt: str) -> str:
    import json

    mapping = {
        "返库类别": "返库类别",
        "退回日期": "返库日期",
        "项目": "项目名称",
        "销售合同号": "维保销售订单",
        "需求单": "维保需求单(备件)",
        "状态": "数据状态",
        "料号": "备件明细.备件PN",
        "检测结果": "备件明细.备件测试结果",
        "数量": "备件明细.返库数量",
        "行号": "备件明细.序号",
    }
    return json.dumps({"column_mapping": mapping, "notes": ["变体表头映射"]})


def test_catalog_is_closed():
    catalog = ai.canonical_catalog("return_order")
    assert "返库类别" in catalog
    assert "备件明细.返库数量" in catalog
    # 目录外字段必须被拒绝
    with pytest.raises(ai.AIProposalInvalid):
        ai._validate_proposal(
            "return_order",
            {"column_mapping": {"料号": "不存在的字段"}},
        )


def test_propose_with_fake_llm_and_trial_parse(db):
    data = _variant_return_workbook()
    headers = ["返库类别", "退回日期", "项目", "销售合同号", "需求单", "状态",
               "自贴码", "料号", "SN", "检测结果", "库位", "数量", "行号"]
    samples = [["其他返库", "2026-08-03", "AI项目", "XSDD-20260203-0029", "", "已生效",
                "", "02311AYV", "", "成品", "", "2", "L-1"]]
    result = ai.propose(
        db,
        doc_type="return_order",
        data=data,
        filename="变体返库单.xlsx",
        headers=headers,
        samples=samples,
        operated_by="合成管理员",
        llm_call=_fake_llm,
    )
    assert result["trial_error"] is None
    assert result["trial_counts"]["lines"] == 1
    assert result["proposal_id"]
    # 原生解析器（无映射）必须失败——证明这是兜底路径
    with pytest.raises(docs.DocParseError):
        docs.parse_doc_workbook("return_order", data, "变体返库单.xlsx")


def test_propose_rejects_unknown_field(db):
    def bad_llm(prompt: str) -> str:
        import json

        return json.dumps({"column_mapping": {"料号": "发明的新字段"}})

    data = _variant_return_workbook()
    with pytest.raises(ai.AIProposalInvalid):
        ai.propose(
            db,
            doc_type="return_order",
            data=data,
            filename="x.xlsx",
            headers=["料号"],
            samples=[["02311AYV"]],
            operated_by="合成管理员",
            llm_call=bad_llm,
        )


def test_prompt_is_deterministic():
    headers = ["料号", "数量"]
    samples = [["A", "1"]]
    prompt1, hash1 = ai.build_prompt("return_order", headers, samples)
    prompt2, hash2 = ai.build_prompt("return_order", headers, samples)
    assert hash1 == hash2
    assert prompt1 == prompt2


def test_accept_proposal_uses_standard_preview(db):
    data = _variant_return_workbook()
    headers = ["返库类别", "退回日期", "项目", "销售合同号", "需求单", "状态",
               "自贴码", "料号", "SN", "检测结果", "库位", "数量", "行号"]
    samples = [["其他返库", "2026-08-03", "AI项目", "XSDD-20260203-0029", "", "已生效",
                "", "02311AYV", "", "成品", "", "2", "L-1"]]
    result = ai.propose(
        db,
        doc_type="return_order",
        data=data,
        filename="变体返库单.xlsx",
        headers=headers,
        samples=samples,
        operated_by="合成管理员",
        llm_call=_fake_llm,
    )
    batch_id = ai.accept_proposal(
        db,
        proposal_id=result["proposal_id"],
        data=data,
        filename="变体返库单.xlsx",
        operated_by="合成管理员",
        idempotency_key="ai-accept-test-key-0001",
    )
    assert batch_id
    from app.models.maintenance_doc_import import MaintenanceDocHeadRow

    head = db.query(MaintenanceDocHeadRow).filter_by(batch_id=batch_id).one()
    # 变体表头经 AI 映射后按标准语义落库；无返库单号/数据ID列 → 单据编号缺失 issue
    assert head.category == "其他返库"
    assert head.xsdd_no == "XSDD-20260203-0029"
    assert head.wbdd_no == "WBDD-20260203-0026"


def test_accept_wrong_file_rejected(db):
    data = _variant_return_workbook()
    headers = ["料号"]
    samples = [["02311AYV"]]
    result = ai.propose(
        db,
        doc_type="return_order",
        data=data,
        filename="x.xlsx",
        headers=headers,
        samples=samples,
        operated_by="合成管理员",
        llm_call=_fake_llm,
    )
    other = _variant_return_workbook()
    # 追加一个字节制造哈希不一致
    tampered = other + b"\x00"
    with pytest.raises(ai.AIProposalError):
        ai.accept_proposal(
            db,
            proposal_id=result["proposal_id"],
            data=tampered,
            filename="x.xlsx",
            operated_by="合成管理员",
            idempotency_key="ai-accept-test-key-0002",
        )


def test_ai_unavailable_when_not_configured(monkeypatch):
    def fake_configured() -> bool:
        return False

    monkeypatch.setattr("app.services.maintenance_ai_fallback.llm_provider.is_configured", fake_configured)
    with pytest.raises(ai.AIUnavailable):
        ai.call_llm_for_mapping("return_order", ["料号"], [["A"]])


def test_validate_proposal_rejects_duplicate_canonical():
    """两个源列映射同一 canonical 字段必须 fail-closed（round-4 Blocker 4）。"""
    with pytest.raises(ai.AIProposalInvalid, match="同一目标字段"):
        ai._validate_proposal(
            "return_order",
            {
                "column_mapping": {
                    "料号": "备件明细.备件PN",
                    "PN列": "备件明细.备件PN",
                }
            },
        )


def test_parser_accepts_file_gate():
    from tests.test_maintenance_doc_import import (
        _RETURN_HEAD,
        _RETURN_LINE,
        _doc_workbook,
    )

    # 漂移表头 → 主解析器失败 → 允许走 AI
    ok, error = ai.parser_accepts_file(
        "return_order", _variant_return_workbook(), "返库单.xlsx"
    )
    assert ok is False
    assert error

    # 标准表头 → 主解析器成功 → 不允许走 AI
    standard = _doc_workbook(
        sheet_title="Sheet1",
        head_headers=_RETURN_HEAD,
        line_headers=_RETURN_LINE,
        rows=[{
            "head": ["维保返件", "其他退回", "备件", "2026-08-03", "新华三集团",
                     "AI项目", "XSDD-20260203-0029", "WBDD-20260203-0026", "",
                     "广州仓", "已生效", "", "RKN-001", "HEAD-1"],
            "lines": [
                ["", "02311AYV", "", "成品", "", "", "2", "LID-1", "1", ""],
            ],
        }],
    )
    ok, _ = ai.parser_accepts_file("return_order", standard, "返库单.xlsx")
    assert ok is True


def test_proposal_list_scoped_to_creator_for_non_admin(db):
    data = _variant_return_workbook()
    headers = ["返库类别", "退回日期", "项目"]
    samples = [["其他返库", "2026-08-03", "AI项目"]]
    ai.propose(
        db,
        doc_type="return_order",
        data=data,
        filename="返库单.xlsx",
        headers=headers,
        samples=samples,
        operated_by="alice",
        llm_call=_fake_llm,
    )
    ai.propose(
        db,
        doc_type="return_order",
        data=data,
        filename="返库单.xlsx",
        headers=headers,
        samples=samples,
        operated_by="bob",
        llm_call=_fake_llm,
    )
    alice_view = ai.list_proposals(db, username="alice", role="sales")
    assert {row["created_by"] for row in alice_view} == {"alice"}
    admin_view = ai.list_proposals(db, username="admin", role="admin")
    assert {row["created_by"] for row in admin_view} == {"alice", "bob"}


def test_propose_api_rejects_file_the_parser_handles(db):
    """主解析器可解析的文件不允许走 AI 提案（round-4 Blocker 4 反例）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import auth
    from app.api import maintenance_ai_fallback
    from app.auth import hash_password
    from app.models.system import SysUser
    from tests.test_maintenance_doc_import import (
        _RETURN_HEAD,
        _RETURN_LINE,
        _doc_workbook,
    )

    db.add(
        SysUser(
            username="ai_gate_admin",
            role="admin",
            display_name="AI门禁管理员",
            password_hash=hash_password("synthetic-password-123"),
        )
    )
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_ai_fallback.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "ai_gate_admin", "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"

    standard = _doc_workbook(
        sheet_title="Sheet1",
        head_headers=_RETURN_HEAD,
        line_headers=_RETURN_LINE,
        rows=[{
            "head": ["维保返件", "其他退回", "备件", "2026-08-03", "新华三集团",
                     "AI项目", "XSDD-20260203-0029", "WBDD-20260203-0026", "",
                     "广州仓", "已生效", "", "RKN-001", "HEAD-1"],
            "lines": [
                ["", "02311AYV", "", "成品", "", "", "2", "LID-1", "1", ""],
            ],
        }],
    )
    rejected = client.post(
        "/api/maintenance/ai-fallback/propose?doc_type=return_order",
        files={"file": ("返库单.xlsx", standard, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["detail"]["code"] == "parser_already_succeeds"

    # 漂移表头文件允许通过门禁；LLM 未配置 → 优雅降级 503
    drifted = client.post(
        "/api/maintenance/ai-fallback/propose?doc_type=return_order",
        files={"file": ("返库单.xlsx", _variant_return_workbook(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert drifted.status_code == 503, drifted.text
    assert drifted.json()["detail"]["code"] == "ai_not_configured"


def test_accept_second_call_replays_same_batch(db):
    """同一提案第二次 accept（不同幂等键）稳定重放既有 batch，不产生孤儿 batch。"""
    data = _variant_return_workbook()
    headers = ["返库类别", "退回日期", "项目", "销售合同号", "需求单", "状态",
               "自贴码", "料号", "SN", "检测结果", "库位", "数量", "行号"]
    samples = [["其他返库", "2026-08-03", "AI项目", "XSDD-20260203-0029", "", "已生效",
                "", "02311AYV", "", "成品", "", "2", "L-1"]]
    result = ai.propose(
        db,
        doc_type="return_order",
        data=data,
        filename="变体返库单.xlsx",
        headers=headers,
        samples=samples,
        operated_by="合成管理员",
        llm_call=_fake_llm,
    )
    first = ai.accept_proposal(
        db,
        proposal_id=result["proposal_id"],
        data=data,
        filename="变体返库单.xlsx",
        operated_by="合成管理员",
        idempotency_key="ai-accept-replay-key-0001",
    )
    second = ai.accept_proposal(
        db,
        proposal_id=result["proposal_id"],
        data=data,
        filename="变体返库单.xlsx",
        operated_by="合成管理员",
        idempotency_key="ai-accept-replay-key-0002",
    )
    assert second == first
    from app.models.maintenance_doc_import import (
        MaintenanceDocHeadRow,
        MaintenanceDocImportBatch,
    )

    batches = db.execute(
        text("SELECT count(*) FROM maintenance_doc_import_batch")
    ).scalar_one()
    assert batches == 1  # 无孤儿批次
    head = db.query(MaintenanceDocHeadRow).filter_by(batch_id=first).one()
    assert head.wbdd_no == "WBDD-20260203-0026"
