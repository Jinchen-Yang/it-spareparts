from collections.abc import Iterable

from app.etl import expense_void, mapping, reader, sheet_selection

# 修复模式预检解析数据行的**每请求**预算。实测（客户 3802 行氚云 inline-string 导出，
# 4 核开发机）：复用表头扫描后 ≈3.6ms/行（物化 + transform，transform 只占 ~15%），
# 前端 precheck 超时 30s、一请求最多 20 个文件——预算按整个请求累计，不是按文件。
# 超出预算的文件不解析、明确告知「未预演」，导入时仍按同一规则执行；比让请求超时
# 后 phase 回 dirty、修复模式在 UI 上不可达要好。
UPSERT_PRECHECK_ROW_BUDGET = 6_000


def new_upsert_budget() -> dict:
    return {"rows": UPSERT_PRECHECK_ROW_BUDGET}


_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}


def _issue(severity: str, code: str, message: str) -> dict:
    return {"severity": severity, "code": code, "message": message}


def _flatten_issues(file_issues: list[dict], sheets: list[dict]) -> list[dict]:
    return [*file_issues, *(issue for sheet in sheets for issue in sheet["issues"])]


def _severity(issues: Iterable[dict]) -> str:
    return max(
        (issue["severity"] for issue in issues),
        key=_SEVERITY_RANK.__getitem__,
        default="info",
    )


def _legacy_warning(issues: Iterable[dict]) -> str | None:
    issues = list(issues)
    severity = _severity(issues)
    if severity == "info":
        return None
    return next(
        (
            issue["message"]
            for issue in issues
            if issue["severity"] == severity
        ),
        None,
    )


def failed_file_result(filename: str, code: str, message: str) -> dict:
    issues = [_issue("error", code, message)]
    return {
        "filename": filename,
        "file_type": None,
        "ok": False,
        "missing_price": False,
        "warning": message,
        "can_import": False,
        "severity": "error",
        "selected_sheets": [],
        "sheets": [],
        "issues": issues,
        "exact_success_match": None,
        "blocked_reason": None,
    }


def inspect_file(path: str, filename: str, *, mode: str = "skip",
                 budget: dict | None = None) -> dict:
    reader.reject_roundtrip_workbook(path)
    inspected = reader.inspect_workbook(path, load_data=False)
    selection = sheet_selection.select_workbook_sheets(inspected)
    file_issues: list[dict] = []
    if not selection.selected:
        file_issues.append(_issue(
            "error",
            "no_recognized_sheet",
            "无法识别任何可导入工作表，请确认文件包含采购、销售、库存、维保出库或报销明细。",
        ))

    sheet_results: list[dict] = []
    missing_price = False
    for sheet in inspected:
        action = selection.action_for(sheet)
        issues: list[dict] = []
        if action == sheet_selection.IGNORED_RECOGNIZED:
            issues.append(_issue(
                "warning",
                "sheet_ignored_recognized",
                f"工作表「{sheet.sheet_name}」已识别为 {sheet.file_type}，但本次不会导入。",
            ))
        elif action == sheet_selection.IGNORED_UNRECOGNIZED:
            issues.append(_issue(
                "info",
                "sheet_ignored_unrecognized",
                f"工作表「{sheet.sheet_name}」无法识别，本次不会导入。",
            ))

        if sheet.dup_cols:
            if action == sheet_selection.SELECTED:
                issues.append(_issue(
                    "error",
                    "duplicate_headers",
                    f"工作表「{sheet.sheet_name}」存在重复非空表头：{sheet.dup_cols}。",
                ))
            else:
                issues.append(_issue(
                    "warning",
                    "duplicate_headers_ignored",
                    f"被忽略的工作表「{sheet.sheet_name}」存在重复非空表头：{sheet.dup_cols}，"
                    "不影响本次导入。",
                ))

        sheet_missing_price = (
            action == sheet_selection.SELECTED
            and sheet.file_type in {mapping.PURCHASE, mapping.SALES}
            and not mapping.has_price_columns(sheet.columns, sheet.file_type)
        )
        if sheet_missing_price:
            missing_price = True
            issues.append(_issue(
                "warning",
                "missing_price_columns",
                f"工作表「{sheet.sheet_name}」未识别到价格列，导入后采购或销售单将没有金额。",
            ))

        sheet_results.append({
            "sheet_name": sheet.sheet_name,
            "detected_type": sheet.file_type,
            "action": action,
            "header_row": sheet.header_row,
            "data_rows": sheet.data_rows,
            "duplicate_headers": sheet.dup_cols,
            "issues": issues,
        })

    if mode == "upsert" and selection.selected:
        file_issues.extend(_upsert_expense_issues(
            path, inspected, selection, sheet_results,
            budget if budget is not None else new_upsert_budget()))

    all_issues = _flatten_issues(file_issues, sheet_results)
    severity = _severity(all_issues)
    can_import = bool(selection.selected) and severity != "error"
    warning = _legacy_warning(all_issues)
    return {
        "filename": filename,
        "file_type": selection.file_type,
        "ok": warning is None,
        "missing_price": missing_price,
        "warning": warning,
        "can_import": can_import,
        "severity": severity,
        "selected_sheets": [sheet.sheet_name for sheet in selection.selected],
        "sheets": sheet_results,
        "issues": file_issues,
        "exact_success_match": None,
        "blocked_reason": None,
    }


def _upsert_expense_issues(path: str, inspected, selection, sheet_results,
                           budget: dict) -> list[dict]:
    """修复模式 + 报销页：把门禁与删除侧的**可证明**结论提前到预检。

    此前预检只看表头，含撞键行的报销页报 can_import=true / severity=info，前端直接给
    「开始导入」按钮，而 pipeline 会整批拒绝——预检口径与门禁口径当下就不一致。

    这里只做零查库、与导入**逐字节同源**的两件事（读法走 pipeline.load_workbook /
    transform_workbook，判定走 expense_void，不存在第二份实现）：
      1. 会导致整批拒绝的错误行 → error（与 pipeline 门禁同一个 blocking_errors）；
      2. 删除侧会不会被抑制（有行被排除 / 触及多合同 / 无页级锚）→ **warning**：用户
         明确选了修复模式，「你要的删除不会发生」必须被确认过，不能是可忽略的提示。
         这是 expense_void 的纯函数结论，与库状态无关，预检时刻就是精确的。
    「会作废多少行/多少钱」依赖库状态与锁，不在此处给数字——给不准的数字比不给更糟。
    """
    from app.etl import pipeline

    selected_types = {sheet.file_type for sheet in selection.selected}
    if mapping.EXPENSE not in selected_types:
        return []
    if any(i["severity"] == "error" for sheet in sheet_results for i in sheet["issues"]):
        return []          # 表头级错误已在 sheet 级上报，不再白跑一次全量解析再报一遍
    total_rows = sum(sheet.data_rows or 0 for sheet in selection.selected)
    if total_rows > budget["rows"]:
        return [_issue(
            "warning", "upsert_precheck_skipped",
            f"报销页共 {total_rows} 行，超出本次预检剩余解析预算 {budget['rows']} 行"
            f"（每次预检合计 {UPSERT_PRECHECK_ROW_BUDGET} 行），未预演修复模式的门禁与删除侧"
            "结论；导入时仍会按同一规则执行。文件较多时请分批预检。",
        )]
    budget["rows"] -= total_rows
    try:
        loaded = pipeline.load_workbook(path, inspected=inspected)
        result = pipeline.transform_workbook(loaded).result
    except reader.ReaderError as exc:
        return [_issue("error", exc.code or "reader_error", str(exc))]
    except Exception as exc:  # noqa: BLE001 — 一个坏单元格不能让整个多文件预检 500
        return [_issue("error", "upsert_precheck_failed",
                       f"预演解析失败：{type(exc).__name__}: {exc}")]

    issues: list[dict] = []
    blocking = expense_void.blocking_errors(result)
    if blocking:
        kinds = "、".join(sorted(expense_void.iter_error_types(blocking)))
        issues.append(_issue(
            "error", "upsert_blocking_errors",
            f"修复模式（以本表为准）要求报销页无错误行：发现 {len(blocking)} 行错误"
            f"（{kinds}），导入将被整批拒绝。请修正后重试，或改用「跳过」模式仅补新行。",
        ))
    inputs = expense_void.plan_inputs(result, mode="upsert")
    reason = inputs.suppressed_reason
    if reason == expense_void.SUPPRESS_DROPPED:
        issues.append(_issue(
            "warning", "upsert_void_suppressed_dropped",
            f"本表有 {inputs.dropped_no_contract} 行因缺少销售订单被排除。修复模式将只做"
            "同键覆盖，不作废任何旧报销行（本表不完整，不能代表删除侧）。",
        ))
    elif reason == expense_void.SUPPRESS_MULTI_CONTRACT:
        issues.append(_issue(
            "warning", "upsert_void_suppressed_multi_contract",
            f"本表触及 {len(inputs.contracts)} 个销售订单（合同号写法不一致也会被算作多个）。"
            "修复模式将只做同键覆盖，不作废任何旧报销行——「以本表为准」的删除只在单合同、"
            "带页级锚的项目工作簿报销页上执行。",
        ))
    elif reason == expense_void.SUPPRESS_UNANCHORED:
        issues.append(_issue(
            "warning", "upsert_void_suppressed_unanchored",
            "本表没有页级「销售订单」锚（不是系统导出的项目工作簿报销页）。修复模式将只做"
            "同键覆盖，不作废任何旧报销行。要按本表删除旧行，请从对应项目下载工作簿、"
            "在报销页上修改后回传。",
        ))
    elif inputs.contracts and not blocking:
        (contract,) = inputs.contracts
        issues.append(_issue(
            "warning", "upsert_void_armed",
            f"本表为单合同（{contract}）项目工作簿报销页且无错误行：修复模式将把该合同"
            "名下未出现在本表的旧报销行作废。请确认本表完整覆盖了该合同的全部报销。",
        ))
    return issues

def apply_exact_success_matches(results_with_hashes: list[tuple[dict, str | None]],
                                matches: dict[str, int], mode: str) -> None:
    for result, file_hash in results_with_hashes:
        batch_id = matches.get(file_hash) if file_hash is not None else None
        if batch_id is None:
            continue
        result["exact_success_match"] = {"batch_id": batch_id}
        if mode == "skip":
            result["blocked_reason"] = "exact_success_duplicate"
            result["can_import"] = False


def response(results: list[dict], mode: str = "skip") -> dict:
    return {
        "mode": mode,
        "files": results,
        "any_warning": any(not result["ok"] for result in results),
        "missing_price_any": any(result["missing_price"] for result in results),
        "has_errors": any(result["severity"] == "error" for result in results),
        "can_import_all": all(result["can_import"] for result in results),
    }
