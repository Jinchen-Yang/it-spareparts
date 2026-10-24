import { useContextQuery } from "../../../authz/contextQuery";
import { surfaceRule } from "../../../authz/rules";
import { useContextAdmission, useModuleAccess } from "../../../authz/access";
/** T3 PN 排名与估价：数量榜人人可见；估价列/参考价只对金额权账号请求与渲染。
 *
 * 有金额权 → /valuation（R34 政策 + 总卡 + 行级估价与三域参考）；
 * 无金额权 → /parts（纯数量），金额列整个不存在，不是 0 或 CSS 遮盖。
 *
 * R34：三域参考（采购/销售/领用已计成本）并列、不相加；未设试算比例时
 * 价值列显示「待试算」（awaiting_ratio），参考价照常展示。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Card, Select, Space, Table, Typography } from "antd";
import type { ColumnsType, TableProps } from "antd/es/table";
import { TaxMoney } from "../../../context/TaxBasis";
import {
  fetchDashboardParts,
  fetchDashboardValuation,
  type CostReferencePayload,
  type PartRow,
  type PagedRows,
  type ValuationPolicy,
  type ValuationResponse,
} from "../../../api/maintenanceDashboard";
import type { ReceiptsDrill } from "./ReceiptsDrillDrawer";
import {
  AWAITING_RATIO_TEXT,
  DashboardExportButtons,
  DOMAIN_LABELS,
  DomainCoverageLines,
  KpiCard,
  PENDING_ESTIMATE_TEXT,
  ReferenceLine,
  SORT_LABELS,
  UnitBreakdown,
  apiErrorMessage,
  drillByPn,
  dualTaxText,
  moneyText,
  qtyText,
  rateText,
  reasonText,
  sessionToken,
  sessionUnchanged,
  type DashboardTabProps,
} from "./dashboardShared";

const { Text } = Typography;

const PAGE_SIZE_OPTIONS = [20, 50, 100];

function policySummary(plc: ValuationPolicy): string {
  const ratio = plc.retention_ratio === null
    ? "当前未设比例（待试算，不出金额）"
    : `当前试算比例 ${Math.round(Number(plc.retention_ratio) * 10000) / 100}%`;
  return (
    `试算口径：${plc.notes}；${ratio}；${plc.retention_rule}；`
    + `窗口：${plc.window_rule}；${plc.weighting}；${plc.three_domain_rule}；`
    + `${plc.unknown_condition_rule}；${plc.finished_goods_rule}；版本 ${plc.policy_version}`
  );
}

/** 成本参考域一行：可用 → 单位成本双税并列（B2-F01：直接用持久化两侧，
 * 绝不拿含税÷1.13 重建未税）+ 样本行 + 领用已计覆盖（issued_lines 口径，
 * 契约 v2 §2）；不可用 → 原因。与三域估值列的返件估值覆盖分母不同，
 * 命名上显式区分，绝不混用。 */
function CostReferenceLine({ reference }: { reference: CostReferencePayload }) {
  if (!reference.available) {
    return (
      <Text type="secondary" style={{ fontSize: 12 }}>
        {DOMAIN_LABELS.cost}：无参考（{reasonText(reference.unavailable_reason)}）
      </Text>
    );
  }
  const money = dualTaxText(reference.unit_cost_inc_tax, reference.unit_cost_ex_tax);
  return (
    <Text style={{ fontSize: 12 }}>
      {DOMAIN_LABELS.cost} {money ?? "¥—"}
      {reference.sample_line_count !== undefined ? `（${reference.sample_line_count} 行领用）` : null}
      {reference.coverage_pct !== null && reference.coverage_pct !== undefined
        ? ` · 领用已计覆盖 ${reference.coverage_pct}%`
        : null}
    </Text>
  );
}

/** 行级参考列：三域并列（采购/销售/领用已计成本）+ 选用说明（采购优先/回退原因）。 */
function ReferencesCell({ row }: { row: PartRow }) {
  const refs = row.references;
  if (!refs) return <>—</>;
  return (
    <Space direction="vertical" size={0}>
      <ReferenceLine domain="purchase" reference={refs.purchase} />
      <ReferenceLine domain="sales" reference={refs.sales} />
      <CostReferenceLine reference={refs.cost} />
      {refs.chosen ? (
        <Text type="secondary" style={{ fontSize: 12 }}>
          主展示选用：{refs.chosen === "purchase" ? "采购" : "销售"}
          {refs.chosen_reason ? `（${reasonText(refs.chosen_reason)}）` : ""}
        </Text>
      ) : (
        <Text type="secondary" style={{ fontSize: 12 }}>市场两域均无样本（成本域独立展示）</Text>
      )}
    </Space>
  );
}

/** 三域试算价值列：分值 + 各域返件估值覆盖（已估/待估/覆盖）并列、缺金额键
 * 的域显示待估、绝不求和；成品按参考全值另列。awaiting_ratio（缺比例）与
 * pending（无样本）分开呈现；契约 v2 §2：有坏件行即三域都出现（覆盖 0%
 * 是真实 0%），pending 但成本域可用时 domains.cost 仍有并列值。 */
function DomainValuesCell({ row }: { row: PartRow }) {
  const valuation = row.valuation;
  if (!valuation) return <>—</>;
  if (valuation.state === "awaiting_ratio") {
    return <span data-testid="part-domains-awaiting">{AWAITING_RATIO_TEXT}</span>;
  }
  const domains = valuation.domains ?? {};
  if (!Object.keys(domains).length) {
    return <span data-testid="part-domains-pending">{PENDING_ESTIMATE_TEXT}</span>;
  }
  return (
    <Space direction="vertical" size={0} align="start">
      <DomainCoverageLines domains={domains} />
      {valuation.finished_estimated_value_inc_tax !== undefined
        || valuation.finished_estimated_value_ex_tax !== undefined ? (
        <Text style={{ fontSize: 12 }}>
          成品（参考全值）{" "}
          {dualTaxText(
            valuation.finished_estimated_value_inc_tax,
            valuation.finished_estimated_value_ex_tax,
          ) ?? "¥—"}
        </Text>
      ) : null}
      <Text type="secondary" style={{ fontSize: 12 }}>三域并列，不相加</Text>
    </Space>
  );
}

export default function DashboardPartsTab({
  params, amountsVisible: amountsEligible, sorts, sort, page, pageSize, refreshKey,
  onSortChange, onPageChange, maxExportRows, ownerToken, onSessionMismatch, onReceiptsDrill,
}: DashboardTabProps & {
  /** M-AC09：行内「返件明细」→ 壳层 Drawer（原筛选 ∩ 该 PN，DL-022）。 */
  onReceiptsDrill?: (drill: ReceiptsDrill) => void;
}) {
  const admission = useContextAdmission();
  const useValuation = amountsEligible && admission.surface("maintenance_dashboard.valuation", true) !== "deny";
  const valueQuery = useContextQuery({ entryId: "GET /api/maintenance/analytics/maintenance-dashboard/valuation",
    rule: surfaceRule("maintenance_dashboard.valuation"), queryKey: [params, sort, page, pageSize, refreshKey], legacyAllowed: true,
    enabled: useValuation, onStale: onSessionMismatch,
    query: capture => fetchDashboardValuation({ ...params, sort, page, page_size: pageSize }, capture) });
  const partQuery = useContextQuery({ entryId: "GET /api/maintenance/analytics/maintenance-dashboard/parts",
    rule: surfaceRule("maintenance_dashboard.parts"), queryKey: [params, sort, page, pageSize, refreshKey], legacyAllowed: true,
    enabled: !useValuation, onStale: onSessionMismatch,
    query: capture => fetchDashboardParts({ ...params, sort, page, page_size: pageSize }, capture) });
  const valuation = valueQuery.data, qtyData = partQuery.data;
  const loading = valueQuery.loading || partQuery.loading;
  const error = valueQuery.error || partQuery.error ? apiErrorMessage(valueQuery.error || partQuery.error) : null;
  const load = () => { valueQuery.retry(); partQuery.retry(); };

  const fieldAccess = useModuleAccess("maintenance_dashboard", valueQuery.context);
  const amountsVisible = amountsEligible && fieldAccess.field("data_profit", amountsEligible);

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
    if (onReceiptsDrill) {
      base.push({
        title: "明细", key: "receipts-drill", width: 100, fixed: "right",
        render: (_: unknown, row) => (
          <Button
            size="small"
            aria-label={`查看 ${row.pn} 返件明细`}
            onClick={() => onReceiptsDrill({
              title: row.pn,
              params: drillByPn(params, row.pn),
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
          title: "坏件试算价值（主展示口径）", key: "valuation", width: 200, align: "right",
          render: (_: unknown, row) => {
            const v = row.valuation;
            if (!v) return "—";
            if (v.bad_estimated_value_inc_tax !== undefined) {
              return (
                <TaxMoney scope="maintenance" exact stack
                  inc={Number(v.bad_estimated_value_inc_tax)}
                  ex={v.bad_estimated_value_ex_tax !== undefined
                    ? Number(v.bad_estimated_value_ex_tax)
                    : null} />
              );
            }
            if (v.state === "pending") {
              return <span data-testid="part-value-pending">{PENDING_ESTIMATE_TEXT}</span>;
            }
            return <span data-testid="part-value-awaiting">{AWAITING_RATIO_TEXT}</span>;
          },
        },
        {
          title: "已估 / 待估 / 待试算坏件量", key: "estimated", width: 170, align: "right",
          render: (_: unknown, row) => (row.valuation ? (
            <span>
              {qtyText(row.valuation.estimated_bad_qty)}
              {" / "}
              {qtyText(row.valuation.pending_bad_qty)}
              {" / "}
              {qtyText(row.valuation.awaiting_ratio_bad_qty)}
            </span>
          ) : "—"),
        },
        {
          title: "覆盖率", key: "coverage_pct", width: 90, align: "right",
          render: (_: unknown, row) => rateText(row.valuation?.coverage_pct ?? null),
        },
        {
          title: "三域试算价值（含税／不含税，并列不相加）", key: "domains", width: 220,
          render: (_: unknown, row) => <DomainValuesCell row={row} />,
        },
        { title: "三域参考（含税／不含税）", key: "references", width: 240,
          render: (_: unknown, row) => <ReferencesCell row={row} /> },
      );
    }
    return base;
  }, [amountsVisible, onReceiptsDrill, params]);

  const cards = valuation?.cards;
  const ratioReady = cards?.bad_estimated_value !== undefined;

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
        <div className="mdb-kpi-grid" data-testid="valuation-cards">
          <KpiCard label="坏件试算价值（主展示口径）" loading={loading} testId="kpi-bad-value"
            value={ratioReady ? (
              <TaxMoney scope="maintenance" stack exact
                inc={Number(cards.bad_estimated_value?.inc_tax)}
                ex={Number(cards.bad_estimated_value?.ex_tax)} />
            ) : (
              <span data-testid="kpi-bad-value-awaiting">{AWAITING_RATIO_TEXT}</span>
            )}
            sub={valuation ? valuation.note : undefined} />
          <KpiCard label="已估坏件量" loading={loading}
            value={qtyText(cards.estimated_bad_qty)} />
          <KpiCard label="待估坏件量" loading={loading}
            value={qtyText(cards.pending_bad_qty)}
            sub="待估不计为 0 元" />
          <KpiCard label="待试算坏件量（缺比例）" loading={loading} testId="kpi-awaiting-ratio"
            value={qtyText(cards.awaiting_ratio_bad_qty)}
            sub="设置价值保留比例后出金额" />
          <KpiCard label="覆盖率" loading={loading}
            value={rateText(cards.coverage_pct)}
            sub={(
              <Space direction="vertical" size={0} align="start">
                <span>已估坏件数量 ÷ 坏件实收数量</span>
                {/* 契约 v2 §2：总卡三域覆盖（返件估值口径），与主覆盖并列呈现 */}
                {cards.domains ? <DomainCoverageLines domains={cards.domains} /> : null}
                {cards.unit_breakdown?.length ? (
                  <UnitBreakdown
                    entries={cards.unit_breakdown}
                    render={(e) => (
                      <>已估 {qtyText(e.estimated_bad_qty)} / 待估 {qtyText(e.pending_bad_qty)} / 覆盖 {rateText(e.coverage_pct)}</>
                    )} />
                ) : null}
              </Space>
            )} />
        </div>
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
          ownerToken={ownerToken}
          onSessionMismatch={onSessionMismatch}
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
          scroll={{ x: amountsVisible ? 1900 : 1020 }}
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
