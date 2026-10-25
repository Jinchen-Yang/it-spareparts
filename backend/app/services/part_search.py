"""M1 实时检索：投影召回、分档排序，调用方只依赖 search。"""
import re

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy import Integer
from sqlalchemy.orm import Session

from app.services.part_resolver import preprocess
from app.services.part_search_normalize import compact, normalize
from app.services.spec_extract import SPEC_KEYS

__all__ = ["search"]


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
    operator = "LIKE" if column in ("s.doc", "s.doc_compact", "p.pn_compact") else "ILIKE"
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
    return "(" + cond + ")"


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
    # 联想是短查询；候选车道较多时 JIT 编译本身会超过交互预算。
    db.execute(text("SET LOCAL jit = off"))
    filters = dict(filters or {})
    boost = filters.get("boost_part_ids", _boost_part_ids)
    boost = boost(db, q) if callable(boost) else boost
    params = {"q": q, "limit": limit, "boost_ids": list(boost or ())}
    terms = list(dict.fromkeys(q.split()))
    conditions = [_term_condition(t, i, params) for i, t in enumerate(terms)]
    extra = _filter_sql(filters, params)
    # 沿用 resolver 的整查询 compact 精确判定；M1 另加 active 别名门。
    joined = preprocess(q)[0]["joined"] if not any(c in q for c in "*?%_\\") else ""
    params.update(exact=joined, prefix=(_pattern(q) or "") + "%",
                  pn_contains="%" + (_pattern(q) or "") + "%",
                  compact_prefix=compact(q) + "%", compact_contains="%" + compact(q) + "%")
    params["literal_exact"] = not any(c in q for c in "*?")
    pn_exact = f"((:exact <> '' AND p.pn_compact = :exact) OR (:literal_exact AND (:exact='' OR p.pn_std ~ '[^ -~]') AND ({_normalized('p.pn_std')})=:q))"
    alias_exact = """p.pn_std IN (SELECT a.pn_std FROM part_alias a
        WHERE a.status='active' AND :exact<>'' AND a.pn_compact=:exact
        UNION SELECT a.pn_std FROM part_alias a WHERE a.status='active'
        AND :literal_exact AND (:exact='' OR a.pn_raw ~ '[^ -~]')
        AND (""" + _normalized("a.pn_raw") + ")=:q)"
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
    columns = "p.id AS part_id, p.pn_std, p.brand, p.manufacturer, p.summary, p.description, p.category_major, p.category_minor"
    base = " FROM part_search_text s JOIN dim_part p ON p.id=s.part_id WHERE p.status <> 'merged' AND "
    tier = (f"CASE WHEN {pn_exact} OR {alias_exact} THEN 0 WHEN ({prefix}) THEN 1 "
            f"WHEN ({contains}) THEN 2 WHEN p.id=ANY(CAST(:boost_ids AS integer[])) THEN 3 ELSE 4 END")
    # 单字符常命中大多数 PN，分档查询可在顺序索引上提前停止。
    rows: list[dict] = []
    if len(q) == 1:
        params.update(exclude=[], remaining=limit)
        for condition, rank in ((f"{pn_exact} OR {alias_exact}", 0), (prefix, 1)):
            params.update(exclude=[r["part_id"] for r in rows], remaining=limit - len(rows))
            stmt = text(f"SELECT {columns}, {rank} AS tier, {pn_exact} AS pn_exact "
                        + base + " AND ".join(conditions) + extra + f" AND ({condition}) "
                        "AND NOT(p.id=ANY(CAST(:exclude AS integer[]))) ORDER BY p.pn_std LIMIT :remaining")
            rows.extend(dict(r) for r in db.execute(stmt, params).mappings())
            if len(rows) >= limit:
                break
    if len(rows) < limit:
        stmt = text(f"WITH ranked AS MATERIALIZED (SELECT {columns}, {tier} AS tier, {pn_exact} AS pn_exact "
                    + base + " AND ".join(conditions) + extra + ") "
                    "SELECT * FROM ranked p ORDER BY tier, "
                    + f"(CASE WHEN tier=4 THEN ({_field_weight(terms)}) ELSE 0 END) DESC, "
                    "p.pn_std LIMIT :limit")
        rows = [dict(r) for r in db.execute(stmt, params).mappings()]
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
                              "JOIN part_search_text s ON s.part_id=p.id WHERE "
                              + " AND ".join(fuzzy_conditions) + extra
                              + " AND NOT(p.id=ANY(CAST(:exclude AS integer[]))) "
                              "ORDER BY greatest(c.sim,word_similarity(:q,s.doc)) DESC,p.pn_std LIMIT :remaining")
            rows.extend(dict(r) for r in db.execute(fuzzy_stmt, params).mappings())
        if len(rows) < limit:
            # 已有 PN/别名纠错证据时，额外资料候选要求更强证据，避免共用片段
            # 补入一屏无关型号；纯资料纠错仍沿用 resolver 的 0.35 阈值。
            doc_threshold = "0.6" if any(r.get("fuzzy") for r in rows) else "0.35"
            db.execute(text("SET LOCAL pg_trgm.word_similarity_threshold = " + doc_threshold))
            params.update(exclude=[r["part_id"] for r in rows], remaining=limit - len(rows))
            # 先独立召回文档，再限制候选数；把 LIMIT 放在索引召回内会诱导
            # 优化器沿主键逐行算 word_similarity，稀疏命中时耗时失控。
            fuzzy_stmt = text("WITH doc_matches AS MATERIALIZED (SELECT s.part_id "
                              "FROM part_search_text s WHERE " + " AND ".join(doc_fuzzy_conditions)
                              + "), doc_candidates AS MATERIALIZED (SELECT d.part_id FROM doc_matches d "
                              "JOIN dim_part p ON p.id=d.part_id WHERE p.status<>'merged' " + extra
                              + " AND NOT(p.id=ANY(CAST(:exclude AS integer[]))) LIMIT 120) "
                              f"SELECT {columns},true AS fuzzy, "
                              "greatest(word_similarity(:q,s.doc),similarity(:cq,s.doc_compact)) AS fuzzy_score "
                              "FROM doc_candidates c JOIN dim_part p ON p.id=c.part_id "
                              "JOIN part_search_text s ON s.part_id=p.id "
                              "ORDER BY greatest(word_similarity(:q,s.doc),similarity(:cq,s.doc_compact)) DESC, "
                              "p.pn_std LIMIT :remaining")
            rows.extend(dict(r) for r in db.execute(fuzzy_stmt, params).mappings())
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
        label, unit = SPEC_KEYS.get(key, (key, None))
        specs.setdefault(pid, []).append({"label": label, "value": value})
    return _decorate(rows, terms, specs)
