import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Alert, Button, Card, DatePicker, Drawer, Grid, Input, Segmented, Select, Table, Tag, Tooltip,
  message,
} from "antd";
import type { ColumnsType, TablePaginationConfig } from "antd/es/table";
import type { SorterResult } from "antd/es/table/interface";
import dayjs, { type Dayjs } from "dayjs";
import PageHeader from "../components/PageHeader";
import {
  dashboardKpi, dashboardTrend, dashboardPartRanking, dashboardSales, dashboardPurchaseOrders,
  dashboardPools, dashboardPool,
  type DashboardKpi, type TrendPoint, type PartRankingResp, type PartRankingRow,
  type SalesOrderRow, type PurchaseOrderRow, type OrdersResp, type OrdersQuery,
  type PoolsResp, type PoolListItem, type PoolDetail as PoolDetailT,
  type PoolMemberRow, type PoolOpportunity,
} from "../api";

const { RangePicker } = DatePicker;

const money = (v: number | null | undefined) =>
  v == null ? "—" : "¥" + Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
const pct = (v: number | null | undefined) =>
  v == null ? "—" : (Number(v) * 100).toFixed(1) + "%";
const num = (v: number | null | undefined) =>
  v == null ? "—" : Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 3 });

type RangeKey = "today" | "7d" | "30d" | "custom";

function useRange() {
  const [key, setKey] = useState<RangeKey>("30d");
  const [custom, setCustom] = useState<[Dayjs, Dayjs] | null>(null);
  const range = useMemo(() => {
    const today = dayjs();
    if (key === "today") return { date_from: today.format("YYYY-MM-DD"), date_to: today.format("YYYY-MM-DD") };
    if (key === "7d") return { date_from: today.subtract(6, "day").format("YYYY-MM-DD"), date_to: today.format("YYYY-MM-DD") };
    if (key === "30d") return { date_from: today.subtract(29, "day").format("YYYY-MM-DD"), date_to: today.format("YYYY-MM-DD") };
    if (custom) return { date_from: custom[0].format("YYYY-MM-DD"), date_to: custom[1].format("YYYY-MM-DD") };
    return {};
  }, [key, custom]);
  return { key, setKey, custom, setCustom, range };
}

/** 极简内联 SVG 折线图（销售/采购/毛利三线，不引图表库）。 */
function TrendChart({ series }: { series: TrendPoint[] }) {
  const W = 720, H = 200, PAD = 32;
  if (!series.length) return <div style={{ color: "var(--mb-text-3)", padding: 24 }}>窗口内无数据</div>;
  const keys: [keyof TrendPoint, string, string][] = [
    ["sales_ex_tax", "销售额", "#3E6FD1"], ["purchase_ex_tax", "采购额", "#B07C33"],
    ["gross_profit", "毛利额", "#4F875C"],
  ];
  const max = Math.max(1, ...series.flatMap((p) => keys.map(([k]) => Number(p[k]) || 0)));
  const x = (i: number) => PAD + (i * (W - 2 * PAD)) / Math.max(1, series.length - 1);
  const y = (v: number) => H - PAD - (v / max) * (H - 2 * PAD);
  return (
    <div style={{ overflowX: "auto" }}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" style={{ maxWidth: W }}>
        <line x1={PAD} y1={H - PAD} x2={W - PAD} y2={H - PAD} stroke="var(--mb-border)" />
        {keys.map(([k, , color]) => (
          <polyline key={k} fill="none" stroke={color} strokeWidth="2"
            points={series.map((p, i) => `${x(i)},${y(Number(p[k]) || 0)}`).join(" ")} />
        ))}
        {series.map((p, i) => (
          <text key={i} x={x(i)} y={H - PAD + 14} fontSize="9"
            textAnchor="middle" fill="var(--mb-text-3)">{p.period.slice(5)}</text>
        ))}
      </svg>
      <div style={{ display: "flex", gap: 16, fontSize: 12.5, marginTop: 4 }}>
        {keys.map(([k, label, color]) => (
          <span key={k as string}><span style={{ display: "inline-block", width: 10, height: 10, background: color, borderRadius: 2, marginRight: 5 }} />{label}</span>
        ))}
      </div>
    </div>
  );
}

function KpiStrip({ k }: { k: DashboardKpi }) {
  const cards: [string, ReactNode, string?, boolean?][] = [
    ["销售额（未税）", money(k.sales_ex_tax)],
    ["采购额（未税）", money(k.purchase_ex_tax)],
    ["毛利额", money(k.gross_profit)],
    ["毛利率", pct(k.gross_margin), "分母=已配成本营收"],
    ["成本覆盖率", pct(k.cost_coverage), "已配成本营收 / 销售额", true],
    ["未配成本营收", money(k.sales_uncosted_ex_tax), "这部分利润未计入", true],
  ];
  return (
    <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 12 }}>
      {cards.map(([label, val, sub, warn]) => (
        <Card key={label} size="small" style={{ flex: "1 1 170px", minWidth: 150,
          background: warn ? "var(--ant-color-warning-bg,#fdf3e3)" : undefined }}>
          <div style={{ fontSize: 12.5, color: warn ? "#9a7b43" : "var(--mb-text-3)" }}>{label}</div>
          <div style={{ fontSize: 20, fontWeight: 500, marginTop: 4, color: warn ? "#9a7b43" : undefined }}>{val}</div>
          {sub && <div style={{ fontSize: 11.5, color: warn ? "#9a7b43" : "var(--mb-text-3)", marginTop: 2 }}>{sub}</div>}
        </Card>
      ))}
    </div>
  );
}

/** 登录时落到 localStorage 的 data_* 权限（App.tsx 同源）。仅用于首渲染 UI 门控；
 * 数据面真值源永远是后端（响应旗标 + 字段脱敏 + 排序退回），本地值过期也不泄漏。 */
function readLocalPerms(): Record<string, boolean> {
  try { return JSON.parse(localStorage.getItem("permissions") || "{}"); } catch { return {}; }
}

export default function BossBoardPage() {
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false;
  const isAdmin = (localStorage.getItem("role") || "") === "admin";
  const { key, setKey, setCustom, range } = useRange();
  const [granularity, setGranularity] = useState("day");
  const [costMethod, setCostMethod] = useState<"moving_avg" | "fifo">("moving_avg");
  // 权限门控（复审四轮）：从**第一次渲染**起就按权限禁用隐藏字段排序，不等首个响应；
  // 响应旗标到达后以后端为准（且只会更严，不会因切换排序而重新放开）。
  const localPerms = useMemo(readLocalPerms, []);
  const localProfitRestricted = !isAdmin && localPerms.data_profit === false;
  const localCostRestricted = !isAdmin && localPerms.data_purchase_cost === false;

  const [kpi, setKpi] = useState<DashboardKpi | null>(null);
  const [trend, setTrend] = useState<TrendPoint[]>([]);
  const [ranking, setRanking] = useState<PartRankingResp | null>(null);
  const [sales, setSales] = useState<OrdersResp<SalesOrderRow> | null>(null);
  const [purchases, setPurchases] = useState<OrdersResp<PurchaseOrderRow> | null>(null);
  const [pools, setPools] = useState<PoolsResp | null>(null);
  const [poolDetail, setPoolDetail] = useState<PoolDetailT | null>(null);
  const [loading, setLoading] = useState(false);

  // 订单拉通两张表各自的服务端查询态（分页/搜索/状态/排序），独立于顶部时间范围。
  // 无利润权限的账号默认排序直接用日期（毛利排序本会被后端退回，不发注定被拦的请求）。
  const [salesQ, setSalesQ] = useState<OrdersQuery>(() => (
    localProfitRestricted
      ? { page: 1, page_size: 20, sort: "order_date", order: "desc" }
      : { page: 1, page_size: 20, sort: "gross_profit", order: "asc" }));
  const [purchaseQ, setPurchaseQ] = useState<OrdersQuery>({ page: 1, page_size: 20, sort: "order_date", order: "desc" });
  const [salesLoading, setSalesLoading] = useState(false);
  const [purchaseLoading, setPurchaseLoading] = useState(false);
  // 数据池表：服务端分页（复审三轮：>100 池也要能翻到）
  const [poolPage, setPoolPage] = useState({ page: 1, page_size: 10 });
  const [poolLoading, setPoolLoading] = useState(false);
  const poolDetailGen = useRef(0);

  // 上部板块（KPI/趋势/盈亏榜/池）：每块独立落库，一块失败不拖垮其余（复审 Standards：不再全有或全无）。
  // 代次守卫（复审三轮 Standards）：快速切时间范围时，旧请求即便最后返回也不得覆盖新范围数据。
  const loadGen = useRef(0);
  const load = useCallback(async () => {
    const gen = ++loadGen.current;
    const alive = () => gen === loadGen.current;
    setLoading(true);
    const errs: string[] = [];
    const into = async <T,>(p: Promise<{ data: T }>, set: (v: T | null) => void, label: string) => {
      try { const { data } = await p; if (alive()) set(data); }
      catch { if (alive()) { set(null); errs.push(label); } }
    };
    await Promise.all([
      into(dashboardKpi(range), setKpi, "经营KPI"),
      into(dashboardTrend({ ...range, granularity }), (d) => setTrend(d?.series ?? []), "趋势"),
      into(dashboardPartRanking({ ...range, cost_method: costMethod, top: 10 }), setRanking, "盈亏榜"),
    ]);
    if (!alive()) return;   // 已被更新的 load 取代 → 丢弃本次收尾（含 loading/错误提示）
    if (errs.length) message.error(`部分板块加载失败：${errs.join("、")}`);
    setLoading(false);
  }, [range, granularity, costMethod]);

  useEffect(() => { load(); }, [load]);

  // 订单表独立拉取（服务端分页/搜索/筛选/排序）：range 或本表查询态变化即重取
  useEffect(() => {
    let alive = true;
    setSalesLoading(true);
    dashboardSales({ ...range, ...salesQ })
      .then(({ data }) => { if (alive) setSales(data); })
      .catch(() => { if (alive) { setSales(null); message.error("销售订单加载失败"); } })
      .finally(() => { if (alive) setSalesLoading(false); });
    return () => { alive = false; };
  }, [range, salesQ]);

  useEffect(() => {
    let alive = true;
    setPurchaseLoading(true);
    dashboardPurchaseOrders({ ...range, ...purchaseQ })
      .then(({ data }) => { if (alive) setPurchases(data); })
      .catch(() => { if (alive) { setPurchases(null); message.error("采购订单加载失败"); } })
      .finally(() => { if (alive) setPurchaseLoading(false); });
    return () => { alive = false; };
  }, [range, purchaseQ]);

  // 数据池：服务端分页（sort=savings 时后端先分析全部池再全局排序分页）
  useEffect(() => {
    let alive = true;
    setPoolLoading(true);
    dashboardPools({ ...range, sort: "savings", page: poolPage.page, page_size: poolPage.page_size })
      .then(({ data }) => { if (alive) setPools(data); })
      .catch(() => { if (alive) { setPools(null); message.error("数据池加载失败"); } })
      .finally(() => { if (alive) setPoolLoading(false); });
    return () => { alive = false; };
  }, [range, poolPage]);

  // 换时间范围时订单表与池表回到第 1 页（避免停在越界页看到空表）
  useEffect(() => {
    setSalesQ((q) => (q.page === 1 ? q : { ...q, page: 1 }));
    setPurchaseQ((q) => (q.page === 1 ? q : { ...q, page: 1 }));
    setPoolPage((q) => (q.page === 1 ? q : { ...q, page: 1 }));
  }, [range]);

  const openPool = async (gid: number) => {
    const gen = ++poolDetailGen.current;
    setPoolDetail(null);
    try {
      const { data } = await dashboardPool(gid, range);
      if (gen === poolDetailGen.current) setPoolDetail(data);
    } catch {
      if (gen === poolDetailGen.current) message.error("池详情加载失败");
    }
  };

  // 时间范围变化后，已打开详情不能继续显示旧窗口；同时使旧请求失效。
  useEffect(() => {
    poolDetailGen.current += 1;
    setPoolDetail(null);
    return () => { poolDetailGen.current += 1; };
  }, [range]);

  // AntD Table onChange → 服务端分页/排序。columnKey 即后端 sort 字段名。
  const onOrdersChange = (
    setQ: React.Dispatch<React.SetStateAction<OrdersQuery>>,
    fallbackSort: string,
  ) => (pag: TablePaginationConfig, _f: unknown, sorter: SorterResult<any> | SorterResult<any>[]) => {
    const s = Array.isArray(sorter) ? sorter[0] : sorter;
    setQ((q) => ({
      ...q,
      page: pag.current || 1,
      page_size: pag.pageSize || q.page_size,
      sort: s?.order ? String(s.columnKey || fallbackSort) : q.sort,
      order: s?.order === "ascend" ? "asc" : s?.order === "descend" ? "desc" : q.order,
    }));
  };
  // 权限化排序禁用（复审四轮）：用**权限旗标**而非 ranking_restricted（后者只表示
  // "本次请求的排序被拦"，切走再切回会重新放开、首渲染也拦不住）。本地权限先行，
  // 响应旗标到达后取并集——只收紧不放开。
  const profitRestricted = localProfitRestricted
    || (sales?.profit_restricted ?? false) || (ranking?.profit_restricted ?? false);
  const costRestricted = localCostRestricted || (purchases?.cost_restricted ?? false);
  const orderSortProps = (
    q: OrdersQuery, key: string, effectiveSort: string | undefined, disabled = false,
  ) => ({
    sorter: !disabled,
    sortOrder: effectiveSort === key ? (q.order === "asc" ? "ascend" as const : "descend" as const) : null,
  });
  const ordersToolbar = (q: OrdersQuery, setQ: React.Dispatch<React.SetStateAction<OrdersQuery>>) => (
    <div style={{ display: "flex", gap: 8, marginBottom: 8, flexWrap: "wrap" }}>
      <Input.Search allowClear placeholder="搜索 型号 / 单号 / 描述 / 品牌" style={{ width: 260 }}
        defaultValue={q.q} onSearch={(v) => setQ((s) => ({ ...s, q: v || undefined, page: 1 }))} />
      <Select style={{ width: 130 }} value={q.status ?? ""}
        onChange={(v) => setQ((s) => ({ ...s, status: v || undefined, page: 1 }))}
        options={[{ label: "仅已生效", value: "" }, { label: "全部状态", value: "全部" }]} />
    </div>
  );

  const rankCols = (loss: boolean): ColumnsType<PartRankingRow> => [
    { title: "型号", dataIndex: "pn_std", width: 160, render: (v, r) => (
      <span><span style={{ fontFamily: "monospace", fontSize: 12.5 }}>{v}</span>
        {r.brand && <Tag style={{ marginLeft: 6 }}>{r.brand}</Tag>}</span>) },
    { title: "销量", dataIndex: "qty_sold", width: 72, align: "right", render: num },
    { title: "营收(未税)", dataIndex: "revenue", width: 110, align: "right", render: money },
    { title: costMethod === "fifo" ? "毛利(FIFO)" : "毛利(移动加权)",
      dataIndex: costMethod === "fifo" ? "gross_profit_fifo" : "gross_profit_moving",
      width: 110, align: "right",
      render: (v) => <span style={{ color: v < 0 ? "#c0524a" : "#3f7a45" }}>{money(v)}</span> },
    { title: "毛利率", dataIndex: costMethod === "fifo" ? "gross_margin_fifo" : "gross_margin_moving",
      width: 80, align: "right", render: pct },
    { title: "采购均价", key: "pw", width: 90, align: "right",
      render: (_, r) => money(r.purchase_price?.wavg) },
    { title: "覆盖率", dataIndex: "cost_coverage", width: 72, align: "right", render: pct },
  ];

  // 订单粒度：一张销售订单一行（多型号聚合）。列 key = 后端 sort 字段名（用于服务端排序）。
  const salesCols: ColumnsType<SalesOrderRow> = [
    { title: "日期", dataIndex: "order_date", key: "order_date", width: 116, ...orderSortProps(salesQ, "order_date", sales?.effective_sort),
      render: (v, r) => (<span>{v || "—"}{r.is_future && <Tag color="red" style={{ marginLeft: 4 }}>未来</Tag>}</span>) },
    { title: "销售单号", dataIndex: "order_no", width: 130, render: (v) => <span style={{ fontFamily: "monospace", fontSize: 12 }}>{v}</span> },
    { title: "客户", dataIndex: "customer", width: 130, ellipsis: true, render: (v) => v ?? "—" },
    { title: "销售员", dataIndex: "salesperson", width: 80, render: (v) => v ?? "—" },
    { title: "型号数", dataIndex: "part_count", key: "part_count", width: 88, align: "right", ...orderSortProps(salesQ, "part_count", sales?.effective_sort) },
    { title: "总量", dataIndex: "total_qty", width: 70, align: "right", render: num },
    { title: "营收(未税)", dataIndex: "total_revenue", key: "revenue", width: 124, align: "right",
      ...orderSortProps(salesQ, "revenue", sales?.effective_sort), render: money },
    { title: "毛利", dataIndex: "total_gross_profit", key: "gross_profit", width: 116, align: "right",
      ...orderSortProps(salesQ, "gross_profit", sales?.effective_sort, profitRestricted),
      render: (v) => profitRestricted ? <Tag>无利润权限</Tag>
        : v == null ? <Tag>无成本</Tag>
        : <span style={{ color: v < 0 ? "#c0524a" : undefined }}>{money(v)}</span> },
    { title: "状态", dataIndex: "data_status", width: 84, render: (v) => v ? <Tag>{v}</Tag> : "—" },
    { title: "采购拉通", dataIndex: "linked_purchase", width: 84, align: "center",
      render: (v) => v ? <Tag color="blue">已生效</Tag> : <span style={{ color: "var(--mb-text-3)" }}>—</span> },
  ];

  // 订单粒度：一张采购订单一行
  const purchaseCols: ColumnsType<PurchaseOrderRow> = [
    { title: "日期", dataIndex: "order_date", key: "order_date", width: 116, ...orderSortProps(purchaseQ, "order_date", purchases?.effective_sort),
      render: (v, r) => (<span>{v || "—"}{r.is_future && <Tag color="red" style={{ marginLeft: 4 }}>未来</Tag>}</span>) },
    { title: "采购单号", dataIndex: "order_no", width: 140, render: (v) => <span style={{ fontFamily: "monospace", fontSize: 12 }}>{v}</span> },
    { title: "采购员", dataIndex: "purchaser", width: 80, render: (v) => v ?? "—" },
    { title: "类型", dataIndex: "source_type", width: 90, render: (v) => v ? <Tag>{v}</Tag> : "—" },
    { title: "型号数", dataIndex: "part_count", key: "part_count", width: 88, align: "right", ...orderSortProps(purchaseQ, "part_count", purchases?.effective_sort) },
    { title: "总量", dataIndex: "total_qty", width: 70, align: "right", render: num },
    { title: "金额(未税)", dataIndex: "total_ex_tax", key: "amount", width: 124, align: "right",
      ...orderSortProps(purchaseQ, "amount", purchases?.effective_sort, costRestricted),
      render: (v) => costRestricted ? <Tag>无成本权限</Tag> : money(v) },
    { title: "关联销售单", dataIndex: "linked_sales_order", width: 130, render: (v) => v || <span style={{ color: "var(--mb-text-3)" }}>—</span> },
    { title: "状态", dataIndex: "data_status", width: 84, render: (v) => v ? <Tag>{v}</Tag> : "—" },
  ];

  const poolCols: ColumnsType<PoolListItem> = [
    { title: "池号", dataIndex: "group_id", width: 70 },
    { title: "成员", dataIndex: "member_count", width: 70, align: "right" },
    { title: "池需求量", dataIndex: "demand_qty", width: 90, align: "right", render: num },
    { title: "池营收(未税)", dataIndex: "demand_revenue_ex_tax", width: 120, align: "right", render: money },
    { title: "理论节省上限", dataIndex: "theoretical_saving", width: 120, align: "right",
      render: (v) => <span style={{ color: "#9a7b43" }}>{money(v)}</span> },
    { title: "供应层面上限", dataIndex: "supply_available_upper", width: 120, align: "right",
      render: (v) => <Tooltip title="仍待人工核实兼容性/客户指定品牌，非可执行金额">{money(v)}</Tooltip> },
    { title: "标记", key: "flags", width: 140, render: (_, r) => (
      <span>{r.needs_calibration && <Tag color="orange">关系待校准</Tag>}{r.oversized && <Tag color="red">超限</Tag>}</span>) },
    { title: "", key: "act", width: 70, render: (_, r) => <a onClick={() => openPool(r.group_id)}>详情</a> },
  ];

  return (
    <>
      <PageHeader title="老板经营看板"
        subtitle="今天发生了什么 · 哪些型号赚钱/亏钱 · 哪些订单要拉通 · 通用号降本机会（金额均未税）"
        extra={
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <Segmented value={key} onChange={(v) => setKey(v as RangeKey)}
              options={[{ label: "今天", value: "today" }, { label: "近7天", value: "7d" },
                { label: "近30天", value: "30d" }, { label: "自定义", value: "custom" }]} />
            {key === "custom" && <RangePicker onChange={(v) => setCustom(v as [Dayjs, Dayjs])} />}
            <Button size="small" onClick={load} loading={loading}>刷新</Button>
          </div>
        } />

      {kpi && kpi.orders_future > 0 && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }}
          message={`发现 ${kpi.orders_future} 张未来日期订单（已排除出经营 KPI，请核对是否录入错误）`} />
      )}
      {kpi && <KpiStrip k={kpi} />}
      {kpi && (
        <div style={{ marginBottom: 16, fontSize: 12.5, color: "var(--mb-text-3)" }}>
          订单健康：已生效 {kpi.orders_active} · 进行中 {kpi.orders_in_progress} · 取消/作废 {kpi.orders_cancelled} ·
          异常行 {kpi.anomaly_lines} · 被排除营收 {money(kpi.excluded_revenue)}
        </div>
      )}

      <Card title="经营趋势" style={{ marginBottom: 16 }} size="small"
        extra={<Segmented size="small" value={granularity} onChange={(v) => setGranularity(v as string)}
          options={[{ label: "日", value: "day" }, { label: "周", value: "week" }, { label: "月", value: "month" }]} />}>
        <TrendChart series={trend} />
      </Card>

      <Card title="型号盈亏排名" style={{ marginBottom: 16 }} size="small"
        extra={<Segmented size="small" value={costMethod} onChange={(v) => setCostMethod(v as any)}
          options={[{ label: "移动加权", value: "moving_avg" }, { label: "FIFO", value: "fifo" }]} />}>
        {ranking?.profit_restricted ? (
          <Alert type="info" showIcon message="无利润查看权限：盈亏排名（含型号赚/亏归属与计数）对当前账号不可见。" />
        ) : (<>
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
            <div style={{ flex: "1 1 480px", minWidth: 300 }}>
              <div style={{ fontWeight: 500, marginBottom: 6, color: "#3f7a45" }}>💰 赚钱榜</div>
              <Table size="small" rowKey="part_id" pagination={false} loading={loading}
                dataSource={ranking?.profitable || []} columns={rankCols(false)} scroll={{ x: 700 }} />
            </div>
            <div style={{ flex: "1 1 480px", minWidth: 300 }}>
              <div style={{ fontWeight: 500, marginBottom: 6, color: "#c0524a" }}>📉 亏损榜</div>
              <Table size="small" rowKey="part_id" pagination={false} loading={loading}
                dataSource={ranking?.loss || []} columns={rankCols(true)} scroll={{ x: 700 }} />
            </div>
          </div>
          {ranking?.counts && (
            <div style={{ marginTop: 8, fontSize: 12, color: "var(--mb-text-3)" }}>
              共 {ranking.counts.total_parts} 型号，有成本 {ranking.counts.with_cost}（赚 {ranking.counts.profitable} / 亏 {ranking.counts.loss}），
              无成本 {ranking.counts.no_cost_parts}（毛利未知，不入榜）
            </div>
          )}
        </>)}
      </Card>

      <Card title="订单拉通 · 销售订单（一单一行，点表头排序）" style={{ marginBottom: 16 }} size="small">
        {ordersToolbar(salesQ, setSalesQ)}
        <Table<SalesOrderRow> size="small" rowKey="order_id" loading={salesLoading}
          dataSource={sales?.items || []} columns={salesCols} scroll={{ x: 1080 }}
          onChange={onOrdersChange(setSalesQ, "gross_profit")}
          pagination={{ current: sales?.page ?? 1, pageSize: salesQ.page_size, total: sales?.total ?? 0,
            showSizeChanger: true, pageSizeOptions: [20, 50, 100], showTotal: (t) => `共 ${t} 单` }} />
      </Card>

      <Card title="订单拉通 · 采购订单（一单一行，点表头排序）" style={{ marginBottom: 16 }} size="small">
        {ordersToolbar(purchaseQ, setPurchaseQ)}
        <Table<PurchaseOrderRow> size="small" rowKey="order_id" loading={purchaseLoading}
          dataSource={purchases?.items || []} columns={purchaseCols} scroll={{ x: 1080 }}
          onChange={onOrdersChange(setPurchaseQ, "order_date")}
          pagination={{ current: purchases?.page ?? 1, pageSize: purchaseQ.page_size, total: purchases?.total ?? 0,
            showSizeChanger: true, pageSizeOptions: [20, 50, 100], showTotal: (t) => `共 ${t} 单` }} />
      </Card>

      <Card title="通用号数据池 · 潜在降本机会" size="small">
        <Alert type="info" showIcon style={{ marginBottom: 10 }}
          message="只读分析·潜在降本机会：所有替换均「待核实」兼容性/客户指定品牌/合同，当前无可执行金额。库存 8 月盘点前不作推荐条件，供应稳定性看采购频次/供应商数/最近采购日。" />
        {pools?.ranking_capped && (
          <Alert type="warning" showIcon style={{ marginBottom: 10 }}
            message={`池数量超过分析上限，已退回「按成员数排序」——当前非按节省金额全局排名。`} />
        )}
        {pools?.ranking_restricted && (
          <Alert type="info" showIcon style={{ marginBottom: 10 }}
            message="无成本权限，当前非按节省金额排序。" />
        )}
        <Table<PoolListItem> size="small" rowKey="group_id" loading={poolLoading}
          dataSource={pools?.items || []} columns={poolCols} scroll={{ x: 800 }}
          onChange={(pag) => setPoolPage((q) => ({ page: pag.current || 1, page_size: pag.pageSize || q.page_size }))}
          pagination={{ current: pools?.page ?? 1, pageSize: poolPage.page_size, total: pools?.total ?? 0,
            showSizeChanger: true, pageSizeOptions: [10, 20, 50], showTotal: (t) => `共 ${t} 个池` }} />
      </Card>

      <Drawer width={isMobile ? "100%" : 640} open={poolDetail != null} onClose={() => setPoolDetail(null)}
        title={poolDetail ? `通用号池 #${poolDetail.group_id}（${poolDetail.member_count} 个型号）` : ""}>
        {poolDetail && <PoolDetail d={poolDetail} />}
      </Drawer>
    </>
  );
}

function PoolDetail({ d }: { d: PoolDetailT }) {
  return (
    <>
      {(d.needs_calibration || d.oversized) && (
        <div style={{ marginBottom: 10 }}>
          {d.needs_calibration && <Tag color="orange">关系待校准（有边缺替代类型）</Tag>}
          {d.oversized && <Tag color="red">成员超限，需人工确认</Tag>}
        </div>
      )}
      <div style={{ marginBottom: 12, fontSize: 13 }}>
        <b>池总需求（跨品牌）</b>：{num(d.demand.total_qty)} 件 · {money(d.demand.total_revenue_ex_tax)}
        <div style={{ fontSize: 11.5, color: "var(--mb-text-3)" }}>{d.demand.note}</div>
      </div>
      <div style={{ marginBottom: 12, fontSize: 11.5, color: "var(--mb-text-3)" }}>
        供应证据窗口：{d.supply_window.date_from} 至 {d.supply_window.date_to}（as of {d.supply_window.as_of}）；销售需求按看板时间范围统计
      </div>
      {/* benchmark/savings 属成本组，无权限时被脱敏为 null → 显示占位而非崩溃 */}
      {d.savings ? (
        <div style={{ marginBottom: 12 }}>
          <b>降本机会（只读）</b>：理论上限 <span style={{ color: "#9a7b43" }}>{money(d.savings.theoretical_max)}</span> ·
          供应层面上限 {money(d.savings.supply_available_upper)} · <Tag color="orange">无可执行金额</Tag>
          <div style={{ fontSize: 11.5, color: "var(--mb-text-3)" }}>{d.savings.label}</div>
        </div>
      ) : (
        <div style={{ marginBottom: 12, color: "var(--mb-text-3)", fontSize: 12.5 }}>降本金额按权限不可见</div>
      )}
      <Table<PoolMemberRow> size="small" rowKey="part_id" pagination={false} dataSource={d.members}
        columns={[
          { title: "型号", dataIndex: "pn_std", render: (v, r) => (
            <span>{v} {r.brand && <Tag>{r.brand}</Tag>}
              {d.benchmark && r.part_id === d.benchmark.cost_part_id && <Tag color="green">性价比标杆</Tag>}</span>) },
          { title: "采购均价", key: "pw", align: "right", render: (_, r) => money(r.purchase_price?.wavg) },
          { title: "采购溢价", dataIndex: "purchase_premium_pct", align: "right",
            render: (v: number | null, r) => v == null ? "—" :
              <span style={{ color: r.brand_premium_purchase ? "#c0524a" : undefined }}>{pct(v)}</span> },
          { title: "销售均价", key: "sw", align: "right", render: (_, r) => money(r.sale_price?.wavg) },
          { title: "销量", key: "q", align: "right", render: (_, r) => num(r.sale_price?.qty_sold) },
          { title: "供应", key: "sup", render: (_, r) => r.purchase_price?.supply ?
            `${r.purchase_price.supply.purchase_orders}次/${r.purchase_price.supply.suppliers}商` : "—" },
        ]} />
      {d.savings && d.savings.opportunities.length > 0 && (
        <>
          <div style={{ fontWeight: 500, margin: "14px 0 6px" }}>降本机会明细</div>
          <Table<PoolOpportunity> size="small" rowKey={(r) => r.from_part_id} pagination={false}
            dataSource={d.savings.opportunities}
            columns={[
              { title: "高价型号", dataIndex: "from_pn" },
              { title: "→ 标杆", dataIndex: "to_pn" },
              { title: "单件省", dataIndex: "unit_saving", align: "right", render: money },
              { title: "销量", dataIndex: "qty_sold", align: "right", render: num },
              { title: "理论节省", dataIndex: "theoretical_saving", align: "right", render: money },
              { title: "供应", dataIndex: "supply_available", align: "center",
                render: (v: boolean, r) => v ? <Tag color="blue">可得</Tag> :
                  <Tooltip title={r.block_reason || undefined}><Tag>不稳</Tag></Tooltip> },
              { title: "核实状态", dataIndex: "verification_status", align: "center",
                render: (v: string) => <Tag color="orange">{v}</Tag> },
            ]} />
        </>
      )}
      {d.customer_cross_brand && !d.customer_cross_brand.restricted && (
        <>
          <div style={{ fontWeight: 500, margin: "14px 0 6px" }}>
            客户跨品牌（{d.customer_cross_brand.multi_brand_customers} 个客户买过≥2品牌）
          </div>
          <Table size="small" rowKey="customer" pagination={false}
            dataSource={d.customer_cross_brand.customers}
            columns={[
              { title: "客户", dataIndex: "customer", ellipsis: true },
              { title: "品牌数", dataIndex: "brand_count", align: "right" },
              { title: "集中度", dataIndex: "concentration", align: "right", render: pct },
            ]} />
        </>
      )}
    </>
  );
}
