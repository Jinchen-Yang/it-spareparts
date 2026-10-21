/** T2 项目对比：范围内项目全量行（含零行）；免返领用单列不冲减；金额列随权限。 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Card, Select, Space, Table, Typography } from "antd";
import type { ColumnsType, TableProps } from "antd/es/table";
import { Link } from "react-router-dom";
import { TaxMoney } from "../../../context/TaxBasis";
import {
  fetchDashboardProjects,
  type PagedRows,
  type ProjectRow,
} from "../../../api/maintenanceDashboard";
import {
  DashboardExportButtons,
  SORT_LABELS,
  apiErrorMessage,
  qtyText,
  rateText,
  type DashboardTabProps,
} from "./dashboardShared";

const { Text } = Typography;

const PAGE_SIZE_OPTIONS = [20, 50, 100];

/** 率列统一渲染：值 + 分子/分母小字（分母≤0 显示「无率」而不是 0%）。 */
function RateCell({ row }: { row: ProjectRow }) {
  return (
    <Space direction="vertical" size={0}>
      <span>{rateText(row.return_rate_pct)}</span>
      <Text type="secondary" style={{ fontSize: 12 }}>
        {row.return_rate_pct === null
          ? "无率（领用≤0）"
          : `${qtyText(row.rate_numerator)} ÷ ${qtyText(row.rate_denominator)}`}
      </Text>
    </Space>
  );
}

export default function DashboardProjectsTab({
  params, amountsVisible, sorts, sort, page, pageSize, refreshKey,
  onSortChange, onPageChange, maxExportRows,
}: DashboardTabProps) {
  const [data, setData] = useState<PagedRows<ProjectRow> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = seqRef.current + 1;
    seqRef.current = seq;
    setLoading(true);
    setError(null);
    try {
      const resp = await fetchDashboardProjects({
        ...params, sort, page, page_size: pageSize,
      });
      if (seqRef.current !== seq) return;
      setData(resp);
    } catch (err) {
      if (seqRef.current !== seq) return;
      setData(null);
      setError(apiErrorMessage(err));
    } finally {
      if (seqRef.current === seq) setLoading(false);
    }
  }, [params, sort, page, pageSize]);

  useEffect(() => {
    void load();
    return () => { seqRef.current += 1; };
  }, [load, refreshKey]);

  const onTableChange: TableProps<ProjectRow>["onChange"] = (pg) => {
    if (pg.pageSize && pg.pageSize !== pageSize) onPageChange(1, pg.pageSize);
    else if (pg.current && pg.current !== page) onPageChange(pg.current, pageSize);
  };

  const columns: ColumnsType<ProjectRow> = useMemo(() => {
    const base: ColumnsType<ProjectRow> = [
      {
        title: "项目", dataIndex: "display_name", width: 240, fixed: "left",
        render: (v: string, row) => (
          <Space direction="vertical" size={0}>
            <Link to={`/maintenance/projects/${row.project_id}`}>{v}</Link>
            <Text type="secondary" style={{ fontSize: 12 }}>{row.project_code ?? "—"}</Text>
          </Space>
        ),
      },
      { title: "业务类型", dataIndex: "business_type", width: 100,
        render: (v: string | null) => v || "—" },
      { title: "返件实收", dataIndex: "receipt_qty", width: 100, align: "right",
        render: (v: string) => qtyText(v) },
      { title: "坏件实收", dataIndex: "bad_receipt_qty", width: 100, align: "right",
        render: (v: string) => qtyText(v) },
      { title: "其中废品", dataIndex: "scrap_receipt_qty", width: 90, align: "right",
        render: (v: string) => qtyText(v) },
      { title: "实际领用（未采集件况）", dataIndex: "issued_qty", width: 150, align: "right",
        render: (v: string) => qtyText(v) },
      { title: "免返领用量", dataIndex: "exempt_issued_qty", width: 100, align: "right",
        render: (v: string) => qtyText(v) },
      { title: "待判定返还要求量", dataIndex: "pending_requirement_qty", width: 130, align: "right",
        render: (v: string) => qtyText(v) },
      { title: "未关联需求单实收", dataIndex: "unlinked_receipt_qty", width: 130, align: "right",
        render: (v: string) => qtyText(v) },
      { title: "同期返还率", key: "return_rate_pct", width: 150,
        render: (_: unknown, row) => <RateCell row={row} /> },
      { title: "无返还备注", dataIndex: "no_return_note", width: 140, ellipsis: true,
        render: (v: string | null) => v || "—" },
    ];
    if (amountsVisible) {
      base.push(
        {
          title: "坏件估算价值（估算）", key: "valuation", width: 190, align: "right",
          render: (_: unknown, row) => (row.valuation ? (
            <TaxMoney scope="maintenance" exact stack
              inc={Number(row.valuation.bad_estimated_value_inc_tax)}
              ex={Number(row.valuation.bad_estimated_value_ex_tax)} />
          ) : "—"),
        },
        {
          title: "待估坏件量", key: "pending_bad_qty", width: 100, align: "right",
          render: (_: unknown, row) => qtyText(row.valuation?.pending_bad_qty ?? null),
        },
        {
          title: "覆盖率", key: "coverage_pct", width: 90, align: "right",
          render: (_: unknown, row) => rateText(row.valuation?.coverage_pct ?? null),
        },
      );
    }
    return base;
  }, [amountsVisible]);

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {error ? (
        <Alert
          type="error" showIcon message="项目对比数据加载失败" description={error}
          action={<Button size="small" onClick={() => void load()}>重试</Button>}
        />
      ) : null}

      <Space wrap size={12}>
        <Select<string>
          aria-label="项目排序"
          value={sort}
          style={{ width: 180 }}
          options={sorts.map((key) => ({ label: SORT_LABELS[key] ?? key, value: key }))}
          onChange={onSortChange}
        />
        <DashboardExportButtons
          params={params}
          kind="projects"
          maxExportRows={maxExportRows}
        />
      </Space>

      <Card size="small" title="项目对比（含零行项目）">
        <Table<ProjectRow>
          rowKey="project_id"
          size="small"
          loading={loading}
          dataSource={data?.rows ?? []}
          columns={columns}
          onChange={onTableChange}
          scroll={{ x: amountsVisible ? 1680 : 1360 }}
          pagination={{
            current: page,
            pageSize,
            total: data?.pagination.total ?? 0,
            pageSizeOptions: PAGE_SIZE_OPTIONS,
            showSizeChanger: true,
            showTotal: (total) => `共 ${total} 个项目`,
          }}
          locale={{ emptyText: "当前窗口没有授权范围内的项目" }}
        />
      </Card>
    </Space>
  );
}
