"""核对「项目名称」变体文件：这些文件最后写入的单头 project_raw 是否为空。只读。"""
import json, subprocess, collections
d = json.load(open("work_raw_scan.json", encoding="utf-8"))
ids = collections.defaultdict(list)
for fn, info in d.items():
    s = [x for x in info["sheets"] if "columns" in x][0]
    names = {c["name"] for c in s["columns"]}
    key = "项目名称" if "项目名称" in names else ("项目名" if "项目名" in names else "无")
    key2 = "有仓储中心" if "仓储中心" in names else "无仓储中心"
    for b in info["batches"]:
        if b["status"] == "success": ids[key].append(b["id"]); ids[key2].append(b["id"])
for k, v in ids.items():
    sql = (f"select count(*), count(project_raw), count(storage_center), min(b.uploaded_at)::date, max(b.uploaded_at)::date from f_maintenance_order o "
           f"join sys_import_batch b on b.id=o.import_batch_id where o.import_batch_id in ({','.join(map(str, v))})")
    out = subprocess.run(["docker","exec","maint-survey-pg-20261030","psql","-U","survey","-d","itsp","-At","-c",sql],capture_output=True,text=True).stdout.strip()
    print(k, "batches", len(v), "orders|project_raw非空|storage_center非空|最早|最晚 =", out)
