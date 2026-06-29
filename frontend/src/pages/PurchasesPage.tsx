import { useEffect, useRef, useState } from "react";
import { Card, Input, Segmented, Tag, Checkbox, Table, Tooltip, theme, message } from "antd";
import { RightOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import ResizableTable from "../components/ResizableTable";
import PageHeader from "../components/PageHeader";
import {
  listRecentPurchases, fetchPurchaseAnalysis, fetchPurchaseDrill, fetchCancellationStats,
} from "../api";
import type {
  RecentPurchaseRow, PurchaseAnalysis, PurchaseAnalysisRow, PurchaseDrillItem,
  CancellationStats, CancellationPeriodRow,
} from "../api";

// 流程状态：颜色 + 过滤选项（宋总：取消单也要能看/能统计）
const STATUS_COLOR: Record<string, string> = {
  已生效: "green", 已取消: "red", 作废: "red", 进行中: "blue", 草稿: "default",
};
const STATUS_FILTER = [
  { label: "已生效", value: "已生效" },
  { label: "已取消", value: "已取消" },
  { label: "进行中", value: "进行中" },
  { label: "全部", value: "全部" },
];
const GRAN_OPTIONS = [
  { label: "按月", value: "month" },
  { label: "按季", value: "quarter" },
  { label: "按年", value: "year" },
];

const DAY_OPTIONS = [
  { label: "近 7 天", value: 7 },
  { label: "近 30 天", value: 30 },
  { label: "近 90 天", value: 90 },
  { label: "近一年", value: 365 },
];
const ANALYSIS_DAYS = [
  { label: "近 7 天", value: 7 },
  { label: "近 14 天", value: 14 },
  { label: "近 30 天", value: 30 },
  { label: "近 90 天", value: 90 },
];
const TAX_OPTIONS = [
  { label: "含税", value: "inc" },
  { label: "未税", value: "ex" },
  { label: "两列", value: "both" },
];
const ADVICE_COLOR: Record<string, string> = {
  批量补库: "gold", 谈价: "blue", 偶发: "default",
};

const fmt = (v: number | null | undefined) =>
  v == null ? "—" : Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
// 金额场景固定两位小数（含¥0.00 对齐）；数量/比值用 fmt
const fmtMoney = (v: number | null | undefined) =>
  v == null ? "—" : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function Sparkline({ data, accent }: { data: number[] | null; accent: string }) {
  if (!data || data.length === 0) return <span style={{ color: "var(--mb-text-3)" }}>—</span>;
  const max = Math.max(...data, 1);
  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 2, height: 22 }}>
      {data.map((v, i) => (
        <Tooltip key={i} title={`${v}`}>
          <div style={{
            width: 5, height: Math.max(2, Math.round((v / max) * 22)),
            background: v > 0 ? accent : "#e9e5de",
            opacity: v > 0 ? 0.6 : 1, borderRadius: 1,
          }} />
        </Tooltip>
      ))}
    </div>
  );
}

function TrendArrow({ t }: { t: PurchaseAnalysisRow["price_trend"] }) {
  if (t === "up") return <span style={{ color: "#c0524a" }}>↑</span>;
  if (t === "down") return <span style={{ color: "#3f7a45" }}>↓</span>;
  return <span style={{ color: "var(--mb-text-3)" }}>→</span>;
}

function PriceCell({ row, basis }: { row: PurchaseAnalysisRow; basis: string }) {
  const line = (label: string, min: number | null, max: number | null, last: number | null) => (
    <div style={{ whiteSpace: "nowrap" }}>
      {label && <span style={{ color: "var(--mb-text-3)" }}>{label} </span>}
      {fmt(min)}–{fmt(max)}
      <span style={{ color: "#6b665e" }}> · 最近 {fmt(last)} </span>
      {last != null && <TrendArrow t={row.price_trend} />}
    </div>
  );
  if (basis === "both")
    return (
      <>
        {line("含", row.price_inc_min, row.price_inc_max, row.price_inc_last)}
        {line("未", row.price_ex_min, row.price_ex_max, row.price_ex_last)}
      </>
    );
  if (basis === "ex") return line("", row.price_ex_min, row.price_ex_max, row.price_ex_last);
  return line("", row.price_inc_min, row.price_inc_max, row.price_inc_last);
}

function KpiCard({ label, value, sub, highlight }: {
  label: string; value: string; sub?: string; highlight?: boolean;
}) {
  return (
    <div style={{
      flex: "1 1 150px", minWidth: 140, padding: "12px 14px", borderRadius: 8,
      background: highlight ? "var(--ant-color-warning-bg, #fdf3e3)" : "rgba(0,0,0,0.025)",
    }}>
      <div style={{ fontSize: 12.5, color: highlight ? "#9a7b43" : "#6b665e" }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 500, marginTop: 4, color: highlight ? "#9a7b43" : undefined }}>{value}</div>
      {sub && <div style={{ fontSize: 11.5, color: highlight ? "#9a7b43" : "var(--mb-text-3)", marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

function SourceComposition({ comp, accent }: {
  comp: PurchaseAnalysis["source_composition"]; accent: string;
}) {
  const total = comp.reduce((s, c) => s + (c.amount || 0), 0) || 1;
  const palette = [accent, "#7c9bd6", "#caa46a", "#8aa888", "#b58fb0", "#b3b0a6"];
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", height: 10, borderRadius: 5, overflow: "hidden", marginBottom: 8 }}>
        {comp.map((c, i) => (
          <Tooltip key={c.channel} title={`${c.channel} ¥${fmt(c.amount)}`}>
            <div style={{ width: `${((c.amount || 0) / total) * 100}%`, background: palette[i % palette.length] }} />
          </Tooltip>
        ))}
      </div>
      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", fontSize: 12.5 }}>
        {comp.map((c, i) => (
          <span key={c.channel} style={{ color: "#6b665e" }}>
            <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: 2, background: palette[i % palette.length], marginRight: 5 }} />
            {c.channel} ¥{fmt(c.amount)} · {c.order_count}单
          </span>
        ))}
      </div>
      <div style={{ fontSize: 11.5, color: "var(--mb-text-3)", marginTop: 4 }}>
        金额为实付口径（含税单含税 + 不含税单未税），与上方采购额一致
      </div>
    </div>
  );
}

function DrillTable({ partId, days, excludeDesignated }: { partId: number; days: number; excludeDesignated: boolean }) {
  const [items, setItems] = useState<PurchaseDrillItem[] | null>(null);
  useEffect(() => {
    let alive = true;
    fetchPurchaseDrill({ part_id: partId, days, exclude_designated: excludeDesignated })
      .then(({ data }) => { if (alive) setItems(data.items); })
      .catch(() => { if (alive) setItems([]); });
    return () => { alive = false; };
  }, [partId, days, excludeDesignated]);
  return (
    <Table<PurchaseDrillItem>
      size="small"
      rowKey={(r, i) => `${r.order_no}-${i}`}
      loading={items === null}
      dataSource={items || []}
      pagination={false}
      columns={[
        { title: "日期", dataIndex: "order_date", width: 100, render: (v) => v || "—" },
        { title: "采购员", dataIndex: "purchaser", width: 90, render: (v) => v || "—" },
        { title: "供应商", dataIndex: "supplier", ellipsis: true, render: (v) => v || <span style={{ color: "var(--mb-text-3)" }}>（不可见）</span> },
        { title: "渠道", dataIndex: "source_channel", width: 100, render: (v) => v ? <Tag>{v}</Tag> : "—" },
        { title: "口径", dataIndex: "is_tax_inclusive", width: 70,
          render: (v) => (v == null ? "—" : <Tag color={v ? "default" : "blue"}>{v ? "含税" : "未税"}</Tag>) },
        { title: "未税价", dataIndex: "price_ex", width: 90, align: "right", render: fmt },
        { title: "含税价", dataIndex: "price_inc", width: 90, align: "right", render: fmt },
        { title: "数量", dataIndex: "qty", width: 70, align: "right", render: (v) => (v == null ? "—" : Number(v)) },
      ]}
    />
  );
}

function AnalysisPanel() {
  const { token } = theme.useToken();
  const accent = token.colorPrimary;
  const [days, setDays] = useState(7);
  const [basis, setBasis] = useState("inc");
  const [excludeDesignated, setExcludeDesignated] = useState(true);
  const [data, setData] = useState<PurchaseAnalysis | null>(null);
  const [loading, setLoading] = useState(false);
  const [expandedKeys, setExpandedKeys] = useState<number[]>([]);
  const [open, setOpen] = useState(() => localStorage.getItem("pap_analysis_open") !== "0");
  const seqRef = useRef(0);

  const toggle = () => setOpen((o) => { localStorage.setItem("pap_analysis_open", o ? "0" : "1"); return !o; });

  useEffect(() => {
    const seq = ++seqRef.current;
    setLoading(true);
    setExpandedKeys([]);   // 切窗口/口径时收起所有展开行，避免一批下钻并发重拉
    fetchPurchaseAnalysis({ days, exclude_designated: excludeDesignated })
      .then(({ data }) => { if (seq === seqRef.current) setData(data); })
      .catch(() => { if (seq === seqRef.current) message.error("采购分析加载失败"); })
      .finally(() => { if (seq === seqRef.current) setLoading(false); });
  }, [days, excludeDesignated]);

  const kpi = data?.kpi;
  const bySrc = kpi ? Object.entries(kpi.order_count_by_source).map(([k, v]) => `${k} ${v}`).join(" · ") : "";

  const columns: ColumnsType<PurchaseAnalysisRow> = [
    { title: "型号 / 描述", dataIndex: "pn_std", width: 200, fixed: "left",
      render: (v, r) => (
        <div>
          <span style={{ fontFamily: "monospace", fontSize: 12.5 }}>{v}</span>
          {r.needs_review && <Tag color="orange" style={{ marginLeft: 6 }}>待复核</Tag>}
          <div style={{ fontSize: 11.5, color: "var(--mb-text-3)" }}>{r.description || r.brand || ""}</div>
        </div>
      ) },
    { title: "次数", dataIndex: "buy_times", width: 64, align: "center",
      sorter: (a, b) => a.buy_times - b.buy_times,
      render: (v, r) => <span style={{ fontWeight: r.is_frequent ? 500 : 400 }}>{v}</span> },
    { title: "总量", dataIndex: "total_qty", width: 64, align: "center", render: (v) => (v == null ? "—" : Number(v)) },
    { title: `${days} 天分布`, dataIndex: "daily", width: 86, render: (v) => <Sparkline data={v} accent={accent} /> },
    { title: basis === "ex" ? "采购价(未税)·最近" : basis === "both" ? "采购价 含/未·最近" : "采购价(含税)·最近",
      key: "price", width: 200, render: (_, r) => <PriceCell row={r} basis={basis} /> },
    { title: "来源拆分", key: "channels", width: 200,
      render: (_, r) => (
        <span>
          {r.channels.map((c) => (
            <Tag key={c.channel} style={{ marginBottom: 2 }}>{c.channel} {c.times}次</Tag>
          ))}
        </span>
      ) },
    { title: "库存", key: "stock", width: 72, align: "center",
      render: () => <span style={{ color: "var(--mb-text-3)" }}>未启用</span> },
    { title: "建议", dataIndex: "advice", width: 90,
      render: (v: string) => (v && v !== "普通" ? <Tag color={ADVICE_COLOR[v]}>{v}</Tag> : <span style={{ color: "var(--mb-text-3)" }}>—</span>) },
  ];

  return (
    <Card style={{ marginBottom: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap", marginBottom: open ? 14 : 0 }}>
        <span onClick={toggle} style={{ fontSize: 16, fontWeight: 500, cursor: "pointer", userSelect: "none", display: "flex", alignItems: "center", gap: 8 }}>
          <RightOutlined style={{ fontSize: 12, color: "var(--mb-text-3)", transition: "transform .15s", transform: open ? "rotate(90deg)" : "none" }} />
          采购分析 · 高频采购待计划
          {!open && kpi && (
            <span style={{ fontSize: 12.5, fontWeight: 400, color: "#9a7b43" }}>
              频发待计划 {kpi.frequent_count} · 近{days}天采购额 ¥{fmt(kpi.total_amount)}
            </span>
          )}
        </span>
        {open && (
          <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            <Segmented options={ANALYSIS_DAYS} value={days} onChange={(v) => setDays(v as number)} />
            <Segmented options={TAX_OPTIONS} value={basis} onChange={(v) => setBasis(v as string)} />
            <Checkbox checked={excludeDesignated} onChange={(e) => setExcludeDesignated(e.target.checked)}>排除指定采购</Checkbox>
          </div>
        )}
      </div>
      {open && (
      <>

      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 14 }}>
        <KpiCard label={`近 ${days} 天采购额`} value={`¥${fmt(kpi?.total_amount)}`} sub="实付（含税单含税·不含单未税）" />
        <KpiCard label="采购单数" value={kpi ? String(kpi.order_count) : "—"} sub={bySrc} />
        <KpiCard label="涉及型号" value={kpi ? String(kpi.part_count) : "—"} sub={kpi?.truncated ? `仅显示前 ${kpi.shown}` : undefined} />
        <KpiCard label="频发待计划" value={kpi ? String(kpi.frequent_count) : "—"} sub={`≥ ${data?.window.freq_threshold ?? 3} 次 / 窗口`} highlight />
      </div>

      {data && data.source_composition.length > 0 && <SourceComposition comp={data.source_composition} accent={accent} />}

      <Table<PurchaseAnalysisRow>
        size="small"
        rowKey={(r) => r.part_id}
        loading={loading}
        dataSource={data?.rows || []}
        scroll={{ x: 1000 }}
        pagination={{ pageSize: 20, showSizeChanger: true, showTotal: (t) => `共 ${t} 个型号` }}
        columns={columns}
        expandable={{
          expandedRowKeys: expandedKeys,
          onExpandedRowsChange: (keys) => setExpandedKeys(keys as number[]),
          expandedRowRender: (r) => <DrillTable partId={r.part_id} days={days} excludeDesignated={excludeDesignated} />,
          rowExpandable: () => true,
        }}
      />
      <div style={{ marginTop: 8, fontSize: 12, color: "var(--mb-text-3)" }}>
        点开型号看逐笔比价（谁采的 / 哪家 / 含税未税）。建议：
        <Tag color="gold">批量补库</Tag>频发应急→早会谈量 ·
        <Tag color="blue">谈价</Tag>价格在涨→压价 ·
        <Tag>偶发</Tag>一次性→跳过
      </div>
      </>
      )}
    </Card>
  );
}

function CancellationPanel() {
  const [gran, setGran] = useState("month");
  const [data, setData] = useState<CancellationStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(() => localStorage.getItem("pap_cancel_open") !== "0");
  const seqRef = useRef(0);
  const toggle = () => setOpen((o) => { localStorage.setItem("pap_cancel_open", o ? "0" : "1"); return !o; });

  useEffect(() => {
    if (!open) return;
    const seq = ++seqRef.current;
    setLoading(true);
    fetchCancellationStats({ granularity: gran })
      .then(({ data }) => { if (seq === seqRef.current) setData(data); })
      .catch(() => { if (seq === seqRef.current) message.error("取消单统计加载失败"); })
      .finally(() => { if (seq === seqRef.current) setLoading(false); });
  }, [gran, open]);

  const columns: ColumnsType<CancellationPeriodRow> = [
    { title: "期间", dataIndex: "period", width: 110 },
    { title: "总单数", dataIndex: "total", width: 88, align: "right" },
    { title: "已生效", key: "active", width: 84, align: "right",
      render: (_, r) => r.by_status["已生效"]?.count ?? 0 },
    { title: "进行中", key: "doing", width: 84, align: "right",
      render: (_, r) => r.by_status["进行中"]?.count ?? 0 },
    { title: "取消/作废", dataIndex: "cancelled", width: 98, align: "right",
      render: (v: number) => <span style={{ color: v > 0 ? "#c0524a" : undefined }}>{v}</span> },
    { title: "取消率", dataIndex: "cancel_rate", width: 88, align: "right",
      render: (v: number) => `${v}%` },
    { title: "取消金额", dataIndex: "cancelled_amount", width: 130, align: "right", render: fmtMoney },
  ];

  return (
    <Card style={{ marginBottom: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap", marginBottom: open ? 14 : 0 }}>
        <span onClick={toggle} style={{ fontSize: 16, fontWeight: 500, cursor: "pointer", userSelect: "none", display: "flex", alignItems: "center", gap: 8 }}>
          <RightOutlined style={{ fontSize: 12, color: "var(--mb-text-3)", transition: "transform .15s", transform: open ? "rotate(90deg)" : "none" }} />
          取消单统计 · 采购未成功
          {!open && data && (
            <span style={{ fontSize: 12.5, fontWeight: 400, color: "#9a7b43" }}>
              累计取消 {data.summary.cancelled} 单 · 取消率 {data.summary.cancel_rate}%
            </span>
          )}
        </span>
        {open && <Segmented options={GRAN_OPTIONS} value={gran} onChange={(v) => setGran(v as string)} />}
      </div>
      {open && (
        <>
          <Table<CancellationPeriodRow>
            size="small" rowKey={(r) => r.period} loading={loading}
            dataSource={data?.rows || []} pagination={false} columns={columns}
          />
          <div style={{ marginTop: 8, fontSize: 12, color: "var(--mb-text-3)" }}>
            含全部状态的采购单（含已取消/作废）。取消单仅用于此处统计，<b>不计入</b>成本/库存/利润——那些口径只算「已生效」。
          </div>
        </>
      )}
    </Card>
  );
}

export default function PurchasesPage() {
  const [q, setQ] = useState("");
  const [supplier, setSupplier] = useState("");
  const [days, setDays] = useState(30);
  const [status, setStatus] = useState("已生效");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [total, setTotal] = useState(0);
  const [rows, setRows] = useState<RecentPurchaseRow[]>([]);
  const [loading, setLoading] = useState(false);
  const loadSeqRef = useRef(0);

  const load = async (p = page, ps = pageSize) => {
    const seq = ++loadSeqRef.current;
    setLoading(true);
    try {
      const { data } = await listRecentPurchases({
        q: q.trim() || undefined,
        supplier: supplier.trim() || undefined,
        days, page: p, page_size: ps, status,
      });
      if (seq !== loadSeqRef.current) return;
      setRows(data.items);
      setTotal(data.total);
    } catch {
      if (seq === loadSeqRef.current) message.error("查询失败，请稍后重试");
    } finally {
      if (seq === loadSeqRef.current) setLoading(false);
    }
  };

  useEffect(() => {
    setPage(1);
    load(1, pageSize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days, status]);

  return (
    <>
      <PageHeader
        title="采购记录"
        subtitle="早会先看上方「采购分析」找频发待计划，下方是逐笔采购明细"
      />
      <AnalysisPanel />
      <CancellationPanel />
      <Card>
      <div style={{ display: "flex", gap: 12, marginBottom: 16, flexWrap: "wrap", alignItems: "center" }}>
        <Segmented options={DAY_OPTIONS} value={days} onChange={(v) => setDays(v as number)} />
        <Input.Search
          placeholder="型号 / 描述 / 品牌关键词"
          style={{ width: 280 }}
          allowClear
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onSearch={() => { setPage(1); load(1, pageSize); }}
        />
        <Input.Search
          placeholder="供应商"
          style={{ width: 200 }}
          allowClear
          value={supplier}
          onChange={(e) => setSupplier(e.target.value)}
          onSearch={() => { setPage(1); load(1, pageSize); }}
        />
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontSize: 13, color: "var(--mb-text-3)" }}>状态</span>
          <Segmented options={STATUS_FILTER} value={status} onChange={(v) => setStatus(v as string)} />
        </span>
      </div>
      <ResizableTable<RecentPurchaseRow>
        storageKey="purchases-recent"
        size="small"
        rowKey={(r) => r.line_id}
        loading={loading}
        dataSource={rows}
        scroll={{ x: 1100 }}
        pagination={{
          current: page, pageSize, total, showSizeChanger: true,
          showTotal: (t) => `共 ${t} 条`,
          onChange: (p, ps) => { setPage(p); setPageSize(ps); load(p, ps); },
        }}
        columns={[
          { title: "采购日期", dataIndex: "order_date", width: 110, fixed: "left",
            render: (v) => v || "—" },
          { title: "型号", dataIndex: "pn_std", width: 190,
            render: (v, r) => (
              <span>
                {v}
                {r.needs_review && <Tag style={{ marginLeft: 6 }} color="orange">待复核</Tag>}
              </span>
            ) },
          { title: "描述", dataIndex: "description", ellipsis: true },
          { title: "品牌", dataIndex: "brand", width: 110, ellipsis: true },
          { title: "数量", dataIndex: "qty", width: 80, align: "right",
            render: (v) => (v == null ? "—" : Number(v)) },
          { title: "单价(含税)", dataIndex: "unit_price", width: 110, align: "right",
            render: fmtMoney },
          { title: "金额", dataIndex: "line_amount", width: 120, align: "right",
            render: fmtMoney },
          { title: "供应商", dataIndex: "supplier", width: 160, ellipsis: true },
          { title: "采购类型", dataIndex: "source_type", width: 100,
            render: (v) => v ? <Tag>{v}</Tag> : "—" },
          { title: "流程状态", dataIndex: "data_status", width: 96,
            render: (v: string | null) => v ? <Tag color={STATUS_COLOR[v] || "default"}>{v}</Tag> : "—" },
          { title: "采购员", dataIndex: "purchaser", width: 90, render: (v) => v || "—" },
          { title: "单号", dataIndex: "order_no", width: 170,
            render: (v) => <span style={{ fontFamily: "monospace", fontSize: 12 }}>{v}</span> },
        ]}
      />
      </Card>
    </>
  );
}
