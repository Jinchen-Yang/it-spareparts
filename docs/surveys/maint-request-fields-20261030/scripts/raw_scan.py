"""只读扫描 WBDD 原件：sheet、双表头、列名、合并单元格、逐列覆盖率（按行/按单据/按文件）。
用法：python raw_scan.py <uploads_dir> <batches_txt> <out_json>
不输出任何单元格值，只输出列名与计数。"""
import json, sys, os, collections, datetime
import openpyxl

up, batches_txt, out = sys.argv[1:4]
batches = collections.defaultdict(list)
for line in open(batches_txt, encoding="utf-8"):
    p = line.rstrip("\n").split("|")
    batches[p[9]].append({"id": int(p[0]), "filename": p[1], "status": p[2], "uploaded_at": p[3]})

def blank(v):
    return v is None or (isinstance(v, str) and v.strip() == "")

res = {}
for fn in sorted(os.listdir(up)):
    h = fn[:-5]
    wb = openpyxl.load_workbook(os.path.join(up, fn), data_only=True)
    info = {"batches": batches.get(h, []), "sheets": []}
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        sh = {"name": ws.title, "state": ws.sheet_state, "nrows": len(rows),
              "merged": [str(r) for r in ws.merged_cells.ranges][:50],
              "merged_count": len(ws.merged_cells.ranges)}
        hdr = None
        for i, r in enumerate(rows[:3]):
            names = [("" if c is None else str(c).strip()) for c in r]
            if "需求单号" in names or any(n.startswith("需求明细.") for n in names):
                hdr = i; break
        if hdr is not None:
            names = [("" if c is None else str(c).strip()) for c in rows[hdr]]
            codes = [("" if c is None else str(c).strip()) for c in rows[hdr - 1]] if hdr >= 1 else None
            data = rows[hdr + 1:]
            # 去掉全空尾行
            data = [r for r in data if any(not blank(c) for c in r)]
            idcol = names.index("数据ID(不可修改)") if "数据ID(不可修改)" in names else None
            # 单据分组：数据ID 非空开新单，空行续上一单（与氚云导出合并展示一致）
            groups, cur = [], None
            for r in data:
                if idcol is not None and not blank(r[idcol]):
                    cur = str(r[idcol]).strip()
                groups.append(cur)
            col_stats = []
            for j, n in enumerate(names):
                nonblank_rows = sum(1 for r in data if j < len(r) and not blank(r[j]))
                orders_with = len({g for g, r in zip(groups, data) if j < len(r) and not blank(r[j])})
                types = collections.Counter(type(r[j]).__name__ for r in data if j < len(r) and not blank(r[j]))
                maxlen = max((len(str(r[j]).strip()) for r in data if j < len(r) and not blank(r[j])), default=0)
                col_stats.append({"idx": j, "name": n, "code": codes[j] if codes and j < len(codes) else None,
                                  "rows_nonblank": nonblank_rows, "orders_nonblank": orders_with,
                                  "types": dict(types), "maxlen": maxlen})
            sh.update({"header_row": hdr + 1, "ncols": len(names), "data_rows": len(data),
                       "orders": len({g for g in groups if g}), "columns": col_stats})
        info["sheets"].append(sh)
    res[fn] = info
json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False)
print("files", len(res))
