"""PN 近似解析器（二期检索地基）。

智能体核心回路第一环：用户模糊文本（"super 4089RT-x 准系统"）→ 排序后的 pn_std 候选。
分两段：召回靠库内 pg_trgm（pn_compact / search_doc / part_alias 三路，GIN 毫秒级），
精排在 Python（型号 token 相似度为主 + 词覆盖 + 包含/精确/别名加成，产出可解释 match_reason）。

规模取舍：2~10 万型号在库内近似检索即毫秒级，不引入外部搜索引擎（部署零新增组件）。
part_alias 在此从"只写不读"变为活数据：人工修订别名即刻影响匹配。

整改 P3：召回排除 status='merged' 墓碑（与 search_parts 一致）。查询已合并型号的
旧 pn 仍能命中——merge 会留下 pn_raw=旧pn→pn_std=目标 的 active 别名，走别名召回路
重定向到目标，不靠墓碑本身。

统一搜索（第②块，02311DYQ 案）：resolve() 是采购/销售/看板/型号全景共用的
"文本→part_id"唯一出口。规则：PN/别名精确命中 → 只返回唯一标准型号（exact=True），
相似候选降级到 similar_items 单独区域；无精确命中才走 前缀/包含/描述/模糊 排序。
每条结果带统一结构：part_id / match_type / matched_text / score / 池身份(pool_group_id+pool_name)。
"""
import re

from sqlalchemy import Text as SAText
from sqlalchemy import bindparam, select, text
from sqlalchemy import case as sa_case
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from app import config
from app.etl.cleaner import standardize_pn
from app.models.dimensions import DimPart, PartAlias
from app.models.inventory import PartPool, PartPoolMember
from app.models.system import SysAuditLog
from app.services.query_filters import col_matches_any, keyword_term_groups

_CJK = re.compile(r"[一-鿿]{2,}")
_ALNUM = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-\._/]*")
_STRIP = re.compile(r"[^A-Z0-9]")

_CAND_LIMIT = 60

# 召回 SQL：四路 OR —— 主 token 相似(%)/词相似(<%)/双向包含 + 其余 token 包含 + 检索文档命中
_PART_SQL = text("""
SELECT p.id AS part_id, p.pn_std, p.pn_compact, p.search_doc, p.description, p.brand,
       p.category_major, p.needs_review, p.is_excluded,
       CASE WHEN length(:main) >= 2
            THEN greatest(similarity(p.pn_compact, :main),
                          word_similarity(:main, p.pn_compact))
            ELSE 0 END AS sim
FROM dim_part p
WHERE p.status <> 'merged' AND (
       (length(:main) >= 2 AND (
          p.pn_compact % :main
          OR :main <% p.pn_compact
          OR (length(:main) >= 3 AND strpos(p.pn_compact, :main) > 0)
          OR (length(p.pn_compact) >= 4 AND strpos(:main, p.pn_compact) > 0)))
   OR EXISTS (SELECT 1 FROM unnest(CAST(:others AS text[])) AS t
              WHERE length(t) >= 3 AND strpos(p.pn_compact, t) > 0)
   OR EXISTS (SELECT 1 FROM unnest(CAST(:doc_terms AS text[])) AS t
              WHERE p.search_doc ILIKE '%' || t || '%'))
ORDER BY sim DESC, length(p.pn_std) ASC
LIMIT :cand_limit
""").bindparams(
    bindparam("others", type_=ARRAY(SAText())),
    bindparam("doc_terms", type_=ARRAY(SAText())),
)

# 描述命中召回：查询词（含规格变体归一：6Gbps↔6Gb/s、3.5寸↔3.5-inch、7200rpm↔7.2K）
# 在 search_doc 上按"命中词数"召回。PN 车道对这类查询失效——token 化剥掉 ./-，主 token 还会
# 选中 '35INCH' 这类规格词去和全库编号做 trigram。≥4 词允许错 1 个（覆盖率降级，按比例给分），
# 2~3 词与单词要求全中；命中数降序保证全中行绝不被部分命中行挤出候选池。
_DOC_CAND_LIMIT = 120


def _doc_recall(db, groups):
    """按变体词组召回：返回 {pn_std: 命中词数}，并给出候选行。"""
    n = len(groups)
    conds = [col_matches_any(DimPart.search_doc, g) for g in groups]
    hits = sum(sa_case((c, 1), else_=0) for c in conds)
    min_hits = n if n <= 3 else n - 1
    stmt = (
        select(DimPart.id.label("part_id"), DimPart.pn_std, DimPart.pn_compact,
               DimPart.search_doc, DimPart.description,
               DimPart.brand, DimPart.category_major, DimPart.needs_review, DimPart.is_excluded,
               hits.label("hits"))
        .where(DimPart.status != "merged", hits >= min_hits)
        .order_by(hits.desc(), DimPart.pn_std)
        .limit(_DOC_CAND_LIMIT)
    )
    return db.execute(stmt).mappings().all()

# 别名召回：对"原值写法"模糊匹配，折叠到 pn_std。
# 排除恒等别名（compact 与型号自身相同）——那只是导入痕迹，不是额外证据
_ALIAS_SQL = text("""
SELECT a.pn_std, a.pn_raw, a.pn_compact,
       greatest(similarity(a.pn_compact, :main),
                word_similarity(:main, a.pn_compact)) AS sim
FROM part_alias a
JOIN dim_part p ON p.pn_std = a.pn_std
WHERE a.pn_compact IS DISTINCT FROM p.pn_compact
  AND p.status <> 'merged'
  AND length(:main) >= 2 AND (
          a.pn_compact % :main
          OR :main <% a.pn_compact
          OR (length(:main) >= 3 AND strpos(a.pn_compact, :main) > 0)
          OR (length(a.pn_compact) >= 4 AND strpos(:main, a.pn_compact) > 0))
ORDER BY sim DESC
LIMIT 20
""")


def _expand_brand_synonyms(tokens_lower: set[str]) -> list[str]:
    """查询命中同义词组任一写法 → 返回组内其余写法（原样大小写，用于 ILIKE）。"""
    extra: list[str] = []
    for group in config.BRAND_SYNONYMS:
        if any(g.lower() in tokens_lower for g in group):
            extra.extend(g for g in group if g.lower() not in tokens_lower)
    return extra


def preprocess(query: str) -> tuple[dict, list[str]]:
    """查询预处理：V 码截断（复用导入侧 cleaner）→ 提取 token → 选主 token → 同义词扩展。

    主 token = 最具区分度的型号 token：含数字者优先，再取最长。
    """
    std, _, _ = standardize_pn(query)
    q = (std or "").strip()
    cjk = _CJK.findall(q)
    alnum = [_STRIP.sub("", t.upper()) for t in _ALNUM.findall(q)]
    alnum = [t for t in alnum if len(t) >= 2]
    main = max(alnum, key=lambda t: (any(c.isdigit() for c in t), len(t)), default="")
    others = [t for t in alnum if t != main]
    syn = _expand_brand_synonyms({t.lower() for t in alnum} | set(cjk))
    # 检索文档命中词：中文词 + 同义词扩展 + 其余字母数字 token；去 ILIKE 通配符防注入
    doc_terms = [t.replace("%", "").replace("_", "") for t in (cjk + syn + others)]
    doc_terms = [t for t in doc_terms if len(t) >= 2]
    # joined：整个查询压成一个 compact（"02311 DYQ"→"02311DYQ"）。空格/连字符把一个 PN
    # 拆成多 token 时，靠它走"精确即唯一"车道；含中文的查询是描述语义，不参与。
    joined = _STRIP.sub("", q.upper()) if not cjk else ""
    return {"main": main, "others": others, "cjk": cjk, "syn": syn, "joined": joined,
            "coverage_terms": list(dict.fromkeys(cjk + syn + others))}, doc_terms


def _score(row: dict, ctx: dict, alias_hit: tuple[float, str] | None) -> tuple[float, str]:
    """精排打分 + 可解释理由。权重：型号相似 0.55 / 精确 0.25 / 包含 0.20 / 覆盖 0.15 / 别名 0.10。

    精确 > 包含是硬约束：trigram 对重复片段会打满分（ST8000NM00A vs ST8000NM000A
    相似度=1.0），唯有精确加成能保证"完全一致"恒排第一。
    """
    sim = float(row.get("sim") or 0)
    if alias_hit:
        sim = max(sim, float(alias_hit[0] or 0))
    reasons: list[str] = []
    score = 0.55 * sim
    # 描述命中（含规格变体归一）：全中=强证据；≥4 词错 1 个按比例给分排在全中之后；
    # 单词命中给较低加成，避免"描述提及"盖过真 PN 匹配（PN 包含≈0.55*sim+0.20 > 0.40）
    k = ctx.get("doc_hits", {}).get(row.get("pn_std"), 0)
    n = ctx.get("term_count", 0)
    if k and n:
        if k >= n:
            if n >= 2:
                # 吻合密度：查询词总长 / 描述长——用户敲的就是（近）整段描述时，
                # 描述≈查询本身的行排在"描述更长、含更多别的词"的同规格行之前（同分并列破局）
                desc_len = len(row.get("description") or "") or 1
                density = min(1.0, ctx.get("q_len", 0) / desc_len)
                score += 0.5 + 0.1 * density
                reasons.append("描述全词命中")
            else:
                score += 0.25
                reasons.append("描述命中")
        else:
            score += 0.5 * k / n
            reasons.append(f"描述命中{k}/{n}词")
    if sim >= 0.3:
        reasons.append(f"PN相似{sim:.2f}")
    pc, main = row.get("pn_compact") or "", ctx["main"]
    if main and pc:
        if pc == main:
            score += 0.25
            reasons.append("PN精确匹配")
        elif (len(main) >= 3 and main in pc) or (len(pc) >= 4 and pc in main):
            score += 0.20
            reasons.append("PN包含匹配")
    doc = (row.get("search_doc") or "").upper()
    terms = ctx["coverage_terms"]
    if terms and doc:
        hits = [t for t in terms if t.upper() in doc]
        if hits:
            score += 0.15 * len(hits) / len(terms)
            reasons.append("命中" + "/".join(f"'{h}'" for h in hits[:3]))
    if alias_hit:
        score += 0.10
        reasons.append(f"别名命中({alias_hit[1][:40]})")
    if row.get("needs_review"):
        reasons.append("PN待复核")
    if row.get("is_excluded"):
        reasons.append("已治理排除")
    return min(round(score, 4), 1.0), "；".join(reasons) if reasons else "弱相关"


def _log_miss(db: Session, query: str, operated_by: str | None) -> None:
    if not config.SEARCH_MISS_LOG:
        return
    try:
        db.add(SysAuditLog(entity_type="part_search", entity_id=0, action="search_miss",
                           after_json={"query": query[:200]},
                           operated_by=operated_by or "unknown"))
        db.commit()
    except Exception:  # noqa: BLE001 —— 审计失败不影响搜索本身
        db.rollback()


def _exact_lookup(db: Session, main: str) -> list[dict]:
    """PN/别名与查询 compact **完全一致**的非合并型号（统一搜索：精确即唯一）。
    别名命中沿 pn_std 折叠到目标型号（merged 墓碑重定向天然生效）；同型号双路命中只留一条，
    别名证据保留在 alias_raw。"""
    found: dict[int, dict] = {}
    for p in db.execute(select(DimPart).where(
            DimPart.pn_compact == main, DimPart.status != "merged")).scalars():
        found[p.id] = {"part": p, "alias_raw": None}
    rows = db.execute(
        select(PartAlias.pn_raw, DimPart)
        .join(DimPart, DimPart.pn_std == PartAlias.pn_std)
        .where(PartAlias.pn_compact == main, DimPart.status != "merged"))
    for pn_raw, p in rows:
        if p.id not in found:
            found[p.id] = {"part": p, "alias_raw": pn_raw}
    # 确定性排序：PN 直中优先于别名命中，同层按 pn_std——多精确歧义时展示顺序稳定
    return sorted(found.values(),
                  key=lambda h: (h["alias_raw"] is not None, h["part"].pn_std))


def _exact_item(hit: dict) -> dict:
    p, alias_raw = hit["part"], hit["alias_raw"]
    reasons = ["PN精确匹配"] if alias_raw is None else [f"别名命中({alias_raw[:40]})", "PN精确匹配"]
    if p.needs_review:
        reasons.append("PN待复核")
    if p.is_excluded:
        reasons.append("已治理排除")
    return {"part_id": p.id, "pn_std": p.pn_std, "description": p.description,
            "brand": p.brand, "category": p.category_major, "category_major": p.category_major,
            "needs_review": bool(p.needs_review), "is_excluded": bool(p.is_excluded),
            "match_type": "exact_pn" if alias_raw is None else "exact_alias",
            "matched_text": p.pn_std if alias_raw is None else alias_raw,
            "score": 1.0, "match_reason": "；".join(reasons)}


def pool_identity_map(db: Session, part_ids: list[int]) -> dict[int, tuple[int, str]]:
    """part_id → (pool_group_id, pool_name)，只看有效池。
    "一个有效 PN 只属一个有效池"由 pool_catalog 写路保证；若脏数据多池并存，
    取 group_id 最小者，保持确定性。池身份对登录用户全员可读（与 GET /pools 门一致），
    不带任何约束价字段——价格治理数据仍走 data_pool_price_governance 权限桶。"""
    ids = [i for i in part_ids if i]
    if not ids:
        return {}
    rows = db.execute(
        select(PartPoolMember.part_id, PartPool.group_id, PartPool.name)
        .join(PartPool, PartPool.group_id == PartPoolMember.group_id)
        .where(PartPoolMember.part_id.in_(ids), PartPool.status == "active")
        .order_by(PartPoolMember.part_id, PartPool.group_id)).all()
    out: dict[int, tuple[int, str]] = {}
    for pid, gid, name in rows:
        out.setdefault(pid, (gid, name))
    return out


def _attach_pool_identity(db: Session, items: list[dict]) -> None:
    pools = pool_identity_map(db, [it.get("part_id") for it in items])
    for it in items:
        hit = pools.get(it.get("part_id"))
        it["pool_group_id"] = hit[0] if hit else None
        it["pool_name"] = hit[1] if hit else None


def _match_meta(row: dict, ctx: dict, alias_hit: tuple[float, str] | None) -> tuple[str, str]:
    """按最强证据分类 match_type + 给出命中文本 matched_text（库侧被命中的字符串）。
    优先级：exact_pn > exact_alias > fuzzy_pn > alias > description > weak。"""
    pc, main = row.get("pn_compact") or "", ctx["main"]
    q_compact = ctx.get("joined") or main   # 整查询 compact（空格/连字符拆分后仍算精确）
    pn_std = row.get("pn_std") or ""
    if q_compact and pc == q_compact:
        return "exact_pn", pn_std
    if alias_hit and q_compact and _STRIP.sub("", alias_hit[1].upper()) == q_compact:
        return "exact_alias", alias_hit[1]
    sim = float(row.get("sim") or 0)
    if alias_hit:
        sim = max(sim, float(alias_hit[0] or 0))
    if main and pc and ((len(main) >= 3 and main in pc) or (len(pc) >= 4 and pc in main)
                        or sim >= 0.3):
        return "fuzzy_pn", pn_std
    if alias_hit:
        return "alias", alias_hit[1]
    if ctx.get("doc_hits", {}).get(pn_std):
        return "description", row.get("description") or pn_std
    return "weak", pn_std


def _fuzzy_rank(db: Session, query: str, ctx: dict, doc_terms: list[str], limit: int) -> list[dict]:
    """模糊召回 + 精排（原 resolve 主体）：返回排序、截断后的 items（不含池身份）。"""
    # 放宽 trigram 召回阈值（仅本事务）：长库值×短 token 时默认 0.3 偏紧
    db.execute(text("SET LOCAL pg_trgm.similarity_threshold = 0.25"))
    db.execute(text("SET LOCAL pg_trgm.word_similarity_threshold = 0.35"))

    rows = db.execute(_PART_SQL, {
        "main": ctx["main"], "others": ctx["others"],
        "doc_terms": doc_terms, "cand_limit": _CAND_LIMIT,
    }).mappings().all()
    cands: dict[str, dict] = {r["pn_std"]: dict(r) for r in rows}

    # 描述命中车道：不占 PN 车道的 60 候选名额，命中行直接入池并按命中词数加分。
    # 单词也跑（"8TB" 这类规格词此前只会和全库编号做相似度，描述里的 8TB 盘一个不出），
    # 但加分低于多词全命中（0.25 vs 0.5，见 _score）——敲完整 PN 时 PN 证据仍稳赢。
    groups = keyword_term_groups(query)
    ctx["doc_hits"] = {}
    ctx["term_count"] = len(groups)
    ctx["q_len"] = sum(len(g[0]) for g in groups)
    if groups:
        for r in _doc_recall(db, groups):
            ctx["doc_hits"][r["pn_std"]] = int(r["hits"])
            if r["pn_std"] not in cands:
                d = dict(r)
                d.pop("hits", None)
                d["sim"] = 0
                cands[r["pn_std"]] = d

    # 别名召回 → 折叠到 pn_std；别名独有的型号补取 dim_part 行
    alias_hits: dict[str, tuple[float, str]] = {}
    if ctx["main"]:
        for a in db.execute(_ALIAS_SQL, {"main": ctx["main"]}).mappings():
            prev = alias_hits.get(a["pn_std"])
            if prev is None or a["sim"] > prev[0]:
                alias_hits[a["pn_std"]] = (float(a["sim"] or 0), a["pn_raw"])
        missing = [pn for pn in alias_hits if pn not in cands]
        if missing:
            for p in db.execute(select(DimPart).where(
                    DimPart.pn_std.in_(missing), DimPart.status != "merged")).scalars():
                cands[p.pn_std] = {
                    "part_id": p.id, "pn_std": p.pn_std, "pn_compact": p.pn_compact,
                    "search_doc": p.search_doc,
                    "description": p.description, "brand": p.brand,
                    "category_major": p.category_major, "needs_review": p.needs_review,
                    "is_excluded": p.is_excluded, "sim": 0,
                }

    items = []
    for pn_std, row in cands.items():
        score, reason = _score(row, ctx, alias_hits.get(pn_std))
        match_type, matched_text = _match_meta(row, ctx, alias_hits.get(pn_std))
        items.append({
            "part_id": row.get("part_id"), "pn_std": pn_std, "description": row.get("description"),
            "brand": row.get("brand"), "category": row.get("category_major"),
            "category_major": row.get("category_major"),
            "needs_review": bool(row.get("needs_review")),
            "is_excluded": bool(row.get("is_excluded")),
            "match_type": match_type, "matched_text": matched_text,
            "score": score, "match_reason": reason,
        })
    items.sort(key=lambda x: (-x["score"], len(x["pn_std"]), x["pn_std"]))

    # 规格词查询的品牌轮播（甲方：搜规格看不到希捷）：几十个同规格行全词命中时，展示位只有
    # limit 个，同分微差会让某品牌整体沉底。对"全词命中"段按品牌轮流取行（段内原序保留），
    # 每个品牌的最佳行都进前排；想只看某品牌，查询里加品牌词即可。PN 查询（单词/无全中段）不受影响。
    n_terms = ctx.get("term_count", 0)
    if n_terms >= 2 and ctx.get("doc_hits"):
        full = [it for it in items if ctx["doc_hits"].get(it["pn_std"], 0) >= n_terms]
        if len(full) > 2:
            rest = [it for it in items if ctx["doc_hits"].get(it["pn_std"], 0) < n_terms]
            lanes: dict[str, list] = {}
            order: list[str] = []
            for it in full:
                b = (it.get("brand") or "").strip()
                if b not in lanes:
                    lanes[b] = []
                    order.append(b)
                lanes[b].append(it)
            mixed: list = []
            while len(mixed) < len(full):
                for b in order:
                    if lanes[b]:
                        mixed.append(lanes[b].pop(0))
            items = mixed + rest
    return items[:limit]


_SIMILAR_LIMIT = 8   # exact 命中时"相似型号"区最多带几条（响应体量可控）


def resolve(db: Session, query: str, limit: int = 10,
            operated_by: str | None = None, log_miss: bool = True,
            include_similar: bool = False) -> dict:
    """统一解析：返回 {"query", "exact", "items", "similar_items", "low_confidence", "ambiguous"}。

    items 按 score 降序，每条含 part_id（统一商品身份）/ match_type / matched_text /
    pool_group_id / pool_name / match_reason（如 "PN相似0.67；PN包含匹配；命中'超微'"）。
    **精确即唯一**（02311DYQ 案）：查询本身就是一个 PN（无中文/无其余 token）且与某型号
    或别名的 compact 完全一致时，items 只有那唯一的标准型号（exact=True），trigram 相似
    候选不再混入同等级结果；include_similar=True 时相似候选降级到 similar_items（供前端
    "相似型号"独立区域，内部消费方默认不取、零额外开销）。
    多个未合并型号同 compact（脏数据）或多个别名同写法指向不同型号 → 不短路，
    走排序并标 ambiguous=True 要求消歧。
    零命中时落 sys_audit_log（action=search_miss）供治理回看。
    log_miss=False：内部复用（如新建去重查重）不写 search_miss、不 commit——
    避免污染治理工单、也避免在调用方事务中途 commit（见 master_edit.find_near_duplicates）。
    """
    ctx, doc_terms = preprocess(query)
    if not ctx["main"] and not doc_terms:
        # 纯符号/无有效 token（如 "!!!"、"---"）：早返回也要带全部键，
        # 否则 /parts/search 与 _lookup_prices_bulk 无条件读 ambiguous 会 KeyError(500)
        return {"query": query, "exact": False, "items": [], "similar_items": [],
                "low_confidence": True, "ambiguous": False}

    # 精确即唯一短路：整查询压成一个 compact 后与 PN/别名完全一致（无中文——中文是
    # 描述语义）。joined 车道让"02311 DYQ"/"02311-DYQ"这类空格/连字符拆分照样精确。
    exact_hits: list[dict] = []
    if ctx["joined"]:
        exact_hits = _exact_lookup(db, ctx["joined"])
    if len(exact_hits) == 1:
        item = _exact_item(exact_hits[0])
        similar: list[dict] = []
        if include_similar:
            fuzzy = _fuzzy_rank(db, query, ctx, doc_terms, limit)
            similar = [it for it in fuzzy if it["part_id"] != item["part_id"]][:_SIMILAR_LIMIT]
        _attach_pool_identity(db, [item] + similar)
        return {"query": query, "exact": True, "items": [item], "similar_items": similar,
                "low_confidence": False, "ambiguous": False}

    items = _fuzzy_rank(db, query, ctx, doc_terms, limit)

    # 多精确命中（脏数据同 compact / 多别名同写法）：全部精确目标统一置顶后标歧义。
    # 常规召回按 compact 包含几乎必中，但为防"歧义但目标缺席"的不可判局面，统一用
    # _exact_item 重建这几条（同时保证 match_type=exact_* 口径一致）。
    if len(exact_hits) >= 2:
        exact_ids = {h["part"].id for h in exact_hits}
        tail = [it for it in items if it["part_id"] not in exact_ids]
        items = ([_exact_item(h) for h in exact_hits] + tail)[:limit]

    if not items and log_miss:
        _log_miss(db, query, operated_by)
    low_conf = (not items) or items[0]["score"] < config.RESOLVE_LOW_CONFIDENCE

    # 歧义检测：多候选并列高分 = 口头型号对应多个规格变体（如 V100 → 16G/32G/PCIE/NVLINK）。
    # 例外：top1 精确匹配（用户敲了完整 PN）不算歧义——但**多个型号/别名同 compact 精确命中**
    # 是真歧义，必须反问（此时不走"精确即唯一"短路）。
    ambiguous = False
    if len(exact_hits) >= 2:
        ambiguous = True
        low_conf = False   # 精确命中不算低置信，只是需要人工消歧
    elif (len(items) >= 2 and not low_conf
          and items[0]["match_type"] not in ("exact_pn", "exact_alias")):
        near = [it for it in items if it["score"] >= max(items[0]["score"] * 0.85, 0.3)]
        ambiguous = len(near) >= 2

    _attach_pool_identity(db, items)
    return {"query": query, "exact": False, "items": items, "similar_items": [],
            "low_confidence": low_conf, "ambiguous": ambiguous}
