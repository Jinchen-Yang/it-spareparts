/**
 * 互通池列表：池名/成员数/采购指标/销售指标/越线计数/最近业务日期。
 * 采购、销售指标表头带循环切换按钮（金额合计 ↔ 平均单价）：
 * - 图标 + 文字 + 颜色三重提示，键盘可操作，aria-label 说明当前值与下一次切换目标
 * - 切换后服务端排序字段同步（purchase_total ↔ purchase_average 等）
 * - 「金额合计」与「平均单价」单位不混：列头与单元格都带明确口径
 * 后端排序恒降序（无 order 参数），表头只提供 descend，不伪造升序。
 */
import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Table, Tag, Tooltip } from "antd";
import type { ColumnsType, TablePaginationConfig } from "antd/es/table";
import type { SorterResult } from "antd/es/table/interface";
import { SwapOutlined } from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import { dashboardPools, type PoolListItem, type PoolSort, type PoolsResp } from "../../api";
import { EMPTY, moneyExact } from "../../utils/format";
import { MUTED, useGuardedFetch, type DateRange } from "./shared";

type MetricMode = "total" | "average";
const MODE_LABEL: Record<MetricMode, string> = { total: "金额合计(未税)", average: "平均单价(未税)" };
// 双编码之三：颜色（合计=蓝、均价=紫），文字标签始终在场，色觉障碍不丢信息
const MODE_COLOR: Record<MetricMode, string> = { total: "#3E6FD1", average: "#7A5AC8" };

function metricSort(side: "purchase" | "sales", mode: MetricMode): PoolSort {
  return `${side}_${mode}` as PoolSort;
}

function MetricHeaderToggle({ side, mode, onToggle }: {
  side: "purchase" | "sales"; mode: MetricMode; onToggle: () => void;
}) {
  const sideLabel = side === "purchase" ? "采购" : "销售";
  const next = mode === "total" ? "平均单价" : "金额合计";
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, whiteSpace: "nowrap" }}>
      {sideLabel}
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); onToggle(); }}
        onKeyDown={(e) => e.stopPropagation()}
        aria-label={`${sideLabel}指标：当前显示${MODE_LABEL[mode]}，点击切换为${next}`}
        title={`点击切换为${next}`}
        style={{
          display: "inline-flex", alignItems: "center", gap: 3, cursor: "pointer",
          border: `1px solid ${MODE_COLOR[mode]}`, borderRadius: 4, background: "transparent",
          color: MODE_COLOR[mode], fontSize: 12, padding: "0 6px", lineHeight: "18px",
        }}>
        <SwapOutlined aria-hidden />
        {MODE_LABEL[mode]}
      </button>
    </span>
  );
}

interface PoolsBlockProps {
  dateRange: DateRange;
  scopeNote: string;
  localCostRestricted: boolean;
  localGovernanceRestricted: boolean;
}

export default function PoolsBlock({ dateRange, scopeNote, localCostRestricted, localGovernanceRestricted }: PoolsBlockProps) {
  const navigate = useNavigate();
  const [pMode, setPMode] = useState<MetricMode>("total");
  const [sMode, setSMode] = useState<MetricMode>("total");
  const [sort, setSort] = useState<PoolSort>("savings");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);

  useEffect(() => { setPage(1); }, [dateRange.date_from, dateRange.date_to, sort]);

  const { data, loading, error, reload } = useGuardedFetch<PoolsResp>(
    () => dashboardPools({ ...dateRange, sort, page, page_size: pageSize }),
    [dateRange, sort, page, pageSize]);

  // 表头切换（合计↔均价）时，若该列正是当前排序列 → 排序字段同步切换
  const togglePMode = () => {
    const next: MetricMode = pMode === "total" ? "average" : "total";
    setPMode(next);
    setSort((s) => (s === metricSort("purchase", pMode) ? metricSort("purchase", next) : s));
  };
  const toggleSMode = () => {
    const next: MetricMode = sMode === "total" ? "average" : "total";
    setSMode(next);
    setSort((s) => (s === metricSort("sales", sMode) ? metricSort("sales", next) : s));
  };

  const metricValue = (m: PoolListItem["purchase_metrics"], mode: MetricMode) =>
    mode === "total" ? m?.total_amount : m?.weighted_avg_unit_price;

  const violationCell = (v: number | null, hasLimitLabel: string) => {
    if (v == null) {
      return localGovernanceRestricted
        ? <span style={MUTED}>无权限</span>
        : <Tooltip title={`该池未设${hasLimitLabel}（无约束 ≠ 零越线）`}><span style={MUTED}>未设约束</span></Tooltip>;
    }
    return v > 0 ? <Tag color="red">{v}</Tag> : <span>0</span>;
  };

  const sortProps = (key: PoolSort) => ({
    sorter: true,
    sortDirections: ["descend" as const],
    sortOrder: (data?.effective_sort ?? sort) === key && sort === key ? ("descend" as const) : null,
  });

  const latestBiz = (r: PoolListItem) => {
    const d1 = r.purchase_metrics?.latest_date || "";
    const d2 = r.sales_metrics?.latest_date || "";
    const m = d1 > d2 ? d1 : d2;
    return m || EMPTY;
  };

  const cols: ColumnsType<PoolListItem> = [
    { title: "池名", key: "name", width: 170, ellipsis: true,
      render: (_, r) => (
        <a onClick={() => navigate(`/pool-analysis/${r.group_id}?from=${dateRange.date_from ?? ""}&to=${dateRange.date_to ?? ""}`)}
          aria-label={`进入池「${r.name || r.group_id}」分析详情`}>
          {r.name || `池 #${r.group_id}`}
        </a>) },
    { title: "成员", dataIndex: "member_count", key: "member_count", width: 68, align: "right",
      ...sortProps("member_count") },
    { title: <MetricHeaderToggle side="purchase" mode={pMode} onToggle={togglePMode} />,
      key: metricSort("purchase", pMode), width: 150, align: "right",
      ...sortProps(metricSort("purchase", pMode)),
      render: (_, r) => {
        const v = metricValue(r.purchase_metrics, pMode);
        if (v == null && localCostRestricted) return <span style={MUTED}>无成本权限</span>;
        return v == null ? <span style={MUTED}>{EMPTY}</span>
          : <span style={{ color: MODE_COLOR[pMode] }}>{moneyExact(v)}</span>;
      } },
    { title: <MetricHeaderToggle side="sales" mode={sMode} onToggle={toggleSMode} />,
      key: metricSort("sales", sMode), width: 150, align: "right",
      ...sortProps(metricSort("sales", sMode)),
      render: (_, r) => {
        const v = metricValue(r.sales_metrics, sMode);
        // 销售 total_amount 与成本键同名，对 cost-blind 账号被后端有意过遮（契约既定取舍）
        if (v == null && sMode === "total" && localCostRestricted) {
          return <span style={MUTED}>无成本权限</span>;
        }
        return v == null ? <span style={MUTED}>{EMPTY}</span>
          : <span style={{ color: MODE_COLOR[sMode] }}>{moneyExact(v)}</span>;
      } },
    { title: "采购超限", key: "purchase_violation_count", width: 92, align: "right",
      ...sortProps("purchase_violation_count"),
      render: (_, r) => violationCell(r.purchase_violation_count, "采购最高价") },
    { title: "销售低限", key: "sale_violation_count", width: 92, align: "right",
      ...sortProps("sale_violation_count"),
      render: (_, r) => violationCell(r.sale_violation_count, "销售最低价") },
    { title: "最近业务", key: "latest", width: 100, render: (_, r) => latestBiz(r) },
    { title: "理论节省上限", dataIndex: "theoretical_saving", key: "savings", width: 116, align: "right",
      ...sortProps("savings"),
      render: (v) => v == null
        ? (localCostRestricted ? <span style={MUTED}>无成本权限</span> : <span style={MUTED}>{EMPTY}</span>)
        : <span style={{ color: "#9a7b43" }}>{moneyExact(v)}</span> },
    { title: "标记", key: "flags", width: 130, render: (_, r) => (
      <span>
        {r.needs_calibration && <Tag color="orange">关系待校准</Tag>}
        {r.oversized && <Tag color="red">超限</Tag>}
      </span>) },
  ];

  const onChange = (pag: TablePaginationConfig, _f: unknown,
                    sorter: SorterResult<PoolListItem> | SorterResult<PoolListItem>[]) => {
    const s = Array.isArray(sorter) ? sorter[0] : sorter;
    setPage(pag.current || 1);
    setPageSize(pag.pageSize || pageSize);
    if (s?.order && s.columnKey) setSort(String(s.columnKey) as PoolSort);
    else if (s && !s.order) setSort("savings");   // 取消排序回默认
  };

  return (
    <Card size="small" style={{ marginBottom: 16 }} title="互通池列表"
      extra={<span style={MUTED}>{scopeNote}</span>}>
      <Alert type="info" showIcon style={{ marginBottom: 10 }}
        message="只读分析：池指标按当前时间窗口统计；越线计数=窗口内严格越过人工约束价的行数（等于不算）。" />
      {data?.ranking_capped && (
        <Alert type="warning" showIcon style={{ marginBottom: 10 }}
          message="池数量超过分析上限，已退回「按成员数排序」——当前非按节省金额全局排名。" />
      )}
      {data?.ranking_restricted && (
        <Alert type="info" showIcon style={{ marginBottom: 10 }}
          message="当前账号权限不足以按该指标排序，已退回默认排序。" />
      )}
      {error ? (
        <Alert type="error" showIcon message={`互通池列表加载失败：${error}`}
          action={<Button size="small" onClick={reload}>重试</Button>} />
      ) : (
        <Table<PoolListItem>
          size="small" rowKey="group_id" loading={loading}
          dataSource={data?.items || []} columns={cols} scroll={{ x: 1100 }}
          locale={{ emptyText: "当前时间范围内暂无有效池" }}
          onChange={onChange}
          pagination={{
            current: data?.page ?? 1, pageSize, total: data?.total ?? 0,
            showSizeChanger: true, pageSizeOptions: [10, 20, 50],
            showTotal: (t) => `共 ${t} 个池`,
          }} />
      )}
    </Card>
  );
}
