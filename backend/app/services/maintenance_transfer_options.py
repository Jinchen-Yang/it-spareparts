"""Pure transfer form/field presentation, shared by transport and authorized services."""
from app.security import UserContext
from app.services import maintenance_bulk_import as bulk
from app.services import maintenance_project_export as project_export

_DOWNLOAD_FORMS = (
    {
        "key": "project_master",
        "label": "项目主档与期限",
        "description": "项目名称、负责人、期限、生命周期等主数据",
        "groups": {"项目基础", "期限"},
        "default_selected": True,
    },
    {
        "key": "contract",
        "label": "合同",
        "description": "销售单号、含税合同额及完整性状态",
        "groups": {"合同与回款"},
        "key_prefixes": ("contract_",),
        "explicit_keys": {"contract_nos"},
        "default_selected": True,
    },
    {
        "key": "collection",
        "label": "累计回款",
        "description": "已确认累计实收及数据状态",
        "groups": {"合同与回款"},
        "key_prefixes": ("collection_",),
        "default_selected": True,
    },
    {
        "key": "maintenance_activity",
        "label": "维保业务统计",
        "description": "单据、数量、预交付与供退货统计",
        "groups": {"业务统计"},
        "default_selected": False,
    },
    {
        "key": "cost",
        "label": "成本与状态",
        "description": "按账号权限提供已知成本、成本率和卡片状态",
        "groups": {"成本"},
        "default_selected": False,
    },
)


def _field_form_keys(field: project_export.ExportField) -> list[str]:
    keys: list[str] = []
    for form in _DOWNLOAD_FORMS:
        if field.group not in form.get("groups", set()):
            continue
        prefixes = form.get("key_prefixes")
        explicit = form.get("explicit_keys", set())
        if prefixes or explicit:
            if field.key not in explicit and not any(
                field.key.startswith(prefix) for prefix in prefixes or ()
            ):
                continue
        keys.append(str(form["key"]))
    return keys


def _download_options(ctx: UserContext) -> tuple[list[dict], list[dict], list[str], list[str]]:
    export = project_export.export_options(ctx)
    available_keys = {field["key"] for field in export["fields"]}
    fields_by_key = {field.key: field for field in project_export.EXPORT_FIELDS}
    fields = [
        {
            **field,
            "form_keys": _field_form_keys(fields_by_key[field["key"]]),
        }
        for field in export["fields"]
        if _field_form_keys(fields_by_key[field["key"]])
    ]
    default_fields = [
        key
        for key in export["default_fields"]
        if key in available_keys
        and any(field["key"] == key for field in fields)
    ]
    default_forms = list(
        dict.fromkeys(
            form_key
            for field in fields
            if field["key"] in default_fields
            for form_key in field["form_keys"]
        )
    )
    forms = [
        {
            "key": form["key"],
            "label": form["label"],
            "description": form["description"],
            "default_selected": form["key"] in default_forms,
        }
        for form in _DOWNLOAD_FORMS
        if any(form["key"] in field["form_keys"] for field in fields)
    ]
    return forms, fields, default_forms, default_fields


def _options_payload(ctx, *, can_import):
    forms, fields, default_forms, default_fields = _download_options(ctx)
    return {
        "can_import": can_import,
        "can_download": True,
        "max_files": bulk.MAX_TRANSFER_FILES,
        "accepted_extensions": [".xlsx"],
        "import_kinds": [
            {
                "key": (
                    "sales_contract"
                    if form["form_type"] == "sales_contract_amount"
                    else "receipt"
                ),
                "label": form["label"],
                "description": (
                    "含税额按订单金额/含税标记/税率/税金/未税金额交叉校验"
                    if form["form_type"] == "sales_contract_amount"
                    else "累计值只取实收金额；销售收款额和优惠额仅用于对账"
                ),
                "required_fields": form["required_fields"],
                "accepted_aliases": form["accepted_headers"],
                "metric_basis": (
                    {
                        "contract_amount": "inc_tax",
                        "sales_source_sync": "amount_ex_tax+tax_rate_if_unique",
                    }
                    if form["form_type"] == "sales_contract_amount"
                    else {
                        "collection": "actual_received_inc_tax",
                        "source_field": "收款明细.实收金额",
                    }
                ),
            }
            for form in bulk.registered_forms()
        ],
        "download_forms": forms,
        "download_fields": fields,
        "default_forms": default_forms,
        "default_fields": default_fields,
    }


