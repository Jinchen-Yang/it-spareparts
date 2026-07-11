"""通用号数据池：稳定 group_id 重算（老板看板池化分析地基）。

甲方 2026-07-11 修正版第①条：不复用运行时 BFS（型号页临时展示、4 层/60 成员上限、
入口不同结果不同、加一个型号可能触顶）——建稳定池：
- 池边 = 已生效双向互替（status='active' AND direction='both'）；单向替代不成池。
- 连通分量为池，成员≥2（单点不成池）。
- 关系变化时 rebuild：**保留稳定 group_id**（按成员重叠复用），并报告本次合并/拆分。
- 池内任一边缺 substitute_type → needs_calibration（关系待校准）。
- 成员超 POOL_OVERSIZE_MEMBERS → oversized（需人工确认）。
"""
from collections import defaultdict

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import config
from app.models.inventory import PartPool, PartPoolMember, PartSubstitute


def _components(edges: list[tuple[int, int]]) -> list[set[int]]:
    """并查集求连通分量（只含出现在边里的点，故天然成员≥2）。"""
    parent: dict[int, int] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for a, b in edges:
        union(a, b)
    groups: dict[int, set[int]] = defaultdict(set)
    for x in parent:
        groups[find(x)].add(x)
    return list(groups.values())


def rebuild(db: Session, dry_run: bool = False) -> dict:
    """从已生效双向互替关系重算稳定池。dry_run=True 只返回预览不落库。

    返回：pools/parts_pooled 计数、new/merged/split/unchanged 报告、
    needs_calibration/oversized 池清单。合并=一个新分量吃了多个旧池；拆分=一个旧池散到多个新分量。
    """
    # 池边：已生效 + 双向互替
    edge_rows = db.execute(
        select(PartSubstitute.part_id_a, PartSubstitute.part_id_b, PartSubstitute.substitute_type)
        .where(PartSubstitute.status == "active", PartSubstitute.direction == "both")
    ).all()
    edges = [(a, b) for a, b, _ in edge_rows]
    comps = _components(edges)

    # 每个分量内是否有缺类型的边（关系待校准）
    comp_of: dict[int, int] = {}   # part_id -> comp index
    for i, m in enumerate(comps):
        for p in m:
            comp_of[p] = i
    comp_missing_type = [False] * len(comps)
    for a, b, stype in edge_rows:
        if stype is None and a in comp_of:
            comp_missing_type[comp_of[a]] = True

    # 现有映射（用于稳定 ID 复用）
    existing = dict(db.execute(select(PartPoolMember.part_id, PartPoolMember.group_id)).all())
    existing_ids = set(existing.values())
    existing_groups: dict[int, set[int]] = defaultdict(set)
    for p, g in existing.items():
        existing_groups[g].add(p)
    next_id = (max(existing_ids) + 1) if existing_ids else 1

    # 大分量先认领主导 ID（成员重叠最多的旧 group_id）；稳定排序保证可复现
    order = sorted(range(len(comps)), key=lambda i: (-len(comps[i]), min(comps[i])))
    claimed: set[int] = set()
    assigned: dict[int, int] = {}          # comp index -> group_id
    existing_ids_in_comp: dict[int, set[int]] = {}
    report = {"new": [], "merged": [], "split": [], "unchanged": 0}

    for i in order:
        members = comps[i]
        tally: dict[int, int] = defaultdict(int)
        for p in members:
            if p in existing:
                tally[existing[p]] += 1
        existing_ids_in_comp[i] = set(tally)
        # 候选=重叠最多的旧 ID（并列取小），未被本轮认领才可复用
        cand = None
        for gid in sorted(tally, key=lambda g: (-tally[g], g)):
            if gid not in claimed:
                cand = gid
                break
        if cand is not None:
            assigned[i] = cand
            claimed.add(cand)
            if set(tally) == {cand} and existing_groups.get(cand) == members:
                report["unchanged"] += 1
        else:
            assigned[i] = next_id
            next_id += 1

    # 合并/拆分报告
    for i in order:
        others = existing_ids_in_comp[i] - {assigned[i]}
        if others:
            report["merged"].append({"into": assigned[i], "from": sorted(others), "size": len(comps[i])})
    # 拆分：某旧 group_id 的成员散到 ≥2 个新分量
    old_to_comps: dict[int, set[int]] = defaultdict(set)
    for i in range(len(comps)):
        for gid in existing_ids_in_comp[i]:
            old_to_comps[gid].add(i)
    for gid, cs in old_to_comps.items():
        if len(cs) > 1:
            report["split"].append({"from": gid, "into": sorted(assigned[i] for i in cs)})
    for i in order:
        if not existing_ids_in_comp[i]:
            report["new"].append({"group_id": assigned[i], "size": len(comps[i])})

    calib, over = [], []
    pools_out = []
    for i in range(len(comps)):
        gid = assigned[i]
        size = len(comps[i])
        nc = comp_missing_type[i]
        ov = size > config.POOL_OVERSIZE_MEMBERS
        if nc:
            calib.append(gid)
        if ov:
            over.append(gid)
        pools_out.append((gid, sorted(comps[i]), size, nc, ov))

    result = {
        "dry_run": dry_run,
        "pools": len(comps),
        "parts_pooled": sum(len(m) for m in comps),
        "needs_calibration": sorted(calib),
        "oversized": sorted(over),
        **report,
    }
    if dry_run:
        return result

    db.execute(delete(PartPoolMember))
    db.execute(delete(PartPool))
    db.flush()
    for gid, members, size, nc, ov in pools_out:
        db.add(PartPool(group_id=gid, member_count=size, needs_calibration=nc, oversized=ov))
    db.flush()
    for gid, members, size, nc, ov in pools_out:
        for p in members:
            db.add(PartPoolMember(part_id=p, group_id=gid))
    db.commit()
    return result
