"""对已生成的 work_*.json 样例做二次打码（敏感字段一律只保留长度）。"""
import json, sys
SENS = {"delivery_address_option","customer_name","end_customer","receiver","receiver_phone","receiver_address","salesperson","maintainer_raw","created_by_raw",
        "purchaser_raw","purchaser2_raw","project_manager_raw","project_manager_staff_raw","co_salesperson_raw","partner_raw",
        "warehouse_keeper_raw","change_warehouse_handler","warehouse_handler","adjust_keeper","image_urls","attachments",
        "line_image_urls","express_no","express_no2","serial_numbers","project_raw","project_std","description","line_note","purchase_note"}
for p in sys.argv[1:]:
    d = json.load(open(p, encoding="utf-8"))
    for k, lst in d.get("samples", {}).items():
        f = k.split("|")[-1].replace("order.", "")
        if f in SENS:
            for s in lst:
                for kk in ("file_value", "db_value", "raw", "parsed"):
                    v = s.get(kk)
                    if v is not None and not str(v).startswith("<"): s[kk] = f"<{len(str(v))}字>"
    json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
