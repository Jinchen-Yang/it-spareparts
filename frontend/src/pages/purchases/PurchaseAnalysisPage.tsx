import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Card, Checkbox, Grid, List, Segmented, Spin, Table, Tag, Tooltip, theme, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import PageHeader from "../../components/PageHeader";
import MobileDetailDrawer from "../../components/MobileDetailDrawer";
import PoolIdentityLink from "../../components/pools/PoolIdentityLink";
import PoolReferencePanel from "../../components/pools/PoolReferencePanel";
import {
  fetchPurchaseAnalysis, fetchPurchaseDrill,
} from "../../api";
import type {
  PurchaseAnalysis, PurchaseAnalysisRow, PurchaseDrillItem,
} from "../../api";
import { useTaxBasis, TaxMoney } from "../../context/TaxBasis";
import {
  ANALYSIS_DAYS, ADVICE_COLOR, fmt, Sparkline, PriceCell, KpiCard, readNum,
  useUrlPatch, activatableProps,
} from "./shared";

// 逐笔比价下钻的列定义 + 拉取（桌面展开行、移动端抽屉共用同一数据源 fetchPurchaseDrill）
const DRILL_COLUMNS: ColumnsType<PurchaseDrillItem> = [
  { title: "日期", dataIndex: "order_date", width: 100, render: (v) => v || "—" },
  { title: "采购员", dataIndex: "purchaser", width: 90, render: (v) => v || "—" },
  { title: "供应商", dataIndex: "supplier", ellipsis: true, render: (v) => v || <span style={{ color: "var(--mb-text-3)" }}>（不可见）</span> },
  { title: "渠道", dataIndex: "source_channel", width: 100, render: (v) => v ? <Tag>{v}</Tag> : "—" },
  { title: "口径", dataIndex: "is_tax_inclusive", width: 70,
    render: (v) => (v == null ? "—" : <Tag color={v ? "default" : "blue"}>{v ? "含税" : "未税"}</Tag>) },
  { title: "未税价", dataIndex: "price_ex", width: 90, align: "right", render: fmt },
  { title: "含税价", dataIndex: "price_inc", width: 90, align: "right", render: fmt },
  { title: "数量", dataIndex: "qty", width: 70, align: "right", render: (v) => (v == null ? "—" : Number(v)) },
];

function useDrill(partId: number | null, days: number, excludeDesignated: boolean) {
  const [items, setItems] = useState<PurchaseDrillItem[] | null>(null);
  useEffect(() => {
    if (partId == null) { setItems(null); return; }
    let alive = true;
    setItems(null);
    fetchPurchaseDrill({ part_id: partId, days, exclude_designated: excludeDesignated })
      .then(({ data }) => { if (alive) setItems(data.items); })
      .catch(() => { if (alive) setItems([]); });
    return () => { alive = false; };
  }, [partId, days, excludeDesignated]);
  return items;
}

// 桌面展开行下钻表
function DrillTable({ partId, days, excludeDesignated }: { partId: number; days: number; excludeDesignated: boolean }) {
  const items = useDrill(partId, days, excludeDesignated);
  return (
    <Table<PurchaseDrillItem>
      size="small"
      rowKey={(r, i) => `${r.order_no}-${i}`}
      loading={items === null}
      dataSource={items || []}
      pagination={false}
      columns={DRILL_COLUMNS}
    />
  );
}

function ExpandedAnalysis({ partId, days, excludeDesignated, dateFrom, dateTo }: {
  partId: number; days: number; excludeDesignated: boolean; dateFrom?: string; dateTo?: string;
}) {
  return (
    <div style={{ display: "grid", gap: 12 }}>
      <PoolReferencePanel partId={partId} side="purchase" range="custom"
        dateFrom={dateFrom} dateTo={dateTo} compact />
      <DrillTable partId={partId} days={days} excludeDesignated={excludeDesignated} />
    </div>
  );
}

// 移动端抽屉里的逐笔下钻：卡片式（谁采的/哪家/含税未税/数量），恢复桌面下钻的全部逐笔信息
function MobileDrill({ partId, days, excludeDesignated }: { partId: number; days: number; excludeDesignated: boolean }) {
  const items = useDrill(partId, days, excludeDesignated);
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ fontSize: 13, fontWeight: 500, margin: "8px 0" }}>逐笔比价</div>
      {items === null ? (
        <div style={{ textAlign: "center", padding: 20 }}><Spin /></div>
      ) : items.length === 0 ? (
        <div style={{ color: "var(--mb-text-3)", padding: "8px 0" }}>窗口内无逐笔记录</div>
      ) : (
        items.map((it, i) => (
          <div key={`${it.order_no}-${i}`} style={{ padding: "10px 0", borderTop: i ? "1px solid var(--mb-border)" : "none" }}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
              <span style={{ fontWeight: 500 }}>{it.order_date || "—"}</span>
              <span>
                {it.is_tax_inclusive == null ? null : <Tag color={it.is_tax_inclusive ? "default" : "blue"}>{it.is_tax_inclusive ? "含税" : "未税"}</Tag>}
                {it.source_channel ? <Tag>{it.source_channel}</Tag> : null}
              </span>
            </div>
            <div style={{ fontSize: 13, color: "var(--mb-text-2)", marginTop: 4 }}>
              采购员 {it.purchaser || "—"} · 供应商 {it.supplier || "（不可见）"}
            </div>
            <div style={{ fontSize: 13, color: "var(--mb-text-2)", marginTop: 2 }}>
              单号 {it.order_no || "—"} · 数量 {it.qty == null ? "—" : Number(it.qty)} · 未税 {fmt(it.price_ex)} · 含税 {fmt(it.price_inc)}
            </div>
          </div>
        ))
      )}
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
            {c.channel} <TaxMoney inc={c.amount_inc} ex={c.amount_ex} /> · {c.order_count}单
          </span>
        ))}
      </div>
      <div style={{ fontSize: 11.5, color: "var(--mb-text-3)", marginTop: 4 }}>
        含税/不含税均为订单级真实总额（Excel 原值，零计算），跟随顶栏「价格口径」开关
      </div>
    </div>
  );
}

export default function PurchaseAnalysisPage() {
  const { token } = theme.useToken();
  const accent = token.colorPrimary;
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false;
  const { basis } = useTaxBasis();

  const [sp, setSp] = useSearchParams();
  const patch = useUrlPatch(sp, setSp);
  const days = readNum(sp, "days", 7);
  const excludeDesignated = sp.get("exclude_designated") !== "0"; // 默认 true

  const [data, setData] = useState<PurchaseAnalysis | null>(null);
  const [loading, setLoading] = useState(false);
  const [expandedKeys, setExpandedKeys] = useState<number[]>([]);
  const [detail, setDetail] = useState<PurchaseAnalysisRow | null>(null);
  const seqRef = useRef(0);

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
          <PoolIdentityLink groupId={r.pool_group_id} name={r.pool_name} pn={r.pn_std}
            range="custom" dateFrom={data?.window.since} dateTo={data?.window.until} />
          <div style={{ fontSize: 11.5, color: "var(--mb-text-3)" }}>{r.description || r.brand || ""}</div>
        </div>
      ) },
    { title: "次数", dataIndex: "buy_times", width: 64, align: "center",
      sorter: (a, b) => a.buy_times - b.buy_times,
      render: (v, r) => <span style={{ fontWeight: r.is_frequent ? 500 : 400 }}>{v}</span> },
    { title: "总量", dataIndex: "total_qty", width: 64, align: "center", render: (v) => (v == null ? "—" : Number(v)) },
    { title: `${days} 天分布`, dataIndex: "daily", width: 86, render: (v) => <Sparkline data={v} accent={accent} /> },
    { title: basis === "ex" ? "采购价(不含税)·最近" : basis === "both" ? "采购价 含/不含·最近" : "采购价(含税)·最近",
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

  const filters = (
    <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
      <Segmented options={ANALYSIS_DAYS} value={days} onChange={(v) => patch({ days: v as number })} />
      <Checkbox checked={excludeDesignated} onChange={(e) => patch({ exclude_designated: e.target.checked ? undefined : "0" })}>
        排除指定采购
      </Checkbox>
    </div>
  );

  return (
    <>
      <PageHeader
        title="采购分析"
        subtitle="最近采购了哪些型号、哪些频率高、主要来自哪些业务类型——早会先看这里找频发待计划"
        extra={!isMobile ? filters : undefined}
      />
      <Card>
        {isMobile && <div style={{ marginBottom: 14 }}>{filters}</div>}

        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 14 }}>
          <KpiCard label={`近 ${days} 天采购额`} value={<TaxMoney inc={kpi?.total_amount_inc ?? null} ex={kpi?.total_amount_ex ?? null} stack />} sub="订单级真实总额（含税 / 不含税）" />
          <KpiCard label="采购单数" value={kpi ? String(kpi.order_count) : "—"} sub={bySrc} />
          <KpiCard label="涉及型号" value={kpi ? String(kpi.part_count) : "—"} sub={kpi?.truncated ? `仅显示前 ${kpi.shown}` : undefined} />
          <KpiCard label="频发待计划" value={kpi ? String(kpi.frequent_count) : "—"} sub={`≥ ${data?.window.freq_threshold ?? 3} 次 / 窗口`} highlight />
        </div>

        {data && data.source_composition.length > 0 && <SourceComposition comp={data.source_composition} accent={accent} />}

        {isMobile ? (
          <List
            loading={loading}
            dataSource={data?.rows || []}
            locale={{ emptyText: "窗口内没有采购数据" }}
            pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 个型号` }}
            renderItem={(r) => (
              <List.Item
                key={r.part_id}
                {...activatableProps(() => setDetail(r), `查看型号 ${r.pn_std} 的采购分析与逐笔比价`)}
                style={{ cursor: "pointer" }}
              >
                <div style={{ width: "100%" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                    <span style={{ fontFamily: "monospace", fontWeight: r.is_frequent ? 500 : 400 }}>{r.pn_std}</span>
                    {r.advice && r.advice !== "普通" && <Tag color={ADVICE_COLOR[r.advice]}>{r.advice}</Tag>}
                  </div>
                  <div style={{ marginTop: 4 }}>
                    <PoolIdentityLink groupId={r.pool_group_id} name={r.pool_name} pn={r.pn_std}
                      range="custom" dateFrom={data?.window.since} dateTo={data?.window.until} />
                  </div>
                  <div style={{ marginTop: 4, fontSize: 13, color: "var(--mb-text-2)" }}>
                    采购 {r.buy_times} 次 · 总量 {r.total_qty == null ? "—" : Number(r.total_qty)} · 库存 <span style={{ color: "var(--mb-text-3)" }}>未启用</span>
                  </div>
                </div>
              </List.Item>
            )}
          />
        ) : (
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
              expandedRowRender: (r) => <ExpandedAnalysis partId={r.part_id} days={days}
                excludeDesignated={excludeDesignated}
                dateFrom={data?.window.since} dateTo={data?.window.until} />,
              rowExpandable: () => true,
            }}
          />
        )}

        <div style={{ marginTop: 8, fontSize: 12, color: "var(--mb-text-3)" }}>
          {isMobile ? "点型号看逐笔比价与建议。" : "点开型号看逐笔比价（谁采的 / 哪家 / 含税未税）。"}建议：
          <Tag color="gold">批量补库</Tag>频发应急→早会谈量 ·
          <Tag color="blue">谈价</Tag>价格在涨→压价 ·
          <Tag>偶发</Tag>一次性→跳过
        </div>
      </Card>

      <MobileDetailDrawer
        open={detail != null}
        title={detail ? detail.pn_std : ""}
        fields={detail ? [
          { label: "描述", value: detail.description || detail.brand },
          { label: "采购次数", value: String(detail.buy_times) },
          { label: "总量", value: detail.total_qty == null ? null : String(detail.total_qty) },
          { label: "采购价", value: <PriceCell row={detail} basis={basis} /> },
          { label: "来源拆分", value: detail.channels.map((c) => `${c.channel} ${c.times}次`).join("，") },
          { label: "库存", value: "未启用" },
          { label: "建议", value: detail.advice && detail.advice !== "普通" ? <Tag color={ADVICE_COLOR[detail.advice]}>{detail.advice}</Tag> : "—" },
        ] : []}
        onClose={() => setDetail(null)}
      >
        {detail && (
          <div style={{ display: "grid", gap: 12 }}>
            <PoolReferencePanel partId={detail.part_id} side="purchase" range="custom"
              dateFrom={data?.window.since} dateTo={data?.window.until} compact />
            <MobileDrill partId={detail.part_id} days={days} excludeDesignated={excludeDesignated} />
          </div>
        )}
      </MobileDetailDrawer>
    </>
  );
}
