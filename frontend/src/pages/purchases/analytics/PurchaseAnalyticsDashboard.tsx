import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { Alert, Button, Card, ConfigProvider, Space, Spin, Tag } from "antd";
import "./purchaseDashboard.css";
import PageHeader from "../../../components/PageHeader";
import { businessToday } from "../../../utils/date";
import { SESSION_EXPIRED_MESSAGE, sessionStale, useSessionOwner } from "../../../utils/session";
import {
  fetchPurchaseAnalyticsOptions,
  fetchPurchaseAnalyticsOrders,
  type PurchaseAnalyticsOptions,
  type PurchaseAnalyticsOrdersResp,
  type PurchaseScope,
} from "../../../api/purchaseAnalytics";
import { analyticsControlTheme } from "../../sales/analytics/dashboardColors";
import ProductScopeFilter from "./ProductScopeFilter";
import { resolveTimeScope } from "./purchaseScopeDates";
import FilterBar from "./FilterBar";
import DrillTable from "./DrillTable";
import OrderDetailDrawer from "./OrderDetailDrawer";
import DocumentPreview, { makePreviewCache, PreviewCacheContext } from "./DocumentPreview";
import { ChartSampleCache, makeChartSampleCache } from "./ChartOrderEvidence";
import { makeReadCache, useDashboardRead } from "./useDashboardRead";
import { parseAnalyticsUrl, toAnalyticsApiFilters, useAnalyticsPatch } from "./urlState";
import { WorkbenchProvider, type WorkbenchView } from "./WorkbenchUI";

const Workbench = lazy(() => import("./AnalyticsWorkbench"));
const Pool = lazy(() => import("./PurchasePoolAnalytics"));
const Legacy = lazy(() => import("./PurchaseAnalyticsPage"));
const views = {
  boards: "多框排行",
  matrix: "紧凑矩阵",
  compare: "期间对比",
  work: "人员工作量",
  structure: "采购结构与关联",
  pool: "PN 互通池",
  legacy: "汇总与导出",
} as const;
type View = keyof typeof views;
const drillKeys = ["drill", "drill_name", "drill_from", "drill_to", "page", "order_id", "sort", "dir"];
function firstWorkbenchPage(params: URLSearchParams) {
  const next = new URLSearchParams(params);
  for (const key of Array.from(next.keys())) {
    if (key.startsWith("wb_") && key.toLowerCase().endsWith("page")) next.delete(key);
  }
  return next;
}
function readScope(value: string | null): PurchaseScope | undefined {
  if (value == null) return undefined;
  try {
    const result = JSON.parse(value);
    if (
      result &&
      typeof result === "object" &&
      !Array.isArray(result) &&
      Object.values(result).every((x) => x === null || typeof x === "string")
    )
      return result;
  } catch {
    /* malformed deep link */
  }
  throw new Error("链接中的下钻条件无效，请清除范围后重试。");
}
function permissions() {
  try {
    return JSON.parse(localStorage.getItem("permissions") || "{}");
  } catch {
    return {};
  }
}

export default function PurchaseAnalyticsDashboard() {
  const [sp, setSp] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const initialIndex = useRef(window.history.state?.idx ?? 0);
  const viewParam = sp.get("view");
  const legacyLink = ["group_by", "group_key", "order_id", "gpage", "basis"].some((k) => sp.has(k));
  const view: View =
    viewParam && Object.prototype.hasOwnProperty.call(views, viewParam)
      ? (viewParam as View)
      : !viewParam && legacyLink
        ? "legacy"
        : "boards";
  const owner = useSessionOwner();
  const [dead, setDead] = useState(false);
  const stale = useCallback(() => {
    if (sessionStale(owner)) setDead(true);
  }, [owner]);
  useEffect(() => {
    window.addEventListener("storage", stale);
    return () => window.removeEventListener("storage", stale);
  }, [stale]);
  const guard = () => {
    stale();
    return !dead && !sessionStale(owner);
  };
  const [revision, refresh] = useState(0);
  const optionsCache = useMemo(() => makeReadCache<PurchaseAnalyticsOptions>(), [owner, revision]);
  const ordersCache = useMemo(() => makeReadCache<PurchaseAnalyticsOrdersResp>(12), [owner, revision]);
  const previewCache = useMemo(makePreviewCache, [owner, revision]);
  const chartSampleCache = useMemo(makeChartSampleCache, [owner, revision]);
  const state = parseAnalyticsUrl(sp);
  const patch = useAnalyticsPatch(sp, setSp);
  const patchFilters = useAnalyticsPatch(firstWorkbenchPage(sp), setSp);
  const admin = localStorage.getItem("role") === "admin";
  const perms = useMemo(permissions, [owner]);
  const costRestricted = !admin && perms.data_purchase_cost === false;
  const customerRestricted = !admin && perms.data_customer === false;
  const supplierRestricted = !admin && perms.data_supplier === false;
  const effective = {
    ...state,
    customerIds: customerRestricted ? [] : state.customerIds,
    supplier: supplierRestricted ? "" : state.supplier,
  };
  const filters = toAnalyticsApiFilters(effective, businessToday());
  const [customerQuery, search] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(customerQuery.trim()), 220);
    return () => clearTimeout(timer);
  }, [customerQuery]);
  const ids = effective.customerIds.slice(0, 200);
  const optKey = JSON.stringify([query, ids, revision]);
  const opt = useDashboardRead(
    view !== "legacy" && !dead ? optKey : null,
    () => optionsCache.get(optKey, async () => (await fetchPurchaseAnalyticsOptions(query, ids)).data),
    owner,
    stale,
  );
  const options = opt?.data;
  const customers = [
    ...new Map(
      [...(options?.selected_customers || []), ...(options?.customers || [])].map((c) => [
        c.customer_id,
        { value: c.customer_id, label: c.customer || String(c.customer_id) },
      ]),
    ).values(),
  ];

  let activeScope: PurchaseScope | undefined,
    drill: PurchaseScope | undefined,
    scopeError = "";
  try {
    activeScope = readScope(sp.get("scope"));
    drill = readScope(sp.get("drill"));
  } catch (e) {
    scopeError = (e as Error).message;
  }
  const scopedFilters = {
    ...filters,
    ...(sp.has("drill_from") ? { date_from: sp.get("drill_from")! } : {}),
    ...(sp.has("drill_to") ? { date_to: sp.get("drill_to")! } : {}),
  };
  const listQuery = {
    ...scopedFilters,
    scope: drill,
    sort: state.sort,
    order: state.dir,
    page: state.page,
    page_size: state.pageSize,
  };
  const listKey = JSON.stringify([listQuery, revision]);
  const orders = useDashboardRead(
    drill && !dead && !scopeError && view !== "legacy" ? listKey : null,
    () => ordersCache.get(listKey, async () => (await fetchPurchaseAnalyticsOrders(listQuery)).data),
    owner,
    stale,
  );
  const push = (next: URLSearchParams) => {
    if (guard()) setSp(next, { state: { procurement: true } });
  };
  const base = () => {
    const p = new URLSearchParams(sp);
    drillKeys.forEach((k) => p.delete(k));
    return p;
  };
  const switchView = (target: View, carry = false) => {
    if (target === view && !drill && !carry) return;
    const p = carry ? firstWorkbenchPage(base()) : base();
    p.set("view", target);
    for (const k of Array.from(p.keys())) if (["group_by", "group_key", "gpage"].includes(k)) p.delete(k);
    if (target === "legacy") {
      p.delete("scope");
      p.delete("pn_label");
      p.delete("pool_label");
      p.delete("scope_name");
    }
    if (carry && drill) {
      p.delete("pn_label");
      p.delete("pool_label");
      p.set("scope", JSON.stringify(drill));
      p.set("scope_name", sp.get("drill_name") || "下钻范围");
      if (sp.has("drill_from") && sp.has("drill_to")) {
        p.set("preset", "custom");
        p.set("from", sp.get("drill_from")!);
        p.set("to", sp.get("drill_to")!);
      }
    }
    if (target !== "legacy" && (carry || target === "compare")) {
      const period = resolveTimeScope((carry ? drill : activeScope) || {}, carry ? scopedFilters : filters);
      if (period?.hasBucket && period.from && period.to) {
        p.set("preset", "custom");
        p.set("from", period.from);
        p.set("to", period.to);
        if (Object.keys(period.scope).length) p.set("scope", JSON.stringify(period.scope));
        else {
          p.delete("scope");
          p.delete("scope_name");
        }
      }
    }
    push(p);
  };
  const back = () => {
    if (!guard()) return;
    if (location.state?.procurement || (window.history.state?.idx ?? 0) > initialIndex.current) {
      navigate(-1);
      return;
    }
    const next = new URLSearchParams(sp);
    if (state.orderId) next.delete("order_id");
    else if (drill) drillKeys.forEach((k) => next.delete(k));
    else if (view === "work" && next.has("wb_workBuyer")) {
      for (const key of ["wb_workBuyer", "wb_trendPage", "wb_goodsPage", "wb_supplierPage"]) next.delete(key);
    } else {
      next.set("view", "boards");
      for (const k of Array.from(next.keys())) if (k.startsWith("wb_")) next.delete(k);
    }
    setSp(next, { replace: true });
  };
  const onDrill = (scope: PurchaseScope, label: string) => {
    const p = base();
    const { date_from, date_to, ...exact } = scope;
    p.set("view", view);
    p.set("drill", JSON.stringify({ ...activeScope, ...exact }));
    p.set("drill_name", label);
    if (date_from) p.set("drill_from", date_from);
    if (date_to) p.set("drill_to", date_to);
    push(p);
  };
  const reset = () => {
    search("");
    setQuery("");
    setSp({ view }, { replace: true });
  };
  const common = {
    filters,
    ownerToken: owner,
    onOwnerStale: stale,
    onDrill,
    costRestricted,
    customerRestricted,
    supplierRestricted,
    scope: activeScope,
    refreshKey: revision,
  };
  const refreshButton = (
    <Button
      onClick={() => {
        if (guard()) refresh((x) => x + 1);
      }}
    >
      刷新数据
    </Button>
  );
  const activeView = view as WorkbenchView;
  const nav = (
    <nav aria-label="采购分析看板" className="purchase-dashboard-nav">
      {(Object.entries(views) as [View, string][]).map(([key, label]) => (
        <Button
          type={key === view ? "primary" : "text"}
          key={key}
          aria-current={key === view ? "page" : undefined}
          onClick={() => switchView(key)}
        >
          {label}
        </Button>
      ))}
    </nav>
  );
  if (dead || sessionStale(owner)) return <Alert type="warning" showIcon message={SESSION_EXPIRED_MESSAGE} />;
  return (
    <ConfigProvider theme={analyticsControlTheme}>
      <PreviewCacheContext.Provider value={previewCache}>
        {view === "legacy" ? (
          <>
            <Button size="small" onClick={back}>
              返回上一个面板
            </Button>
            {nav}
            <Suspense fallback={<Spin />}>
              <Legacy />
            </Suspense>
          </>
        ) : (
          <>
            <PageHeader
              title="采购分析"
              subtitle="从经手工作量、采购结构和 PN 供货情况追溯到采购单。"
              extra={
                <Space>
                  {(view !== "boards" ||
                    drill ||
                    Array.from(sp.keys()).some((key) => key.startsWith("wb_")) ||
                    location.state?.procurement) && <Button onClick={back}>返回上一个面板</Button>}
                  {refreshButton}
                </Space>
              }
            />
            {nav}
            <Card size="small" className="purchase-dashboard-filter">
              <FilterBar
                state={drill && sp.has("drill_from") && sp.has("drill_to")
                  ? { ...effective, preset: "custom", from: sp.get("drill_from"), to: sp.get("drill_to") }
                  : effective}
                patch={patchFilters}
                legacyControls={false}
                sourceTypes={options?.source_types ?? []}
                purchasers={options?.purchasers ?? []}
                salespersons={options?.salespersons ?? []}
                categories={options?.categories ?? []}
                customerOptions={customers}
                customerOptionsLoading={!opt}
                onCustomerSearch={search}
                customersHasMore={options?.customers_has_more}
                costRestricted={costRestricted}
                customerRestricted={customerRestricted}
                supplierRestricted={supplierRestricted}
                onReset={reset}
              />
              {!drill && (
                <ProductScopeFilter
                  filters={filters}
                  scope={activeScope || {}}
                  labels={{ pn: sp.get("pn_label"), pool: sp.get("pool_label") }}
                  owner={owner}
                  stale={stale}
                  revision={revision}
                  onChange={(axis, item) => {
                    if (!guard()) return;
                    const next = firstWorkbenchPage(base()),
                      exact = { ...activeScope };
                    if (axis === "pn") delete exact.partId;
                    if (item?.label && axis !== "unit")
                      next.set(axis + "_label", axis === "pn" ? `${item.key} · ${item.label}` : item.label);
                    else next.delete(axis + "_label");
                    if (item) exact[axis] = item.key;
                    else delete exact[axis];
                    if (Object.keys(exact).length) next.set("scope", JSON.stringify(exact));
                    else next.delete("scope");
                    next.delete("scope_name");
                    setSp(next, { replace: true });
                  }}
                />
              )}
              {opt?.error && <Alert type="error" message={opt.error} action={refreshButton} />}
              {!!options?.hidden_departed_salesperson_count && (
                <small>
                  已隐藏 {options.hidden_departed_salesperson_count}{" "}
                  位离职销售的关联采购单。汇总保留历史成交。
                </small>
              )}
              {activeScope && (
                <Tag
                  closable
                  onClose={() => {
                    const p = base();
                    p.delete("scope");
                    p.delete("scope_name");
                    push(p);
                  }}
                >
                  当前范围 · {sp.get("scope_name") || "精确筛选"}
                </Tag>
              )}
            </Card>
            {scopeError ? (
              <Alert type="error" message={scopeError} action={<Button onClick={reset}>清除范围</Button>} />
            ) : (
              <Suspense fallback={<Spin />}>
                <ChartSampleCache.Provider value={chartSampleCache}>
                  <WorkbenchProvider {...common} view={activeView}>
                    {!drill && view === "compare" && !resolveTimeScope(activeScope || {}, filters) ? (
                      <Alert
                        type="info"
                        message="当前范围缺少可比较的日期。清除无日期的分组或选择有成交的区间。"
                      />
                    ) : (
                      !drill &&
                      (view === "pool" ? <Pool {...common} /> : <Workbench {...common} view={activeView} />)
                    )}
                    {drill && (
                      <Card
                        size="small"
                        title={`${views[view]} / 采购单`}
                        extra={
                          <Space wrap>
                            <Button size="small" onClick={() => switchView("boards", true)}>
                              沿用范围看排行
                            </Button>
                            <Button size="small" onClick={() => switchView("matrix", true)}>
                              沿用范围看矩阵
                            </Button>
                          </Space>
                        }
                      >
                        <p>
                          核对当前范围的可见采购单与匹配行。悬停或聚焦单号可看货品和数量，点订单行看明细。
                        </p>
                        {(sp.has("drill_from") || sp.has("drill_to")) && (
                          <p>
                            下钻期间 {scopedFilters.date_from} 至 {scopedFilters.date_to}
                          </p>
                        )}
                        {orders?.error ? (
                          <Alert type="error" message={orders.error} action={refreshButton} />
                        ) : (
                          <DrillTable
                            data={orders?.data ?? null}
                            loading={!orders}
                            basis={costRestricted ? [] : state.basis}
                            costRestricted={costRestricted}
                            groupByLabel="下钻范围"
                            groupKey={sp.get("drill_name") || "当前范围"}
                            sort={state.sort}
                            dir={state.dir}
                            page={state.page}
                            pageSize={state.pageSize}
                            onSortChange={(sort, dir) => patch({ sort, dir, page: 1 })}
                            onPageChange={(page, pageSize) => patch({ page, pageSize })}
                            onBack={back}
                            onOpenOrder={(id) => {
                              const p = new URLSearchParams(sp);
                              p.set("order_id", String(id));
                              push(p);
                            }}
                            renderNumber={(row, sale) => {
                              const number = sale ? row.linked_sales_order_no : row.order_no;
                              return number ? (
                                <DocumentPreview
                                  kind={sale ? "sale" : "purchase"}
                                  number={number}
                                  orderId={row.order_id}
                                  filters={scopedFilters}
                                  scope={drill}
                                  ownerToken={owner}
                                  onOwnerStale={stale}
                                />
                              ) : (
                                "—"
                              );
                            }}
                          />
                        )}
                        <OrderDetailDrawer
                          orderId={state.orderId}
                          category={effective.category}
                          basis={costRestricted ? [] : state.basis}
                          scope={drill}
                          filters={scopedFilters}
                          ownerToken={owner}
                          onOwnerStale={stale}
                          onClose={back}
                        />
                      </Card>
                    )}
                  </WorkbenchProvider>
                </ChartSampleCache.Provider>
              </Suspense>
            )}
          </>
        )}
      </PreviewCacheContext.Provider>
    </ConfigProvider>
  );
}
