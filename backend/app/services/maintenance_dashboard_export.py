"""Pure dashboard export rows; callers supply the already-authorized snapshot."""
from decimal import Decimal, InvalidOperation
from fastapi import HTTPException
from app.services import maintenance_dashboard as dash
from app.services import maintenance_dashboard_consumption as consumption
from app.services import maintenance_dashboard_valuation_policy as policy


def materialize(db, filters, *, kind, visible, ratio, q=None, test_result=None, source=None, created_by=None):
    if kind == "projects":
        rows = dash.build_project_rows(
            db, filters, include_amounts=visible,
            ratio=ratio if visible else None,
        )
        header, columns = _project_export_columns(visible)
        # 静态审查补充（§106）：混合单位按 项目×单位 展开——数量/率用本桶
        # 值，金额不重复父项总额（单位内已估金额；待估单位留空）。单一单位
        # 行保持原样。
        data = []
        for row in rows:
            buckets = row.get("unit_breakdown") or []
            if len(buckets) <= 1:
                item = dict(row)
                item["_unit_label"] = _unit_label_of(row)
                data.append(item)
                continue
            valuation = dict(row.get("valuation") or {})
            main_by_unit = {
                entry.get("unit"): entry
                for entry in valuation.get("unit_breakdown", [])
            }
            domains_by_unit = {
                domain: {
                    entry.get("unit"): entry
                    for entry in payload.get("unit_breakdown", [])
                }
                for domain, payload in valuation.get("domains", {}).items()
            }
            for entry in buckets:
                unit = entry.get("unit")
                item = dict(row)
                item["_unit_label"] = unit or "待核"
                for key in ("receipt_qty", "bad_receipt_qty",
                            "scrap_receipt_qty", "issued_qty",
                            "exempt_issued_qty", "pending_requirement_qty",
                            "unlinked_receipt_qty", "return_rate_pct",
                            "rate_numerator", "rate_denominator"):
                    item[key] = entry.get(key)
                if valuation:
                    main = main_by_unit.get(unit) or {
                        "estimated_bad_qty": "0.000",
                        "pending_bad_qty": "0.000",
                        "awaiting_ratio_bad_qty": "0.000",
                        "coverage_pct": None,
                    }
                    unit_valuation = dict(valuation)
                    unit_valuation["estimated_bad_qty"] = main.get(
                        "estimated_bad_qty"
                    )
                    unit_valuation["pending_bad_qty"] = main.get(
                        "pending_bad_qty"
                    )
                    unit_valuation["awaiting_ratio_bad_qty"] = main.get(
                        "awaiting_ratio_bad_qty"
                    )
                    unit_valuation["coverage_pct"] = main.get("coverage_pct")
                    # 值键用本单位已估金额（B-F06：全待估单位不写 0.00）。
                    if "value_inc_tax" in main:
                        unit_valuation["bad_estimated_value_inc_tax"] = (
                            main["value_inc_tax"]
                        )
                        unit_valuation["bad_estimated_value_ex_tax"] = (
                            main["value_ex_tax"]
                        )
                    else:
                        unit_valuation.pop("bad_estimated_value_inc_tax", None)
                        unit_valuation.pop("bad_estimated_value_ex_tax", None)
                    if valuation.get("domains"):
                        unit_domains = {}
                        for domain, by_unit in domains_by_unit.items():
                            domain_entry = by_unit.get(unit) or {
                                "coverage_pct": None,
                            }
                            merged = {
                                **valuation["domains"][domain],
                                "coverage_pct": domain_entry.get("coverage_pct"),
                            }
                            # 三域值用本单位已估金额（B3 05:12）：无已估本桶
                            # → 键移除（空格，B-F06 口径），不摊/复制父域总额。
                            for value_key in (
                                "value_inc_tax", "value_ex_tax"
                            ):
                                if value_key in domain_entry:
                                    merged[value_key] = domain_entry[value_key]
                                else:
                                    merged.pop(value_key, None)
                            unit_domains[domain] = merged
                        unit_valuation["domains"] = unit_domains
                    item["valuation"] = unit_valuation
                data.append(item)
    elif kind == "parts":
        rows = dash.build_part_rows(
            db, filters, include_amounts=visible,
            ratio=ratio if visible else None,
        )
        header, columns = _part_export_columns(visible)
        data = [dict(row) for row in rows]
    elif kind == "receipts":
        rows, _total = dash.build_receipt_detail_rows(
            db, filters, include_amounts=visible,
            test_result=(test_result.strip() or None) if test_result else None,
            source=(source.strip() or None) if source else None,
            created_by=(created_by.strip() or None) if created_by else None,
            ratio=ratio if visible else None,
        )
        header, columns = _receipt_export_columns(visible)
        for row in rows:
            # 登记人：有显示名给「显示名（账号）」，否则账号原样。
            name = row.get("created_by_name")
            row["_registrant"] = (
                f"{name}（{row['created_by']}）" if name else row.get("created_by")
            )
        data = rows
    elif kind == "quantity_checks":
        # q 整单选择与 API 同参同集（B-F10）。
        part_ids, keyword = dash._part_keyword(db, q)
        payload = dash.build_quantity_checks_payload(
            db, filters.scope,
            projects=filters.projects,
            order_date_from=filters.start,
            order_date_to=filters.end,
            part_ids=part_ids, keyword=keyword,
        )
        header, columns = _check_export_columns()
        # 静态审查补充（§104）：导出按 单×单位 展开——件/台分母不并算，
        # 待核量不丢；无单位事实的行保留原行。
        data = []
        for row in payload["rows"]:
            entries = row.get("unit_breakdown") or []
            if not entries:
                item = dict(row)
                item["_unit_label"] = _unit_label_of(row)
                data.append(item)
                continue
            for entry in entries:
                item = dict(row)
                item["_unit_label"] = entry.get("unit") or "待核"
                for key in ("demand_qty", "received_qty", "difference_qty",
                            "over_received_qty", "undated_receipt_qty"):
                    item[key] = entry.get(key)
                data.append(item)
    elif kind == "orders":
        payload = consumption.period_orders(
            db, start=filters.start, end=filters.end, projects=filters.projects,
        )
        header, columns = _orders_export_columns()
        # 静态审查补充（§106）：按 项目×单位 展开，数量不丢；去重单数是
        # 不可相加元信息——只在首单位行出现一次。
        data = []
        for row in payload["rows"]:
            dated = {
                entry.get("unit"): entry
                for entry in row.get("unit_breakdown", [])
            }
            undated = {
                entry.get("unit"): entry
                for entry in row.get("undated_unit_breakdown", [])
            }
            units = dash.sorted_unit_keys(set(dated) | set(undated))
            if not units:
                item = dict(row)
                item["_unit_label"] = _unit_label_of(row)
                data.append(item)
                continue
            for index, unit in enumerate(units):
                item = dict(row)
                item["_unit_label"] = unit or "待核"
                item["order_qty"] = dated.get(unit, {}).get(
                    "order_qty", "0.000"
                )
                item["undated_order_qty"] = undated.get(unit, {}).get(
                    "order_qty", "0.000"
                )
                item["order_count"] = row["order_count"] if index == 0 else None
                item["undated_order_count"] = (
                    row["undated_order_count"] if index == 0 else None
                )
                data.append(item)
    elif kind == "consumption_daily":
        payload = consumption.daily_consumption(
            db, start=filters.start, end=filters.end, projects=filters.projects,
            include_amounts=visible,
        )
        # B-F09：导出按 日×项目×单位 展开（不再输出跨单位合算行）。
        data = []
        for row in payload["rows"]:
            for entry in row.get("unit_breakdown", []):
                data.append({
                    "issue_date": row["issue_date"],
                    "project_code": row["project_code"],
                    "display_name": row["display_name"],
                    "unit": entry.get("unit") or "待核",
                    "issued_qty": entry.get("issued_qty"),
                    "cost_amount_ex_tax": entry.get("cost_amount_ex_tax"),
                    "cost_amount_inc_tax": entry.get("cost_amount_inc_tax"),
                    "uncosted_qty": entry.get("uncosted_qty"),
                    "coverage_pct": entry.get("coverage_pct"),
                    "sources": row.get("sources"),
                })
        header, columns = _consumption_daily_export_columns(visible)
    else:
        rows = consumption.consumption_issue_rows(
            db, start=filters.start, end=filters.end, projects=filters.projects,
            include_amounts=visible,
        )
        data = [dict(row) for row in rows]
        for row in data:
            row["_unit_label"] = row.get("unit") or "待核"
        header, columns = _consumption_issues_export_columns(visible)
    if len(data) > dash.MAX_EXPORT_ROWS:
        raise HTTPException(
            status_code=413,
            detail=f"筛选集超过 {dash.MAX_EXPORT_ROWS} 行，请缩小范围后导出",
        )
    if visible and kind in ("projects", "parts", "receipts"):
        # R34：试算参数伴随导出（仅金额可见）；缺省已按 50% 解析（显式值回显，
        # 未提供时回显默认 0.5）。注意：本端点参数 format 遮蔽内建，不能写
        # format(ratio, "f")。
        for row in data:
            row["_retention_ratio"] = f"{ratio:f}"
    values = [_export_row(row, columns) for row in data]
    return header, values


ExportColumn = tuple[str, str | None, bool]  # (标签, 取值路径或常量列, 是否数值)


def _unit_label_of(row: dict) -> str:
    """单位列显示（B-F09/M12）：单一单位照写；仅待核写「待核」；混合「混合」。"""
    unit = row.get("unit")
    if unit is not None:
        return unit
    breakdown = row.get("unit_breakdown") or []
    if not breakdown:
        return ""
    if len(breakdown) == 1 and breakdown[0].get("unit") is None:
        return "待核"
    return "混合"


def _cell(row: dict, path: str):
    node: object = row
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _export_row(row: dict, columns: list[ExportColumn]) -> list:
    values = []
    for _label, path, numeric in columns:
        if path is None:  # 常量列：估价政策版本（仅金额可见导出存在）
            values.append(policy.POLICY_VERSION)
            continue
        value = _cell(row, path)
        if isinstance(value, (list, tuple)):
            value = ",".join(str(item) for item in value)
        if isinstance(value, dict):
            value = ",".join(f"{k}={v}" for k, v in value.items())
        if numeric and value is not None and value != "":
            value = _decimal_or_text(value)
        values.append(value)
    return values


def _decimal_or_text(value):
    """数值列：Decimal 保留精确文本（CSV），XLSX 侧再转 float；解析失败原样。"""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return value


def _project_export_columns(visible: bool) -> tuple[list[str], list[ExportColumn]]:
    columns: list[ExportColumn] = [
        ("项目编码", "project_code", False),
        ("项目名称", "display_name", False),
        ("业务类型", "business_type", False),
        ("单位", "_unit_label", False),
        ("返件实收合计", "receipt_qty", True),
        ("其中坏件实收", "bad_receipt_qty", True),
        ("其中废品", "scrap_receipt_qty", True),
        ("实际领用（未采集件况）", "issued_qty", True),
        ("免返领用量", "exempt_issued_qty", True),
        ("待判定返还要求量", "pending_requirement_qty", True),
        ("未关联需求单实收", "unlinked_receipt_qty", True),
        ("同期返还率%", "return_rate_pct", True),
        ("率分子（同期实收）", "rate_numerator", True),
        ("率分母（同期领用）", "rate_denominator", True),
        ("无返还备注", "no_return_note", False),
    ]
    if visible:
        columns += [
            ("坏件试算价值-含税", "valuation.bad_estimated_value_inc_tax", True),
            ("坏件试算价值-不含税", "valuation.bad_estimated_value_ex_tax", True),
            ("已估坏件量", "valuation.estimated_bad_qty", True),
            ("待估坏件量", "valuation.pending_bad_qty", True),
            ("待试算坏件量（缺比例）", "valuation.awaiting_ratio_bad_qty", True),
            ("覆盖率%", "valuation.coverage_pct", True),
            ("采购域覆盖率%", "valuation.domains.purchase.coverage_pct", True),
            ("销售域覆盖率%", "valuation.domains.sales.coverage_pct", True),
            ("成本域覆盖率%", "valuation.domains.cost.coverage_pct", True),
            # 三域各双税（B3 05:12/M10/R34-06）：双税分别计算，缺估域空格。
            ("三域-采购-坏件价值-含税", "valuation.domains.purchase.value_inc_tax", True),
            ("三域-采购-坏件价值-不含税", "valuation.domains.purchase.value_ex_tax", True),
            ("三域-销售-坏件价值-含税", "valuation.domains.sales.value_inc_tax", True),
            ("三域-销售-坏件价值-不含税", "valuation.domains.sales.value_ex_tax", True),
            ("三域-领用已计成本-价值-含税", "valuation.domains.cost.value_inc_tax", True),
            ("三域-领用已计成本-价值-不含税", "valuation.domains.cost.value_ex_tax", True),
            ("价值保留比例", "_retention_ratio", False),
            ("估价政策版本", None, False),
        ]
    return [label for label, _path, _numeric in columns], columns


def _part_export_columns(visible: bool) -> tuple[list[str], list[ExportColumn]]:
    columns: list[ExportColumn] = [
        ("PN", "pn", False),
        ("描述", "description", False),
        ("单位", "unit", False),
        ("返件实收", "receipt_qty", True),
        ("坏件实收", "bad_receipt_qty", True),
        ("实际领用（未采集件况）", "issued_qty", True),
        ("项目数", "project_count", True),
        ("同期返还率%", "return_rate_pct", True),
        ("率分子（同期实收）", "rate_numerator", True),
        ("率分母（同期领用）", "rate_denominator", True),
    ]
    if visible:
        columns += [
            ("坏件试算价值-含税", "valuation.bad_estimated_value_inc_tax", True),
            ("坏件试算价值-不含税", "valuation.bad_estimated_value_ex_tax", True),
            ("已估坏件量", "valuation.estimated_bad_qty", True),
            ("待估坏件量", "valuation.pending_bad_qty", True),
            ("待试算坏件量（缺比例）", "valuation.awaiting_ratio_bad_qty", True),
            ("覆盖率%", "valuation.coverage_pct", True),
            ("三域-采购-坏件价值-含税", "valuation.domains.purchase.value_inc_tax", True),
            ("三域-采购-坏件价值-不含税", "valuation.domains.purchase.value_ex_tax", True),
            ("三域-销售-坏件价值-含税", "valuation.domains.sales.value_inc_tax", True),
            ("三域-销售-坏件价值-不含税", "valuation.domains.sales.value_ex_tax", True),
            ("三域-领用已计成本-价值-含税", "valuation.domains.cost.value_inc_tax", True),
            ("三域-领用已计成本-价值-不含税", "valuation.domains.cost.value_ex_tax", True),
            ("采购域已估坏件量", "valuation.domains.purchase.estimated_bad_qty", True),
            ("采购域待估坏件量", "valuation.domains.purchase.pending_bad_qty", True),
            ("采购域覆盖率%", "valuation.domains.purchase.coverage_pct", True),
            ("销售域已估坏件量", "valuation.domains.sales.estimated_bad_qty", True),
            ("销售域待估坏件量", "valuation.domains.sales.pending_bad_qty", True),
            ("销售域覆盖率%", "valuation.domains.sales.coverage_pct", True),
            ("成本域已估坏件量", "valuation.domains.cost.estimated_bad_qty", True),
            ("成本域待估坏件量", "valuation.domains.cost.pending_bad_qty", True),
            ("成本域覆盖率%", "valuation.domains.cost.coverage_pct", True),
            ("价值保留比例", "_retention_ratio", False),
            ("估价政策版本", None, False),
        ]
    return [label for label, _path, _numeric in columns], columns


def _receipt_export_columns(visible: bool) -> tuple[list[str], list[ExportColumn]]:
    columns: list[ExportColumn] = [
        ("业务日", "occurred_at", False),
        ("项目编码", "project_code", False),
        ("来源需求单号", "source_order_no", False),
        ("PN", "pn", False),
        ("描述", "description", False),
        ("类型", "receipt_kind", False),
        ("单位", "unit", False),
        ("件况", "test_result", False),
        ("数量", "qty", True),
        ("来源", "source", False),
        ("登记人", "_registrant", False),
        ("需复核", "review_required", False),
    ]
    if visible:
        columns += [
            ("估价状态", "valuation.state", False),
            ("选用参考", "valuation.chosen", False),
            ("件况分类", "valuation.classification", False),
            ("折算系数", "valuation.factor", True),
            ("参考价-含税", "valuation.price_inc_tax", True),
            ("参考价-不含税", "valuation.price_ex_tax", True),
            ("行价值-含税", "valuation.value_inc_tax", True),
            ("行价值-不含税", "valuation.value_ex_tax", True),
            ("三域-采购-行价值-含税", "valuation.domains.purchase.value_inc_tax", True),
            ("三域-采购-行价值-不含税", "valuation.domains.purchase.value_ex_tax", True),
            ("三域-销售-行价值-含税", "valuation.domains.sales.value_inc_tax", True),
            ("三域-销售-行价值-不含税", "valuation.domains.sales.value_ex_tax", True),
            ("三域-领用已计成本-价值-含税", "valuation.domains.cost.value_inc_tax", True),
            ("三域-领用已计成本-价值-不含税", "valuation.domains.cost.value_ex_tax", True),
            ("待估原因", "valuation.reason", False),
            ("价值保留比例", "_retention_ratio", False),
            ("估价政策版本", None, False),
        ]
    return [label for label, _path, _numeric in columns], columns


def _check_export_columns() -> tuple[list[str], list[ExportColumn]]:
    columns: list[ExportColumn] = [
        ("需求单号", "order_no", False),
        ("项目编码", "project_code", False),
        ("项目名称", "display_name", False),
        ("制单日期", "order_date", False),
        ("需求类型", "demand_type", False),
        ("仓库", "warehouse", False),
        ("单位", "_unit_label", False),
        ("需求量N", "demand_qty", True),
        ("实收R", "received_qty", True),
        ("差额max(N-R,0)", "difference_qty", True),
        ("超收max(R-N,0)", "over_received_qty", True),
        ("基数状态", "basis_state", False),
        ("待核对原因", "reason_codes", False),
        ("无业务日实收", "undated_receipt_qty", True),
        ("无返还备注", "no_return_note", False),
    ]
    return [label for label, _path, _numeric in columns], columns


def _orders_export_columns() -> tuple[list[str], list[ExportColumn]]:
    """M01 导出：纯数量域，无金额列（任何角色）；无日期单单列（B-F12）。"""
    columns: list[ExportColumn] = [
        ("项目编码", "project_code", False),
        ("项目名称", "display_name", False),
        ("业务类型", "business_type", False),
        ("单位", "_unit_label", False),
        ("期间下单量（原qty，不扣退货/免返）", "order_qty", True),
        ("期间下单单数", "order_count", True),
        ("无制单日期下单量（未计入期间）", "undated_order_qty", True),
        ("无制单日期单数", "undated_order_count", True),
    ]
    return [label for label, _path, _numeric in columns], columns


def _consumption_daily_export_columns(
    visible: bool,
) -> tuple[list[str], list[ExportColumn]]:
    """M03 日表导出：日×项目×单位展开（B-F09）；成本/覆盖/来源列仅利润门。"""
    columns: list[ExportColumn] = [
        ("业务日", "issue_date", False),
        ("项目编码", "project_code", False),
        ("项目名称", "display_name", False),
        ("单位", "unit", False),
        ("领用量", "issued_qty", True),
    ]
    if visible:
        columns += [
            ("已计成本-不含税", "cost_amount_ex_tax", True),
            ("已计成本-含税", "cost_amount_inc_tax", True),
            ("未计成本量", "uncosted_qty", True),
            ("已计成本覆盖率%", "coverage_pct", True),
            ("成本来源", "sources", False),
        ]
    return [label for label, _path, _numeric in columns], columns


def _consumption_issues_export_columns(
    visible: bool,
) -> tuple[list[str], list[ExportColumn]]:
    """M03 领用明细导出：行级数量 + （有权时）成本证据，可核对。"""
    columns: list[ExportColumn] = [
        ("业务日", "issue_date", False),
        ("项目编码", "project_code", False),
        ("项目名称", "display_name", False),
        ("领用单号", "issue_no", False),
        ("PN", "pn", False),
        ("单位", "_unit_label", False),
        ("数量", "qty", True),
    ]
    if visible:
        columns += [
            ("成本来源", "cost_source", False),
            ("参考侧", "reference_side", False),
            ("算法版本", "algorithm_version", False),
            ("不含税单价", "unit_cost_ex_tax", True),
            ("含税单价", "unit_cost_inc_tax", True),
            ("不含税成本", "cost_amount_ex_tax", True),
            ("含税成本", "cost_amount_inc_tax", True),
        ]
    return [label for label, _path, _numeric in columns], columns


