"""比对：每个原件文件的重放解析结果 vs 生产快照(隔离库)中「最后由该批次写入」的行。
只读查询隔离库 maint-survey-pg-20261030。输出差异计数与打码样例。
用法：PYTHONPATH=<worktree>/backend python db_compare.py <uploads> <batches_txt> <out_json>"""
import sys, os, json, collections, decimal, datetime
import psycopg
from app.etl import pipeline, mapping, cleaner

up, batches_txt, out = sys.argv[1:4]
pw = open(os.path.expanduser("~/workspaces/maint-survey-20261030/.pgpass-local")).read().strip()
conn = psycopg.connect(host="127.0.0.1", port=55433, user="survey", password=pw, dbname="itsp")
conn.read_only = True
by_hash = collections.defaultdict(list)
for line in open(batches_txt, encoding="utf-8"):
    p = line.rstrip("\n").split("|")
    by_hash[p[9]].append((int(p[0]), p[1], p[2]))
SENS = {"delivery_address_option","customer_name","end_customer","receiver","receiver_phone","receiver_address","salesperson","maintainer_raw","created_by_raw",
        "purchaser_raw","purchaser2_raw","project_manager_raw","project_manager_staff_raw","co_salesperson_raw","partner_raw",
        "warehouse_keeper_raw","change_warehouse_handler","warehouse_handler","adjust_keeper","image_urls","attachments",
        "line_image_urls","express_no","express_no2","serial_numbers","project_raw","project_std","description","line_note","purchase_note"}
def mask(f, v):
    if v is None: return None
    return f"<{len(str(v))}字>" if f in SENS else str(v)[:40]
def eq(a, b):
    if a is None and b is None: return True
    if isinstance(a, decimal.Decimal) or isinstance(b, decimal.Decimal):
        try: return decimal.Decimal(str(a)) == decimal.Decimal(str(b))
        except Exception: return False
    return a == b
LCOLS = [c for c in ["line_no","pn_std","pn_raw","description","qty","return_qty","serial_numbers", *mapping.MAINTENANCE_LINE_DISPLAY_FIELDS]]
OCOLS = ["order_no","order_date","linked_sales_order_no","project_raw","project_std","end_customer","demand_type","business_type",
         "salesperson","warehouse","maint_start","maint_end","data_status", *mapping.MAINTENANCE_HEAD_DISPLAY_FIELDS]
diff = collections.Counter(); samples = collections.defaultdict(list); explained = collections.Counter()
stats = collections.Counter(); notin = collections.Counter()
for fn in sorted(os.listdir(up)):
    h = fn[:-5]; bl = by_hash.get(h, [])
    ids = [b[0] for b in bl]; fname = bl[-1][1] if bl else fn
    try:
        lw = pipeline.load_workbook(os.path.join(up, fn)); res = pipeline.transform_workbook(lw).result
    except Exception as e:
        stats["file_load_error"] += 1; continue
    with conn.cursor() as cur:
        cur.execute(f"select l.raw_line_id, l.edited_source, l.manual_override::text, l.is_active, l.import_batch_id, {','.join('l.'+c for c in LCOLS)} "
                    "from f_maintenance_line l where l.import_batch_id = any(%s)", (ids,))
        dbl = {r[0]: r for r in cur.fetchall()}
        cur.execute(f"select o.raw_order_id, o.import_batch_id, {','.join('o.'+c for c in OCOLS)} from f_maintenance_order o where o.import_batch_id = any(%s)", (ids,))
        dbo = {r[0]: r for r in cur.fetchall()}
    stats["file_lines_parsed"] += len(res.lines); stats["db_lines_last_written"] += len(dbl)
    stats["file_orders_parsed"] += len(res.orders); stats["db_orders_last_written"] += len(dbo)
    seen = set()
    for ln in res.lines:
        r = dbl.get(ln["raw_line_id"])
        if r is None: continue
        seen.add(ln["raw_line_id"])
        stats["lines_compared"] += 1
        override = json.loads(r[2] or "{}"); src = r[1]
        for i, c in enumerate(LCOLS):
            dv = r[5 + i]; fv = ln.get(c)
            if not eq(dv, fv):
                key = c
                if c in override or src != "wbdd": explained["manual|" + c] += 1; continue
                diff[key] += 1
                if len(samples[key]) < 5: samples[key].append({"file": fname, "raw_line_id": ln["raw_line_id"][-6:], "file_value": mask(c, fv), "db_value": mask(c, dv)})
    for oid, o in res.orders.items():
        r = dbo.get(oid)
        if r is None: continue
        stats["orders_compared"] += 1
        for i, c in enumerate(OCOLS):
            dv = r[2 + i]; fv = o.get(c)
            if c == "receiver_address" and fv is None: continue
            if not eq(dv, fv):
                diff["order." + c] += 1
                if len(samples["order." + c]) < 5: samples["order." + c].append({"file": fname, "raw_order_id": oid[-6:], "file_value": mask(c, fv), "db_value": mask(c, dv)})
    print(fn[:12], len(res.lines), len(dbl), flush=True)
json.dump({"stats": dict(stats), "diff": dict(diff), "explained_manual": dict(explained), "samples": dict(samples)},
          open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
