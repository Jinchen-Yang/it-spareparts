/**
 * 池分析详情页（独立深链 /pool-analysis/:groupId，可刷新/前进后退）：
 * 池信息 + 人工约束价 → 成员 PN 表 → 采购/销售横向柱状排名（平均/合计切换，高到低）
 * → 采购订单板块 + 销售订单板块（点单号看订单内容）。
 * 时间窗口 from/to 写入 URL；订单板块分页也入 URL（刷新不丢位置）。
 */
import { useMemo, useState, type ReactNode } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  Alert, Button, Card, DatePicker, Descriptions, Grid, List, Result, Segmented, Table, Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs, { type Dayjs } from "dayjs";
import PageHeader from "../components/PageHeader";
import { fetchPoolAnalysis } from "../api/poolAnalysis";
import type {
  PoolAnalysisDetail, PoolAnalysisMember, PoolAnalysisOrderLine, PoolAnalysisPurchaseOrderLine,
  PoolAnalysisRange, PoolAnalysisSaleOrderLine, PoolReferenceSide,
} from "../api/poolAnalysis";
import HorizontalMetricBar, { type MetricBarItem } from "../components/charts/HorizontalMetricBar";
import MobileDetailDrawer, { type DetailField } from "../components/MobileDetailDrawer";
import PoolOrderDetailModal from "../components/pools/PoolOrderDetailModal";
import { EMPTY, moneyExact, qty } from "../utils/format";
import { ISO_DATE_FORMAT, strictIsoDateRange } from "../utils/date";
import { canOpenPartDetail, PartLink } from "./boss/PartsTable";
import { MUTED, useGuardedFetch, useLocalRestrictions } from "./boss/shared";
import { activatableProps } from "./purchases/shared";

const { RangePicker } = DatePicker;
const D = ISO_DATE_FORMAT;
const STANDARD_RANGES = new Set<PoolAnalysisRange>(["30d", "90d", "365d", "all"]);

type MetricMode = "average" | "total";

const RANGE_PRESETS = [
  { label: "近30天", value: [dayjs().subtract(29, "day"), dayjs()] as [Dayjs, Dayjs] },
  { label: "近90天", value: [dayjs().subtract(89, "day"), dayjs()] as [Dayjs, Dayjs] },
  { label: "本月", value: [dayjs().startOf("month"), dayjs()] as [Dayjs, Dayjs] },
  { label: "今年", value: [dayjs().startOf("year"), dayjs()] as [Dayjs, Dayjs] },
];

function readPage(sp: URLSearchParams, key: string): number {
  const n = Number(sp.get(key));
  return Number.isInteger(n) && n > 0 ? n : 1;
}

/** 任一治理哨兵收紧即整侧收紧；防御后端短暂不一致，绝不选择性信任金额。 */
function isReferenceRestricted(reference: PoolReferenceSide | null | undefined, localRestricted: boolean) {
  return localRestricted || !!reference
    && (reference.restricted || reference.constraint.status === "restricted");
}

export default function PoolAnalysisPage() {
  const { groupId: rawId } = useParams<{ groupId: string }>();
  const groupId = Number(rawId);
  const navigate = useNavigate();
  const [sp, setSp] = useSearchParams();
  const local = useLocalRestrictions();
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false;

  const parsedWindow = strictIsoDateRange(sp.get("from"), sp.get("to"));
  const rawRange = sp.get("range");
  const rawSide = sp.get("side");
  const focusSide = rawSide === "sales" ? "sales" : "purchase";
  const sideParam: "purchase" | "sales" | null = rawSide === "purchase" || rawSide === "sales"
    ? rawSide : null;
  const focusPn = sp.get("pn")?.trim() || null;
  const hasCustomWindow = sp.has("from") || sp.has("to");
  const range = !parsedWindow
    ? (rawRange == null ? "90d" : STANDARD_RANGES.has(rawRange as PoolAnalysisRange)
      ? rawRange as PoolAnalysisRange : null)
    : null;
  const invalidWindow = (hasCustomWindow && !parsedWindow) || (!parsedWindow && range == null);
  // 池统计只接受完整闭区间；坏日期或半开窗口都不下发，避免后端 422。
  const from = parsedWindow?.from ?? null;
  const to = parsedWindow?.to ?? null;
  const purchasePage = readPage(sp, "pp");
  const salesPage = readPage(sp, "spg");
  const [pMode, setPMode] = useState<MetricMode>("average");
  const [sMode, setSMode] = useState<MetricMode>("average");
  const [orderModal, setOrderModal] = useState<{ side: "purchase" | "sales"; orderId: number } | null>(null);
  const [memberDetail, setMemberDetail] = useState<PoolAnalysisMember | null>(null);

  const patch = (next: Record<string, string | number | null>, replace = true) => {
    const merged = new URLSearchParams(sp);
    for (const [k, v] of Object.entries(next)) {
      if (v === null || v === "") merged.delete(k);
      else merged.set(k, String(v));
    }
    setSp(merged, { replace });
  };

  const params = useMemo(() => ({
    ...(from && to
      ? { date_from: from, date_to: to }
      : { range: range ?? undefined }),
    ...(sideParam ? { side: sideParam } : {}),
    ...(focusPn ? { pn: focusPn } : {}),
    purchase_page: purchasePage, sales_page: salesPage, orders_page_size: 20,
  }), [from, to, range, sideParam, focusPn, purchasePage, salesPage]);

  const backQuery = new URLSearchParams();
  if (from && to) {
    backQuery.set("range", "custom");
    backQuery.set("from", from);
    backQuery.set("to", to);
  } else if (range && range !== "90d") {
    backQuery.set("range", range);
  }
  if (sideParam) backQuery.set("side", sideParam);
  if (focusPn) backQuery.set("pn", focusPn);
  const backPath = `/pools${backQuery.size ? `?${backQuery.toString()}` : ""}`;

  const validId = Number.isInteger(groupId) && groupId > 0;
  const { data: d, loading, error, reload } = useGuardedFetch<PoolAnalysisDetail>(
    () => (validId && !invalidWindow ? fetchPoolAnalysis(groupId, params).then((data) => ({ data }))
      : Promise.resolve({ data: null as unknown as PoolAnalysisDetail })),   // 非法编号：走 404 空态，不发请求
    [groupId, params, validId, invalidWindow]);

  const sideReference = (side: "purchase" | "sales") => side === "purchase"
    ? d?.purchase_reference : d?.sales_reference;
  const sideRestricted = (side: "purchase" | "sales") =>
    isReferenceRestricted(sideReference(side), local.governance);
  const memberRestricted = (member: PoolAnalysisMember, side: "purchase" | "sales") =>
    isReferenceRestricted(side === "purchase" ? member.purchase_reference : member.sales_reference,
      local.governance);

  // ---- 横向柱状排名数据（组件内自动降序 + 剔除无值项并计数）----
  const barItems = (side: "purchase" | "sales", mode: MetricMode): MetricBarItem[] =>
    (d?.members ?? []).map((m) => {
      const reference = side === "purchase" ? m.purchase_reference : m.sales_reference;
      const metrics = reference.part_stats;
      const poolMetrics = reference.pool_stats;
      const restricted = isReferenceRestricted(reference, local.governance);
      return {
        part_id: m.part_id,
        pn: m.pn_std ?? `#${m.part_id}`,
        description: m.description,
        qty: restricted ? null : metrics?.total_qty ?? null,
        order_count: restricted ? null : metrics?.order_count ?? null,
        last_date: restricted ? null : metrics?.latest_date ?? null,
        value: restricted ? null : (mode === "total" ? metrics?.total_amount : metrics?.weighted_avg) ?? null,
        pool_avg: !restricted && mode === "average" ? (poolMetrics?.weighted_avg ?? null) : null,
        constraint_price: !restricted && mode === "average"
          ? (reference.constraint.value ?? null) : null,
      };
    });

  const openPart = canOpenPartDetail()
    ? (partId: number) => navigate(`/parts?part_id=${partId}`)
    : undefined;

  // ---- 成员表 ----
  const focusedMembers = useMemo(() => {
    const members = [...(d?.members ?? [])];
    if (!focusPn) return members;
    const target = focusPn.toLocaleUpperCase();
    return members.sort((a, b) => Number((b.pn_std ?? "").toLocaleUpperCase() === target)
      - Number((a.pn_std ?? "").toLocaleUpperCase() === target));
  }, [d?.members, focusPn]);

  const signedMoney = (value: number | null | undefined) => value == null
    ? null : `${value > 0 ? "+" : value < 0 ? "−" : ""}${moneyExact(Math.abs(value))}`;

  const memberCols: ColumnsType<PoolAnalysisMember> = [
    { title: "型号", key: "pn", width: 190, render: (_, m) => (
      <span>
        <PartLink partId={m.part_id} pn={m.pn_std} />
        {m.brand && <Tag style={{ marginLeft: 6 }}>{m.brand}</Tag>}
        {focusPn && m.pn_std?.toLocaleUpperCase() === focusPn.toLocaleUpperCase()
          && <Tag color="processing">当前型号</Tag>}
      </span>) },
    { title: "描述", dataIndex: "description", width: 180, ellipsis: true,
      render: (v) => v || <span style={MUTED}>{EMPTY}</span> },
    { title: "采购均价(窗口)", key: "pavg", width: 118, align: "right",
      render: (_, m) => {
        const v = m.purchase_reference.part_stats?.weighted_avg;
        if (memberRestricted(m, "purchase")) return <span style={MUTED}>无池价格权限</span>;
        return v == null ? <span style={MUTED}>{EMPTY}</span> : moneyExact(v);
      } },
    { title: "采购量", key: "pq", width: 82, align: "right",
      render: (_, m) => memberRestricted(m, "purchase")
        ? <span style={MUTED}>无池价格权限</span>
        : qty(m.purchase_reference.part_stats?.total_qty) },
    { title: "采购 vs 池均", key: "pd", width: 106, align: "right",
      render: (_, m) => {
        const v = m.purchase_reference.delta_to_pool_avg;
        if (memberRestricted(m, "purchase")) return <span style={MUTED}>无池价格权限</span>;
        return v == null ? <span style={MUTED}>{EMPTY}</span>
          : <span style={{ color: v > 0 ? "#c0524a" : undefined }}>{signedMoney(v)}</span>;
      } },
    { title: "销售均价(窗口)", key: "savg", width: 118, align: "right",
      render: (_, m) => {
        const v = m.sales_reference.part_stats?.weighted_avg;
        if (memberRestricted(m, "sales")) return <span style={MUTED}>无池价格权限</span>;
        return v == null ? <span style={MUTED}>{EMPTY}</span> : moneyExact(v);
      } },
    { title: "销量", key: "sq", width: 78, align: "right",
      render: (_, m) => memberRestricted(m, "sales")
        ? <span style={MUTED}>无池价格权限</span>
        : qty(m.sales_reference.part_stats?.total_qty) },
    { title: "销售 vs 池均", key: "sd", width: 106, align: "right",
      render: (_, m) => {
        const v = m.sales_reference.delta_to_pool_avg;
        if (memberRestricted(m, "sales")) return <span style={MUTED}>无池价格权限</span>;
        return v == null ? <span style={MUTED}>{EMPTY}</span>
          : <span style={{ color: v < 0 ? "#c0524a" : undefined }}>{signedMoney(v)}</span>;
      } },
  ];

  const memberFields = (member: PoolAnalysisMember): DetailField[] => {
    const purchase = member.purchase_reference;
    const sales = member.sales_reference;
    const unavailable = <span style={MUTED}>无池价格权限</span>;
    const value = (side: "purchase" | "sales", content: ReactNode) =>
      memberRestricted(member, side) ? unavailable : content;
    const constraint = (side: "purchase" | "sales", reference: PoolReferenceSide) => value(side,
      reference.constraint.status === "unset"
        ? <span style={MUTED}>未设置</span>
        : moneyExact(reference.constraint.value));
    return [
      { label: "型号", value: <PartLink partId={member.part_id} pn={member.pn_std} /> },
      { label: "品牌", value: member.brand || EMPTY },
      { label: "描述", value: member.description || EMPTY },
      { label: "采购均价", value: value("purchase", moneyExact(purchase.part_stats?.weighted_avg)) },
      { label: "采购量", value: value("purchase", qty(purchase.part_stats?.total_qty)) },
      { label: "采购 vs 池均", value: value("purchase", signedMoney(purchase.delta_to_pool_avg) || EMPTY) },
      { label: "采购池约束", value: constraint("purchase", purchase) },
      { label: "销售均价", value: value("sales", moneyExact(sales.part_stats?.weighted_avg)) },
      { label: "销售量", value: value("sales", qty(sales.part_stats?.total_qty)) },
      { label: "销售 vs 池均", value: value("sales", signedMoney(sales.delta_to_pool_avg) || EMPTY) },
      { label: "销售池约束", value: constraint("sales", sales) },
    ];
  };

  const mobileMemberSummary = (member: PoolAnalysisMember, side: "purchase" | "sales") => {
    const reference = side === "purchase" ? member.purchase_reference : member.sales_reference;
    if (memberRestricted(member, side)) return <span style={MUTED}>无池价格权限</span>;
    return <>{side === "purchase" ? "采购" : "销售"}均价 {moneyExact(reference.part_stats?.weighted_avg)} ·
      数量 {qty(reference.part_stats?.total_qty)}</>;
  };

  // ---- 订单板块（行粒度，点单号看订单全貌）----
  const orderCols = (side: "purchase" | "sales"): ColumnsType<PoolAnalysisOrderLine> => [
    { title: "日期", dataIndex: "order_date", width: 104, render: (v) => v || EMPTY },
    { title: "单号", dataIndex: "order_no", width: 140, render: (v, row) => (
      <Button type="link" size="small" onClick={() => setOrderModal({ side, orderId: row.order_id })}
        style={{ padding: 0, height: "auto", fontFamily: "monospace", fontSize: 12 }}
        aria-label={`查看订单 ${v} 内容`}>{v}</Button>) },
    ...(side === "purchase" ? [
      { title: "采购员", dataIndex: "purchaser", width: 84,
        render: (v: string | null) => v || <span style={MUTED}>{EMPTY}</span> },
      { title: "供应商", dataIndex: "supplier", width: 140, ellipsis: true,
        render: (v: string | null) => local.supplier
          ? <span style={MUTED}>无供应商权限</span>
          : v || <span style={MUTED}>{EMPTY}</span> },
      { title: "类型", dataIndex: "source_type", width: 92,
        render: (v: string | null) => (v ? <Tag>{v}</Tag> : EMPTY) },
    ] : [
      { title: "销售员", dataIndex: "salesperson", width: 84,
        render: (v: string | null) => v || <span style={MUTED}>{EMPTY}</span> },
      { title: "客户", dataIndex: "customer", width: 140, ellipsis: true,
        render: (v: string | null) => (local.customer ? <span style={MUTED}>无客户权限</span>
          : v || <span style={MUTED}>{EMPTY}</span>) },
      { title: "业务类型", dataIndex: "business_type", width: 92,
        render: (v: string | null) => (v ? <Tag>{v}</Tag> : EMPTY) },
    ]),
    { title: "PN", key: "pn", width: 160, render: (_, r) => <PartLink partId={r.part_id} pn={r.pn_std} /> },
    { title: "数量", dataIndex: "quantity", width: 72, align: "right", render: qty },
    { title: "未税单价", key: "unit_price", width: 96, align: "right",
      render: (_, row) => {
        const value = side === "purchase"
          ? (row as PoolAnalysisPurchaseOrderLine).purchase_unit_price_ex_tax
          : (row as PoolAnalysisSaleOrderLine).sale_unit_price_ex_tax;
        return sideRestricted(side)
          ? <span style={MUTED}>无池价格权限</span>
          : value == null ? <span style={MUTED}>{EMPTY}</span> : moneyExact(value);
      } },
    { title: "金额(未税)", key: "line_value", width: 104, align: "right",
      render: (_, row) => {
        const value = side === "purchase"
          ? (row as PoolAnalysisPurchaseOrderLine).purchase_line_value_ex_tax
          : (row as PoolAnalysisSaleOrderLine).sale_line_value_ex_tax;
        return sideRestricted(side)
          ? <span style={MUTED}>无池价格权限</span>
          : value == null ? <span style={MUTED}>{EMPTY}</span> : moneyExact(value);
    } },
  ];

  const orderPagination = (side: "purchase" | "sales") => {
    const block = side === "purchase" ? d?.purchase_orders : d?.sales_orders;
    const current = side === "purchase" ? purchasePage : salesPage;
    return {
      current: block?.page ?? current,
      pageSize: 20,
      total: block?.total ?? 0,
      showSizeChanger: false,
      showTotal: (total: number) => `共 ${total} 行`,
      onChange: (page: number) => patch(side === "purchase"
        ? { pp: page === 1 ? null : page }
        : { spg: page === 1 ? null : page }),
    };
  };

  const mobileOrders = (side: "purchase" | "sales") => {
    const block = side === "purchase" ? d?.purchase_orders : d?.sales_orders;
    return (
      <List<PoolAnalysisOrderLine>
        loading={loading}
        dataSource={block?.items ?? []}
        pagination={orderPagination(side)}
        locale={{ emptyText: `窗口内暂无${side === "purchase" ? "采购" : "销售"}记录` }}
        renderItem={(row) => {
          const price = side === "purchase"
            ? (row as PoolAnalysisPurchaseOrderLine).purchase_unit_price_ex_tax
            : (row as PoolAnalysisSaleOrderLine).sale_unit_price_ex_tax;
          const employee = side === "purchase" ? row.purchaser : row.salesperson;
          return (
            <List.Item key={row.line_id}
              {...activatableProps(
                () => setOrderModal({ side, orderId: row.order_id }),
                `查看${side === "purchase" ? "采购" : "销售"}订单 ${row.order_no} 内容`,
              )}
              style={{ cursor: "pointer", paddingInline: 2 }}>
              <div style={{ width: "100%", minWidth: 0 }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                  <span style={{ fontFamily: "monospace", fontWeight: 600 }}>{row.order_no}</span>
                  <span style={MUTED}>{row.order_date || EMPTY}</span>
                </div>
                <div style={{ marginTop: 5, overflowWrap: "anywhere" }}>
                  <span style={{ fontFamily: "monospace" }}>{row.pn_std || `#${row.part_id}`}</span>
                  {employee && <Tag style={{ marginInlineStart: 6 }}>{employee}</Tag>}
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8, marginTop: 5, fontSize: 13 }}>
                  <span>数量 {qty(row.quantity)}</span>
                  {sideRestricted(side)
                    ? <span style={MUTED}>无池价格权限</span>
                    : <span>未税单价 {moneyExact(price)}</span>}
                </div>
              </div>
            </List.Item>
          );
        }}
      />
    );
  };

  if (!validId) {
    return <Result status="404" title="无效的池编号" subTitle="请从看板互通池列表进入。"
      extra={<Button onClick={() => navigate(backPath)}>返回互通池</Button>} />;
  }
  if (invalidWindow) {
    return <Result status="warning" title="无效的统计时间范围"
      subTitle="起止日期必须是真实日期，且开始日期不能晚于结束日期。为避免扩大成全历史，本页未发起查询。"
      extra={<Button onClick={() => navigate(backPath)}>返回互通池</Button>} />;
  }

  const windowNote = d?.window
    ? `统计窗口：${d.window.date_from ?? "全部历史"} ~ ${d.window.date_to ?? d.window.as_of}（as of ${d.window.as_of}）`
    : "";

  return (
    <div data-testid="pool-analysis-page"
      style={{ width: "100%", minWidth: 0, maxWidth: "100%", overflowX: "hidden" }}>
      <PageHeader
        title={d ? `${d.name || `通用号池 #${groupId}`}` : `通用号池 #${groupId}`}
        subtitle="池分析详情（只读）：成员排名 · 约束价参考 · 订单明细（金额均未税）"
        extra={
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap",
            width: isMobile ? "100%" : undefined, minWidth: 0 }}>
            <Segmented
              size="small"
              aria-label="当前关注方向"
              value={focusSide}
              options={[{ label: "采购", value: "purchase" }, { label: "销售", value: "sales" }]}
              onChange={(value) => patch({ side: String(value) }, false)}
            />
            <RangePicker size="small" allowClear presets={RANGE_PRESETS}
              style={{ width: isMobile ? "100%" : undefined, maxWidth: "100%" }}
              value={from && to ? [dayjs(from), dayjs(to)] : null}
              disabledDate={(day) => day.isAfter(dayjs(), "day")}
              onChange={(v) => {
                if (v && v[0] && v[1]) patch({ range: "custom", from: v[0].format(D), to: v[1].format(D), pp: null, spg: null }, false);
                else patch({ range: null, from: null, to: null, pp: null, spg: null }, false);
              }} />
            <Button size="small" onClick={() => navigate(backPath)}>返回互通池</Button>
          </div>
        } />

      {error ? (
        <Alert type="error" showIcon message={`池详情加载失败：${error}`}
          action={<Button size="small" onClick={reload}>重试</Button>} />
      ) : (
        <>
          {/* 池信息 + 人工约束价 */}
          <Card size="small" style={{ marginBottom: 16 }} loading={loading && !d}>
            {d && (
              <>
                <div style={{ marginBottom: 8 }}>
                  {d.needs_calibration && <Tag color="orange">关系待校准（有边缺替代类型）</Tag>}
                  {d.oversized && <Tag color="red">成员超限，需人工确认</Tag>}
                </div>
                <Descriptions size="small" column={{ xs: 1, sm: 2, md: 4 }}>
                  <Descriptions.Item label="池编号">#{d.group_id}</Descriptions.Item>
                  <Descriptions.Item label="成员数">{d.member_count}</Descriptions.Item>
                  <Descriptions.Item label="人工最高采购价">
                    {sideRestricted("purchase") ? <span style={MUTED}>无池价格权限</span>
                      : d.purchase_reference.constraint.status === "unset"
                        ? <span style={MUTED}>未设置</span>
                        : moneyExact(d.purchase_reference.constraint.value)}
                  </Descriptions.Item>
                  <Descriptions.Item label="人工最低销售价">
                    {sideRestricted("sales") ? <span style={MUTED}>无池价格权限</span>
                      : d.sales_reference.constraint.status === "unset"
                        ? <span style={MUTED}>未设置</span>
                        : moneyExact(d.sales_reference.constraint.value)}
                  </Descriptions.Item>
                  <Descriptions.Item label="窗口采购">
                    {sideRestricted("purchase")
                      ? <span style={MUTED}>无池价格权限</span>
                      : <>{moneyExact(d.purchase_reference.pool_stats?.total_amount)} ·
                          {qty(d.purchase_reference.pool_stats?.total_qty)} 件 ·
                          {d.purchase_reference.pool_stats?.order_count ?? 0} 单</>}
                  </Descriptions.Item>
                  <Descriptions.Item label="窗口销售">
                    {sideRestricted("sales")
                      ? <span style={MUTED}>无池价格权限</span>
                      : <>{moneyExact(d.sales_reference.pool_stats?.total_amount)} ·
                          {qty(d.sales_reference.pool_stats?.total_qty)} 件 ·
                          {d.sales_reference.pool_stats?.order_count ?? 0} 单</>}
                  </Descriptions.Item>
                  <Descriptions.Item label="采购超限行">
                    {sideRestricted("purchase")
                      ? <span style={MUTED}>无池价格权限</span>
                      : d.purchase_reference.pool_stats?.violation_count == null
                        ? <span style={MUTED}>未设约束</span>
                        : d.purchase_reference.pool_stats.violation_count > 0
                          ? <Tag color="red">{d.purchase_reference.pool_stats.violation_count}</Tag> : "0"}
                  </Descriptions.Item>
                  <Descriptions.Item label="销售低限行">
                    {sideRestricted("sales")
                      ? <span style={MUTED}>无池价格权限</span>
                      : d.sales_reference.pool_stats?.violation_count == null
                        ? <span style={MUTED}>未设约束</span>
                        : d.sales_reference.pool_stats.violation_count > 0
                          ? <Tag color="red">{d.sales_reference.pool_stats.violation_count}</Tag> : "0"}
                  </Descriptions.Item>
                </Descriptions>
                {d.description && <div style={{ ...MUTED, marginTop: 6 }}>{d.description}</div>}
                <div style={{ ...MUTED, marginTop: 6 }}>{windowNote}</div>
              </>
            )}
          </Card>

          {/* 采购 / 销售 横向柱状排名 */}
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: 16 }}>
            <Card size="small" style={{ flex: "1 1 480px", minWidth: 320 }}
              title={<span>成员采购排名（高→低）{focusSide === "purchase" && <Tag color="processing">当前关注</Tag>}</span>}
              extra={<Segmented size="small" value={pMode} aria-label="采购排名指标：平均单价或金额合计"
                disabled={sideRestricted("purchase")}
                onChange={(v) => setPMode(v as MetricMode)}
                options={[{ label: "平均单价", value: "average" }, { label: "金额合计", value: "total" }]} />}>
              {sideRestricted("purchase") ? (
                <Alert type="info" showIcon message="无池价格权限：采购价格、差额与排序不可见。" />
              ) : (
                <HorizontalMetricBar items={barItems("purchase", pMode)} mode="purchase" metric={pMode}
                  loading={loading} onPartClick={openPart} />
              )}
            </Card>
            <Card size="small" style={{ flex: "1 1 480px", minWidth: 320 }}
              title={<span>成员销售排名（高→低）{focusSide === "sales" && <Tag color="processing">当前关注</Tag>}</span>}
              extra={<Segmented size="small" value={sMode} aria-label="销售排名指标：平均单价或金额合计"
                disabled={sideRestricted("sales")}
                onChange={(v) => setSMode(v as MetricMode)}
                options={[{ label: "平均单价", value: "average" }, { label: "金额合计", value: "total" }]} />}>
              {sideRestricted("sales") ? (
                <Alert type="info" showIcon message="无池价格权限：销售价格、差额与排序不可见。" />
              ) : (
                <HorizontalMetricBar items={barItems("sales", sMode)} mode="sales" metric={sMode}
                  loading={loading} onPartClick={openPart} />
              )}
            </Card>
          </div>

          {/* 成员 PN 表 */}
          <Card size="small" style={{ marginBottom: 16 }} title="成员型号（窗口指标）">
            {isMobile ? (
              <List<PoolAnalysisMember>
                loading={loading}
                dataSource={focusedMembers}
                locale={{ emptyText: "池内暂无成员" }}
                renderItem={(member) => (
                  <List.Item key={member.part_id}
                    {...activatableProps(
                      () => setMemberDetail(member),
                      `查看成员 ${member.pn_std || `#${member.part_id}`} 价格详情`,
                    )}
                    style={{ cursor: "pointer", paddingInline: 2 }}>
                    <div style={{ width: "100%", minWidth: 0 }}>
                      <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                        <span style={{ fontFamily: "monospace", fontWeight: 600, overflowWrap: "anywhere" }}>
                          {member.pn_std || `#${member.part_id}`}
                        </span>
                        <span>
                          {member.brand && <Tag>{member.brand}</Tag>}
                          {focusPn && member.pn_std?.toLocaleUpperCase() === focusPn.toLocaleUpperCase()
                            && <Tag color="processing">当前型号</Tag>}
                        </span>
                      </div>
                      {member.description && <div style={{ ...MUTED, marginTop: 4 }}>{member.description}</div>}
                      <div style={{ marginTop: 6, fontSize: 13 }}>{mobileMemberSummary(member, "purchase")}</div>
                      <div style={{ marginTop: 3, fontSize: 13 }}>{mobileMemberSummary(member, "sales")}</div>
                    </div>
                  </List.Item>
                )}
              />
            ) : (
              <Table<PoolAnalysisMember> size="small" rowKey="part_id" pagination={false}
                loading={loading} dataSource={focusedMembers} columns={memberCols} scroll={{ x: 980 }}
                locale={{ emptyText: "池内暂无成员" }} />
            )}
          </Card>

          {/* 采购订单板块 */}
          <Card size="small" style={{ marginBottom: 16 }} title="采购订单（池内成员，窗口内已生效）">
            {isMobile ? mobileOrders("purchase") : (
              <Table<PoolAnalysisOrderLine> size="small" rowKey="line_id" loading={loading}
                dataSource={d?.purchase_orders?.items ?? []} columns={orderCols("purchase")}
                scroll={{ x: 1000 }} locale={{ emptyText: "窗口内暂无采购记录" }}
                pagination={orderPagination("purchase")} />
            )}
          </Card>

          {/* 销售订单板块 */}
          <Card size="small" style={{ marginBottom: 16 }} title="销售订单（池内成员，窗口内已生效）">
            {d?.sales_orders?.restricted ? (
              <Alert type="info" showIcon message="当前账号无逐单销售明细查看权限（仅聚合可见）。" />
            ) : isMobile ? (
              mobileOrders("sales")
            ) : (
              <Table<PoolAnalysisOrderLine> size="small" rowKey="line_id" loading={loading}
                dataSource={d?.sales_orders?.items ?? []} columns={orderCols("sales")}
                scroll={{ x: 1000 }} locale={{ emptyText: "窗口内暂无销售记录" }}
                pagination={orderPagination("sales")} />
            )}
          </Card>

        </>
      )}

      <MobileDetailDrawer
        open={memberDetail != null}
        title={memberDetail ? `成员 ${memberDetail.pn_std || `#${memberDetail.part_id}`} 详情` : "成员详情"}
        fields={memberDetail ? memberFields(memberDetail) : []}
        height="100%"
        onClose={() => setMemberDetail(null)}
      />

      <PoolOrderDetailModal
        side={orderModal?.side ?? "purchase"}
        orderId={orderModal?.orderId ?? null}
        range={from && to ? "custom" : range ?? "90d"}
        dateFrom={from ?? undefined}
        dateTo={to ?? undefined}
        forcePriceRestricted={sideRestricted(orderModal?.side ?? "purchase")}
        onClose={() => setOrderModal(null)}
      />
    </div>
  );
}
