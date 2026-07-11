import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Alert, Button, Card, DatePicker, Drawer, Grid, Segmented, Table, Tag, Tooltip,
  message, theme,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs, { type Dayjs } from "dayjs";
import PageHeader from "../components/PageHeader";
import {
  dashboardKpi, dashboardTrend, dashboardPartRanking, dashboardSales,
  dashboardPools, dashboardPool, dashboardPoolRebuild,
  type DashboardKpi, type TrendPoint,
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

export default function BossBoardPage() {
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false;
  const { token } = theme.useToken();
  const isAdmin = (localStorage.getItem("role") || "") === "admin";
  const { key, setKey, setCustom, range } = useRange();
  const [granularity, setGranularity] = useState("day");
  const [costMethod, setCostMethod] = useState<"moving_avg" | "fifo">("moving_avg");

  const [kpi, setKpi] = useState<DashboardKpi | null>(null);
  const [trend, setTrend] = useState<TrendPoint[]>([]);
  const [ranking, setRanking] = useState<any>(null);
  const [sales, setSales] = useState<any>(null);
  const [pools, setPools] = useState<any>(null);
  const [poolDetail, setPoolDetail] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [k, t, r, s, p] = await Promise.all([
        dashboardKpi(range), dashboardTrend({ ...range, granularity }),
        dashboardPartRanking({ ...range, cost_method: costMethod, top: 10 }),
        dashboardSales({ ...range, page_size: 20, sort: "gross_profit", order: "asc" }),
        dashboardPools(range),
      ]);
      setKpi(k.data); setTrend(t.data.series); setRanking(r.data); setSales(s.data); setPools(p.data);
    } catch {
      message.error("看板加载失败");
    } finally { setLoading(false); }
  }, [range, granularity, costMethod]);

  useEffect(() => { load(); }, [load]);

  const openPool = async (gid: number) => {
    try { const { data } = await dashboardPool(gid, range); setPoolDetail(data); }
    catch { message.error("池详情加载失败"); }
  };

  const rebuildPools = async () => {
    try {
      const { data } = await dashboardPoolRebuild(true);
      message.info(`预览：${data.pools} 个池，合并 ${data.merged.length}、拆分 ${data.split.length}、新建 ${data.new.length}`);
    } catch { message.error("重算预览失败"); }
  };

  const rankCols = (loss: boolean): ColumnsType<any> => [
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

  const salesCols: ColumnsType<any> = [
    { title: "日期", dataIndex: "order_date", width: 100, render: (v, r) => (
      <span>{v || "—"}{r.is_future && <Tag color="red" style={{ marginLeft: 4 }}>未来</Tag>}</span>) },
    { title: "单号", dataIndex: "order_no", width: 130, render: (v) => <span style={{ fontFamily: "monospace", fontSize: 12 }}>{v}</span> },
    { title: "型号", dataIndex: "pn_std", width: 140 },
    { title: "客户", dataIndex: "customer", width: 120, ellipsis: true },
    { title: "销售员", dataIndex: "salesperson", width: 80 },
    { title: "数量", dataIndex: "qty", width: 64, align: "right", render: num },
    { title: "单价(未税)", dataIndex: "unit_price_ex_tax", width: 100, align: "right", render: money },
    { title: "营收", dataIndex: "revenue_amount", width: 100, align: "right", render: money },
    { title: "毛利", dataIndex: "gross_profit", width: 100, align: "right",
      render: (v) => v == null ? <Tag>无成本</Tag> : <span style={{ color: v < 0 ? "#c0524a" : undefined }}>{money(v)}</span> },
    { title: "状态", dataIndex: "data_status", width: 84, render: (v) => v ? <Tag>{v}</Tag> : "—" },
    { title: "采购拉通", dataIndex: "linked_purchase", width: 84, align: "center",
      render: (v) => v ? <Tag color="blue">有</Tag> : <span style={{ color: "var(--mb-text-3)" }}>—</span> },
  ];

  const poolCols: ColumnsType<any> = [
    { title: "池号", dataIndex: "group_id", width: 70 },
    { title: "成员", dataIndex: "member_count", width: 70, align: "right" },
    { title: "池需求量", dataIndex: "demand_qty", width: 90, align: "right", render: num },
    { title: "池营收(未税)", dataIndex: "demand_revenue_ex_tax", width: 120, align: "right", render: money },
    { title: "理论节省", dataIndex: "theoretical_saving", width: 110, align: "right",
      render: (v) => <span style={{ color: "#9a7b43" }}>{money(v)}</span> },
    { title: "可执行", dataIndex: "executable_saving", width: 100, align: "right", render: money },
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
      </Card>

      <Card title="订单拉通 · 销售（按毛利升序，亏损在前）" style={{ marginBottom: 16 }} size="small"
        extra={<span style={{ fontSize: 12, color: "var(--mb-text-3)" }}>采购侧见「采购明细」页</span>}>
        <Table size="small" rowKey="line_id" loading={loading}
          dataSource={sales?.items || []} columns={salesCols} scroll={{ x: 1100 }}
          pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 行` }} />
      </Card>

      <Card title="通用号数据池 · 潜在降本机会" size="small"
        extra={isAdmin && <Button size="small" onClick={rebuildPools}>重算预览</Button>}>
        <Alert type="info" showIcon style={{ marginBottom: 10 }}
          message="只读分析：替换需人工确认兼容性 / 客户指定品牌 / 合同限制。库存 8 月盘点前不作推荐条件，供应稳定性看采购频次/供应商数/最近采购日。" />
        <Table size="small" rowKey="group_id" loading={loading}
          dataSource={pools?.items || []} columns={poolCols} scroll={{ x: 800 }}
          pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 个池` }} />
      </Card>

      <Drawer width={isMobile ? "100%" : 640} open={poolDetail != null} onClose={() => setPoolDetail(null)}
        title={poolDetail ? `通用号池 #${poolDetail.group_id}（${poolDetail.member_count} 个型号）` : ""}>
        {poolDetail && <PoolDetail d={poolDetail} accent={token.colorPrimary} />}
      </Drawer>
    </>
  );
}

function PoolDetail({ d, accent }: { d: any; accent: string }) {
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
      <div style={{ marginBottom: 12 }}>
        <b>降本机会</b>：理论上限 <span style={{ color: "#9a7b43" }}>{money(d.savings.theoretical_max)}</span> ·
        可执行 {money(d.savings.executable)}
        <div style={{ fontSize: 11.5, color: "var(--mb-text-3)" }}>{d.savings.label}</div>
      </div>
      <Table size="small" rowKey="part_id" pagination={false} dataSource={d.members}
        columns={[
          { title: "型号", dataIndex: "pn_std", render: (v, r: any) => (
            <span>{v} {r.brand && <Tag>{r.brand}</Tag>}
              {r.part_id === d.benchmark.cost_part_id && <Tag color="green">性价比标杆</Tag>}</span>) },
          { title: "采购均价", key: "pw", align: "right", render: (_, r: any) => money(r.purchase?.wavg) },
          { title: "采购溢价", dataIndex: "purchase_premium_pct", align: "right",
            render: (v: number, r: any) => v == null ? "—" :
              <span style={{ color: r.brand_premium_purchase ? "#c0524a" : undefined }}>{pct(v)}</span> },
          { title: "销售均价", key: "sw", align: "right", render: (_, r: any) => money(r.sale?.wavg) },
          { title: "销量", key: "q", align: "right", render: (_, r: any) => num(r.sale?.qty_sold) },
          { title: "供应", key: "sup", render: (_, r: any) => r.purchase?.supply ?
            `${r.purchase.supply.purchase_orders}次/${r.purchase.supply.suppliers}商` : "—" },
        ]} />
      {d.savings.opportunities?.length > 0 && (
        <>
          <div style={{ fontWeight: 500, margin: "14px 0 6px" }}>降本机会明细</div>
          <Table size="small" rowKey={(r: any) => r.from_part_id} pagination={false}
            dataSource={d.savings.opportunities}
            columns={[
              { title: "高价型号", dataIndex: "from_pn" },
              { title: "→ 标杆", dataIndex: "to_pn" },
              { title: "单件省", dataIndex: "unit_saving", align: "right", render: money },
              { title: "销量", dataIndex: "qty_sold", align: "right", render: num },
              { title: "理论节省", dataIndex: "theoretical_saving", align: "right", render: money },
              { title: "可执行", dataIndex: "executable", align: "center",
                render: (v: boolean, r: any) => v ? <Tag color="green">是</Tag> :
                  <Tooltip title={r.block_reason}><Tag>否</Tag></Tooltip> },
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
