import { useCallback, useMemo } from "react";
import { Alert, Button, Card, Empty, Pagination, Select, Skeleton, Space, Table, Tag, Typography } from "antd";
import { ArrowRightOutlined, BarChartOutlined, FundOutlined, TableOutlined } from "@ant-design/icons";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import type { ExplorerMetric, ExplorerRow, MaintenanceExplorerResponse } from "../../api/maintenanceAnalytics";
import { readPermissionMap } from "../../nav";
import { useVisibleMaintenanceRefresh } from "../../utils/maintenanceRefresh";
import { EXPLORER_DIMENSIONS, EXPLORER_METRICS, explorerParamsFromSearch, formatExplorerValue as fmt, useMaintenanceExplorer } from "./analyticsExplorerModel";
import "./maintenanceRankingsExplorer.css";

type ExplorerData = MaintenanceExplorerResponse;
const layouts = [ { key: "A", label: "分栏钻取", icon: <BarChartOutlined /> }, { key: "B", label: "Pareto 排列图", icon: <FundOutlined /> }, { key: "C", label: "项目热力矩阵", icon: <TableOutlined /> } ];
const percent = (value: string | number | null) => value == null ? "—" : `${Number(value).toFixed(1)}%`;
const valueNumber = (value: string | null) => value == null ? 0 : Number(value);
const selectedCost = (row: ExplorerRow) => row.cost_state === "restricted" ? "无权限" : `${fmt(row.cost_inc, "cost")}${row.cost_state === "partial" ? "（部分缺价）" : ""}`;

export default function MaintenanceRankingsExplorer({ refreshKey = 0 }: { refreshKey?: number }) {
  const [search, setSearch] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const params = explorerParamsFromSearch(search);
  const { data, loading, error, reload } = useMaintenanceExplorer(params, refreshKey);
  useVisibleMaintenanceRefresh(async () => { if (loading) return false; reload(); return true; });
  const metric = params.metric || "issued";
  const dimension = params.dimension || "pn";
  const layout = search.get("layout") === "B" ? "B" : search.get("layout") === "C" ? "C" : "A";
  const perms = readPermissionMap();
  const canCost = !!perms.data_purchase_cost && data?.meta.cost_visibility !== "restricted";
  const canCustomer = !!perms.data_customer && data?.meta.customer_visibility !== "restricted";
  const patch = useCallback((changes: Record<string, string | null>) => setSearch(previous => {
    const next = new URLSearchParams(previous);
    Object.entries(changes).forEach(([key, value]) => value == null ? next.delete(key) : next.set(key, value));
    return next;
  }, { replace: true }), [setSearch]);
  const focus = (row: ExplorerRow) => patch({ focus: row.key, focus_page: null });
  const goProject = (projectId: string | null, origin: ExplorerRow | null, pnKey?: string) => {
    if (!projectId || projectId === "__unassigned__") return;
    const target = new URLSearchParams({ tab: "analytics", return: `${location.pathname}${location.search}` });
    if (origin) { target.set("analysis_dimension", dimension); target.set("analysis_focus", origin.key); }
    if (pnKey) target.set("pnKey", pnKey);
    navigate(`/maintenance/projects/${encodeURIComponent(projectId)}?${target}`);
  };
  const detailClick = (row: ExplorerRow) => {
    if (!data?.focus.row) return;
    if (dimension === "project") goProject(data.focus.row.project_id, data.focus.row, row.key);
    else goProject(row.project_id, data.focus.row, dimension === "pn" ? data.focus.row.key : undefined);
  };
  const selected = data?.focus.row;
  const cumulativeEnabled = !!data?.meta.additive && !data.meta.has_negative && data.summary.value != null && Number(data.summary.value) > 0;
  const chartShare = data?.summary.top_share_pct ?? null;
  return <div className="maintenance-explorer">
    <div className="me-toolbar">
      <div className="me-dimensions" role="group" aria-label="排名维度">
        {Object.entries(EXPLORER_DIMENSIONS).filter(([key]) => key !== "customer" || canCustomer).map(([key, label]) => <Button key={key} type={dimension === key ? "primary" : "default"} aria-pressed={dimension === key} onClick={() => patch({ dim: key, focus: null, focus_page: null, page: null })}>{label}</Button>)}
      </div>
      <Space wrap>
        <Select aria-label="排名指标" value={metric} style={{ minWidth: 170 }} options={Object.entries(EXPLORER_METRICS).filter(([key]) => key !== "cost" || canCost).map(([value, info]) => ({ value, label: info.label }))} onChange={value => patch({ metric: value, focus_page: null, page: null })} />
        <Select aria-label="图表展示数量" value={params.top_n} options={[12, 20, 30].map(value => ({ value, label: `Top ${value}` }))} onChange={value => patch({ top: String(value) })} />
      </Space>
    </div>
    <div className="me-layouts" role="group" aria-label="图表方式">
      {layouts.map(item => <Button key={item.key} aria-label={item.label} icon={item.icon} type={layout === item.key ? "primary" : "default"} aria-pressed={layout === item.key} onClick={() => patch({ layout: item.key })}>{item.label}</Button>)}
    </div>
    <Typography.Paragraph type="secondary" className="me-method">{EXPLORER_METRICS[metric].note} 点击排名查看分布，再点击项目进入“分析定位”；返回时保留全部筛选。</Typography.Paragraph>
    {loading && !data ? <Card><Skeleton active paragraph={{ rows: 6 }} /></Card> : error ? <Alert type="error" showIcon message={error} action={<Button onClick={reload}>重试</Button>} /> : data && <>
      <div className="me-kpis">
        <Kpi label="实际领用" value={`${fmt(data.summary.issued_qty)} 件`} note="当前期间的确认领用" />
        <Kpi label="已知需求成本（含税）" value={data.summary.cost_state === "restricted" ? "无权限" : fmt(data.summary.cost_inc, "cost")} note={`${fmt(data.summary.missing_lines)} 行缺价，未当作零元`} />
        <Kpi label="涉及项目 / PN" value={`${fmt(data.summary.project_count)} / ${fmt(data.summary.pn_count)}`} note="当前筛选范围内" />
        <Kpi label="Top 10 占比" value={cumulativeEnabled ? percent(chartShare) : "不计算"} note={!data.meta.additive ? "去重需求单在组间可能重叠" : data.meta.has_negative ? "包含负值，关闭集中度" : "分母为筛选全集，非当前页"} />
      </div>
      <div className="me-scope"><span>{data.window.date_from || "最早记录"} — {data.window.date_to || "最新记录"} · 共 {fmt(data.total)} 项{loading ? " · 更新中…" : ""}</span><span>金额含税 · 数量最多 3 位小数</span></div>
      {!data.total ? <Card><Empty description="当前期间和筛选条件下没有记录，请调整筛选。" /></Card> : <>
        {layout === "A" && <div className="me-split">
          <Card title={EXPLORER_DIMENSIONS[dimension]} extra={<Tag>{EXPLORER_METRICS[metric].label}</Tag>}><Typography.Paragraph type="secondary">点击一项，查看{dimension === "project" ? "PN" : "项目"}分布</Typography.Paragraph><RankingList rows={data.chart_rows} metric={metric} selected={selected?.key} onSelect={focus} /><div className="me-caption">全范围排序 · 图表固定显示 Top {data.chart_rows.length}，不随明细翻页改变。</div></Card>
          <Distribution data={data} metric={metric} onRow={detailClick} onPage={page => patch({ focus_page: String(page) })} />
        </div>}
        {layout === "B" && <>
          <Card title={`${EXPLORER_DIMENSIONS[dimension]} · 贡献与集中度`}><ParetoChart data={data} metric={metric} onSelect={focus} /><div className="me-caption">{!data.meta.additive ? "各组需求单可能重叠，仅比较规模，不计算累计占比。" : data.meta.has_negative ? "保留正负值的真实方向；包含负值时不计算累计占比。" : `仅绘制前 ${data.chart_rows.length} 项，累计占比分母为全部 ${data.total} 项。未展示部分不拼成一根大柱。`}</div></Card>
          <Distribution data={data} metric={metric} onRow={detailClick} onPage={page => patch({ focus_page: String(page) })} />
        </>}
        {layout === "C" && <>
          <Card title="项目热力矩阵" extra={<Tag>颜色按当前可见单元格</Tag>}><HeatMatrix data={data} metric={metric} onFocus={focus} onCell={(row, column) => {
            if (data.matrix.column_dimension === "pn") goProject(row.project_id, row, column.key);
            else goProject(column.project_id, row, dimension === "pn" ? row.key : undefined);
          }} /><div className="me-caption">只展示前 {data.matrix.rows.length} 项与前 {data.matrix.columns.length} 个{data.matrix.column_dimension === "pn" ? " PN" : "项目"}。空白表示没有对应记录，未知成本单独标记；点击有记录的单元格进入项目。</div></Card>
          <Distribution data={data} metric={metric} onRow={detailClick} onPage={page => patch({ focus_page: String(page) })} />
        </>}
        <Card title={`排名明细 · ${fmt(data.total)} 项`} extra={<Typography.Text type="secondary">全范围排序</Typography.Text>}>
          <Table<ExplorerRow> rowKey="key" size="small" dataSource={data.rows} scroll={{ x: 960 }} rowClassName={row => row.key === selected?.key ? "me-selected-row" : ""} columns={[
            { title: "排名", width: 64, key: "rank", render: (_: unknown, __: ExplorerRow, index: number) => (data.page - 1) * data.page_size + index + 1 },
            { title: EXPLORER_DIMENSIONS[dimension], key: "entity", width: 260, render: (_: unknown, row: ExplorerRow) => <button className="me-name-button" onClick={() => focus(row)}><strong>{row.label}</strong>{row.subtitle && <small>{row.subtitle}</small>}</button> },
            { title: "实际领用 / 件", dataIndex: "issued_qty", align: "right", render: (value: string) => fmt(value) },
            { title: "需求净量 / 件", dataIndex: "effective_qty", align: "right", render: (value: string) => fmt(value) },
            { title: "需求单数", dataIndex: "order_count", align: "right", render: (value: number) => fmt(value) },
            ...(canCost ? [{ title: "已知需求成本 / 元", key: "cost", align: "right" as const, render: (_: unknown, row: ExplorerRow) => selectedCost(row) }] : []),
            { title: "覆盖项目", dataIndex: "project_count", align: "right" },
            { title: "缺价行数", dataIndex: "missing_lines", align: "right" },
            { title: "操作", key: "action", render: (_: unknown, row: ExplorerRow) => <Button type="link" size="small" onClick={() => focus(row)}>查看分布</Button> },
          ]} pagination={{ current: data.page, pageSize: data.page_size, total: data.total, pageSizeOptions: [20, 50, 100], showSizeChanger: true, onChange: (page, size) => patch({ page: size === data.page_size ? String(page) : "1", ps: String(size) }), showTotal: total => `共 ${total} 项` }} />
        </Card>
      </>}
      <Typography.Paragraph type="secondary" className="me-caption">三种图与明细来自同一次后端统计。统计时点：{data.meta.as_of}。客户按关联需求单归属，无法关联时归入“未明确”；销售以项目主档有效归属为先，未设置且未人工覆盖时回退来源需求单。</Typography.Paragraph>
    </>}
  </div>;
}

function Kpi({ label, value, note }: { label: string; value: string; note: string }) {
  return <Card size="small" className="me-kpi"><div className="me-kpi-label">{label}</div><strong>{value}</strong><div className="me-caption">{note}</div></Card>;
}

function RankingList({ rows, metric, selected, onSelect, disabled, offset = 0 }: { rows: ExplorerRow[]; metric: ExplorerMetric; selected?: string; onSelect: (row: ExplorerRow) => void; disabled?: (row: ExplorerRow) => boolean; offset?: number }) {
  const maximum = Math.max(0, ...rows.map(row => valueNumber(row.value)));
  const minimum = Math.min(0, ...rows.map(row => valueNumber(row.value)));
  const span = maximum - minimum || 1;
  const zero = -minimum / span * 100;
  return <div className="me-ranking">{rows.map((row, index) => {
    const amount = valueNumber(row.value);
    const isDisabled = disabled?.(row) || false;
    return <button key={row.key} type="button" className={`me-rank-row ${row.key === selected ? "is-selected" : ""}`} aria-pressed={selected === undefined ? undefined : row.key === selected} aria-label={`${row.label}，${EXPLORER_METRICS[metric].label} ${fmt(row.value, metric)}${isDisabled ? "，未归属项目" : "，查看详情"}`} disabled={isDisabled} onClick={() => onSelect(row)}>
      <span className="me-rank-index">{String(offset + index + 1).padStart(2, "0")}</span><span className="me-rank-label"><strong title={row.label}>{row.label}</strong><small title={row.subtitle}>{row.subtitle || `${row.project_count} 个项目 · ${row.pn_count} 个 PN`}</small><span className="me-rank-track" aria-hidden="true"><i className="me-zero-line" style={{ left: `${zero}%` }} /><i className={amount < 0 ? "is-negative" : ""} style={{ left: `${amount < 0 ? zero + amount / span * 100 : zero}%`, width: `${Math.abs(amount) / span * 100}%` }} /></span></span><span className="me-rank-value">{fmt(row.value, metric)}{metric === "cost" && row.cost_state === "partial" && <small>部分缺价</small>}{row.share_pct != null && <small>{percent(row.share_pct)}</small>}{!isDisabled && <ArrowRightOutlined />}</span>
    </button>;
  })}</div>;
}

function Distribution({ data, metric, onRow, onPage }: { data: ExplorerData; metric: ExplorerMetric; onRow: (row: ExplorerRow) => void; onPage: (page: number) => void }) {
  const { focus } = data;
  return <Card className="me-distribution" title={focus.dimension === "pn" ? "项目内 PN 分布" : "项目使用分布"}>
    {!focus.row ? <Empty description="选中项在当前筛选中没有记录，请从排名中重新选择。" /> : <>
      <div className="me-focus-heading"><div><strong>{focus.row.label}</strong><p>{focus.row.subtitle}</p></div><Tag>{fmt(focus.row.value, metric)} {EXPLORER_METRICS[metric].unit}</Tag></div>
      <Typography.Paragraph type="secondary">{focus.dimension === "pn" ? "点击 PN，进入当前项目的分析定位。" : "点击项目条形，进入该项目并保留当前筛选。"} 共 {focus.total} 项。{metric === "cost" ? "占比只覆盖已知成本。" : ""}</Typography.Paragraph>
      {!focus.rows.length ? <Empty description="当前选中项暂无可展示分布" /> : <RankingList rows={focus.rows} metric={metric} offset={(focus.page - 1) * focus.page_size} onSelect={onRow} disabled={row => focus.dimension === "project" ? !row.project_id : !focus.row?.project_id} />}
      {focus.total > focus.page_size && <Pagination size="small" current={focus.page} pageSize={focus.page_size} total={focus.total} onChange={onPage} showSizeChanger={false} />}
      {focus.rows.some(row => focus.dimension === "project" && !row.project_id) && <div className="me-caption">未归属项目保留统计，不提供无效跳转。</div>}
    </>}
  </Card>;
}

function ParetoChart({ data, metric, onSelect }: { data: ExplorerData; metric: ExplorerMetric; onSelect: (row: ExplorerRow) => void }) {
  const rows = data.chart_rows;
  const width = Math.max(640, rows.length * 74 + 110);
  const height = 330, left = 70, right = width - 45, top = 32, bottom = 255;
  const max = Math.max(0, ...rows.map(row => valueNumber(row.value))) || 1;
  const min = Math.min(0, ...rows.map(row => valueNumber(row.value)));
  const y = (value: number) => bottom - (value - min) / (max - min) * (bottom - top);
  const step = (right - left) / Math.max(1, rows.length);
  const cumulative = data.meta.additive && !data.meta.has_negative && Number(data.summary.value || 0) > 0;
  const points = rows.flatMap((row, index) => row.cumulative_share_pct == null ? [] : [`${left + step * (index + .5)},${bottom - Number(row.cumulative_share_pct) / 100 * (bottom - top)}`]);
  return <><div className="me-chart-legend"><span><i className="me-bar-key" />条形：{EXPLORER_METRICS[metric].label}（{EXPLORER_METRICS[metric].unit}）</span>{cumulative && <span><i className="me-line-key" />金线：累计占比（分母为筛选全集）</span>}<span className="me-scroll-hint">可横向滚动查看更多</span></div><div className="me-chart-scroll"><svg width={width} height={height} role="group" aria-label="排名排列图">
    {[0, .5, 1].map(tick => <g key={tick}><line x1={left} x2={right} y1={top + tick * (bottom - top)} y2={top + tick * (bottom - top)} stroke="#e8edf4" /><text x={left - 10} y={top + tick * (bottom - top) + 4} textAnchor="end" fontSize={11} fill="#69778b">{new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: max < 1 ? 3 : 1 }).format(max - tick * (max - min))}</text>{cumulative && <text x={right + 8} y={top + tick * (bottom - top) + 4} fontSize={11} fill="#ba7930">{(1 - tick) * 100}%</text>}</g>)}
    <line x1={left} x2={right} y1={y(0)} y2={y(0)} stroke="#adbaca" />
    {rows.map((row, index) => { const value = valueNumber(row.value); const x = left + step * (index + .5); return <g key={row.key} role="button" tabIndex={0} aria-label={`${row.label}，${EXPLORER_METRICS[metric].label} ${fmt(row.value, metric)}，查看分布`} onClick={() => onSelect(row)} onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(row); } }} className="me-svg-bar"><title>{row.label}：{fmt(row.value, metric)}</title>{row.value != null && value !== 0 && <rect x={x - step * .31} y={Math.min(y(0), y(value))} width={step * .62} height={Math.abs(y(value) - y(0))} rx={3} fill={value < 0 ? "#b85c50" : "#507dcc"} />}<text x={x} y={285} textAnchor="middle" fontSize={10} fill="#526075">{row.label.length > 9 ? `${row.label.slice(0, 8)}…` : row.label}</text><text x={x} y={305} textAnchor="middle" fontSize={10} fill="#526075">{fmt(row.value, metric)}</text></g>; })}
    {cumulative && <><polyline aria-label="累计占比" data-testid="pareto-cumulative" points={points.join(" ")} fill="none" stroke="#be853a" strokeWidth={2.5} pointerEvents="none" />{points.map((point, index) => { const [cx, cy] = point.split(","); return <circle key={index} cx={cx} cy={cy} r={3} fill="#be853a" pointerEvents="none" />; })}</>}
  </svg></div></>;
}

function HeatMatrix({ data, metric, onFocus, onCell }: { data: ExplorerData; metric: ExplorerMetric; onFocus: (row: ExplorerRow) => void; onCell: (row: ExplorerRow, column: ExplorerRow) => void }) {
  const { matrix } = data;
  const cells = useMemo(() => new Map(matrix.cells.map(cell => [`${cell.row_key}\0${cell.column_key}`, cell])), [matrix.cells]);
  const max = Math.max(Math.abs(Number(matrix.scale_max)), Math.abs(Number(matrix.scale_min)));
  const colorSpan = max > 0 ? max : 1;
  return <div className="me-matrix-scroll"><table className="me-matrix"><caption className="me-visually-hidden">点击有记录的单元格查看对应项目；行是排名分组，列是{matrix.column_dimension === "pn" ? "PN" : "项目"}。</caption><thead><tr><th scope="col">{EXPLORER_DIMENSIONS[data.dimension]}</th>{matrix.columns.map(column => <th scope="col" key={column.key} title={column.label}>{column.label}</th>)}</tr></thead><tbody>{matrix.rows.map(row => <tr key={row.key}><th scope="row"><button className="me-name-button" onClick={() => onFocus(row)}>{row.label}</button></th>{matrix.columns.map(column => {
    const cell = cells.get(`${row.key}\0${column.key}`);
    const projectId = matrix.column_dimension === "pn" ? row.project_id : column.project_id;
    const magnitude = cell?.value == null ? 0 : Math.min(1, Math.abs(Number(cell.value)) / colorSpan);
    const negative = Number(cell?.value) < 0;
    const background = cell?.value == null ? "#f2f3f5" : `rgba(${negative ? "174,75,60" : "62,111,209"},${.08 + magnitude * .82})`;
    const text = !cell ? "—" : cell.cost_state === "restricted" && metric === "cost" ? "无权限" : fmt(cell.value, metric);
    return <td key={column.key}><button disabled={!cell || !projectId} style={{ background, color: magnitude > .65 ? "white" : "#2c405b" }} aria-label={`${row.label} × ${column.label}：${text}${!projectId ? "，未归属项目" : "，查看项目"}`} title={`${row.label} × ${column.label}：${text}`} onClick={() => onCell(row, column)}>{text}{metric === "cost" && cell?.cost_state === "partial" && <small>部分缺价</small>}</button></td>;
  })}</tr>)}</tbody></table><div className="me-heat-legend"><span>低</span><i /><span>高 · 可见最大绝对值 {fmt(String(max), metric)}</span>{Number(matrix.scale_min) < 0 && <span> · 红色为负值</span>}</div></div>;
}
