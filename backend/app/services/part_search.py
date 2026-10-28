"""M1 实时检索：投影召回、分档排序，调用方只依赖 search。"""
import re

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy import Integer, String
from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.part_resolver import preprocess
from app.services.part_search_normalize import compact, normalize
from app.services.spec_extract import spec_label

__all__ = ["search"]

# 与 part_search_short_keys 迁移冻结的汉字范围一致。
_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f\U00030000-\U000323af"
_SHORT_TERM = re.compile(r"(?:[A-Z0-9]{1,2}|[" + _CJK + r"]{1,2})")


def _boost_part_ids(db: Session, q: str) -> set[int]:
    """报价模块接入点；M1 不读取业务报价表。"""
    return set()


def _pattern(term: str, *, compacted: bool = False) -> str | None:
    # 含字面符号的词不走 compact，避免 %/_/反斜杠或纯中文被剥掉后扩大召回。
    if compacted:
        if any(c in term for c in "%_\\"):
            return None
        term = "".join(c if c in "*?" else compact(c) for c in term)
        if not re.search(r"[A-Z0-9]", term):
            return None
    return "".join("%" if c == "*" else "_" if c == "?"
                   else "\\" + c if c in "%_\\" else c for c in term)


def _contains(column: str, parameter: str) -> str:
    # 投影和查询已按同一规则转大写，LIKE 保留大小写不敏感语义并避免重复折叠全文。
    operator = "LIKE" if column in ("s.doc", "s.doc_compact", "p.pn_compact", "x.pn_compact") else "ILIKE"
    return f"{column} {operator} :{parameter} ESCAPE E'\\\\'"


def _normalized(column: str) -> str:
    # ASCII 常见资料无需 Unicode 兼容变换；含非 ASCII 或重复空白时仍走统一函数。
    return (f"CASE WHEN {column} ~ '[^ -~]|  ' THEN part_search_normalize({column}) "
            f"ELSE btrim(upper(coalesce({column},''))) END")


def _compacted(column: str) -> str:
    return (f"CASE WHEN {column} ~ '^[一-鿿 /]+$' THEN '' "
            f"WHEN {column} ~ '[^ -~]' THEN part_search_compact({column}) "
            f"ELSE regexp_replace(upper(coalesce({column},'')), '[^A-Z0-9]', '', 'g') END")


def _match_text(column: str) -> str:
    # 基本汉字/可打印 ASCII 不受 NFKC 改写，可由 ILIKE 直接折叠大小写。
    # 其余字符（如 ß→SS）必须先归一，尤其不能让 ? 按原文字符数判断。
    return (f"CASE WHEN {column} ~ '[^ -~一-鿿]|  ' THEN part_search_normalize({column}) "
            f"ELSE btrim(coalesce({column},'')) END")


def _term_condition(term: str, i: int, params: dict) -> str:
    params[f"t{i}"] = "%" + _pattern(term) + "%"
    cond = _contains("s.doc", f"t{i}")
    cp = _pattern(term, compacted=True)
    if cp:
        params[f"c{i}"] = "%" + cp + "%"
        cond += " OR " + _contains("s.doc_compact", f"c{i}")
    if _SHORT_TERM.fullmatch(term):
        # 每个键都是同一归一化 doc 的字面子串，无需再读取 doc 做 LIKE。
        # 保留文档行关联，纯短词可只读文档主键索引。
        # 混合字符、标点及通配仍走原路径，避免没有键时漏召回。
        params[f"sk{i}"] = term
        return f"(k.keys @> ARRAY[:sk{i}]::text[])"
    return "(" + cond + ")"


def _short_statement(stmt, terms: list[str], q: str):
    # 只把短词自身的值交给规划器，避免通用计划丢失高频短键的选择率。
    # SQLAlchemy 负责安全引用；长词绑定、规划设置及同事务后续语句不变。
    names = {f"{prefix}{i}" for i, term in enumerate(terms) if _SHORT_TERM.fullmatch(term)
             for prefix in ("sk", "t", "c")}
    if not names:
        return stmt
    if _SHORT_TERM.fullmatch(q):
        names.update(("q", "exact", "prefix", "pn_contains", "compact_prefix", "compact_contains"))
    present = set(re.findall(r":([a-z][a-z0-9_]*)", str(stmt)))
    bindings = [bindparam(name, type_=String(), literal_execute=True)
                for name in sorted(names & present)]
    if _SHORT_TERM.fullmatch(q):
        # 单短词的提前停止还依赖实际上限，不能采用通用 LIMIT 的默认估计。
        bindings.extend(bindparam(name, type_=Integer(), literal_execute=True)
                        for name in ("limit", "remaining", "candidate_limit") if name in present)
    return stmt.bindparams(*bindings)


def _field_weight(terms: list[str]) -> str:
    weights = []
    def field_match(column: str, i: int) -> str:
        cp = _pattern(terms[i], compacted=True)
        condition = _contains("(" + _match_text(column) + ")", f"t{i}")
        if cp:
            compact_condition = _contains("(" + _compacted(column) + ")", f"c{i}")
            if cp == _pattern(terms[i]):
                # 原字段已经是 ASCII 字母数字时，两种匹配完全等价。
                compact_condition = f"({column} ~ '[^A-Za-z0-9]' AND {compact_condition})"
            condition += " OR " + compact_condition
        return "(" + condition + ")"

    for i in range(len(terms)):
        brand = " OR ".join(field_match(f"p.{c}", i)
                            for c in ("brand", "manufacturer"))
        category = " OR ".join(field_match(f"p.{c}", i)
                               for c in ("category_major", "category_minor"))
        spec_value = field_match("ps.spec_value", i)
        spec = (field_match("l.label", i) + " OR " + field_match("l.unit", i) + " OR "
                + field_match("concat_ws(' ', l.label, ps.spec_value, l.unit)", i))
        weights.append(f"CASE WHEN ({brand}) THEN 3 WHEN ({category}) OR p.part_id IN ("
                       "SELECT ps.part_id FROM product_specs ps WHERE ps.part_id IN "
                       f"(SELECT part_id FROM ranked WHERE tier=4) AND {spec_value}) OR p.part_id IN ("
                       "SELECT ps.part_id FROM product_specs ps LEFT JOIN spec_key_label l USING (spec_key) "
                       "WHERE ps.part_id IN (SELECT part_id FROM ranked WHERE tier=4) "
                       f"AND ({spec})) THEN 2 ELSE 1 END")
    return " + ".join(weights)


def _filter_sql(filters: dict, params: dict) -> str:
    clauses = []
    for key in ("brand", "category_major", "category_minor"):
        if filters.get(key) is not None:
            params[key] = normalize(filters[key])
            clauses.append(f"part_search_normalize(p.{key}) = :{key}")
    if filters.get("part_ids") is not None:
        params["part_ids"] = list(filters["part_ids"])
        clauses.append("p.id = ANY(CAST(:part_ids AS integer[]))")
    unknown = set(filters) - {"brand", "category_major", "category_minor", "part_ids", "boost_part_ids"}
    if unknown:
        raise ValueError("不支持的检索筛选：" + ", ".join(sorted(unknown)))
    return " AND " + " AND ".join(clauses) if clauses else ""


def _regex(term: str) -> re.Pattern:
    return re.compile("".join(".*" if c == "*" else "." if c == "?" else re.escape(c)
                              for c in term), re.DOTALL)


def _hits(value: str | None, term: str) -> bool:
    value = normalize(value or "")
    if _regex(term).search(value):
        return True
    cp = _pattern(term, compacted=True)
    if cp:
        ct = "".join(c if c in "*?" else compact(c) for c in term)
        return bool(_regex(ct).search(compact(value)))
    return False


def _decorate(rows: list[dict], terms: list[str], specs: dict[int, list[dict]]) -> list[dict]:
    out = []
    for row in rows:
        item = {k: row[k] for k in ("part_id", "pn_std", "brand", "manufacturer", "summary",
                                    "description", "category_major", "category_minor")}
        rank = row.get("tier", 5)
        part_specs = specs.get(item["part_id"], [])
        matched_specs = [s for s in part_specs if any(_hits(s["label"] + " " + s["value"], t)
                                                     for t in terms)]
        item["key_specs"] = (matched_specs + [s for s in part_specs if s not in matched_specs])[:3]
        if row.get("fuzzy"):
            kind, reason = "fuzzy", "相似型号/资料"
        elif rank == 0:
            kind = "exact_pn" if row["pn_exact"] else "exact_alias"
            reason = "PN精确匹配" if row["pn_exact"] else "别名精确匹配"
        elif rank == 1:
            kind, reason = "prefix", "PN前缀"
        elif rank == 2:
            kind, reason = "substring", "PN子串"
        elif rank == 3:
            kind, reason = "boost", "有效报价"
        else:
            kind = "field"
            if any(_hits(row.get(k), t) for k in ("brand", "manufacturer") for t in terms):
                reason = "品牌/制造商命中"
            elif matched_specs:
                reason = "规格命中：" + matched_specs[0]["label"]
            elif any(_hits(row.get(k), t) for k in ("category_major", "category_minor") for t in terms):
                reason = "品类命中"
            else:
                reason = "资料命中"
        item.update(match_type=kind, match_reason=reason, matched_terms=terms)
        out.append(item)
    return out


def search(db: Session, q: str, *, limit: int = 10, filters: dict | None = None) -> list[dict]:
    """全部词命中后按 PN/别名、字段权重排序；不足时以全词 trigram 补足。

    filters 的 boost_part_ids 可传集合或 (db, q) -> 集合的钩子，默认返回空集合。
    通配和字面符号遵守用户原意，不参与纠错；其它词需逐词满足包含或相似。
    """
    q = normalize(q or "")
    limit = min(30, max(0, int(limit)))
    if not q or not limit:
        return []
    # 与基线一致，只关闭 JIT；短词不能改写长词或后续调用的规划设置。
    db.execute(text("SET LOCAL jit = off"))
    filters = dict(filters or {})
    boost = filters.get("boost_part_ids", _boost_part_ids)
    boost = boost(db, q) if callable(boost) else boost
    params = {"q": q, "limit": limit, "boost_ids": list(boost or ())}
    terms = list(dict.fromkeys(q.split()))
    conditions = [_term_condition(t, i, params) for i, t in enumerate(terms)]
    # 短词才关联窄表；纯长词保持原文档 SQL 和物理访问路径。
    projection = "part_search_text s"
    if any(_SHORT_TERM.fullmatch(term) for term in terms):
        projection += " JOIN part_search_short_key k USING (part_id)"
    extra = _filter_sql(filters, params)
    # 沿用 resolver 的整查询 compact 精确判定；M1 另加 active 别名门。
    joined = preprocess(q)[0]["joined"] if not any(c in q for c in "*?%_\\") else ""
    params.update(exact=joined, prefix=(_pattern(q) or "") + "%",
                  pn_contains="%" + (_pattern(q) or "") + "%",
                  compact_prefix=compact(q) + "%", compact_contains="%" + compact(q) + "%")
    params["literal_exact"] = not any(c in q for c in "*?")
    pn_exact = f"((:exact <> '' AND p.pn_compact = :exact) OR (:literal_exact AND (:exact='' OR p.pn_std ~ '[^ -~]') AND ({_normalized('p.pn_std')})=:q))"
    alias_matches = """SELECT a.pn_std FROM part_alias a
        WHERE a.status='active' AND :exact<>'' AND a.pn_compact=:exact
        UNION SELECT a.pn_std FROM part_alias a WHERE a.status='active'
        AND :literal_exact AND (:exact='' OR a.pn_raw ~ '[^ -~]')
        AND (""" + _normalized("a.pn_raw") + ")=:q"
    alias_exact = "p.pn_std IN (" + alias_matches + ")"
    cjk_query = bool(_SHORT_TERM.fullmatch(q) and not q.isascii())
    if cjk_query:
        # 纯 ASCII 原文不可能归一化成汉字，避免对所有 PN/别名执行全文归一。
        pn_exact = f"(p.pn_std ~ '[^ -~]' AND ({_normalized('p.pn_std')})=:q)"
        alias_matches = ("SELECT a.pn_std FROM part_alias a WHERE a.status='active' "
                         "AND a.pn_raw ~ '[^ -~]' AND (" + _normalized("a.pn_raw") + ")=:q")
        alias_exact = "p.pn_std IN (" + alias_matches + ")"
    def pn_match(parameter: str) -> str:
        return _contains("(" + _match_text("p.pn_std") + ")", parameter)

    prefix = pn_match("prefix")
    contains = pn_match("pn_contains")
    # 保留字面符号时不能用 compact 排序，避免 %/_ 丢失。
    if compact(q) and not any(c in q for c in "*?%_\\"):
        prefix += " OR " + _contains("p.pn_compact", "compact_prefix")
        contains += " OR " + _contains("p.pn_compact", "compact_contains")
    if re.fullmatch(r"[A-Z0-9]+", q):
        # 纯字母数字词在 ASCII PN 上的原文命中必然也被 compact 命中覆盖。
        prefix = (_contains("p.pn_compact", "compact_prefix")
                  + " OR (p.pn_std ~ '[^ -~]' AND "
                  + _contains("part_search_normalize(p.pn_std)", "prefix") + ")")
        contains = (_contains("p.pn_compact", "compact_contains")
                    + " OR (p.pn_std ~ '[^ -~]' AND "
                    + _contains("part_search_normalize(p.pn_std)", "pn_contains") + ")")
        if len(q) < 3:
            # 短 PN 子串同样提不出 trigram；strpos 保留包含语义，避开全 GIN 扫描。
            contains = ("strpos(p.pn_compact,:q)>0 OR (p.pn_std ~ '[^ -~]' "
                        "AND " + _contains("part_search_normalize(p.pn_std)", "pn_contains") + ")")
    elif cjk_query:
        prefix = "p.pn_std ~ '[^ -~]' AND (" + prefix + ")"
        contains = "p.pn_std ~ '[^ -~]' AND (" + contains + ")"

    def exact_ids(alias_source: str) -> str:
        return ("SELECT x.id FROM dim_part x WHERE :exact<>'' AND x.pn_compact=:exact "
                "UNION SELECT x.id FROM dim_part x WHERE :literal_exact "
                "AND x.pn_std ~ '[^ -~]' AND (" + _normalized("x.pn_std") + ")=:q "
                "UNION SELECT x.id FROM dim_part x WHERE x.pn_std IN (" + alias_source + ")")

    fields = "p.pn_std, p.brand, p.manufacturer, p.summary, p.description, p.category_major, p.category_minor"
    columns = "p.id AS part_id, " + fields
    base = " FROM " + projection + " JOIN dim_part p ON p.id=s.part_id WHERE p.status <> 'merged' AND "
    direct_base = base + " AND ".join(conditions)
    key_check = (" WHERE p.status <> 'merged' AND EXISTS ("
                 "SELECT 1 FROM " + projection + " WHERE s.part_id=p.id AND "
                 + " AND ".join(conditions) + " OFFSET 0)")
    bounded_base = " FROM dim_part p" + key_check
    # 字母短词常命中大量 PN，分档查询可在顺序索引上提前停止。
    rows: list[dict] = []
    if len(q) == 1 or re.fullmatch(r"[A-Z]{2}", q):
        params.update(exclude=[], remaining=limit)
        # 先按 PN 条件找行，再逐个核对短词投影。
        fast_base = bounded_base if _SHORT_TERM.fullmatch(q) else direct_base
        for condition, rank in ((f"{pn_exact} OR {alias_exact}", 0), (prefix, 1)):
            params.update(exclude=[r["part_id"] for r in rows], remaining=limit - len(rows))
            lane_from = fast_base
            if _SHORT_TERM.fullmatch(q):
                # 三种精确证据分别利用 compact、非 ASCII 子集和别名索引。
                unicode_prefix = ("SELECT x.id FROM dim_part x WHERE x.pn_std ~ '[^ -~]' AND "
                                  + _contains("part_search_normalize(x.pn_std)", "prefix"))
                prefix_ids = (("SELECT x.id FROM dim_part x WHERE "
                               + _contains("x.pn_compact", "compact_prefix") + " UNION ")
                              if compact(q) else "") + unicode_prefix
                ids = exact_ids(alias_matches) if rank == 0 else prefix_ids
                lane_from = " FROM (" + ids + ") hits JOIN dim_part p ON p.id=hits.id" + key_check
            stmt = text(f"SELECT {columns}, {rank} AS tier, {pn_exact} AS pn_exact "
                        + lane_from + extra + f" AND ({condition}) "
                        "AND NOT(p.id=ANY(CAST(:exclude AS integer[]))) ORDER BY p.pn_std LIMIT :remaining")
            rows.extend(dict(r) for r in db.execute(_short_statement(stmt, terms, q), params).mappings())
            if len(rows) >= limit:
                break
    if len(rows) < limit:
        params["candidate_limit"] = get_settings().part_search_candidate_limit
        # 资料先只召回 ID，再截断；不在几千个高频词命中上做分档或字段打分。
        # PN 各档和报价加权独立保留前 limit 条，避免被资料候选上限挤掉。
        # 同一份匹配型号、精确别名供所有车道复用，避免每条车道重复扫业务表。
        alias_exact = "p.pn_std IN (SELECT pn_std FROM exact_aliases)"
        tier = (f"CASE WHEN {pn_exact} OR {alias_exact} THEN 0 WHEN ({prefix}) THEN 1 "
                f"WHEN ({contains}) THEN 2 WHEN p.id=ANY(CAST(:boost_ids AS integer[])) THEN 3 ELSE 4 END")
        lane_base = " FROM matched_parts p WHERE true "
        lanes = [f"(SELECT p.id AS part_id {lane_base} AND ({condition}) "
                 "ORDER BY p.pn_std LIMIT :limit)"
                 for condition in (f"{pn_exact} OR {alias_exact}", prefix, contains,
                                   "p.id=ANY(CAST(:boost_ids AS integer[]))")]
        ranked_from = "FROM candidates c JOIN dim_part p ON p.id=c.part_id"
        stmt = text("WITH exact_aliases AS MATERIALIZED (" + alias_matches + "), "
                    "direct_matches AS MATERIALIZED (SELECT s.part_id "
                    "FROM " + projection + " WHERE " + " AND ".join(conditions) + "), "
                    f"matched_parts AS MATERIALIZED (SELECT p.id, p.pn_compact, {fields} "
                    "FROM direct_matches s JOIN dim_part p ON p.id=s.part_id "
                    "WHERE p.status <> 'merged' " + extra + "), "
                    "doc_candidates AS MATERIALIZED (SELECT p.id AS part_id " + lane_base
                    + " ORDER BY p.pn_std LIMIT :candidate_limit), "
                    "candidates AS (" + " UNION ".join(lanes + ["SELECT part_id FROM doc_candidates"])
                    + f"), ranked AS MATERIALIZED (SELECT {columns}, {tier} AS tier, {pn_exact} AS pn_exact "
                    "FROM candidates c JOIN matched_parts p ON p.id=c.part_id) "
                    "SELECT * FROM ranked p ORDER BY tier, "
                    + f"(CASE WHEN tier=4 THEN ({_field_weight(terms)}) ELSE 0 END) DESC, "
                    "p.pn_std LIMIT :limit")
        # 含数字短词复用上面的匹配集合，避免稀疏 PN 子串另扫全库。
        if all(_SHORT_TERM.fullmatch(term) for term in terms) and not re.search(r"[0-9]", q):
            # 高频短词不展开全部文档命中：PN 各档独立找前 limit 条，
            # 资料按 PN 只留前 N 条。稀疏词仍可由数组 GIN 自然召回。
            identity_base = (" FROM (" + exact_ids("SELECT pn_std FROM exact_aliases")
                             + ") hits JOIN dim_part p ON p.id=hits.id" + key_check)
            short_lanes = [f"(SELECT p.id AS part_id {identity_base if rank == 0 else bounded_base} "
                           f"{extra} AND ({condition}) "
                           "ORDER BY p.pn_std LIMIT :limit)"
                           for rank, condition in enumerate((f"{pn_exact} OR {alias_exact}", prefix, contains,
                                                              "p.id=ANY(CAST(:boost_ids AS integer[]))"))]
            stmt = text("WITH exact_aliases AS MATERIALIZED (" + alias_matches + "), "
                        "doc_candidates AS MATERIALIZED (SELECT p.id AS part_id "
                        + direct_base + extra + " ORDER BY p.pn_std LIMIT :candidate_limit), "
                        "candidates AS MATERIALIZED (" + " UNION ".join(
                            short_lanes + ["SELECT part_id FROM doc_candidates"])
                        + f"), ranked AS MATERIALIZED (SELECT {columns}, {tier} AS tier, {pn_exact} AS pn_exact "
                        + ranked_from + ") SELECT * FROM ranked p ORDER BY tier, "
                        + f"(CASE WHEN tier=4 THEN ({_field_weight(terms)}) ELSE 0 END) DESC, "
                        "p.pn_std LIMIT :limit")
        rows = [dict(r) for r in db.execute(_short_statement(stmt, terms, q), params).mappings()]
    # 短词、显式通配或字面符号保持直接匹配，其余词可纠错，仍逐词满足条件。
    fuzzy_eligible = [len(t) >= 3 and not any(c in t for c in "*?%_\\") for t in terms]
    if len(rows) < limit and any(fuzzy_eligible):
        db.execute(text("SET LOCAL pg_trgm.similarity_threshold = 0.25"))
        db.execute(text("SET LOCAL pg_trgm.word_similarity_threshold = 0.35"))
        fuzzy_conditions = []
        doc_fuzzy_conditions = []
        for i, term in enumerate(terms):
            if not fuzzy_eligible[i]:
                doc_fuzzy_conditions.append(conditions[i])
                fuzzy_conditions.append(conditions[i])
                continue
            params[f"f{i}"] = term
            params[f"fc{i}"] = compact(term)
            doc_condition = f"({conditions[i]} OR :f{i} <% s.doc)"
            doc_fuzzy_conditions.append(doc_condition)
            fuzzy_conditions.append(
                f"({doc_condition} OR (:fc{i}<>'' AND (p.pn_compact % :fc{i} OR :fc{i} <% p.pn_compact)) "
                "OR EXISTS (SELECT 1 FROM part_alias a WHERE a.pn_std=p.pn_std AND a.status='active' "
                f"AND :fc{i}<>'' AND (a.pn_compact % :fc{i} OR :fc{i} <% a.pn_compact)))")
        params.update(exclude=[r["part_id"] for r in rows], remaining=limit - len(rows), cq=compact(q))
        # PN/别名先按词相似召回，再以整体相似补足；避免宽松 % 分支让
        # 整个 OR 退化为全表打分。每条结果仍要逐词满足上面的完整条件。
        for operator in ("word", "similarity"):
            if len(rows) >= limit:
                break
            params.update(exclude=[r["part_id"] for r in rows], remaining=limit - len(rows))
            pn_condition = ":cq <% p.pn_compact" if operator == "word" else "p.pn_compact % :cq"
            alias_condition = ":cq <% a.pn_compact" if operator == "word" else "a.pn_compact % :cq"
            fuzzy_stmt = text("WITH candidates AS MATERIALIZED ((SELECT p.id AS part_id, "
                              "greatest(similarity(p.pn_compact,:cq),word_similarity(:cq,p.pn_compact)) AS sim "
                              "FROM dim_part p WHERE p.status<>'merged' AND :cq<>'' AND "
                              + pn_condition + extra + " ORDER BY sim DESC,p.pn_std LIMIT 60) UNION ALL "
                              "(SELECT p.id AS part_id, greatest(similarity(a.pn_compact,:cq), "
                              "word_similarity(:cq,a.pn_compact)) AS sim FROM part_alias a JOIN dim_part p "
                              "ON p.pn_std=a.pn_std WHERE p.status<>'merged' AND a.status='active' "
                              "AND :cq<>'' AND " + alias_condition + extra
                              + " ORDER BY sim DESC,p.pn_std LIMIT 60)), "
                              "cands AS (SELECT part_id,max(sim) AS sim FROM candidates GROUP BY part_id) "
                              f"SELECT {columns},true AS fuzzy, "
                              "greatest(c.sim,word_similarity(:q,s.doc)) AS fuzzy_score "
                              "FROM cands c JOIN dim_part p ON p.id=c.part_id "
                              "JOIN " + projection + " ON s.part_id=p.id WHERE "
                              + " AND ".join(fuzzy_conditions) + extra
                              + " AND NOT(p.id=ANY(CAST(:exclude AS integer[]))) "
                              "ORDER BY greatest(c.sim,word_similarity(:q,s.doc)) DESC,p.pn_std LIMIT :remaining")
            rows.extend(dict(r) for r in db.execute(_short_statement(fuzzy_stmt, terms, q), params).mappings())
        if len(rows) < limit:
            # 已有 PN/别名纠错证据时，额外资料候选要求更强证据，避免共用片段
            # 补入一屏无关型号；纯资料纠错仍沿用 resolver 的 0.35 阈值。
            doc_threshold = "0.6" if any(r.get("fuzzy") for r in rows) else "0.35"
            db.execute(text("SET LOCAL pg_trgm.word_similarity_threshold = " + doc_threshold))
            params.update(exclude=[r["part_id"] for r in rows], remaining=limit - len(rows))
            # 先独立召回文档，再限制候选数；把 LIMIT 放在索引召回内会诱导
            # 优化器沿主键逐行算 word_similarity，稀疏命中时耗时失控。
            fuzzy_stmt = text("WITH doc_matches AS MATERIALIZED (SELECT s.part_id "
                              "FROM " + projection + " WHERE " + " AND ".join(doc_fuzzy_conditions)
                              + "), doc_candidates AS MATERIALIZED (SELECT d.part_id FROM doc_matches d "
                              "JOIN dim_part p ON p.id=d.part_id WHERE p.status<>'merged' " + extra
                              + " AND NOT(p.id=ANY(CAST(:exclude AS integer[]))) LIMIT 120) "
                              f"SELECT {columns},true AS fuzzy, "
                              "greatest(word_similarity(:q,s.doc),similarity(:cq,s.doc_compact)) AS fuzzy_score "
                              "FROM doc_candidates c JOIN dim_part p ON p.id=c.part_id "
                              "JOIN " + projection + " ON s.part_id=p.id "
                              "ORDER BY greatest(word_similarity(:q,s.doc),similarity(:cq,s.doc_compact)) DESC, "
                              "p.pn_std LIMIT :remaining")
            rows.extend(dict(r) for r in db.execute(_short_statement(fuzzy_stmt, terms, q), params).mappings())
        direct = [r for r in rows if not r.get("fuzzy")]
        fuzzy = sorted((r for r in rows if r.get("fuzzy")),
                       key=lambda r: (-r["fuzzy_score"], r["pn_std"]))
        rows = direct + fuzzy
    if not rows:
        return []
    ids = [r["part_id"] for r in rows]
    spec_stmt = text("SELECT part_id, spec_key, spec_value FROM product_specs "
                     "WHERE part_id = ANY(:ids) ORDER BY spec_key").bindparams(bindparam("ids", type_=ARRAY(Integer)))
    specs: dict[int, list[dict]] = {}
    for pid, key, value in db.execute(spec_stmt, {"ids": ids}):
        specs.setdefault(pid, []).append({"label": spec_label(key), "value": value})
    return _decorate(rows, terms, specs)
