/**
 * 早会价格纪律摘要：只读回看所选时间范围内，严格越过人工池约束价的历史记录。
 * 只陈述次数、订单、池、经办人与金额差事实；不审批、不拦截，也不评价员工。
 */
import { useMemo, useState } from "react";
import { Alert, Button, Card, Empty, Grid, Skeleton, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  dashboardPriceDisciplineSummary,
  type PriceDisciplineHandlerSummary,
  type PriceDisciplineSide,
  type PriceDisciplineSideSummary,
  type PriceDisciplineSummary,
  type PriceDisciplineViolation,
} from "../../api";
import {
  fetchDashboardOrderDetail,
} from "../../api/poolAnalysis";
import PoolOrderDetailModal from "../../components/pools/PoolOrderDetailModal";
import { EMPTY, moneyExact, qty } from "../../utils/format";
import { PartLink, PoolLink } from "./PartsTable";
import { MUTED, useGuardedFetch, type DateRange } from "./shared";

const ZERO_SIDE: PriceDisciplineSideSummary = {
  violation_line_count: 0, order_count: 0, pool_count: 0, total_gap: 0,
};

const sideName = (side: PriceDisciplineSide) => side === "purchase" ? "采购" : "销售";

function FactCard({ label, children, tone = "default" }: {
  label: string;
  children: React.ReactNode;
  tone?: "default" | "danger" | "warning";
}) {
  const palette = tone === "danger"
    ? { border: "#efb7b2", background: "#fff6f5", text: "#a83f38" }
    : tone === "warning"
      ? { border: "#efd29c", background: "#fffaf0", text: "#8a641d" }
      : { border: "var(--mb-border,#e8e8e8)", background: "var(--mb-surface,#fff)", text: "inherit" };
  return (
    <div style={{ flex: "1 1 220px", minWidth: 0, border: `1px solid ${palette.border}`,
      borderRadius: 8, background: palette.background, padding: "12px 14px", color: palette.text,
      overflowWrap: "anywhere" }}>
      <div style={{ fontSize: 12.5, color: tone === "default" ? "var(--mb-text-3)" : palette.text }}>
        {label}
      </div>
      <div style={{ marginTop: 5 }}>{children}</div>
    </div>
  );
}

function SideFact({ value }: { value: PriceDisciplineSideSummary }) {
  return (
    <>
      <div style={{ fontSize: 22, fontWeight: 600 }}>{moneyExact(value.total_gap)}</div>
      <div style={{ fontSize: 12.5, marginTop: 4 }}>
        {value.violation_line_count} 行 · {value.order_count} 单 · {value.pool_count} 个池
      </div>
      <div style={{ ...MUTED, marginTop: 2 }}>金额差按未税单价差 × 数量计算</div>
    </>
  );
}

function HandlerLines({ data }: { data: PriceDisciplineSummary["handler_summary"] }) {
  if (data.purchase.length === 0 && data.sales.length === 0) {
    return <span style={MUTED}>当前范围内无涉及经办人</span>;
  }
  const renderSide = (side: PriceDisciplineSide, rows: PriceDisciplineHandlerSummary[]) => (
    rows.length > 0 && (
      <div style={{ marginTop: 4 }}>
        <Tag color={side === "purchase" ? "orange" : "blue"}>{sideName(side)}</Tag>
        {rows.slice(0, 3).map((row, i) => (
          <span key={`${row.person ?? "未记录"}-${i}`} style={{ marginRight: 8, fontSize: 12.5 }}>
            {row.person || "未记录"} {row.violation_line_count}行/{row.order_count}单 ·
            {moneyExact(row.total_gap)}
          </span>
        ))}
      </div>
    )
  );
  const people = new Set([...data.purchase, ...data.sales].map((row) => row.person || "未记录"));
  return (
    <>
      <div style={{ fontSize: 22, fontWeight: 600 }}>{people.size} 人</div>
      {renderSide("purchase", data.purchase)}
      {renderSide("sales", data.sales)}
    </>
  );
}

function OrderButton({ record, onOpen }: {
  record: PriceDisciplineViolation;
  onOpen: (record: PriceDisciplineViolation) => void;
}) {
  return (
    <button type="button" onClick={() => onOpen(record)}
      aria-label={`查看${sideName(record.side)}订单 ${record.order_no}（记录 ${record.order_id}）`}
      style={{ border: 0, padding: 0, background: "transparent", color: "var(--ant-color-primary,#1677ff)",
        cursor: "pointer", font: "inherit", textDecoration: "underline", textUnderlineOffset: 2 }}>
      {record.order_no}
    </button>
  );
}

function MissingConstraintAlert({ data }: {
  data: PriceDisciplineSummary["missing_constraints"];
}) {
  if (!data || (data.purchase_ceiling_unset_count === 0 && data.sales_floor_unset_count === 0)) return null;
  return (
    <Alert type="warning" showIcon style={{ marginBottom: 10 }}
      message={(
        <span>
          另有 {data.purchase_ceiling_unset_count} 个池未设采购上限、
          {data.sales_floor_unset_count} 个池未设销售下限，
          其中 {data.both_unset_count} 个池两侧都未设置。
        </span>
      )}
      description="未设置不等于零越线：这些池没有人工约束价，不能纳入越线判断。" />
  );
}

export default function MorningDisciplineSummary({ dateRange, localGovernanceRestricted }: {
  dateRange: DateRange;
  localGovernanceRestricted: boolean;
}) {
  const screens = Grid.useBreakpoint();
  const mobile = screens.md === false;
  const [detail, setDetail] = useState<{ side: PriceDisciplineSide; orderId: number } | null>(null);
  const restrictedEnvelope: PriceDisciplineSummary = useMemo(() => ({
    window: { range: "custom", date_from: dateRange.date_from ?? null,
      date_to: dateRange.date_to ?? null, as_of: dateRange.date_to ?? "" },
    basis: "ex_tax",
    restricted: true,
    purchase: null, sales: null, most_severe_pool: null, handler_summary: { purchase: [], sales: [] },
    recent_violations: [], missing_constraints: null,
  }), [dateRange.date_from, dateRange.date_to]);
  const request = useGuardedFetch<PriceDisciplineSummary>(
    () => localGovernanceRestricted
      ? Promise.resolve({ data: restrictedEnvelope })
      : dashboardPriceDisciplineSummary(dateRange),
    [dateRange, localGovernanceRestricted, restrictedEnvelope]);

  const data = request.data;
  const purchase = data?.purchase ?? ZERO_SIDE;
  const sales = data?.sales ?? ZERO_SIDE;
  const records = data?.recent_violations ?? [];
  const openOrder = (record: PriceDisciplineViolation) => {
    setDetail({ side: record.side, orderId: record.order_id });
  };

  const columns: ColumnsType<PriceDisciplineViolation> = [
    { title: "方向", dataIndex: "side", width: 70,
      render: (side: PriceDisciplineSide) => <Tag color={side === "purchase" ? "orange" : "blue"}>{sideName(side)}</Tag> },
    { title: "日期", dataIndex: "order_date", width: 100, render: (v) => v || EMPTY },
    { title: "订单", key: "order", width: 130, render: (_, row) => <OrderButton record={row} onOpen={openOrder} /> },
    { title: "PN", key: "pn", width: 150, render: (_, row) => <PartLink partId={row.part_id} pn={row.pn_std} /> },
    { title: "互通池", key: "pool", width: 140, render: (_, row) => (
      <PoolLink groupId={row.pool_group_id} name={row.pool_name} dateRange={dateRange} />
    ) },
    { title: "经办人", dataIndex: "person", width: 90, render: (v) => v || "未记录" },
    { title: "数量", dataIndex: "quantity", width: 72, align: "right", render: qty },
    { title: "实际未税单价", dataIndex: "actual_unit_ex_tax", width: 116, align: "right", render: moneyExact },
    { title: "人工约束价", dataIndex: "manual_limit_ex_tax", width: 108, align: "right", render: moneyExact },
    { title: "单价差", dataIndex: "unit_gap", width: 96, align: "right", render: moneyExact },
    { title: "金额差", dataIndex: "total_gap", width: 108, align: "right",
      render: (v) => <strong style={{ color: "#b6423a" }}>{moneyExact(v)}</strong> },
  ];

  const mobileRecords = (
    <div data-testid="discipline-mobile-list" style={{ display: "grid", gap: 8 }}>
      {records.map((row) => (
        <div key={`${row.side}-${row.line_id}`}
          style={{ border: "1px solid var(--mb-border,#e8e8e8)", borderRadius: 8, padding: 10,
            minWidth: 0, overflowWrap: "anywhere" }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
            <span><Tag color={row.side === "purchase" ? "orange" : "blue"}>{sideName(row.side)}</Tag>
              <OrderButton record={row} onOpen={openOrder} /></span>
            <strong style={{ color: "#b6423a" }}>金额差 {moneyExact(row.total_gap)}</strong>
          </div>
          <div style={{ marginTop: 6, display: "flex", gap: 8, flexWrap: "wrap" }}>
            <PartLink partId={row.part_id} pn={row.pn_std} />
            <PoolLink groupId={row.pool_group_id} name={row.pool_name} dateRange={dateRange} />
            <span>{row.person || "未记录"}</span><span>{row.order_date || EMPTY}</span>
          </div>
          <div style={{ ...MUTED, marginTop: 5 }}>
            实际 {moneyExact(row.actual_unit_ex_tax)} · 约束 {moneyExact(row.manual_limit_ex_tax)} ·
            单价差 {moneyExact(row.unit_gap)} · 数量 {qty(row.quantity)}
          </div>
        </div>
      ))}
    </div>
  );

  return (
    <Card size="small" title="早会价格纪律摘要" style={{ marginBottom: 16, maxWidth: "100%", overflow: "hidden" }}
      extra={<span style={MUTED}>{dateRange.date_from} ~ {dateRange.date_to}</span>}>
      <Alert type="info" showIcon style={{ marginBottom: 10 }}
        message="历史分析，只记录展示，不拦截订单"
        description="次数和差额不等于员工评价；请结合订单背景和真实业务原因人工判断。" />

      {request.loading && !data ? (
        <Skeleton active paragraph={{ rows: 4 }} />
      ) : request.error ? (
        <Alert type="error" showIcon message={`价格纪律摘要加载失败：${request.error}`}
          action={<Button size="small" onClick={request.reload}>重试</Button>} />
      ) : data?.restricted ? (
        <Alert type="info" showIcon message="当前账号无池价格纪律查看权限"
          description="次数、金额、池排行、经办人与最近记录均不会显示，避免通过聚合结果反推受限数据。" />
      ) : data ? (
        <>
          <div data-testid="discipline-fact-cards"
            style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 10 }}>
            <FactCard label="高于采购上限" tone={purchase.violation_line_count > 0 ? "danger" : "default"}>
              <SideFact value={purchase} />
            </FactCard>
            <FactCard label="低于销售下限" tone={sales.violation_line_count > 0 ? "danger" : "default"}>
              <SideFact value={sales} />
            </FactCard>
            <FactCard label="差额最大池" tone={data.most_severe_pool ? "warning" : "default"}>
              {data.most_severe_pool ? (
                <>
                  <div style={{ fontSize: 18, fontWeight: 600 }}>
                    <PoolLink groupId={data.most_severe_pool.pool_group_id}
                      name={data.most_severe_pool.pool_name} dateRange={dateRange} />
                  </div>
                  <div style={{ fontSize: 13, marginTop: 5 }}>
                    总差额 {moneyExact(data.most_severe_pool.total_gap)} ·
                    {data.most_severe_pool.violation_line_count} 行
                  </div>
                  <div style={{ ...MUTED, marginTop: 2 }}>
                    采购 {moneyExact(data.most_severe_pool.purchase_total_gap)} ·
                    销售 {moneyExact(data.most_severe_pool.sales_total_gap)}
                  </div>
                </>
              ) : <span style={MUTED}>当前范围内无越线池</span>}
            </FactCard>
            <FactCard label="涉及经办人"><HandlerLines data={data.handler_summary} /></FactCard>
          </div>

          <MissingConstraintAlert data={data.missing_constraints} />

          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
            <strong>最近 10 条越线记录</strong>
            <span style={MUTED}>按订单日期倒序，仅显示当前时间范围</span>
          </div>
          {records.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前范围内未发现越线记录" />
          ) : mobile ? mobileRecords : (
            <div data-testid="discipline-desktop-table" style={{ maxWidth: "100%", overflow: "hidden" }}>
              <Table<PriceDisciplineViolation> size="small" rowKey={(row) => `${row.side}-${row.line_id}`}
                columns={columns} dataSource={records} pagination={false} scroll={{ x: 1190 }} />
            </div>
          )}
        </>
      ) : null}

      <PoolOrderDetailModal
        side={detail?.side ?? "purchase"}
        orderId={detail?.orderId ?? null}
        range="custom"
        dateFrom={dateRange.date_from}
        dateTo={dateRange.date_to}
        loadDetail={fetchDashboardOrderDetail}
        onClose={() => setDetail(null)}
      />
    </Card>
  );
}
