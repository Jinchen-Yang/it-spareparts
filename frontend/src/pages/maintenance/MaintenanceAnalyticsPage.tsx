import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Input,
  Row,
  Segmented,
  Select,
  Skeleton,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import type { ColumnsType, TableProps } from "antd/es/table";
import { useSearchParams, Link } from "react-router-dom";
import dayjs from "dayjs";
import { BarChartOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  fetchAnalyticsFilterOptions,
  fetchPnRanking,
  fetchSpendTrend,
  type PnRanking,
  type PnRankingParams,
  type PnRankingRow,
  type SpendBusinessTypeRow,
  type SpendProjectRow,
  type SpendSalespersonRow,
  type SpendTrendBucket,
  type SpendTrendGranularity,
  type SpendTrendParams,
  type SpendTrendResponse,
} from "../../api/maintenanceAnalytics";
import {
  getMaintenanceProject,
  searchMaintenanceProjects,
} from "../../api/maintenanceProjects";
import {
  BOARD_BUSINESS_TYPE_CODES,
  BOARD_BUSINESS_TYPE_LABELS,
  boardBusinessTypeParam,
  type BoardBusinessTypeCode,
} from "../../api/maintenanceBossBoard";
import { readPermissionMap } from "../../nav";
import PageHeader from "../../components/PageHeader";
import { PnTopBar } from "../../components/charts/PnTopBar";
import {
  SPEND_CATEGORY_CODES,
  SPEND_CATEGORY_LABELS,
  SPEND_GRANULARITY_LABELS,
  SpendTrendBar,
  formatSpendBucket,
} from "../../components/charts/SpendTrendBar";
import { raw } from "../maintenance/panel/panelUtils";
import { moneyExact, qty as qtyFmt } from "../../utils/format";

const { Text } = Typography;

/** 六态信封渲染：restricted 🔒、not_imported ●、绝不渲染 0（铁律 5）。 */
function statText(stat: { state: string; value: unknown } | undefined): string {
  if (!stat) return "—";
  if (stat.state === "restricted") return "🔒 无权限";
  if (stat.state === "not_imported") return "尚未导入";
  if (stat.state === "error") return "暂不可用";
  return stat.value === null || stat.value === "" ? "—" : String(stat.value);
}

/** 金额信封 → 千分位金额（¥1,586,637.81），无值/受限走 statText。 */
function statMoney(stat: { state: string; value: unknown } | undefined): string {
  if (stat && stat.state === "ready" && stat.value !== null && stat.value !== "") {
    return moneyExact(Number(stat.value));
  }
  return statText(stat);
}

const RANGE_OPTIONS = [
  { label: "本年", value: "ytd" },
  { label: "近12月", value: "12m" },
  { label: "全部", value: "all" },
  { label: "自定义", value: "custom" },
];

const SORT_OPTIONS = [
  { label: "含税成本", value: "cost_inc" },
  { label: "未税成本", value: "cost_ex" },
  { label: "有效数量", value: "effective_qty" },
  { label: "需求数量", value: "qty" },
  { label: "行次数", value: "occurrences" },
  { label: "坏件返还量", value: "bad_qty" },
];

/** 表头排序 → 服务端排序键（服务端分页：必须整库排序，不能只排当页）。 */
const SORTER_TO_KEY: Record<string, string> = {
  pn: "pn",
  occurrences: "occurrences",
  order_count: "order_count",
  project_count: "project_count",
  qty: "qty",
  return_qty: "return_qty",
  effective_qty: "effective_qty",
  monthly_avg_qty: "monthly_avg",
  cost_inc: "cost_inc",
  cost_ex: "cost_ex",
  cost_share_pct: "cost_share",
  bad_return_qty: "bad_qty",
  bad_return_rate_pct: "bad_rate",
  missing_lines: "missing_lines",
};

const PAGE_SIZE_OPTIONS = [20, 50, 100];

const DEMAND_TYPE_OPTIONS = [
  { label: "报修供货", value: "repair" },
  { label: "补库供货", value: "stock" },
];
const DEMAND_TYPE_CODES = DEMAND_TYPE_OPTIONS.map((option) => option.value);

const COST_SOURCE_OPTIONS = [
  { label: "系统关联", value: "linked" },
  { label: "估算", value: "estimated" },
  { label: "人工回填", value: "manual" },
  { label: "缺失", value: "missing" },
];
const COST_SOURCE_CODES = COST_SOURCE_OPTIONS.map((option) => option.value);

/** 开支统计粒度（URL 参数 granularity；未知值退回本月度）。 */
const GRANULARITY_OPTIONS = (Object.keys(SPEND_GRANULARITY_LABELS) as SpendTrendGranularity[])
  .map((value) => ({ label: SPEND_GRANULARITY_LABELS[value], value }));

function readGranularity(spec: string | null): SpendTrendGranularity {
  return spec === "day" || spec === "week" || spec === "year" ? spec : "month";
}

/** 后端 detail（字符串或 {message}）→ 页面可读错误文案。 */
function apiErrorMessage(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: { message?: string } | string } } })
    .response?.data?.detail;
  const msg = typeof detail === "string" ? detail : detail?.message;
  return msg || "加载失败";
}

/** URL CSV → 去重选中值（空串/空段丢弃）。 */
function csvValues(spec: string | null): string[] {
  if (!spec) return [];
  return [...new Set(spec.split(",").map((value) => value.trim()).filter(Boolean))];
}

/** URL CSV → 原样值（仓库用）：库内原值精确匹配，去空白会让「 广州仓 」选不中自己。 */
function csvRawValues(spec: string | null): string[] {
  if (!spec) return [];
  return [...new Set(spec.split(",").filter((value) => value !== ""))];
}

/** 选中集合 → URL CSV：空集或全选返回 null（参数省略，等于不过滤）。 */
function csvParam(selected: string[], all: string[]): string | null {
  if (!selected.length || selected.length === all.length) return null;
  return selected.join(",");
}

/** 项目 id 兜底短标签：项目名取不到时不展示裸长 id。 */
function shortProjectLabel(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}

function KpiCard({ label, value, sub, loading }: {
  label: string; value: string; sub?: string; loading?: boolean;
}) {
  return (
    <Card size="small" style={{ flex: "1 1 180px" }}>
      <Text type="secondary" style={{ fontSize: 12 }}>{label}</Text>
      <div style={{ fontSize: 20, fontWeight: 600, marginTop: 4, lineHeight: 1.4 }}>
        {loading ? <Skeleton.Button active size="small" style={{ width: 140 }} /> : value}
      </div>
      {sub && !loading ? <Text type="secondary" style={{ fontSize: 12 }}>{sub}</Text> : null}
    </Card>
  );
}

/**
 * 维保数据分析看板：PN 成本排名 + 损坏频率（2026-08-21）。
 * URL 即筛选状态（PoolAnalysis 范式）：range/sort/q/business_type/page/ps/from/to 与
 * 全字段筛选（project/customer/sp/order_no/demand_type/warehouse/cost_source）全入 query，
 * 刷新/分享不丢上下文。
 */
export function MaintenanceAnalyticsPage() {
  const [sp, setSp] = useSearchParams();
  const rangeKey = sp.get("range") ?? "ytd";
  const sort = sp.get("sort") ?? "cost_inc";
  const q = sp.get("q") ?? "";
  const [searchDraft, setSearchDraft] = useState(q);
  useEffect(() => { setSearchDraft(q); }, [q]);
  const page = Number(sp.get("page") ?? "1") || 1;
  const pageSize = Number(sp.get("ps") ?? "20") || 20;
  const customFrom = sp.get("from");
  const customTo = sp.get("to");
  const businessTypeSpec = sp.get("business_type") ?? "all";
  const businessTypes = useMemo(() => {
    const selected = BOARD_BUSINESS_TYPE_CODES.filter((code) => businessTypeSpec.split(",").includes(code));
    return selected.length && businessTypeSpec !== "all" ? selected : [...BOARD_BUSINESS_TYPE_CODES];
  }, [businessTypeSpec]);
  const businessType = boardBusinessTypeParam(businessTypes);
  const projectSpec = sp.get("project") ?? "";
  const projectIds = useMemo(() => csvValues(projectSpec), [projectSpec]);
  const customer = sp.get("customer") ?? "";
  const sales = sp.get("sp") ?? "";
  const orderNo = sp.get("order_no") ?? "";
  const demandTypeSpec = sp.get("demand_type") ?? "";
  const demandTypes = useMemo(
    () => DEMAND_TYPE_CODES.filter((code) => csvValues(demandTypeSpec).includes(code)),
    [demandTypeSpec],
  );
  const demandTypeParam = csvParam(demandTypes, DEMAND_TYPE_CODES);
  const warehouseSpec = sp.get("warehouse") ?? "";
  const warehouses = useMemo(() => csvRawValues(warehouseSpec), [warehouseSpec]);
  const costSourceSpec = sp.get("cost_source") ?? "";
  const costSources = useMemo(
    () => COST_SOURCE_CODES.filter((code) => csvValues(costSourceSpec).includes(code)),
    [costSourceSpec],
  );
  const costSourceParam = csvParam(costSources, COST_SOURCE_CODES);
  /** 视图页签：pn（默认）/ spend；granularity 仅 spend 使用，但都入 URL 便于分享。 */
  const view = sp.get("view") === "spend" ? "spend" : "pn";
  const granularity = readGranularity(sp.get("granularity"));

  const [customerDraft, setCustomerDraft] = useState(customer);
  useEffect(() => { setCustomerDraft(customer); }, [customer]);
  const [salesDraft, setSalesDraft] = useState(sales);
  useEffect(() => { setSalesDraft(sales); }, [sales]);
  const [orderNoDraft, setOrderNoDraft] = useState(orderNo);
  useEffect(() => { setOrderNoDraft(orderNo); }, [orderNo]);

  const perms = readPermissionMap();
  const canCost = !!perms.data_purchase_cost;

  const patch = useCallback((next: Record<string, string | null>) => {
    setSp((prev) => {
      const merged = new URLSearchParams(prev);
      for (const [k, v] of Object.entries(next)) {
        if (v === null || v === "") merged.delete(k);
        else merged.set(k, v);
      }
      return merged;
    }, { replace: true });
  }, [setSp]);

  // 项目远程搜索：300ms 防抖 + 代次守卫；id→项目名缓存保证已选标签不被新搜索顶掉。
  const [projectOptions, setProjectOptions] = useState<{ value: string; label: string }[]>([]);
  const [projectLabels, setProjectLabels] = useState<Record<string, string>>({});
  const [projectSearching, setProjectSearching] = useState(false);
  const projectSearchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const projectSearchGen = useRef(0);

  const cacheProjectLabels = useCallback((entries: { project_id: string; display_name: string }[]) => {
    setProjectLabels((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const entry of entries) {
        if (next[entry.project_id] !== entry.display_name) {
          next[entry.project_id] = entry.display_name;
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, []);

  // URL 已选项目回填标签：并发取详情，失败退回短 id（页面照常可用）。
  useEffect(() => {
    const missing = projectIds.filter((id) => !projectLabels[id]);
    if (!missing.length) return;
    let cancelled = false;
    void Promise.allSettled(missing.map((id) => getMaintenanceProject(id))).then((results) => {
      if (cancelled) return;
      cacheProjectLabels(results.map((result, index) => ({
        project_id: missing[index],
        display_name: result.status === "fulfilled"
          ? result.value.data.project.display_name
          : shortProjectLabel(missing[index]),
      })));
    });
    return () => { cancelled = true; };
  }, [projectIds, projectLabels, cacheProjectLabels]);

  const onProjectSearch = useCallback((keyword: string) => {
    if (projectSearchTimer.current) clearTimeout(projectSearchTimer.current);
    const term = keyword.trim();
    if (term.length < 2) {
      projectSearchGen.current += 1;
      setProjectOptions([]);
      setProjectSearching(false);
      return;
    }
    projectSearchTimer.current = setTimeout(() => {
      const gen = ++projectSearchGen.current;
      setProjectSearching(true);
      void searchMaintenanceProjects({ q: term, page_size: 20 })
        .then((resp) => {
          if (gen !== projectSearchGen.current) return;
          const rows = resp.data.rows ?? [];
          setProjectOptions(rows.map((p) => ({ value: p.project_id, label: p.display_name })));
          cacheProjectLabels(rows);
        })
        .catch(() => {
          if (gen === projectSearchGen.current) setProjectOptions([]);
        })
        .finally(() => {
          if (gen === projectSearchGen.current) setProjectSearching(false);
        });
    }, 300);
  }, [cacheProjectLabels]);

  useEffect(() => () => {
    if (projectSearchTimer.current) clearTimeout(projectSearchTimer.current);
  }, []);

  const projectSelectOptions = useMemo(() => {
    const seen = new Set<string>();
    const merged: { value: string; label: string }[] = [];
    for (const id of projectIds) {
      if (seen.has(id)) continue;
      seen.add(id);
      merged.push({ value: id, label: projectLabels[id] ?? shortProjectLabel(id) });
    }
    for (const option of projectOptions) {
      if (seen.has(option.value)) continue;
      seen.add(option.value);
      merged.push(option);
    }
    return merged;
  }, [projectIds, projectLabels, projectOptions]);

  // 仓库候选：挂载时拉一次；失败只提示，不阻塞页面。
  const [warehouseOptions, setWarehouseOptions] = useState<string[]>([]);
  useEffect(() => {
    let cancelled = false;
    void fetchAnalyticsFilterOptions()
      .then((data) => { if (!cancelled) setWarehouseOptions(data.warehouses ?? []); })
      .catch(() => { if (!cancelled) message.error("仓库选项加载失败"); });
    return () => { cancelled = true; };
  }, []);

  const warehouseSelectOptions = useMemo(() => {
    const seen = new Set<string>();
    const merged: { value: string; label: string }[] = [];
    for (const value of [...warehouses, ...warehouseOptions]) {
      if (seen.has(value)) continue;
      seen.add(value);
      merged.push({ value, label: value });
    }
    return merged;
  }, [warehouses, warehouseOptions]);

  const [data, setData] = useState<PnRanking | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = seqRef.current + 1;
    seqRef.current = seq;
    setLoading(true);
    setError(null);
    setData(null);
    try {
      const payload: PnRankingParams = {
        range: rangeKey, sort, page, page_size: pageSize,
        business_type: businessType,
      };
      if (q.trim()) payload.q = q.trim();
      if (projectIds.length) payload.project = projectIds.join(",");
      if (customer.trim()) payload.customer = customer.trim();
      if (sales.trim()) payload.sp = sales.trim();
      if (orderNo.trim()) payload.order_no = orderNo.trim();
      if (demandTypeParam) payload.demand_type = demandTypeParam;
      if (warehouses.length) payload.warehouse = warehouses.join(",");
      if (costSourceParam) payload.cost_source = costSourceParam;
      if (rangeKey === "custom") {
        if (customFrom) payload.date_from = customFrom;
        if (customTo) payload.date_to = customTo;
      }
      const resp = await fetchPnRanking(payload);
      if (seqRef.current !== seq) return; // 代次守卫：旧响应不覆盖新请求
      setData(resp);
    } catch (err) {
      if (seqRef.current !== seq) return;
      setData(null);
      const msg = apiErrorMessage(err);
      setError(msg);
      message.error(msg === "加载失败" ? "维保分析数据加载失败" : msg);
    } finally {
      if (seqRef.current === seq) setLoading(false);
    }
  }, [
    rangeKey, sort, q, page, pageSize, customFrom, customTo, businessType,
    projectIds, customer, sales, orderNo, demandTypeParam, warehouses, costSourceParam,
  ]);

  useEffect(() => {
    if (view !== "pn") return;
    void load();
    return () => { seqRef.current += 1; };
  }, [load, view]);

  // 开支统计：独立请求 + 独立代次守卫；切粒度/筛选只影响 spend，不碰 PN 状态。
  const [spendData, setSpendData] = useState<SpendTrendResponse | null>(null);
  const [spendLoading, setSpendLoading] = useState(false);
  const [spendError, setSpendError] = useState<string | null>(null);
  const spendSeqRef = useRef(0);

  const loadSpend = useCallback(async () => {
    const seq = spendSeqRef.current + 1;
    spendSeqRef.current = seq;
    setSpendLoading(true);
    setSpendError(null);
    setSpendData(null);
    try {
      const payload: SpendTrendParams = {
        range: rangeKey, granularity, business_type: businessType,
      };
      if (projectIds.length) payload.project = projectIds.join(",");
      if (customer.trim()) payload.customer = customer.trim();
      if (sales.trim()) payload.sp = sales.trim();
      if (orderNo.trim()) payload.order_no = orderNo.trim();
      if (demandTypeParam) payload.demand_type = demandTypeParam;
      if (warehouses.length) payload.warehouse = warehouses.join(",");
      if (costSourceParam) payload.cost_source = costSourceParam;
      if (rangeKey === "custom") {
        if (customFrom) payload.date_from = customFrom;
        if (customTo) payload.date_to = customTo;
      }
      const resp = await fetchSpendTrend(payload);
      if (spendSeqRef.current !== seq) return; // 代次守卫：旧响应不覆盖新粒度
      setSpendData(resp);
    } catch (err) {
      if (spendSeqRef.current !== seq) return;
      setSpendData(null);
      const msg = apiErrorMessage(err);
      setSpendError(msg);
      message.error(msg === "加载失败" ? "维保分析数据加载失败" : msg);
    } finally {
      if (spendSeqRef.current === seq) setSpendLoading(false);
    }
  }, [
    rangeKey, granularity, businessType, projectIds, customer, sales, orderNo,
    demandTypeParam, warehouses, costSourceParam, customFrom, customTo,
  ]);

  useEffect(() => {
    if (view !== "spend") return;
    void loadSpend();
    return () => { spendSeqRef.current += 1; };
  }, [loadSpend, view]);

  const onTableChange: TableProps<PnRankingRow>["onChange"] = (pg, _fl, sorter) => {
    const field = Array.isArray(sorter) ? sorter[0]?.field : sorter?.field;
    const key = field ? SORTER_TO_KEY[String(field)] : undefined;
    if (key && key !== sort) {
      patch({ sort: key, page: null });
      return;
    }
    if (pg.pageSize && pg.pageSize !== pageSize) {
      patch({ ps: String(pg.pageSize), page: null });
    } else if (pg.current && pg.current !== page) {
      patch({ page: String(pg.current) });
    }
  };

  const columns: ColumnsType<PnRankingRow> = useMemoColumns(sort);

  const summary = data?.summary;
  const activeError = view === "spend" ? spendError : error;
  const showWbddWarning = view === "spend"
    ? spendData !== null && !spendData.summary.wbdd_ready
    : summary !== undefined && !summary.wbdd_ready;
  const costItems = (data?.rows ?? []).map((r) => ({
    pn: r.pn,
    value: r.cost_inc.state === "ready" && r.cost_inc.value !== null
      ? Number(r.cost_inc.value) : null,
  }));
  const qtyItems = (data?.rows ?? []).map((r) => ({
    pn: r.pn,
    value: Number(r.effective_qty) || null,
  }));

  const spendByBusinessColumns: ColumnsType<SpendBusinessTypeRow> = useMemo(() => [
    { title: "分类", dataIndex: "label", width: 140, render: (v: string, r) => v || r.code },
    { title: "含税金额", dataIndex: "cost_inc", width: 160, align: "right",
      render: (_: unknown, r: SpendBusinessTypeRow) => statMoney(r.cost_inc) },
    { title: "未税金额", dataIndex: "cost_ex", width: 160, align: "right",
      render: (_: unknown, r: SpendBusinessTypeRow) => statMoney(r.cost_ex) },
    { title: "有效数量", dataIndex: "effective_qty", width: 110, align: "right",
      render: (v: string | null) => qtyFmt(v === null ? null : Number(v)) },
    { title: "单数", dataIndex: "order_count", width: 90, align: "right",
      render: (v: number) => qtyFmt(v) },
    { title: "缺价行", dataIndex: "missing_lines", width: 90, align: "right",
      render: (v: number) => qtyFmt(v) },
    { title: "占比", dataIndex: "cost_share_pct", width: 100, align: "right",
      render: (v: number | null) => (v === null ? "—" : `${v}%`) },
  ], []);

  const spendByProjectColumns: ColumnsType<SpendProjectRow> = useMemo(() => [
    { title: "项目", dataIndex: "display_name", width: 240,
      render: (v: string, r: SpendProjectRow) => (r.project_id !== null
        ? <Link to={`/maintenance/projects/${r.project_id}`}>{v}</Link>
        : <Text type="secondary">未归属（无项目）</Text>) },
    { title: "业务类型", dataIndex: "business_type_label", width: 120,
      render: (v: string) => v || "—" },
    { title: "含税金额", dataIndex: "cost_inc", width: 160, align: "right",
      render: (_: unknown, r: SpendProjectRow) => statMoney(r.cost_inc) },
    { title: "未税金额", dataIndex: "cost_ex", width: 160, align: "right",
      render: (_: unknown, r: SpendProjectRow) => statMoney(r.cost_ex) },
    { title: "有效数量", dataIndex: "effective_qty", width: 110, align: "right",
      render: (v: string | null) => qtyFmt(v === null ? null : Number(v)) },
    { title: "单数", dataIndex: "order_count", width: 90, align: "right",
      render: (v: number) => qtyFmt(v) },
    { title: "占比", dataIndex: "cost_share_pct", width: 100, align: "right",
      render: (v: number | null) => (v === null ? "—" : `${v}%`) },
  ], []);

  const spendBySalespersonColumns: ColumnsType<SpendSalespersonRow> = useMemo(() => [
    { title: "销售", dataIndex: "salesperson", width: 160,
      render: (v: string | null) => (v ? v : <Text type="secondary">未标注</Text>) },
    { title: "含税金额", dataIndex: "cost_inc", width: 160, align: "right",
      render: (_: unknown, r: SpendSalespersonRow) => statMoney(r.cost_inc) },
    { title: "未税金额", dataIndex: "cost_ex", width: 160, align: "right",
      render: (_: unknown, r: SpendSalespersonRow) => statMoney(r.cost_ex) },
    { title: "有效数量", dataIndex: "effective_qty", width: 110, align: "right",
      render: (v: string | null) => qtyFmt(v === null ? null : Number(v)) },
    { title: "单数", dataIndex: "order_count", width: 90, align: "right",
      render: (v: number) => qtyFmt(v) },
    { title: "占比", dataIndex: "cost_share_pct", width: 100, align: "right",
      render: (v: number | null) => (v === null ? "—" : `${v}%`) },
  ], []);

  const spendBucketColumns: ColumnsType<SpendTrendBucket> = useMemo(() => [
    { title: "时间", dataIndex: "bucket", width: 110, fixed: "left" as const,
      render: (v: string) => formatSpendBucket(v, granularity) },
    ...SPEND_CATEGORY_CODES.map((code) => ({
      title: SPEND_CATEGORY_LABELS[code],
      key: code,
      width: 130,
      align: "right" as const,
      render: (_: unknown, r: SpendTrendBucket) => statMoney(r.by_business_type?.[code]),
    })),
    { title: "合计含税", key: "cost_inc", width: 150, align: "right" as const,
      render: (_: unknown, r: SpendTrendBucket) => statMoney(r.cost_inc) },
    { title: "合计未税", key: "cost_ex", width: 150, align: "right" as const,
      render: (_: unknown, r: SpendTrendBucket) => statMoney(r.cost_ex) },
    { title: "有效数量", dataIndex: "effective_qty", width: 110, align: "right" as const,
      render: (v: string | null) => qtyFmt(v === null ? null : Number(v)) },
    { title: "缺价行", dataIndex: "missing_lines", width: 90, align: "right" as const,
      render: (v: number) => qtyFmt(v) },
  ], [granularity]);

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <PageHeader
        title={(
          <Space>
            <BarChartOutlined />
            <span>维保数据分析</span>
          </Space>
        )}
        subtitle="全项目 PN 维度：备件消耗成本排名 + 损坏频率（RKD 坏件返还佐证）"
        extra={(
          <Space>
            <Button onClick={() => {
              setSearchDraft("");
              setCustomerDraft("");
              setSalesDraft("");
              setOrderNoDraft("");
              setSp(new URLSearchParams(), { replace: true });
            }}>
              重置筛选
            </Button>
            <Button icon={<ReloadOutlined />}
              onClick={() => { if (view === "spend") void loadSpend(); else void load(); }}
              loading={view === "spend" ? spendLoading : loading}>
              刷新
            </Button>
          </Space>
        )}
      />

      <Card size="small">
        <Space wrap size={12}>
          <Segmented options={RANGE_OPTIONS} value={rangeKey}
            onChange={(v) => patch({ range: String(v), page: null })} />
          {rangeKey === "custom" ? (
            <DatePicker.RangePicker
              value={[customFrom ? dayjs(customFrom) : null, customTo ? dayjs(customTo) : null]}
              onChange={(v) => patch({
                from: v?.[0] ? v[0].format("YYYY-MM-DD") : null,
                to: v?.[1] ? v[1].format("YYYY-MM-DD") : null,
                page: null,
              })}
              allowEmpty={[true, true]}
            />
          ) : null}
          <Select<BoardBusinessTypeCode[]>
            mode="multiple"
            aria-label="业务类型筛选"
            placeholder="业务类型（全部）"
            allowClear
            maxTagCount="responsive"
            value={businessTypes}
            style={{ width: 280, maxWidth: "calc(100vw - 96px)" }}
            options={BOARD_BUSINESS_TYPE_CODES.map((code) => ({
              label: BOARD_BUSINESS_TYPE_LABELS[code], value: code,
            }))}
            onChange={(values) => patch({ business_type: boardBusinessTypeParam(values), page: null })}
          />
          <Select options={SORT_OPTIONS} value={sort} style={{ width: 140 }}
            onChange={(v) => patch({ sort: v, page: null })} />
          <Input.Search allowClear value={searchDraft} placeholder="搜 PN / 描述" style={{ width: 220 }}
            onChange={(event) => setSearchDraft(event.target.value)}
            onSearch={(v) => patch({ q: v || null, page: null })} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            成本＝系统回填已知成本（缺价行单列，不按 0 计）；损坏佐证＝RKD 坏件返还（按项目范围）
          </Text>
        </Space>
        <Space wrap size={12} style={{ marginTop: 12 }}>
          <Select<string[]>
            mode="multiple"
            showSearch
            filterOption={false}
            aria-label="项目筛选"
            placeholder="项目（全部）"
            allowClear
            maxCount={50}
            value={projectIds}
            style={{ width: 280, maxWidth: "calc(100vw - 96px)" }}
            options={projectSelectOptions}
            onSearch={onProjectSearch}
            loading={projectSearching}
            notFoundContent={projectSearching ? "搜索中…" : "输入至少 2 个字搜索项目"}
            onChange={(values) => patch({ project: values.length ? values.join(",") : null, page: null })}
          />
          <Input.Search allowClear value={customerDraft} placeholder="客户" style={{ width: 180 }}
            onChange={(event) => setCustomerDraft(event.target.value)}
            onSearch={(v) => patch({ customer: v || null, page: null })} />
          <Input.Search allowClear value={salesDraft} placeholder="销售" style={{ width: 160 }}
            onChange={(event) => setSalesDraft(event.target.value)}
            onSearch={(v) => patch({ sp: v || null, page: null })} />
          <Input.Search allowClear value={orderNoDraft} placeholder="需求单号" style={{ width: 180 }}
            onChange={(event) => setOrderNoDraft(event.target.value)}
            onSearch={(v) => patch({ order_no: v || null, page: null })} />
          <Select<string[]>
            mode="multiple"
            aria-label="需求类型筛选"
            placeholder="需求类型（全部）"
            allowClear
            value={demandTypes}
            style={{ width: 200 }}
            options={DEMAND_TYPE_OPTIONS}
            onChange={(values) => patch({ demand_type: csvParam(values, DEMAND_TYPE_CODES), page: null })}
          />
          <Select<string[]>
            mode="multiple"
            aria-label="仓库筛选"
            placeholder="仓库（全部）"
            allowClear
            maxCount={10}
            value={warehouses}
            style={{ width: 200 }}
            options={warehouseSelectOptions}
            onChange={(values) => patch({ warehouse: values.length ? values.join(",") : null, page: null })}
          />
          <Select<string[]>
            mode="multiple"
            aria-label="成本来源筛选"
            placeholder="成本来源（全部）"
            allowClear
            value={costSources}
            style={{ width: 220 }}
            options={COST_SOURCE_OPTIONS}
            onChange={(values) => patch({ cost_source: csvParam(values, COST_SOURCE_CODES), page: null })}
          />
        </Space>
      </Card>

      {activeError ? <Alert type="error" showIcon message={activeError} /> : null}
      {showWbddWarning ? (
        <Alert type="warning" showIcon message="维保需求单尚未导入，暂无分析数据" />
      ) : null}

      <Tabs
        activeKey={view}
        onChange={(key) => patch({ view: key === "spend" ? "spend" : null })}
        items={[
          {
            key: "pn",
            label: "PN 排名",
            children: (
              <Space direction="vertical" size={16} style={{ width: "100%" }}>
                <Row gutter={12} style={{ display: "flex", flexWrap: "wrap" }}>
                  <KpiCard label="备件总成本（含税）" loading={loading}
                    value={statMoney(summary?.total_cost_inc)}
                    sub={canCost ? "点表格含税成本列头可按成本排序" : "需要成本查看权限"} />
                  <KpiCard label="涉及 PN 数" loading={loading}
                    value={qtyFmt(summary?.part_count ?? null)} />
                  <KpiCard label="总有效消耗量" loading={loading}
                    value={qtyFmt(summary ? Number(summary.total_effective_qty) : null)}
                    sub="需求数量 − 退货数量" />
                  <KpiCard label="坏件返还总量" loading={loading}
                    value={qtyFmt(summary ? Number(summary.total_bad_return_qty) : null)}
                    sub="RKD 入库确认的坏品/坏件/故障" />
                </Row>

                <Row gutter={16}>
                  <Col xs={24} lg={12}>
                    <Card size="small">
                      <PnTopBar items={costItems} title="Top PN 成本" kind="money"
                        metricLabel="金额合计（含税）" loading={loading} error={error}
                        testId="pn-cost-chart" />
                    </Card>
                  </Col>
                  <Col xs={24} lg={12}>
                    <Card size="small">
                      <PnTopBar items={qtyItems} title="Top PN 消耗频率" kind="qty"
                        metricLabel="数量合计（有效数量）" loading={loading} error={error}
                        testId="pn-qty-chart" />
                    </Card>
                  </Col>
                </Row>

                <Card size="small" title={`PN 排名（共 ${qtyFmt(data?.total ?? null)} 个）`}>
                  <Table<PnRankingRow>
                    rowKey="part_id"
                    size="small"
                    loading={loading}
                    dataSource={data?.rows ?? []}
                    columns={columns}
                    onChange={onTableChange}
                    scroll={{ x: 1600 }}
                    pagination={{
                      current: page,
                      pageSize,
                      total: data?.total ?? 0,
                      pageSizeOptions: PAGE_SIZE_OPTIONS,
                      showSizeChanger: true,
                      showTotal: (t, range) => `第 ${range[0]}–${range[1]} 条 / 共 ${t} 个 PN`,
                    }}
                    locale={{ emptyText: "当前窗口没有分析数据" }}
                  />
                </Card>
              </Space>
            ),
          },
          {
            key: "spend",
            label: "开支统计",
            children: (
              <Space direction="vertical" size={16} style={{ width: "100%" }}>
                <Card size="small">
                  <Space wrap size={12} align="center">
                    <Segmented options={GRANULARITY_OPTIONS} value={granularity}
                      onChange={(v) => patch({ granularity: String(v) })} />
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      粒度只影响分桶展示；金额口径＝已知成本合计（含税，缺价行单列、不按 0 计）
                    </Text>
                  </Space>
                </Card>

                <Row gutter={12} style={{ display: "flex", flexWrap: "wrap" }}>
                  <KpiCard label="开支合计（含税）" loading={spendLoading}
                    value={statMoney(spendData?.summary.total_cost_inc)}
                    sub="已知成本合计；缺价行单列、不按 0 计" />
                  <KpiCard label="开支合计（未税）" loading={spendLoading}
                    value={statMoney(spendData?.summary.total_cost_ex)} />
                  <KpiCard label="有效数量" loading={spendLoading}
                    value={qtyFmt(spendData ? Number(spendData.summary.effective_qty) : null)}
                    sub="需求数量 − 退货数量" />
                  <KpiCard label="期数" loading={spendLoading}
                    value={qtyFmt(spendData?.summary.bucket_count ?? null)}
                    sub={`按${SPEND_GRANULARITY_LABELS[granularity]}分桶`} />
                  <KpiCard label="缺价行" loading={spendLoading}
                    value={qtyFmt(spendData?.summary.missing_lines ?? null)}
                    sub="未计入金额" />
                </Row>

                <Card size="small">
                  <SpendTrendBar
                    buckets={spendData?.buckets ?? []}
                    granularity={granularity}
                    loading={spendLoading}
                    error={spendError}
                    testId="spend-trend-chart"
                  />
                </Card>

                <div data-testid="spend-by-business-type">
                  <Card size="small" title="按业务类型汇总">
                    <Table<SpendBusinessTypeRow>
                      rowKey="code"
                      size="small"
                      loading={spendLoading}
                      dataSource={spendData?.by_business_type ?? []}
                      columns={spendByBusinessColumns}
                      pagination={false}
                      scroll={{ x: 900 }}
                      locale={{ emptyText: "当前窗口没有开支数据" }}
                    />
                  </Card>
                </div>

                <div data-testid="spend-by-project">
                  <Card size="small" title="按项目汇总">
                    <Table<SpendProjectRow>
                      rowKey={(r) => r.project_id ?? "__unassigned__"}
                      size="small"
                      loading={spendLoading}
                      dataSource={spendData?.by_project ?? []}
                      columns={spendByProjectColumns}
                      pagination={{
                        pageSize: 20,
                        showSizeChanger: false,
                        showTotal: (t) => `共 ${t} 项`,
                      }}
                      scroll={{ x: 980 }}
                      locale={{ emptyText: "当前窗口没有开支数据" }}
                    />
                  </Card>
                </div>

                <div data-testid="spend-by-salesperson">
                  <Card size="small" title="按销售汇总">
                    <Table<SpendSalespersonRow>
                      rowKey={(r) => r.salesperson ?? "__unlabeled__"}
                      size="small"
                      loading={spendLoading}
                      dataSource={spendData?.by_salesperson ?? []}
                      columns={spendBySalespersonColumns}
                      pagination={false}
                      scroll={{ x: 800 }}
                      locale={{ emptyText: "当前窗口没有开支数据" }}
                    />
                  </Card>
                </div>

                <div data-testid="spend-bucket-detail">
                  <Card size="small" title="按期明细">
                    <Table<SpendTrendBucket>
                      rowKey="bucket"
                      size="small"
                      loading={spendLoading}
                      dataSource={spendData?.buckets ?? []}
                      columns={spendBucketColumns}
                      scroll={{ x: 1560 }}
                      pagination={{
                        pageSize: 20,
                        showSizeChanger: false,
                        showTotal: (t) => `共 ${t} 期`,
                      }}
                      locale={{ emptyText: "当前窗口没有开支数据" }}
                    />
                  </Card>
                </div>
              </Space>
            ),
          },
        ]}
      />
    </Space>
  );
}

/** 列定义工厂：memo 化，避免每次渲染重建 15 列。 */
function useMemoColumns(_sort: string): ColumnsType<PnRankingRow> {
  return useMemo<ColumnsType<PnRankingRow>>(() => [
    { title: "#", dataIndex: "rank", width: 60, fixed: "left" as const },
    {
      title: "PN / 描述", dataIndex: "pn", width: 300, fixed: "left" as const,
      sorter: true,
      render: (v: string, r: PnRankingRow) => (
        <Space direction="vertical" size={0}>
          <Text strong copyable>{raw(v)}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>{raw(r.description)}</Text>
        </Space>
      ),
    },
    { title: "行次数", dataIndex: "occurrences", sorter: true, width: 90,
      render: (v: number) => qtyFmt(v) },
    { title: "单数", dataIndex: "order_count", sorter: true, width: 80,
      render: (v: number) => qtyFmt(v) },
    { title: "项目数", dataIndex: "project_count", sorter: true, width: 80,
      render: (v: number) => qtyFmt(v) },
    { title: "需求数量", dataIndex: "qty", sorter: true, width: 100,
      render: (v: string | null) => qtyFmt(v === null ? null : Number(v)) },
    { title: "退货", dataIndex: "return_qty", sorter: true, width: 90,
      render: (v: string | null) => qtyFmt(v === null ? null : Number(v)) },
    { title: "有效数量", dataIndex: "effective_qty", sorter: true, width: 100,
      render: (v: string | null) => qtyFmt(v === null ? null : Number(v)) },
    { title: "月均", dataIndex: "monthly_avg_qty", sorter: true, width: 80,
      render: (v: number | null) => (v === null ? "—" : qtyFmt(v)) },
    { title: "含税成本", dataIndex: "cost_inc", sorter: true, width: 140,
      render: (_: unknown, r: PnRankingRow) => statMoney(r.cost_inc) },
    { title: "未税成本", dataIndex: "cost_ex", sorter: true, width: 140,
      render: (_: unknown, r: PnRankingRow) => statMoney(r.cost_ex) },
    { title: "成本占比", dataIndex: "cost_share_pct", sorter: true, width: 100,
      render: (v: number | null) => (v === null ? "—" : `${v}%`) },
    { title: "坏件返还", dataIndex: "bad_return_qty", sorter: true, width: 100,
      render: (v: string | null) => qtyFmt(v === null ? null : Number(v)) },
    { title: "坏返率", dataIndex: "bad_return_rate_pct", sorter: true, width: 90,
      render: (v: number | null) => (v === null ? "—"
        : <Tag color={v > 50 ? "red" : v > 20 ? "orange" : "default"}>{v}%</Tag>) },
    { title: "缺价行", dataIndex: "missing_lines", sorter: true, width: 80,
      render: (v: number) => qtyFmt(v) },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ], []);
}

export default MaintenanceAnalyticsPage;
