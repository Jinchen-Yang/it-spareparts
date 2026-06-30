"""PN 近似解析器（二期检索地基）。

智能体核心回路第一环：用户模糊文本（"super 4089RT-x 准系统"）→ 排序后的 pn_std 候选。
分两段：召回靠库内 pg_trgm（pn_compact / search_doc / part_alias 三路，GIN 毫秒级），
精排在 Python（型号 token 相似度为主 + 词覆盖 + 包含/精确/别名加成，产出可解释 match_reason）。

规模取舍：2~10 万型号在库内近似检索即毫秒级，不引入外部搜索引擎（部署零新增组件）。
part_alias 在此从"只写不读"变为活数据：人工修订别名即刻影响匹配。

整改 P3：召回排除 status='merged' 墓碑（与 search_parts 一致）。查询已合并型号的
旧 pn 仍能命中——merge 会留下 pn_raw=旧pn→pn_std=目标 的 active 别名，走别名召回路
重定向到目标，不靠墓碑本身。
"""
import re

from sqlalchemy import Text as SAText
from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from app import config
from app.etl.cleaner import standardize_pn
from app.models.dimensions import DimPart
from app.models.system import SysAuditLog

_CJK = re.compile(r"[一-鿿]{2,}")
_ALNUM = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-\._/]*")
_STRIP = re.compile(r"[^A-Z0-9]")

_CAND_LIMIT = 60

# 召回 SQL：四路 OR —— 主 token 相似(%)/词相似(<%)/双向包含 + 其余 token 包含 + 检索文档命中
_PART_SQL = text("""
SELECT p.pn_std, p.pn_compact, p.search_doc, p.description, p.brand,
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
    return {"main": main, "others": others, "cjk": cjk, "syn": syn,
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


def resolve(db: Session, query: str, limit: int = 10,
            operated_by: str | None = None, log_miss: bool = True) -> dict:
    """近似解析：返回 {"query", "items": [...], "low_confidence"}。

    items 按 score 降序，含 match_reason（如 "PN相似0.67；PN包含匹配；命中'超微'"）。
    零命中时落 sys_audit_log（action=search_miss）供治理回看。
    log_miss=False：内部复用（如新建去重查重）不写 search_miss、不 commit——
    避免污染治理工单、也避免在调用方事务中途 commit（见 master_edit.find_near_duplicates）。
    """
    ctx, doc_terms = preprocess(query)
    if not ctx["main"] and not doc_terms:
        # 纯符号/无有效 token（如 "!!!"、"---"）：早返回也要带全部键，
        # 否则 /parts/search 与 _lookup_prices_bulk 无条件读 ambiguous 会 KeyError(500)
        return {"query": query, "items": [], "low_confidence": True, "ambiguous": False}

    # 放宽 trigram 召回阈值（仅本事务）：长库值×短 token 时默认 0.3 偏紧
    db.execute(text("SET LOCAL pg_trgm.similarity_threshold = 0.25"))
    db.execute(text("SET LOCAL pg_trgm.word_similarity_threshold = 0.35"))

    rows = db.execute(_PART_SQL, {
        "main": ctx["main"], "others": ctx["others"],
        "doc_terms": doc_terms, "cand_limit": _CAND_LIMIT,
    }).mappings().all()
    cands: dict[str, dict] = {r["pn_std"]: dict(r) for r in rows}

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
                    "pn_std": p.pn_std, "pn_compact": p.pn_compact, "search_doc": p.search_doc,
                    "description": p.description, "brand": p.brand,
                    "category_major": p.category_major, "needs_review": p.needs_review,
                    "is_excluded": p.is_excluded, "sim": 0,
                }

    items = []
    for pn_std, row in cands.items():
        score, reason = _score(row, ctx, alias_hits.get(pn_std))
        items.append({
            "pn_std": pn_std, "description": row.get("description"),
            "brand": row.get("brand"), "category_major": row.get("category_major"),
            "needs_review": bool(row.get("needs_review")),
            "is_excluded": bool(row.get("is_excluded")),
            "score": score, "match_reason": reason,
        })
    items.sort(key=lambda x: (-x["score"], len(x["pn_std"]), x["pn_std"]))
    items = items[:limit]

    if not items and log_miss:
        _log_miss(db, query, operated_by)
    low_conf = (not items) or items[0]["score"] < config.RESOLVE_LOW_CONFIDENCE

    # 歧义检测：多候选并列高分 = 口头型号对应多个规格变体（如 V100 → 16G/32G/PCIE/NVLINK）。
    # 例外：top1 精确匹配（用户敲了完整 PN）不算歧义。
    ambiguous = False
    if len(items) >= 2 and not low_conf and "PN精确匹配" not in items[0]["match_reason"]:
        near = [it for it in items if it["score"] >= max(items[0]["score"] * 0.85, 0.3)]
        ambiguous = len(near) >= 2

    return {"query": query, "items": items, "low_confidence": low_conf,
            "ambiguous": ambiguous}
