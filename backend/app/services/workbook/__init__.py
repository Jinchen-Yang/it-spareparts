"""通用工作簿往返引擎；业务通过 schema 和注册回调接入。"""

from app.services.workbook.schema import ColumnSpec, SheetSpec, WorkbookContext, WorkbookError, WorkbookSchema

__all__ = ["ColumnSpec", "SheetSpec", "WorkbookSchema", "WorkbookContext", "WorkbookError"]
