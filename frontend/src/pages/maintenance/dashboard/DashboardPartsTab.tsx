/** T3 PN 排名与估价：数量榜人人可见；估价列/参考价只对金额权账号请求与渲染。
 *
 * 有金额权 → /valuation（政策 + 总卡 + 行级估价与双域参考）；
 * 无金额权 → /parts（纯数量），金额列整个不存在，不是 0 或 CSS 遮盖。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Card, Row, Select, Space, Table, Typography } from "antd";
import type { ColumnsType, TableProps } from "antd/es/table";
import { TaxMoney } from "../../../context/TaxBasis";
import {
  fetchDashboardParts,
  fetchDashboardValuation,
  type PartRow,
  type PagedRows,
  type ValuationPolicy,
  type ValuationResponse,
} from "../../../api/maintenanceDashboard";
import {
  DashboardExportButtons,
  KpiCard,
  ReferenceLine,
  SORT_LABELS,
  apiErrorMessage,
  qtyText,
  rateText,
  reasonText,
  type DashboardTabProps,
} from "./dashboardShared";

const { Text } = Typography;

const PAGE_SIZE_OPTIONS = [20, 50, 100];

function policySummary(policy: ValuationPolicy): string {
  const factors = Object.entries(policy.condition_factors)
    .map(([condition, factor]) => `${condition}×${factor}`)
    .join("、");
  return `估算 · ${policy.notes}；折算系数：${factors}；${policy.unknown_condition_rule}；版本 ${policy.policy_version}`;
}

/** 行级参考列：双域并列两行 + 选用说明（采购优先/回退原因）。 */
function ReferencesCell({ row }: { row: PartRow }) {
  const refs = row.references;
  if (!refs) return <>—</>;
  return (
    <Space direction="vertical" size={0}>
      <ReferenceLine domain="purchase" reference={refs.purchase} />
      <ReferenceLine domain="sales" reference={refs.sales} />
      {refs.chosen ? (
        <Text type="secondary" style={{ fontSize: 12 }}>
          选用：{refs.chosen === "purchase" ? "采购" : "销售"}
          {refs.chosen_reason ? `（${reasonText(refs.chosen_reason)}）` : ""}
        </Text>
      ) : (
        <Text type="secondary" style={{ fontSize: 12 }}>未选用参考</Text>
      )}
    </Space>
  );
}

export default function DashboardPartsTab({
  params, amountsVisible, sorts, sort, page, pageSize, refreshKey,
  onSortChange, onPageChange, maxExportRows,
}: DashboardTabProps) {
  const [valuation, setValuation] = useState<ValuationResponse | null>(null);
  const [qtyData, setQtyData] = useState<PagedRows<PartRow> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = seqRef.current + 1;
    seqRef.current = seq;
    setLoading(true);
    setError(null);
    try {
      // 金额权决定数据源：/valuation 自带政策与估价；/parts 只有数量。
      const resp = amountsVisible
        ? await fetchDashboardValuation({ ...params, sort, page, page_size: pageSize })
        : await fetchDashboardParts({ ...params, sort, page, page_size: pageSize });
      if (seqRef.current !== seq) return;
      if (amountsVisible) {
        setValuation(resp as ValuationResponse);
        setQtyData(null);
      } else {
        setQtyData(resp as PagedRows<PartRow>);
        setValuation(null);
      }
    } catch (err) {
      if (seqRef.current !== seq) return;
      setValuation(null);
      setQtyData(null);
      setError(apiErrorMessage(err));
    } finally {
      if (seqRef.current === seq) setLoading(false);
    }
  }, [params, amountsVisible, sort, page, pageSize]);

  useEffect(() => {
    void load();
    return () => { seqRef.current += 1; };
  }, [load, refreshKey]);

  const onTableChange: TableProps<PartRow>["onChange"] = (pg) => {
    if (pg.pageSize && pg.pageSize !== pageSize) onPageChange(1, pg.pageSize);
    else if (pg.current && pg.current !== page) onPageChange(pg.current, pageSize);
  };

  const rows = amountsVisible ? valuation?.rows ?? [] : qtyData?.rows ?? [];
  const total = amountsVisible
    ? valuation?.pagination.total ?? 0
    : qtyData?.pagination.total ?? 0;

  const columns: ColumnsType<PartRow> = useMemo(() => {
    const base: ColumnsType<PartRow> = [
      {
        title: "PN / 描述", dataIndex: "pn", width: 260, fixed: "left",
        render: (v: string, row) => (
          <Space direction="vertical" size={0}>
            <Text strong>{v}</Text>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {row.description || "—"}{row.unit ? ` · ${row.unit}` : ""}
            </Text>
          </Space>
        ),
      },
      { title: "返件实收", dataIndex: "receipt_qty", width: 100, align: "right",
        render: (v: string) => qtyText(v) },
      { title: "坏件实收", dataIndex: "bad_receipt_qty", width: 100, align: "right",
        render: (v: string) => qtyText(v) },
      { title: "实际领用（未采集件况）", dataIndex: "issued_qty", width: 150, align: "right",
        render: (v: string) => qtyText(v) },
      { title: "项目数", dataIndex: "project_count", width: 80, align: "right",
        render: (v: number) => qtyText(String(v)) },
      { title: "同期返还率", dataIndex: "return_rate_pct", width: 110, align: "right",
        render: (v: number | null) => rateText(v) },
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
          title: "已估 / 待估坏件量", key: "estimated", width: 130, align: "right",
          render: (_: unknown, row) => (row.valuation ? (
            <span>
              {qtyText(row.valuation.estimated_bad_qty)}
              {" / "}
              {qtyText(row.valuation.pending_bad_qty)}
            </span>
          ) : "—"),
        },
        {
          title: "覆盖率", key: "coverage_pct", width: 90, align: "right",
          render: (_: unknown, row) => rateText(row.valuation?.coverage_pct ?? null),
        },
        { title: "交易参考（含税）", key: "references", width: 230,
          render: (_: unknown, row) => <ReferencesCell row={row} /> },
      );
    }
    return base;
  }, [amountsVisible]);

  const cards = valuation?.cards;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {error ? (
        <Alert
          type="error" showIcon message="PN 排名数据加载失败" description={error}
          action={<Button size="small" onClick={() => void load()}>重试</Button>}
        />
      ) : null}

      {valuation ? (
        <Alert type="info" showIcon data-testid="valuation-policy-note"
          message={policySummary(valuation.policy)} />
      ) : (
        <Text type="secondary" style={{ fontSize: 12 }} data-testid="amounts-hidden-note">
          当前账号无金额查看权限：仅展示数量排名（估价需 data_profit 授权）
        </Text>
      )}

      {amountsVisible && cards ? (
        <Row gutter={12} style={{ display: "flex", flexWrap: "wrap" }} data-testid="valuation-cards">
          <KpiCard label="坏件估算价值（估算）" loading={loading} testId="kpi-bad-value"
            value={(
              <TaxMoney scope="maintenance" stack exact
                inc={Number(cards.bad_estimated_value.inc_tax)}
                ex={Number(cards.bad_estimated_value.ex_tax)} />
            )}
            sub={valuation ? valuation.note : undefined} />
          <KpiCard label="已估坏件量" loading={loading}
            value={qtyText(cards.estimated_bad_qty)} />
          <KpiCard label="待估坏件量" loading={loading}
            value={qtyText(cards.pending_bad_qty)}
            sub="待估不计为 0 元" />
          <KpiCard label="覆盖率" loading={loading}
            value={rateText(cards.coverage_pct)}
            sub="已估坏件数量 ÷ 坏件实收数量" />
        </Row>
      ) : null}

      <Space wrap size={12}>
        <Select<string>
          aria-label="PN 排序"
          value={sort}
          style={{ width: 180 }}
          options={sorts.map((key) => ({ label: SORT_LABELS[key] ?? key, value: key }))}
          onChange={onSortChange}
        />
        <DashboardExportButtons
          params={params}
          kind="parts"
          maxExportRows={maxExportRows}
        />
      </Space>

      <Card size="small" title={`PN 排名（共 ${total.toLocaleString("zh-CN")} 个）`}>
        <Table<PartRow>
          rowKey={(row) => (row.part_id == null ? `pn:${row.pn}` : `part:${row.part_id}`)}
          size="small"
          loading={loading}
          dataSource={rows}
          columns={columns}
          onChange={onTableChange}
          scroll={{ x: amountsVisible ? 1500 : 900 }}
          pagination={{
            current: page,
            pageSize,
            total,
            pageSizeOptions: PAGE_SIZE_OPTIONS,
            showSizeChanger: true,
            showTotal: (t) => `第 ${(page - 1) * pageSize + (t ? 1 : 0)}–${Math.min(page * pageSize, t)} 条 / 共 ${t} 个 PN`,
          }}
          locale={{ emptyText: "当前窗口没有 PN 数据" }}
        />
      </Card>
    </Space>
  );
}
