from collections.abc import Iterable

from app.etl import mapping, reader, sheet_selection


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
    }


def inspect_file(path: str, filename: str) -> dict:
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
    }


def response(results: list[dict]) -> dict:
    return {
        "files": results,
        "any_warning": any(not result["ok"] for result in results),
        "missing_price_any": any(result["missing_price"] for result in results),
        "has_errors": any(result["severity"] == "error" for result in results),
        "can_import_all": all(result["can_import"] for result in results),
    }
