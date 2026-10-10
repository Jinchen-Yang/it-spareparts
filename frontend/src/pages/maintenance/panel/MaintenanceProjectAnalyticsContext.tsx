import { Alert, Card, Descriptions, Empty, Skeleton, Table, Typography } from "antd";
import { useSearchParams } from "react-router-dom";
import type { ExplorerRow } from "../../../api/maintenanceAnalytics";
import { EXPLORER_DIMENSIONS, EXPLORER_METRICS, explorerParamsFromSearch, formatExplorerValue, useMaintenanceExplorer } from "../analyticsExplorerModel";

/** 只展示从分析页带来的统计定位；不改变项目原有业务台账的筛选或状态。 */
export default function MaintenanceProjectAnalyticsContext({ projectId, analyticsReturn }: { projectId: string; analyticsReturn: string | null }) {
  const [search] = useSearchParams();
  const source = new URLSearchParams(analyticsReturn ? new URL(analyticsReturn, "https://local.invalid").search : "");
  const params = explorerParamsFromSearch(source);
  params.project = projectId;
  params.page = 1;
  params.focus_page = 1;
  const targetDimension = search.get("analysis_dimension");
  const targetFocus = search.get("analysis_focus");
  if (targetDimension && Object.prototype.hasOwnProperty.call(EXPLORER_DIMENSIONS, targetDimension) && targetFocus && targetFocus.length <= 300) {
    params.dimension = targetDimension as typeof params.dimension;
    params.focus = targetFocus;
  }
  const pnKey = search.get("pnKey");
  if (pnKey && /^(part:[1-9]\d*|pn:.{1,300})$/.test(pnKey)) { params.dimension = "pn"; params.focus = pnKey; }
  const { data, error, loading } = useMaintenanceExplorer(params);
  const metric = params.metric || "issued";
  const selected = data?.focus.row;
  return <section aria-label="项目分析定位">
    <Alert type="info" showIcon message="当前筛选下的分析定位" description="沿用分析页的期间、业务筛选与选中分组，并限定为当前项目。这里的统计不代表项目全周期总量；原业务台账保持自身口径。" style={{ marginBottom: 16 }} />
    {!analyticsReturn && <Alert type="warning" message="未携带分析页筛选，显示本年当前项目的默认分析。" style={{ marginBottom: 16 }} />}
    {loading && !data ? <Skeleton active /> : error ? <Alert type="error" message={error} /> : !selected ? <Empty description="该选中项在当前项目与筛选条件下没有记录，请返回分析重新选择。" /> : <Card title={`${selected.label} · ${EXPLORER_METRICS[metric].label}`}>
      <Typography.Paragraph type="secondary">{EXPLORER_METRICS[metric].note}</Typography.Paragraph>
      <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 3 }} items={[
        { key: "window", label: "统计期间", children: `${data?.window.date_from || "最早记录"} 至 ${data?.window.date_to || "最新记录"}` },
        { key: "issued", label: "实际领用", children: `${formatExplorerValue(selected.issued_qty)} 件` },
        { key: "effective", label: "需求净量", children: `${formatExplorerValue(selected.effective_qty)} 件` },
        { key: "cost", label: "已知需求成本（含税）", children: selected.cost_state === "restricted" ? "无权限" : `${formatExplorerValue(selected.cost_inc, "cost")}${selected.cost_state === "partial" ? "（部分缺价）" : ""}` },
        { key: "orders", label: "需求单数", children: formatExplorerValue(selected.order_count) },
        { key: "missing", label: "缺价明细", children: `${formatExplorerValue(selected.missing_lines)} 行` },
      ]} />
      <Table<ExplorerRow> style={{ marginTop: 16 }} size="small" rowKey="key" dataSource={data?.focus.rows || []} pagination={false} scroll={{ x: 480 }} columns={[
        { title: data?.focus.dimension === "pn" ? "PN" : "项目", dataIndex: "label" },
        { title: EXPLORER_METRICS[metric].label, dataIndex: "value", align: "right", render: (value: string | null) => formatExplorerValue(value, metric) },
        { title: "口径", key: "scope", render: () => "当前期间与筛选" },
      ]} />
      {data && data.focus.total > data.focus.rows.length && <Typography.Paragraph type="secondary">显示前 {data.focus.rows.length} 项；返回分析页可继续翻页查看。</Typography.Paragraph>}
    </Card>}
  </section>;
}
