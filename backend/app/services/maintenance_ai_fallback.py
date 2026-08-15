"""AI 兜底列映射服务（C3）。

单步 LLM 提案 + 确定性执行器：
- canonical 字段目录封闭，AI 只能从目录选，不能发明字段；
- AI 不计算任何派生值（金额/日期/分组仍由规则解析器计算）；
- 提案先过目录校验，再用同一解析器试解析（试解析结果作为提案的确定性证据）；
- 人工确认后 accept 走与 py 路径完全相同的 store_preview/apply；
- LLM 未配置时优雅降级（AIUnavailable），业务可改用人工映射。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent import provider as llm_provider
from app.config import get_settings
from app.models.maintenance_ai_fallback import MaintenanceAiMappingProposal
from app.services import maintenance_ckd_import as ckd
from app.services import maintenance_doc_import as docs
from app.services import maintenance_ledger as ledger

# 封闭 canonical 字段目录：AI 只能从这些字段里选（doc_type → 可选字段集）
DOC_TYPE_KIND = {
    "ckd_shipment": "ckd",
    "rkd_inbound": "doc",
    "return_order": "doc",
    "bxd_expense": "doc",
    "ledger": "ledger",
}


class AIUnavailable(RuntimeError):
    """LLM 未配置：AI 兜底不可用。"""


class AIProposalInvalid(RuntimeError):
    """AI 提案未通过封闭目录校验。"""


class AIProposalError(RuntimeError):
    """AI 兜底流程状态错误。"""


def canonical_catalog(doc_type: str) -> set[str]:
    """返回该单据类型的封闭 canonical 字段集合。"""
    if doc_type == "ckd_shipment":
        return set(ckd._HEAD_COLUMNS) | set(ckd._LINE_COLUMNS)
    if doc_type == "ledger":
        return set(ledger._CONTRACT_HEADERS) | set(ledger._PLAN_HEADERS) | set(
            ledger._EXPENSE_HEADERS
        )
    spec = docs._SPECS.get(doc_type)
    if spec is None:
        raise AIProposalError(f"未知单据类型：{doc_type}")
    return set(spec["head"]) | set(spec["line"])


def _mask_sample_value(value: str) -> str:
    """脱敏样本值：只保留类型与长度特征，绝不外发原始金额/单号/人名。"""
    text = value or ""
    if not text:
        return "<空>"
    if re.fullmatch(r"[A-Z]+-\d{8}-\d{4}", text):
        return "<单号>"
    if re.fullmatch(r"[\d,，.¥￥ ]+", text):
        return "<数字>"
    if re.fullmatch(r"\d{4}[-/年.]\d{1,2}[-/月.]?\d{0,2}[日]?", text):
        return "<日期>"
    if len(text) <= 4:
        return "<短文本>"
    return f"<文本{len(text)}字>"


def build_prompt(doc_type: str, headers: list[str], samples: list[list]) -> tuple[str, str]:
    """确定性构造提案 prompt；样本一律脱敏（N1）。返回 (prompt, prompt_hash)。"""
    catalog = sorted(canonical_catalog(doc_type))
    sample_lines = []
    for row in samples[:5]:
        sample_lines.append(
            json.dumps(
                {
                    headers[i]: (
                        _mask_sample_value(str(row[i]))
                        if i < len(row) and row[i] is not None
                        else "<空>"
                    )
                    for i in range(len(headers))
                },
                ensure_ascii=False,
            )
        )
    prompt = (
        f"你是 Excel 列映射助手。单据类型：{doc_type}。\n"
        f"可选目标字段（封闭目录，只能从中选择）：{json.dumps(catalog, ensure_ascii=False)}\n"
        f"表头：{json.dumps(headers, ensure_ascii=False)}\n"
        f"样本行（值已脱敏为类型特征）：\n" + "\n".join(sample_lines) + "\n"
        "只输出 JSON：{\"column_mapping\": {\"<表头>\": \"<目标字段>\"}, \"notes\": [\"说明\"]}。"
        "未匹配的表头不要出现在 column_mapping 里；禁止发明目录外的字段；"
        "禁止计算金额、日期或任何派生值。"
    )
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return prompt, prompt_hash


def _validate_proposal(doc_type: str, proposal: dict) -> dict:
    catalog = canonical_catalog(doc_type)
    mapping = proposal.get("column_mapping")
    if not isinstance(mapping, dict) or not mapping:
        raise AIProposalInvalid("提案缺少 column_mapping")
    clean = {}
    seen_fields: dict[str, str] = {}
    for header, field in mapping.items():
        if not isinstance(header, str) or not isinstance(field, str):
            raise AIProposalInvalid("提案映射必须是字符串键值对")
        if field not in catalog:
            raise AIProposalInvalid(f"提案包含目录外字段：{field}")
        if field in seen_fields:
            # 重复 canonical 必须 fail-closed（import-field-contract §31-35）
            raise AIProposalInvalid(
                f"多个源列映射同一目标字段：{field}（{seen_fields[field]} / {header}）"
            )
        seen_fields[field] = header
        clean[header] = field
    notes = proposal.get("notes")
    if notes is not None and not isinstance(notes, list):
        raise AIProposalInvalid("notes 必须是字符串数组")
    return {"column_mapping": clean, "notes": notes or []}


def call_llm_for_mapping(
    doc_type: str,
    headers: list[str],
    samples: list[list],
    *,
    llm_call=None,
) -> tuple[dict, dict]:
    """单步 LLM 调用并校验提案。llm_call 可注入（测试用）。

    返回 (proposal, metadata)；未配置 LLM 时抛 AIUnavailable。
    """
    if llm_call is None:
        settings = get_settings()
        if not settings.llm_mapping_external_enabled:
            raise AIUnavailable(
                "AI 兜底外部调用未开通（LLM_MAPPING_EXTERNAL_ENABLED=false）；"
                "可改用人工列映射模板"
            )
        if not llm_provider.is_configured():
            raise AIUnavailable("未配置 LLM_API_KEY，AI 兜底不可用")
        settings = get_settings()

        def _default_call(prompt: str) -> str:
            from openai import OpenAI

            client = OpenAI(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
            )
            response = client.chat.completions.create(
                model=settings.llm_model,
                messages=[
                    {
                        "role": "system",
                        "content": "你是数据管道中的列映射提案器：只输出 JSON，"
                        "从封闭目录选字段，不发明字段，不计算任何值。",
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
            )
            return response.choices[0].message.content or "{}"

        llm_call = _default_call
        provider_name = settings.llm_provider
        model = settings.llm_model
    else:
        provider_name = "injected"
        model = "injected"

    prompt, prompt_hash = build_prompt(doc_type, headers, samples)
    raw = llm_call(prompt)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIProposalInvalid(f"LLM 输出不是合法 JSON：{exc}") from exc
    proposal = _validate_proposal(doc_type, parsed)
    return proposal, {
        "provider": provider_name,
        "model": model,
        "prompt_hash": prompt_hash,
    }


def _parse_with_mapping(doc_type: str, data: bytes, filename: str, mapping: dict):
    """把提案映射转成 alias 并调用对应解析器试解析。"""
    aliases = mapping.get("column_mapping") or {}
    if doc_type == "ckd_shipment":
        return ckd.parse_ckd_workbook(data, filename, column_aliases=aliases)
    if doc_type == "ledger":
        return ledger.parse_ledger_workbook(data, filename, column_aliases=aliases)
    return docs.parse_doc_workbook(doc_type, data, filename, column_aliases=aliases)


def propose(
    db: Session,
    *,
    doc_type: str,
    data: bytes,
    filename: str,
    headers: list[str],
    samples: list[list],
    operated_by: str,
    llm_call=None,
) -> dict:
    """生成并保存一次提案；附确定性试解析证据。"""
    proposal, meta = call_llm_for_mapping(
        doc_type, headers, samples, llm_call=llm_call
    )
    trial_error = None
    trial_counts = None
    try:
        parsed = _parse_with_mapping(doc_type, data, filename, proposal)
        if doc_type == "ckd_shipment":
            trial_counts = {"heads": len(parsed["heads"]), "lines": parsed["line_count"]}
        elif doc_type == "ledger":
            trial_counts = {
                "contract_rows": len(parsed["contract_rows"]),
                "plan_rows": len(parsed["plan_rows"]),
                "expense_rows": len(parsed["expense_rows"]),
            }
        else:
            trial_counts = {"heads": len(parsed["heads"]), "lines": parsed["line_count"]}
    except (docs.DocParseError, ckd.CkdParseError, ledger.LedgerParseError) as exc:
        trial_error = str(exc)
    row = MaintenanceAiMappingProposal(
        proposal_id=str(uuid4()),
        doc_type=doc_type,
        file_hash=hashlib.sha256(data).hexdigest(),
        filename=filename[:255],
        header_snapshot=headers,
        sample_rows=[
            [
                _mask_sample_value(str(cell)) if cell is not None else ""
                for cell in row
            ]
            for row in samples[:5]
        ],
        proposal=proposal,
        provider=meta["provider"],
        model=meta["model"],
        prompt_hash=meta["prompt_hash"],
        status="pending",
        created_by=operated_by,
    )
    db.add(row)
    db.commit()
    return {
        "proposal_id": row.proposal_id,
        "doc_type": doc_type,
        "column_mapping": proposal["column_mapping"],
        "notes": proposal["notes"],
        "provider": meta["provider"],
        "model": meta["model"],
        "trial_error": trial_error,
        "trial_counts": trial_counts,
    }


def accept_proposal(
    db: Session,
    *,
    proposal_id: str,
    data: bytes,
    filename: str,
    operated_by: str,
    idempotency_key: str,
) -> str:
    """人工确认提案后，用同一确定性解析器走标准 store_preview。

    并发正确性（round-4 Blocker 5）：store_preview 内部自行 commit，行锁
    会在关键区中途释放。改用 **session 级 advisory lock**（跨 commit 存活）
    串行化同一提案的 accept：第二个请求等锁后重读 status，已接受则稳定
    重放既有 batch，绝不产生第二个可 apply 的孤儿 batch。
    """
    lock_key = int(
        hashlib.sha256(f"ai-accept:{proposal_id}".encode("utf-8")).hexdigest()[:15],
        16,
    )
    db.execute(select(func.pg_advisory_lock(lock_key)))
    try:
        row = db.execute(
            select(MaintenanceAiMappingProposal)
            .where(MaintenanceAiMappingProposal.proposal_id == proposal_id)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise AIProposalError("提案不存在")
        if row.status == "accepted":
            if row.accepted_batch_id is None:
                raise AIProposalError("提案状态异常")
            return row.accepted_batch_id
        if hashlib.sha256(data).hexdigest() != row.file_hash:
            raise AIProposalError("上传文件与提案不匹配")
        parsed = _parse_with_mapping(row.doc_type, data, filename, row.proposal)
        if row.doc_type == "ledger":
            batch_id = ledger.store_preview(
                db, parsed, operated_by, idempotency_key=idempotency_key
            )
        elif row.doc_type == "ckd_shipment":
            batch_id = ckd.store_preview(
                db, parsed, operated_by, idempotency_key=idempotency_key
            )
        else:
            batch_id = docs.store_preview(
                db, parsed, operated_by, idempotency_key=idempotency_key
            )
        # store_preview 已 commit：重新加载并原子校验仍是 pending（advisory
        # lock 保证期间无并发 accept），再落 accepted 状态
        row = db.get(MaintenanceAiMappingProposal, proposal_id)
        if row is None or row.status != "pending":
            raise AIProposalError("提案状态已变化，请刷新后重试")
        row.status = "accepted"
        row.accepted_batch_id = batch_id
        row.accepted_by = operated_by
        row.accepted_at = datetime.now(timezone.utc)
        db.commit()
        return batch_id
    finally:
        # session 级锁显式释放；异常路径同样释放（进程退出由 PG 兜底清理）
        db.execute(select(func.pg_advisory_unlock(lock_key)))
        db.commit()


def parser_accepts_file(
    doc_type: str, data: bytes, filename: str
) -> tuple[bool, str | None]:
    """确定性解析器无别名试解析。成功 → AI 兜底无必要（fail-closed 拒绝提案）。

    AI 只做「认格式、搬字段」的容错层：只有主解析器失败的文件才允许走 AI。
    """
    try:
        if doc_type == "ledger":
            ledger.parse_ledger_workbook(data, filename)
        elif doc_type == "ckd_shipment":
            ckd.parse_ckd_workbook(data, filename)
        else:
            docs.parse_doc_workbook(doc_type, data, filename)
        return True, None
    except (ledger.LedgerParseError, ckd.CkdParseError, docs.DocParseError) as exc:
        return False, str(exc)


def list_proposals(
    db: Session,
    *,
    doc_type: str | None = None,
    username: str | None = None,
    role: str | None = None,
    limit: int = 50,
) -> list[dict]:
    statement = select(MaintenanceAiMappingProposal).order_by(
        MaintenanceAiMappingProposal.created_at.desc()
    )
    if doc_type is not None:
        statement = statement.where(MaintenanceAiMappingProposal.doc_type == doc_type)
    # 提案含 filename/hash/creator 等元数据：非 admin 只能看自己创建的提案
    if role != "admin":
        statement = statement.where(
            MaintenanceAiMappingProposal.created_by == (username or "")
        )
    rows = db.execute(statement.limit(min(max(limit, 1), 200))).scalars().all()
    return [
        {
            "proposal_id": row.proposal_id,
            "doc_type": row.doc_type,
            "file_hash": row.file_hash,
            "filename": row.filename,
            "status": row.status,
            "provider": row.provider,
            "model": row.model,
            "prompt_hash": row.prompt_hash,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat(),
            "accepted_batch_id": row.accepted_batch_id,
        }
        for row in rows
    ]
