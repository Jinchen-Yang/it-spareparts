/** T2 项目对比：范围内项目全量行（含零行）；免返领用单列不冲减；金额列随权限。
 *
 * R34：期间下单（M01）按 project_id 并入对比表（数量域，人人可见）；
 * 坏件试算价值列在未设比例时显示「待试算」（awaiting_ratio），不拿 0 冒充。
 * 契约 v2：全待估项目 state=pending、无值键（B-F06，不是 ready+¥0.00）；
 * 混合单位 → 平面 null + 单位分桶（单位列全角色；率不可加总，B-F09）；
 * 三域返件估值覆盖列（B-F07，返件估值口径，利润门）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Card, Select, Space, Table, Typography } from "antd";
import type { ColumnsType, TableProps } from "antd/es/table";
import { Link } from "react-router-dom";
import { TaxMoney } from "../../../context/TaxBasis";
import {
  fetchDashboardOrders,
  fetchDashboardProjects,
  type OrderRow,
  type PagedRows,
  type ProjectRow,
} from "../../../api/maintenanceDashboard";
import type { ReceiptsDrill } from "./ReceiptsDrillDrawer";
import {
  AWAITING_RATIO_TEXT,
  DashboardExportButtons,
  DomainCoverageLines,
  PENDING_ESTIMATE_TEXT,
  SORT_LABELS,
  apiErrorMessage,
  drillByProject,
  qtyOnlyParams,
  QtyBreakdownCell,
  qtyText,
  rateText,
  sessionToken,
  sessionUnchanged,
  unitBucketText,
  unitColumnText,
  type DashboardTabProps,
} from "./dashboardShared";

const { Text } = Typography;

const PAGE_SIZE_OPTIONS = [20, 50, 100];

/** 率列统一渲染：值 + 分子/分母小字（分母≤0 显示「无率」而不是 0%；
 * 混合单位（分子分母平面 null）→ 每单位真实分子/分母/率（B2-F03，单位
 * 条目未带率字段就不臆造）+ 率不可加总提示，不是 0%）。 */
function RateCell({ row }: { row: ProjectRow }) {
  const mixed = row.return_rate_pct === null
    && row.rate_numerator === null
    && row.rate_denominator === null;
  const rateBuckets = mixed
    ? (row.unit_breakdown ?? []).filter(
      (e) => e.return_rate_pct !== undefined
        || e.rate_numerator !== undefined
        || e.rate_denominator !== undefined,
    )
    : [];
  return (
    <Space direction="vertical" size={0}>
      <span>{rateText(row.return_rate_pct)}</span>
      {rateBuckets.map((e) => (
        <Text key={e.unit ?? "__unresolved__"} type="secondary" style={{ fontSize: 12 }}>
          {unitBucketText(e.unit)} {e.return_rate_pct != null
            ? `${rateText(e.return_rate_pct)}（${qtyText(e.rate_numerator)} ÷ ${qtyText(e.rate_denominator)}）`
            : `无率（${qtyText(e.rate_numerator)} ÷ ${qtyText(e.rate_denominator)}）`}
        </Text>
      ))}
      <Text type="secondary" style={{ fontSize: 12 }}>
        {row.return_rate_pct === null
          ? (mixed ? "混合单位，率不可加总" : "无率（领用≤0）")
          : `${qtyText(row.rate_numerator)} ÷ ${qtyText(row.rate_denominator)}`}
      </Text>
    </Space>
  );
}

export default function DashboardProjectsTab({
  params, amountsVisible, sorts, sort, page, pageSize, refreshKey,
  onSortChange, onPageChange, maxExportRows, ownerToken, onSessionMismatch, onReceiptsDrill,
}: DashboardTabProps & {
  /** M-AC09：行内「返件明细」→ 壳层 Drawer（原筛选 ∩ 该项目，DL-022）。 */
  onReceiptsDrill?: (drill: ReceiptsDrill) => void;
}) {
  const [data, setData] = useState<PagedRows<ProjectRow> | null>(null);
  const [ordersByProject, setOrdersByProject] = useState<Map<string, OrderRow> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    // 请求前绑定组件所属会话（B-F03/04）：失配即上报壳层停用，不发旧会话请求。
    if (!sessionUnchanged(ownerToken)) {
      onSessionMismatch();
      return;
    }
    const seq = seqRef.current + 1;
    seqRef.current = seq;
    const token = sessionToken();
    setLoading(true);
    setError(null);
    try {
      // M01 下单按 project_id 并入对比（orders 端点不收 q/ratio，显式剥离）。
      const [resp, ordersResp] = await Promise.all([
        fetchDashboardProjects({ ...params, sort, page, page_size: pageSize }),
        fetchDashboardOrders(qtyOnlyParams(params)),
      ]);
      if (seqRef.current !== seq) return;
      if (!sessionUnchanged(token)) { onSessionMismatch(); return; }
      setData(resp);
      setOrdersByProject(new Map(ordersResp.rows.map((row) => [row.project_id, row])));
    } catch (err) {
      if (seqRef.current !== seq) return;
      if (!sessionUnchanged(token)) { onSessionMismatch(); return; }
      setData(null);
      setOrdersByProject(null);
      setError(apiErrorMessage(err));
    } finally {
      if (seqRef.current === seq && sessionUnchanged(token)) setLoading(false);
    }
  }, [params, sort, page, pageSize, ownerToken, onSessionMismatch]);

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
      { title: "单位", key: "unit", width: 90,
        render: (_: unknown, row) => unitColumnText(row.unit, row.unit_breakdown) },
      {
        title: "期间下单量（原qty）", key: "order_qty", width: 140, align: "right",
        render: (_: unknown, row) => {
          const order = ordersByProject?.get(row.project_id);
          return (
            <QtyBreakdownCell
              flat={order?.order_qty ?? null}
              breakdown={order?.unit_breakdown}
              pick={(entry) => entry.order_qty} />
          );
        },
      },
      {
        title: "期间下单单数", key: "order_count", width: 110, align: "right",
        render: (_: unknown, row) => {
          const order = ordersByProject?.get(row.project_id);
          return order ? qtyText(String(order.order_count)) : "0";
        },
      },
      { title: "返件实收", dataIndex: "receipt_qty", width: 100, align: "right",
        render: (v: string | null, row) => (
          <QtyBreakdownCell flat={v} breakdown={row.unit_breakdown} pick={(e) => e.receipt_qty} />
        ) },
      { title: "坏件实收", dataIndex: "bad_receipt_qty", width: 100, align: "right",
        render: (v: string | null, row) => (
          <QtyBreakdownCell flat={v} breakdown={row.unit_breakdown} pick={(e) => e.bad_receipt_qty} />
        ) },
      { title: "其中废品", dataIndex: "scrap_receipt_qty", width: 90, align: "right",
        render: (v: string | null, row) => (
          <QtyBreakdownCell flat={v} breakdown={row.unit_breakdown} pick={(e) => e.scrap_receipt_qty} />
        ) },
      { title: "实际领用（未采集件况）", dataIndex: "issued_qty", width: 150, align: "right",
        render: (v: string | null, row) => (
          <QtyBreakdownCell flat={v} breakdown={row.unit_breakdown} pick={(e) => e.issued_qty} />
        ) },
      { title: "免返领用量", dataIndex: "exempt_issued_qty", width: 100, align: "right",
        render: (v: string | null, row) => (
          <QtyBreakdownCell flat={v} breakdown={row.unit_breakdown} pick={(e) => e.exempt_issued_qty} />
        ) },
      { title: "待判定返还要求量", dataIndex: "pending_requirement_qty", width: 130, align: "right",
        render: (v: string | null, row) => (
          <QtyBreakdownCell flat={v} breakdown={row.unit_breakdown} pick={(e) => e.pending_requirement_qty} />
        ) },
      { title: "未关联需求单实收", dataIndex: "unlinked_receipt_qty", width: 130, align: "right",
        render: (v: string | null, row) => (
          <QtyBreakdownCell flat={v} breakdown={row.unit_breakdown} pick={(e) => e.unlinked_receipt_qty} />
        ) },
      { title: "同期返还率", key: "return_rate_pct", width: 150,
        render: (_: unknown, row) => <RateCell row={row} /> },
      { title: "无返还备注", dataIndex: "no_return_note", width: 140, ellipsis: true,
        render: (v: string | null) => v || "—" },
    ];
    if (onReceiptsDrill) {
      base.push({
        title: "明细", key: "receipts-drill", width: 100, fixed: "right",
        render: (_: unknown, row) => (
          <Button
            size="small"
            aria-label={`查看 ${row.display_name} 返件明细`}
            onClick={() => onReceiptsDrill({
              title: row.display_name,
              params: drillByProject(params, row.project_id),
            })}
          >
            返件明细
          </Button>
        ),
      });
    }
    if (amountsVisible) {
      base.push(
        {
          title: "坏件试算价值（估算/试算）", key: "valuation", width: 190, align: "right",
          render: (_: unknown, row) => {
            const valuation = row.valuation;
            if (!valuation) return "—";
            if (valuation.bad_estimated_value_inc_tax !== undefined) {
              return (
                <TaxMoney scope="maintenance" exact stack
                  inc={Number(valuation.bad_estimated_value_inc_tax)}
                  ex={Number(valuation.bad_estimated_value_ex_tax ?? "0")} />
              );
            }
            if (valuation.state === "pending") {
              return <span data-testid="project-valuation-pending">{PENDING_ESTIMATE_TEXT}</span>;
            }
            return <span data-testid="project-valuation-awaiting">{AWAITING_RATIO_TEXT}</span>;
          },
        },
        {
          title: "待估坏件量", key: "pending_bad_qty", width: 100, align: "right",
          render: (_: unknown, row) => qtyText(row.valuation?.pending_bad_qty ?? null),
        },
        {
          title: "待试算坏件量", key: "awaiting_ratio_bad_qty", width: 110, align: "right",
          render: (_: unknown, row) => qtyText(row.valuation?.awaiting_ratio_bad_qty ?? null),
        },
        {
          title: "覆盖率", key: "coverage_pct", width: 90, align: "right",
          render: (_: unknown, row) => (
            <span data-testid="project-coverage">{rateText(row.valuation?.coverage_pct ?? null)}</span>
          ),
        },
        {
          // B-F07：三域各自返件估值覆盖（bad_receipts 口径；与三域参考列的
          // 领用已计覆盖 issued_lines 口径是两个事实）
          title: "三域覆盖（返件估值）", key: "domains", width: 260,
          render: (_: unknown, row) => (
            row.valuation?.domains ? <DomainCoverageLines domains={row.valuation.domains} /> : "—"
          ),
        },
      );
    }
    return base;
  }, [amountsVisible, ordersByProject, onReceiptsDrill, params]);

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
          ownerToken={ownerToken}
          onSessionMismatch={onSessionMismatch}
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
          scroll={{ x: amountsVisible ? 2420 : 1830 }}
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
