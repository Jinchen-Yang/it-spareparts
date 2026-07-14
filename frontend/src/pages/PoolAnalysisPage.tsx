/**
 * 池分析详情页（独立深链 /pool-analysis/:groupId，可刷新/前进后退）：
 * 池信息 + 人工约束价 → 成员 PN 表 → 采购/销售横向柱状排名（平均/合计切换，高到低）
 * → 采购订单板块 + 销售订单板块（点单号看订单内容）。
 * 时间窗口 from/to 写入 URL；订单板块分页也入 URL（刷新不丢位置）。
 */
import { useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  Alert, Button, Card, DatePicker, Descriptions, Result, Segmented, Table, Tag, Tooltip,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs, { type Dayjs } from "dayjs";
import PageHeader from "../components/PageHeader";
import {
  dashboardPool,
  type PoolDetail, type PoolMemberRow, type PoolOpportunity, type PoolOrderLine,
} from "../api";
import HorizontalMetricBar, { type MetricBarItem } from "../components/charts/HorizontalMetricBar";
import { EMPTY, moneyExact, pct, pctSigned, qty } from "../utils/format";
import { ISO_DATE_FORMAT, strictIsoDateRange } from "../utils/date";
import OrderDetailModal from "./boss/OrderDetailModal";
import { canOpenPartDetail, PartLink } from "./boss/PartsTable";
import { MUTED, useGuardedFetch, useLocalRestrictions } from "./boss/shared";

const { RangePicker } = DatePicker;
const D = ISO_DATE_FORMAT;

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

export default function PoolAnalysisPage() {
  const { groupId: rawId } = useParams<{ groupId: string }>();
  const groupId = Number(rawId);
  const navigate = useNavigate();
  const [sp, setSp] = useSearchParams();
  const local = useLocalRestrictions();

  const parsedWindow = strictIsoDateRange(sp.get("from"), sp.get("to"));
  const invalidWindow = (sp.has("from") || sp.has("to")) && !parsedWindow;
  // 池统计只接受完整闭区间；坏日期或半开窗口都不下发，避免后端 422。
  const from = parsedWindow?.from ?? null;
  const to = parsedWindow?.to ?? null;
  const purchasePage = readPage(sp, "pp");
  const salesPage = readPage(sp, "spg");
  const [pMode, setPMode] = useState<MetricMode>("average");
  const [sMode, setSMode] = useState<MetricMode>("average");
  const [orderModal, setOrderModal] = useState<{ side: "purchase" | "sales"; orderNo: string } | null>(null);

  const patch = (next: Record<string, string | number | null>, replace = true) => {
    const merged = new URLSearchParams(sp);
    for (const [k, v] of Object.entries(next)) {
      if (v === null || v === "") merged.delete(k);
      else merged.set(k, String(v));
    }
    setSp(merged, { replace });
  };

  const params = useMemo(() => ({
    date_from: from ?? undefined, date_to: to ?? undefined,
    purchase_page: purchasePage, sales_page: salesPage, orders_page_size: 20,
  }), [from, to, purchasePage, salesPage]);

  const validId = Number.isInteger(groupId) && groupId > 0;
  const { data: d, loading, error, reload } = useGuardedFetch<PoolDetail>(
    () => (validId && !invalidWindow ? dashboardPool(groupId, params)
      : Promise.resolve({ data: null as unknown as PoolDetail })),   // 非法编号：走 404 空态，不发请求
    [groupId, params, validId, invalidWindow]);

  const govRestricted = local.governance || (d?.manual_reference_restricted ?? false);

  // ---- 横向柱状排名数据（组件内自动降序 + 剔除无值项并计数）----
  const barItems = (side: "purchase" | "sales", mode: MetricMode): MetricBarItem[] =>
    (d?.members ?? []).map((m) => {
      const metrics = side === "purchase" ? m.purchase_metrics : m.sales_metrics;
      const poolMetrics = side === "purchase" ? d?.purchase_metrics : d?.sales_metrics;
      const limit = side === "purchase" ? d?.max_purchase_price : d?.min_sale_price;
      return {
        part_id: m.part_id,
        pn: m.pn_std ?? `#${m.part_id}`,
        description: m.description,
        qty: metrics?.total_quantity ?? null,
        order_count: metrics?.order_count ?? null,
        last_date: metrics?.latest_date ?? null,
        value: (mode === "total" ? metrics?.total_amount : metrics?.weighted_avg_unit_price) ?? null,
        pool_avg: mode === "average" ? (poolMetrics?.weighted_avg_unit_price ?? null) : null,
        constraint_price: mode === "average" && !govRestricted ? (limit ?? null) : null,
      };
    });

  const openPart = canOpenPartDetail()
    ? (partId: number) => navigate(`/parts?part_id=${partId}`)
    : undefined;

  // ---- 成员表 ----
  const memberCols: ColumnsType<PoolMemberRow> = [
    { title: "型号", key: "pn", width: 190, render: (_, m) => (
      <span>
        <PartLink partId={m.part_id} pn={m.pn_std} />
        {m.brand && <Tag style={{ marginLeft: 6 }}>{m.brand}</Tag>}
        {d?.benchmark && m.part_id === d.benchmark.cost_part_id && <Tag color="green">性价比标杆</Tag>}
      </span>) },
    { title: "描述", dataIndex: "description", width: 180, ellipsis: true,
      render: (v) => v || <span style={MUTED}>{EMPTY}</span> },
    { title: "采购均价(窗口)", key: "pavg", width: 118, align: "right",
      render: (_, m) => {
        const v = m.purchase_metrics?.weighted_avg_unit_price;
        if (v == null && local.cost) return <span style={MUTED}>无成本权限</span>;
        return v == null ? <span style={MUTED}>{EMPTY}</span> : moneyExact(v);
      } },
    { title: "采购量", key: "pq", width: 82, align: "right",
      render: (_, m) => qty(m.purchase_metrics?.total_quantity) },
    { title: "采购 vs 池均", key: "pd", width: 106, align: "right",
      render: (_, m) => {
        const v = m.purchase_metrics?.pool_avg_delta_pct;
        return v == null ? <span style={MUTED}>{EMPTY}</span>
          : <span style={{ color: v > 0 ? "#c0524a" : undefined }}>{pctSigned(v)}</span>;
      } },
    { title: "销售均价(窗口)", key: "savg", width: 118, align: "right",
      render: (_, m) => {
        const v = m.sales_metrics?.weighted_avg_unit_price;
        return v == null ? <span style={MUTED}>{EMPTY}</span> : moneyExact(v);
      } },
    { title: "销量", key: "sq", width: 78, align: "right",
      render: (_, m) => qty(m.sales_metrics?.total_quantity) },
    { title: "销售 vs 池均", key: "sd", width: 106, align: "right",
      render: (_, m) => {
        const v = m.sales_metrics?.pool_avg_delta_pct;
        return v == null ? <span style={MUTED}>{EMPTY}</span>
          : <span style={{ color: v < 0 ? "#c0524a" : undefined }}>{pctSigned(v)}</span>;
      } },
    { title: "溢价标记", key: "prem", width: 130, render: (_, m) => (
      <span>
        {m.brand_premium_purchase && <Tag color="orange">采购溢价</Tag>}
        {m.brand_premium_sale && <Tag color="blue">销售溢价</Tag>}
      </span>) },
    { title: "供应", key: "sup", width: 96, render: (_, m) => m.purchase_price?.supply
      ? `${m.purchase_price.supply.purchase_orders}次/${m.purchase_price.supply.suppliers}商`
      : <span style={MUTED}>{EMPTY}</span> },
  ];

  // ---- 订单板块（行粒度，点单号看订单全貌）----
  const orderCols = (side: "purchase" | "sales"): ColumnsType<PoolOrderLine> => [
    { title: "日期", dataIndex: "order_date", width: 104, render: (v) => v || EMPTY },
    { title: "单号", dataIndex: "order_no", width: 140, render: (v) => (
      <Button type="link" size="small" onClick={() => setOrderModal({ side, orderNo: v })}
        style={{ padding: 0, height: "auto", fontFamily: "monospace", fontSize: 12 }}
        aria-label={`查看订单 ${v} 内容`}>{v}</Button>) },
    ...(side === "purchase" ? [
      { title: "采购员", dataIndex: "purchaser", width: 84,
        render: (v: string | null) => v || <span style={MUTED}>{EMPTY}</span> },
      { title: "供应商", dataIndex: "supplier", width: 140, ellipsis: true,
        render: (v: string | null) => v || <span style={MUTED}>{EMPTY}</span> },
      { title: "类型", dataIndex: "source_type", width: 92,
        render: (v: string | null) => (v ? <Tag>{v}</Tag> : EMPTY) },
    ] : [
      { title: "销售员", dataIndex: "salesperson", width: 84,
        render: (v: string | null) => v || <span style={MUTED}>{EMPTY}</span> },
      { title: "客户", dataIndex: "customer", width: 140, ellipsis: true,
        render: (v: string | null) => (local.customer ? <span style={MUTED}>无权限</span>
          : v || <span style={MUTED}>{EMPTY}</span>) },
      { title: "业务类型", dataIndex: "business_type", width: 92,
        render: (v: string | null) => (v ? <Tag>{v}</Tag> : EMPTY) },
    ]),
    { title: "PN", key: "pn", width: 160, render: (_, r) => <PartLink partId={r.part_id} pn={r.pn_std} /> },
    { title: "数量", dataIndex: "quantity", width: 72, align: "right", render: qty },
    { title: "未税单价", dataIndex: "unit_price_ex_tax", width: 96, align: "right",
      render: (v) => (v == null && side === "purchase" && local.cost)
        ? <span style={MUTED}>无成本权限</span>
        : v == null ? <span style={MUTED}>{EMPTY}</span> : moneyExact(v) },
    { title: "金额(未税)", dataIndex: "amount", width: 104, align: "right",
      render: (v) => (v == null && side === "purchase" && local.cost)
        ? <span style={MUTED}>无成本权限</span>
        : v == null ? <span style={MUTED}>{EMPTY}</span> : moneyExact(v) },
  ];

  if (!validId) {
    return <Result status="404" title="无效的池编号" subTitle="请从看板互通池列表进入。"
      extra={<Button onClick={() => navigate("/boss")}>返回经营看板</Button>} />;
  }
  if (invalidWindow) {
    return <Result status="warning" title="无效的统计时间范围"
      subTitle="起止日期必须是真实日期，且开始日期不能晚于结束日期。为避免扩大成全历史，本页未发起查询。"
      extra={<Button onClick={() => navigate("/boss")}>返回经营看板</Button>} />;
  }

  const windowNote = d?.window
    ? `统计窗口：${d.window.date_from ?? "全部历史"} ~ ${d.window.date_to ?? d.window.as_of}（as of ${d.window.as_of}）`
    : "";

  return (
    <>
      <PageHeader
        title={d ? `${d.name || `通用号池 #${groupId}`}` : `通用号池 #${groupId}`}
        subtitle="池分析详情（只读）：成员排名 · 约束价参考 · 订单明细（金额均未税）"
        extra={
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <RangePicker size="small" allowClear presets={RANGE_PRESETS}
              value={from && to ? [dayjs(from), dayjs(to)] : null}
              disabledDate={(day) => day.isAfter(dayjs(), "day")}
              onChange={(v) => {
                if (v && v[0] && v[1]) patch({ from: v[0].format(D), to: v[1].format(D), pp: null, spg: null }, false);
                else patch({ from: null, to: null, pp: null, spg: null }, false);
              }} />
            <Button size="small" onClick={() => navigate("/boss")}>返回看板</Button>
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
                    {govRestricted ? <span style={MUTED}>无权限</span>
                      : d.max_purchase_price == null ? <span style={MUTED}>未设置</span>
                        : moneyExact(d.max_purchase_price)}
                  </Descriptions.Item>
                  <Descriptions.Item label="人工最低销售价">
                    {govRestricted ? <span style={MUTED}>无权限</span>
                      : d.min_sale_price == null ? <span style={MUTED}>未设置</span>
                        : moneyExact(d.min_sale_price)}
                  </Descriptions.Item>
                  <Descriptions.Item label="窗口采购">
                    {moneyExact(d.purchase_metrics?.total_amount)} · {qty(d.purchase_metrics?.total_quantity)} 件 ·
                    {d.purchase_metrics?.order_count ?? 0} 单
                  </Descriptions.Item>
                  <Descriptions.Item label="窗口销售">
                    {moneyExact(d.sales_metrics?.total_amount)} · {qty(d.sales_metrics?.total_quantity)} 件 ·
                    {d.sales_metrics?.order_count ?? 0} 单
                  </Descriptions.Item>
                  <Descriptions.Item label="采购超限行">
                    {d.purchase_violation_count == null
                      ? <span style={MUTED}>{govRestricted ? "无权限" : "未设约束"}</span>
                      : d.purchase_violation_count > 0
                        ? <Tag color="red">{d.purchase_violation_count}</Tag> : "0"}
                  </Descriptions.Item>
                  <Descriptions.Item label="销售低限行">
                    {d.sale_violation_count == null
                      ? <span style={MUTED}>{govRestricted ? "无权限" : "未设约束"}</span>
                      : d.sale_violation_count > 0
                        ? <Tag color="red">{d.sale_violation_count}</Tag> : "0"}
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
              title="成员采购排名（高→低）"
              extra={<Segmented size="small" value={pMode} aria-label="采购排名指标：平均单价或金额合计"
                onChange={(v) => setPMode(v as MetricMode)}
                options={[{ label: "平均单价", value: "average" }, { label: "金额合计", value: "total" }]} />}>
              {local.cost ? (
                <Alert type="info" showIcon message="无采购成本权限：采购金额排名对当前账号不可见。" />
              ) : (
                <HorizontalMetricBar items={barItems("purchase", pMode)} mode="purchase" metric={pMode}
                  loading={loading} onPartClick={openPart} />
              )}
            </Card>
            <Card size="small" style={{ flex: "1 1 480px", minWidth: 320 }}
              title="成员销售排名（高→低）"
              extra={<Segmented size="small" value={sMode} aria-label="销售排名指标：平均单价或金额合计"
                onChange={(v) => setSMode(v as MetricMode)}
                options={[{ label: "平均单价", value: "average" }, { label: "金额合计", value: "total" }]} />}>
              <HorizontalMetricBar items={barItems("sales", sMode)} mode="sales" metric={sMode}
                loading={loading} onPartClick={openPart} />
            </Card>
          </div>

          {/* 成员 PN 表 */}
          <Card size="small" style={{ marginBottom: 16 }} title="成员型号（窗口指标）">
            <Table<PoolMemberRow> size="small" rowKey="part_id" pagination={false}
              loading={loading} dataSource={d?.members ?? []} columns={memberCols} scroll={{ x: 1200 }}
              locale={{ emptyText: "池内暂无成员" }} />
          </Card>

          {/* 降本机会（沿用只读口径） */}
          {d?.savings ? (
            <Card size="small" style={{ marginBottom: 16 }} title="潜在降本机会（只读）">
              <div style={{ marginBottom: 8 }}>
                理论上限 <span style={{ color: "#9a7b43" }}>{moneyExact(d.savings.theoretical_max)}</span> ·
                供应层面上限 {moneyExact(d.savings.supply_available_upper)} · <Tag color="orange">无可执行金额</Tag>
                <div style={MUTED}>{d.savings.label}</div>
              </div>
              {(d.savings.opportunities ?? []).length > 0 && (
                <Table<PoolOpportunity> size="small" rowKey={(r) => r.from_part_id} pagination={false}
                  dataSource={d.savings.opportunities ?? []}
                  scroll={{ x: 720 }}
                  columns={[
                    { title: "高价型号", dataIndex: "from_pn" },
                    { title: "→ 标杆", dataIndex: "to_pn" },
                    { title: "单件省", dataIndex: "unit_saving", align: "right", render: (v) => moneyExact(v) },
                    { title: "销量", dataIndex: "qty_sold", align: "right", render: (v) => qty(v) },
                    { title: "理论节省", dataIndex: "theoretical_saving", align: "right", render: (v) => moneyExact(v) },
                    { title: "供应", dataIndex: "supply_available", align: "center",
                      render: (v: boolean, r) => v ? <Tag color="blue">可得</Tag>
                        : <Tooltip title={r.block_reason || undefined}><Tag>不稳</Tag></Tooltip> },
                    { title: "核实状态", dataIndex: "verification_status", align: "center",
                      render: (v: string) => <Tag color="orange">{v}</Tag> },
                  ]} />
              )}
            </Card>
          ) : (d && !loading && (
            <Card size="small" style={{ marginBottom: 16 }} title="潜在降本机会（只读）">
              <span style={MUTED}>降本金额按权限不可见</span>
            </Card>
          ))}

          {/* 采购订单板块 */}
          <Card size="small" style={{ marginBottom: 16 }} title="采购订单（池内成员，窗口内已生效）">
            <Table<PoolOrderLine> size="small" rowKey="line_id" loading={loading}
              dataSource={d?.purchase_orders?.items ?? []} columns={orderCols("purchase")}
              scroll={{ x: 1000 }} locale={{ emptyText: "窗口内暂无采购记录" }}
              pagination={{
                current: d?.purchase_orders?.page ?? purchasePage, pageSize: 20,
                total: d?.purchase_orders?.total ?? 0, showSizeChanger: false,
                showTotal: (t) => `共 ${t} 行`,
                onChange: (p) => patch({ pp: p === 1 ? null : p }),
              }} />
          </Card>

          {/* 销售订单板块 */}
          <Card size="small" style={{ marginBottom: 16 }} title="销售订单（池内成员，窗口内已生效）">
            {d?.sales_orders?.restricted ? (
              <Alert type="info" showIcon message="当前账号无逐单销售明细查看权限（仅聚合可见）。" />
            ) : (
              <Table<PoolOrderLine> size="small" rowKey="line_id" loading={loading}
                dataSource={d?.sales_orders?.items ?? []} columns={orderCols("sales")}
                scroll={{ x: 1000 }} locale={{ emptyText: "窗口内暂无销售记录" }}
                pagination={{
                  current: d?.sales_orders?.page ?? salesPage, pageSize: 20,
                  total: d?.sales_orders?.total ?? 0, showSizeChanger: false,
                  showTotal: (t) => `共 ${t} 行`,
                  onChange: (p) => patch({ spg: p === 1 ? null : p }),
                }} />
            )}
          </Card>

          {/* 客户跨品牌 */}
          {d?.customer_cross_brand && !d.customer_cross_brand.restricted && (
            <Card size="small" style={{ marginBottom: 16 }}
              title={`客户跨品牌（${d.customer_cross_brand.multi_brand_customers ?? 0} 个客户买过≥2品牌）`}>
              <Table size="small" rowKey="customer" pagination={false}
                dataSource={d.customer_cross_brand.customers ?? []}
                scroll={{ x: 480 }}
                columns={[
                  { title: "客户", dataIndex: "customer", ellipsis: true },
                  { title: "品牌数", dataIndex: "brand_count", align: "right" },
                  { title: "集中度", dataIndex: "concentration", align: "right", render: (v) => pct(v) },
                ]} />
            </Card>
          )}
        </>
      )}

      <OrderDetailModal
        side={orderModal?.side ?? "purchase"}
        orderNo={orderModal?.orderNo ?? null}
        onClose={() => setOrderModal(null)}
        localCostRestricted={local.cost}
        dateRange={{ date_from: from ?? undefined, date_to: to ?? undefined }} />
    </>
  );
}
