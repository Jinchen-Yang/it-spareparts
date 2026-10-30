"""汇总 raw_scan 结果：列集合、模板签名、列名变体、按文件/行/单据覆盖率。输出 markdown 片段。"""
import json, sys, collections, re
d = json.load(open(sys.argv[1], encoding="utf-8"))
strip = lambda n: re.sub(r"[（(](必填|选填|不可修改)[）)]\s*$", "", n).strip()
tmpl = collections.Counter(); files = 0; tot_rows = 0; tot_orders = 0
col_files = collections.Counter(); col_rows = collections.Counter(); col_orders = collections.Counter()
variants = collections.defaultdict(collections.Counter); maxlen = collections.Counter(); types = collections.defaultdict(collections.Counter)
multi = []; merged = []; nohdr = []; tmpl_files = collections.defaultdict(list)
for fn, info in d.items():
    sheets = [s for s in info["sheets"] if "columns" in s]
    if len(info["sheets"]) > 1: multi.append((fn[:12], [(s["name"], s["state"]) for s in info["sheets"]]))
    if not sheets: nohdr.append(fn[:12]); continue
    s = sheets[0]; files += 1
    if len(sheets) > 1: multi.append((fn[:12], "多个可识别页"))
    if s["merged_count"]: merged.append((fn[:12], s["merged_count"]))
    names = [c["name"] for c in s["columns"]]
    sig = (len(names), "数据ID(不可修改)" in names, "是否可以接受通用号" in names)
    tmpl[sig] += 1; tmpl_files[sig].append(fn[:12])
    tot_rows += s["data_rows"]; tot_orders += s["orders"]
    for c in s["columns"]:
        k = strip(c["name"]) or f"<空列名#{c['idx']}>"
        variants[k][c["name"]] += 1
        col_files[k] += 1; col_rows[k] += c["rows_nonblank"]; col_orders[k] += c["orders_nonblank"]
        maxlen[k] = max(maxlen[k], c["maxlen"]); types[k].update(c["types"])
print(f"files_with_header={files} no_header={nohdr} total_rows={tot_rows} total_orders={tot_orders}")
print("templates:", {str(k): v for k, v in tmpl.items()})
for k, v in tmpl_files.items(): print(" ", k, v[:5])
print("multi_sheet:", len(multi), multi[:5]); print("merged:", merged[:10])
print("distinct_fields:", len(col_files))
print("|字段(去注解)|列名变体(次数)|出现文件数|有值行数|行覆盖率|有值单据数|单据覆盖率|最大长度|值类型|")
for k in col_files:
    v = "；".join(f"{n}×{c}" for n, c in variants[k].items())
    print(f"|{k}|{v}|{col_files[k]}|{col_rows[k]}|{col_rows[k]/tot_rows:.1%}|{col_orders[k]}|{col_orders[k]/tot_orders:.1%}|{maxlen[k]}|{dict(types[k])}|")
