import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button } from "antd";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { warehouseSources, type Source } from "../../api/warehouseAnalytics";
import DateRangePresets from "../../components/DateRangePresets";
import { sessionStale, SESSION_EXPIRED_MESSAGE, useSessionOwner } from "../../utils/session";
import {
  WarehouseContext,
  createWarehouseCache,
  Pick,
  useWarehouse,
  useWarehouseRead,
  type Patch,
  type WarehouseContextValue,
} from "./WarehouseShared";
import { Board, boards, hasKey, parseDrill, parseFilters } from "./warehouseState";
import { Overview } from "./Trend";
import { MatrixBoard } from "./Matrix";
import { RankBoard } from "./Rankings";
import { DocumentList, EquipmentBoard, TestingBoard } from "./SecondaryBoards";
import "./warehouseAnalytics.css";

const pagingKeys = [
  "peoplePage",
  "retPage",
  "eqPage",
  "listPage",
  "linePage",
  "peopleFocus",
  "retFocus",
];
function Dashboard({ board }: { board: Board }) {
  const outer = useWarehouse(),
    { params, filters, patch, refresh } = outer;
  const { preset } = parseFilters(params);
  const coverage = useWarehouseRead("coverage", {}),
    options = coverage?.data?.options;
  const [term, setTerm] = useState(filters.q || "");
  useEffect(() => {
    setTerm(filters.q || "");
  }, [filters.q]);
  const location = useLocation(),
    navigate = useNavigate();
  const raw = params.get("drill"),
    selection = parseDrill(raw),
    doc = params.get("doc");
  const isList = raw !== null,
    invalid = isList && !selection;
  const updateFilters = (values: Record<string, string | null>) =>
    patch({ ...Object.fromEntries(pagingKeys.map((k) => [k, null])), ...values });
  function back() {
    const stored = location.state?.warehouseParent;
    if (stored && stored === (doc ? "lines" : "list")) {
      navigate(-1);
      return;
    }
    if (doc) patch({ doc: null, docNo: null, linePage: null });
    else patch({ drill: null, drillName: null, listPage: null, doc: null, docNo: null, linePage: null });
  }
  const name = boards[board];
  const source = filters.source || selection?.scope?.source;
  const selectedSource = source && coverage?.data?.sources.find((s) => s.source === source);
  const awaitingSource = source && !coverage?.data && !coverage?.error;
  const ctx = { ...outer, coverage: coverage?.data };
  const missing = coverage?.data?.sources.find((x) => x.source === "outbound");
  const coverText = coverage?.data?.sources
    .filter((c) => c.available && (!filters.source || c.source === filters.source))
    .map((c) => `${warehouseSources[c.source]} ${c.min_date || "—"} 至 ${c.max_date || "—"}`)
    .join(" ｜ ");
  const buttons = Object.entries(boards) as [Board, string][];
  return (
    <WarehouseContext.Provider value={ctx}>
      <main className="wa-page">
        <header className="wa-title">
          <h1>库存管理分析</h1>
          <Button onClick={refresh}>刷新数据</Button>
        </header>
        {isList ? (
          <div className="wa-crumb">
            <Button size="small" onClick={back}>
              返回上一级
            </Button>
            <span>
              {name} / {params.get("drillName") || "匹配单据"}
              {doc ? " / 单内货品" : ""}
            </span>
          </div>
        ) : (
          <>
            <div className="wa-filters">
              <label className="wa-date">
                已生效单据 · 业务日期{" "}
                <DateRangePresets
                  preset={preset}
                  defaultPreset="30d"
                  from={params.get("from")}
                  to={params.get("to")}
                  onChange={(v) => updateFilters({ period: v.preset, from: v.from, to: v.to })}
                />
              </label>
              <Pick
                label="来源"
                value={filters.source || ""}
                options={{ "": "全部来源", ...warehouseSources }}
                change={(v) => updateFilters({ source: v || null })}
              />
              <Pick
                label="仓储中心"
                value={filters.center || ""}
                options={{
                  "": "全部中心",
                  ...Object.fromEntries(
                    [...(options?.centers || []), ...(filters.center ? [filters.center] : [])].map(
                      (v) => [v, v],
                    ),
                  ),
                }}
                change={(v) => updateFilters({ center: v || null })}
              />
              <Pick
                label="明细单位"
                value={filters.unit || ""}
                options={{
                  "": "全部单位",
                  ...Object.fromEntries(
                    [...(options?.units || []), ...(filters.unit ? [filters.unit] : [])].map((v) => [
                      v,
                      v,
                    ]),
                  ),
                }}
                change={(v) => updateFilters({ unit: v || null })}
              />
              <form
                className="wa-search"
                onSubmit={(e) => {
                  e.preventDefault();
                  updateFilters({ q: term.trim() || null });
                }}
              >
                <label>
                  单号、PN 或描述
                  <input
                    type="search"
                    aria-label="搜索单号、PN或描述"
                    value={term}
                    maxLength={128}
                    onChange={(e) => setTerm(e.target.value)}
                  />
                </label>
                <button type="submit">查询</button>
              </form>
              <Button
                size="small"
                onClick={() => {
                  setTerm("");
                  updateFilters({
                    period: null,
                    from: null,
                    to: null,
                    source: null,
                    center: null,
                    unit: null,
                    q: null,
                  });
                }}
              >
                重置
              </Button>
            </div>
            <nav className="wa-nav" aria-label="库存分析看板">
              {buttons.map(([k, text]) => (
                <button
                  type="button"
                  key={k}
                  aria-current={board === k ? "page" : undefined}
                  onClick={() => patch({ view: k }, true)}
                >
                  {text}
                </button>
              ))}
            </nav>
          </>
        )}
        <div className="wa-range">
          已生效单据 · 业务日期{" "}
          <b>
            {filters.date_from || "全部历史"}
            {filters.date_to ? " 至 " + filters.date_to : ""}
          </b>
          {coverText && <span> · 已导入单据首末日 {coverText}</span>}
          {missing && !missing.available && <span className="wa-missing"> · 实际出库（CKD）待接入</span>}
        </div>
        {coverage?.error && (
          <Alert
            type="error"
            showIcon
            message={coverage.error}
            action={
              <button className="wa-link" onClick={refresh}>
                重试
              </button>
            }
          />
        )}
        {invalid ? (
          <Alert type="warning" message="单据范围无效，请返回上一级重新选择。" />
        ) : awaitingSource ? (
          <p role="status">正在核对来源覆盖…</p>
        ) : selectedSource && !selectedSource.available ? (
          <Alert
            type="info"
            showIcon
            message={warehouseSources[selectedSource.source] + "待接入"}
            description="该来源尚无已导入单据，当前无法计算业务量。"
          />
        ) : isList && selection ? (
          <DocumentList selection={selection} documentId={doc} />
        ) : board === "overview" ? (
          <Overview />
        ) : board === "people" || board === "returns" ? (
          <RankBoard key={board} people={board === "people"} />
        ) : board === "matrix" ? (
          <MatrixBoard />
        ) : board === "equipment" ? (
          <EquipmentBoard />
        ) : (
          <TestingBoard />
        )}
      </main>
    </WarehouseContext.Provider>
  );
}
export default function InventoryAnalyticsPage() {
  const [params, setParams] = useSearchParams(),
    location = useLocation();
  const owner = useSessionOwner(),
    [dead, setDead] = useState(false),
    [revision, bump] = useState(0);
  const stale = useCallback(() => {
    if (sessionStale(owner)) setDead(true);
  }, [owner]);
  useEffect(() => {
    window.addEventListener("storage", stale);
    return () => window.removeEventListener("storage", stale);
  }, [stale]);
  const cache = useMemo(createWarehouseCache, [owner, revision]);
  const { filters } = parseFilters(params),
    raw = params.get("view"),
    board = hasKey(boards, raw) ? raw : "overview";
  const guard = () => {
    stale();
    return !dead && !sessionStale(owner);
  };
  const patch: Patch = (values, push = false) => {
    if (!guard()) return;
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(values))
      if (v === null) next.delete(k);
      else next.set(k, v);
    if (next.toString() === params.toString()) return;
    const layer = values.doc ? "lines" : values.drill ? "list" : null;
    setParams(next, {
      replace: !push,
      state: push ? (layer ? { warehouseParent: layer } : null) : location.state,
    });
  };
  if (dead || sessionStale(owner))
    return <Alert type="warning" showIcon message={SESSION_EXPIRED_MESSAGE} />;
  const value: WarehouseContextValue = {
    owner,
    stale,
    revision,
    cache,
    params,
    filters,
    patch,
    refresh: () => {
      if (guard()) bump((n) => n + 1);
    },
    drill: (s, title) =>
      patch(
        {
          drill: JSON.stringify(s),
          drillName: title,
          doc: null,
          docNo: null,
          linePage: null,
          listPage: null,
        },
        true,
      ),
  };
  return (
    <WarehouseContext.Provider value={value}>
      <Dashboard board={board} />
    </WarehouseContext.Provider>
  );
}
