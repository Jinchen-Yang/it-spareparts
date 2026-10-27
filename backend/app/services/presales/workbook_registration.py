"""售前拥有模板声明和业务回调，通用引擎只持有注册项。"""

from app.services.workbook.registry import NotFound, WorkbookSpec, get_spec, register


def register_workbooks():
    from app.services.presales import inquiry_workbook, quote_workbook, sales_workbook

    specs = [
        WorkbookSpec(inquiry_workbook.INQUIRY_SCHEMA, inquiry_workbook.load_current,
                     inquiry_workbook.apply_inquiry, inquiry_workbook.can_access,
                     inquiry_workbook.validate_inquiry),
        WorkbookSpec(quote_workbook.QUOTE_SCHEMA, quote_workbook.load_current,
                     quote_workbook.apply_quotes, quote_workbook.can_access,
                     quote_workbook.validate_quotes),
        WorkbookSpec(sales_workbook.SUMMARY_SCHEMA, sales_workbook.reject_import,
                     sales_workbook.reject_import, sales_workbook.can_access),
    ]
    for spec in specs:
        try:
            get_spec(spec.schema.name)
        except NotFound:
            register(spec.schema.name, spec)
