"""在内存中用 worktree 代码（main e6cfd25）重放 WBDD 解析，逐字段比对原件单元格与解析结果。
不连数据库、不写任何文件之外的东西。输出 JSON：每文件错误计数、逐字段问题计数与样例（敏感值打码）。
用法：PYTHONPATH=<worktree>/backend python replay.py <uploads> <batches_txt> <out_json>"""
import sys, os, json, collections, decimal, datetime, re
import pandas as pd
from app.etl import pipeline, mapping, cleaner
from app.etl import transform as T

up, batches_txt, out = sys.argv[1:4]
names = collections.defaultdict(list)
for line in open(batches_txt, encoding="utf-8"):
    p = line.rstrip("\n").split("|")
    names[p[9]].append(p[1])

HEAD = mapping.MAINTENANCE_HEAD; LINE = mapping.MAINTENANCE_LINE
SPEC = {**T._WBDD_HEAD_DISPLAY_SPEC, **T._WBDD_LINE_DISPLAY_SPEC}
# DB 长度上限（information_schema 实查）；transform 未钳制的核心列
DBLEN = {"raw_order_id":64,"order_no":64,"linked_sales_order_no":64,"project_raw":256,"end_customer":256,
         "demand_type":16,"business_type":16,"salesperson":64,"warehouse":64,"data_status":16,
         "raw_line_id":64,"pn_std":128,"pn_raw":256,"customer_name":None}
SENS = {"delivery_address_option","customer_name","end_customer","receiver","receiver_phone","receiver_address","salesperson",
        "maintainer_raw","created_by_raw","purchaser_raw","purchaser2_raw","project_manager_raw",
        "project_manager_staff_raw","co_salesperson_raw","partner_raw","warehouse_keeper_raw",
        "change_warehouse_handler","warehouse_handler","adjust_keeper","image_urls","attachments",
        "line_image_urls","express_no","express_no2","serial_numbers","project_raw","description",
        "line_note","purchase_note"}
KIND = {f: k for f, (k, _) in SPEC.items()}
KIND.update({"order_date":"date","maint_start":"date","maint_end":"date","qty":"qty","return_qty":"qty","line_no":"int"})

def mask(field, v):
    if v is None: return None
    s = str(v)
    if field in SENS: return f"<{len(s)}字>"
    return s[:40]

def blank(v): return cleaner._is_blank(v)

def norm_num(v):
    try: return decimal.Decimal(re.sub(r"[,\s¥￥$]", "", str(v).strip()))
    except Exception: return None

agg = {"files": {}, "issues": collections.Counter(), "samples": collections.defaultdict(list),
       "error_types": collections.Counter(), "unmapped_cols": collections.Counter(),
       "col_presence": collections.Counter(), "head_conflicts": collections.Counter()}
def issue(key, fname, row, field, raw, got):
    agg["issues"][key + "|" + field] += 1
    lst = agg["samples"][key + "|" + field]
    if len(lst) < 5: lst.append({"file": fname, "excel_row": row, "raw": mask(field, raw), "parsed": mask(field, got)})

for fn in sorted(os.listdir(up)):
    h = fn[:-5]; fname = (names.get(h) or [fn])[-1]
    try:
        lw = pipeline.load_workbook(os.path.join(up, fn))
        tw = pipeline.transform_workbook(lw)
    except Exception as e:
        agg["files"][fn] = {"name": fname, "load_error": f"{type(e).__name__}: {str(e)[:200]}"}; continue
    res = tw.result; df = lw.primary.df; hdr = lw.primary.header_row
    cols = list(df.columns)
    for c in cols: agg["col_presence"][c] += 1
    known = set(HEAD) | set(LINE)
    for c in cols:
        if c not in known: agg["unmapped_cols"][c] += 1
    ec = collections.Counter(e.error_type for e in res.errors)
    agg["error_types"].update(ec)
    for e in res.errors:
        if e.error_type in ("bad_number","bad_date","missing_order_no","missing_raw_id","empty_pn","maintenance_address_conflict"):
            lst = agg["samples"]["row_error|" + e.error_type]
            if len(lst) < 8: lst.append({"file": fname, "excel_row": hdr + e.row_no, "detail": e.error_detail[:80]})
    inv_h = {v: k for k, v in HEAD.items()}; inv_l = {v: k for k, v in LINE.items()}
    # 行号定位
    lid_col = inv_l["raw_line_id"]; oid_col = inv_h["raw_order_id"]
    row_of_line = {}
    dup_lines = 0
    for idx, v in df[lid_col].items() if lid_col in df else []:
        s = cleaner.clean_str(v)
        if s:
            if s in row_of_line: dup_lines += 1
            row_of_line[s] = idx
    if oid_col not in df.columns or lid_col not in df.columns:
        agg["files"][fn] = {"name": fname, "batches": names.get(h, []), "ncols": len(cols), "rows_total": res.rows_total,
                            "orders": len(res.orders), "lines": len(res.lines), "errors": dict(ec),
                            "missing_identity_cols": [c for c in (oid_col, lid_col) if c not in df.columns],
                            "columns": cols, "header_row": hdr}
        print(fn[:12], fname, "NO-ID-COLS", len(cols), res.rows_total, len(res.lines), dict(ec), flush=True)
        continue
    first_row_of_order = {}
    for idx, v in df[oid_col].items():
        s = cleaner.clean_str(v)
        if s and s not in first_row_of_order: first_row_of_order[s] = idx
    # 行级字段比对
    for ln in res.lines:
        idx = row_of_line.get(ln["raw_line_id"]); er = hdr + idx + 1
        row = df.loc[idx]
        for field, col in inv_l.items():
            if field in ("raw_line_id",): continue
            raw = row.get(col) if col in df.columns else None
            got = ln.get(field)
            if field == "pn_raw": got = ln.get("pn_raw")
            k = KIND.get(field, "str")
            if col not in df.columns: continue
            if blank(raw):
                if got not in (None, False): issue("blank_but_value", fname, er, field, raw, got)
                continue
            if got is None:
                issue("value_lost", fname, er, field, raw, got); continue
            if k == "qty":
                n = norm_num(raw)
                if n is not None and n != got: issue("number_changed", fname, er, field, raw, got)
            elif k == "int":
                n = norm_num(raw)
                if n is not None and n != got: issue("number_changed", fname, er, field, raw, got)
            elif k == "str":
                s = str(raw).strip()
                if field == "pn_raw": continue
                if isinstance(raw, float) and raw.is_integer(): issue("float_text", fname, er, field, raw, got)
                if got != s:
                    issue("truncated" if s.startswith(str(got)) else "text_changed", fname, er, field, raw, got)
        if ln["pn_std"] and DBLEN["pn_std"] and len(ln["pn_std"]) > 128: issue("db_overflow", fname, er, "pn_std", ln["pn_std"], None)
        if ln["pn_raw"] and len(ln["pn_raw"]) > 256: issue("db_overflow", fname, er, "pn_raw", ln["pn_raw"], None)
        if ln.get("needs_review"): agg["issues"]["pn_needs_review|pn_std"] += 1
        if ln["pn_std"] != str(row.get(inv_l["pn_raw"])).strip().upper(): agg["issues"]["pn_vcode_stripped|pn_std"] += 1
    # 单头字段：首行注册 vs 同单其他行（ffill 后）冲突；以及原件值丢失/截断
    _oidser = df[oid_col].map(cleaner.clean_str)
    _groups = {k: g for k, g in df.groupby(_oidser, sort=False)}
    for oid, head in res.orders.items():
        idx = first_row_of_order.get(oid); er = hdr + idx + 1
        grp = _groups.get(oid, df.iloc[0:0])
        for field, col in inv_h.items():
            if col not in df.columns or field in ("raw_order_id",): continue
            vals = {str(v).strip() for v in grp[col] if not blank(v)}
            if len(vals) > 1 and field != "receiver_address":
                agg["head_conflicts"][field] += 1
                lst = agg["samples"]["head_conflict|" + field]
                if len(lst) < 5: lst.append({"file": fname, "excel_row": er, "distinct_values": len(vals)})
            raw = df.loc[idx].get(col)
            got = head.get(field)
            k = KIND.get(field, "str")
            if field == "customer_name": continue
            if field == "receiver_address": continue
            if blank(raw): continue
            if got is None:
                issue("value_lost", fname, er, field, raw, got); continue
            if k == "str":
                s = str(raw).strip()
                if field == "project_std": continue
                if got != s:
                    issue("truncated" if s.startswith(str(got)) else "text_changed", fname, er, field, raw, got)
                lim = DBLEN.get(field)
                if lim and len(s) > lim: issue("db_overflow", fname, er, field, raw, None)
            elif k == "date":
                if isinstance(raw, (datetime.datetime, pd.Timestamp)) and raw.date() != got:
                    issue("date_changed", fname, er, field, raw, got)
            elif k == "qty":
                n = norm_num(raw)
                if n is not None and n != got: issue("number_changed", fname, er, field, raw, got)
    agg["files"][fn] = {"name": fname, "batches": names.get(h, []), "ncols": len(cols), "rows_total": res.rows_total,
                        "orders": len(res.orders), "lines": len(res.lines), "errors": dict(ec),
                        "rows_display_issue": res.rows_display_issue, "headless": len(res.headless_order_ids),
                        "dup_line_ids": dup_lines, "ignored_sheets": tw.extra_report.get("ignored_sheets", []),
                        "header_row": hdr}
    print(fn[:12], fname, len(cols), res.rows_total, len(res.lines), dict(ec), flush=True)

for k in ("issues","error_types","unmapped_cols","col_presence","head_conflicts"):
    agg[k] = dict(agg[k])
agg["samples"] = dict(agg["samples"])
json.dump(agg, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
