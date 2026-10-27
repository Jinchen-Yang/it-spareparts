"""逐行业务错误及复用应用路径的校验工具；保存点由调用方持有。"""
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass

from app.services.workbook.plan import Plan
from app.services.workbook.schema import WorkbookError
from app.services.workflow.registry import WorkflowError


@dataclass(frozen=True)
class RowError:
    sheet: str
    message: str
    row_key: str | None = None
    row_number: int | None = None
    column: str | None = None

    def to_dict(self):
        return {"sheet": self.sheet, "row_key": self.row_key,
                "row_number": self.row_number, "column": self.column,
                "message": self.message, "kind": "business"}


def effective_plan(comparison, rows):
    return Plan(comparison.schema_name, comparison.export_id, deepcopy(rows),
                dict(Counter(row["status"] for row in rows)), [], comparison.warnings,
                comparison.schema_version, comparison.subject_type, comparison.subject_id)


def error_for_row(row, exc, column=None):
    message = str(exc) if isinstance(exc, (WorkbookError, WorkflowError)) else "业务校验失败，请检查该行或回到页面处理"
    return RowError(row["sheet"], message, row["key"], row.get("row_number"), column)


def validate_by_applying(db, comparison, ctx, apply_fn, column_for_error=None):
    """先整份试运行；有错时逐行独立回滚，收集全部定位。"""
    if not comparison.rows:
        return []

    def trial(rows):
        savepoint = db.begin_nested()
        try:
            apply_fn(db, effective_plan(comparison, rows), ctx)
            db.flush()
        except Exception as exc:
            return exc
        finally:
            savepoint.rollback()
        return None

    batch_error = trial(comparison.rows)
    if batch_error is None:
        return []
    errors = []
    for row in comparison.rows:
        exc = trial([row])
        if exc is not None:
            column = column_for_error(exc) if column_for_error else None
            errors.append(error_for_row(row, exc, column))
    if not errors:
        column = column_for_error(batch_error) if column_for_error else None
        # 跨行约束无法归责于单一来源时，标出全部参与行，不猜覆盖目标。
        errors.extend(error_for_row(row, batch_error, column) for row in comparison.rows)
    return errors
