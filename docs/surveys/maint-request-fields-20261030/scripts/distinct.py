"""列出少数枚举/布尔类列的去重取值（非敏感列）。"""
import os, collections, openpyxl, sys
up = sys.argv[1]; want = ["需求明细.个别调整发货仓","是否变仓库","是否可以接受通用号","整机需采备件校验","需求明细.整机需采备件","需求明细.整机/备件","数据状态","需求类型","业务类型","流程状态"]
cnt = collections.defaultdict(collections.Counter)
for fn in sorted(os.listdir(up)):
    wb = openpyxl.load_workbook(os.path.join(up, fn), read_only=True)
    ws = wb.worksheets[0]; it = ws.iter_rows(values_only=True)
    for _ in range(5):
        hdr = [str(h or "").split("(")[0] for h in next(it)]
        if "需求单号" in hdr: break
    idx = {w: hdr.index(w) for w in want if w in hdr}
    for r in it:
        for w, i in idx.items():
            v = r[i] if i < len(r) else None
            if v not in (None, ""): cnt[w][str(v)] += 1
for w in want: print(w, dict(cnt[w].most_common(8)))
