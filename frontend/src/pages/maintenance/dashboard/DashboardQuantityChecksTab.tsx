/** T4 需求单数量核对（当前累计）：N/R/差额/超收整单口径；不发布返还状态。
 *
 * 契约 v2 §6（B-F10）：该页签支持关键词 q——按命中 PN 选择**整张需求单**，
 * 之后仍取整单全 PN/全期当前累计 N/R（不裁局部、不忽略 q）；同一 q 过滤
 * 未关联实收桶；导出与 API 同参同集。无金额列——数量核对本身就是纯数量域。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Card, Row, Select, Space, Table, Typography } from "antd";
import type { ColumnsType, TableProps } from "antd/es/table";
import {
  fetchDashboardQuantityChecks,
  type QuantityCheckRow,
  type QuantityChecksResponse,
} from "../../../api/maintenanceDashboard";
import {
  BasisStateTag,
  DashboardExportButtons,
  KpiCard,
  SORT_LABELS,
  apiErrorMessage,
  qtyText,
  reasonText,
  sessionToken,
  sessionUnchanged,
  type DashboardTabProps,
} from "./dashboardShared";

const { Text } = Typography;

const PAGE_SIZE_OPTIONS = [20, 50, 100];

export default function DashboardQuantityChecksTab({
  params, sorts, sort, page, pageSize, refreshKey,
  onSortChange, onPageChange, maxExportRows, ownerToken, onSessionMismatch,
}: DashboardTabProps) {
  const [data, setData] = useState<QuantityChecksResponse | null>(null);
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
      const resp = await fetchDashboardQuantityChecks({
        ...params, sort, page, page_size: pageSize,
      });
      if (seqRef.current !== seq) return;
      if (!sessionUnchanged(token)) { onSessionMismatch(); return; }
      setData(resp);
    } catch (err) {
      if (seqRef.current !== seq) return;
      if (!sessionUnchanged(token)) { onSessionMismatch(); return; }
      setData(null);
      setError(apiErrorMessage(err));
    } finally {
      if (seqRef.current === seq && sessionUnchanged(token)) setLoading(false);
    }
  }, [params, sort, page, pageSize, ownerToken, onSessionMismatch]);

  useEffect(() => {
    void load();
    return () => { seqRef.current += 1; };
  }, [load, refreshKey]);

  const onTableChange: TableProps<QuantityCheckRow>["onChange"] = (pg) => {
    if (pg.pageSize && pg.pageSize !== pageSize) onPageChange(1, pg.pageSize);
    else if (pg.current && pg.current !== page) onPageChange(pg.current, pageSize);
  };

  const columns: ColumnsType<QuantityCheckRow> = useMemo(() => [
    {
      title: "需求单号", dataIndex: "order_no", width: 160, fixed: "left",
      render: (v: string | null) => v || "—",
    },
    {
      title: "项目", dataIndex: "display_name", width: 220,
      render: (v: string, row) => (
        <Space direction="vertical" size={0}>
          <span>{v}</span>
          <Text type="secondary" style={{ fontSize: 12 }}>{row.project_code ?? "—"}</Text>
        </Space>
      ),
    },
    { title: "制单日期", dataIndex: "order_date", width: 110,
      render: (v: string | null) => v || "—" },
    { title: "需求类型", dataIndex: "demand_type", width: 100,
      render: (v: string | null) => v || "—" },
    { title: "仓库", dataIndex: "warehouse", width: 110,
      render: (v: string | null) => v || "—" },
    { title: "需求量 N", dataIndex: "demand_qty", width: 100, align: "right",
      render: (v: string | null) => qtyText(v) },
    { title: "实收 R", dataIndex: "received_qty", width: 100, align: "right",
      render: (v: string | null) => qtyText(v) },
    { title: "差额 max(N−R,0)", dataIndex: "difference_qty", width: 130, align: "right",
      render: (v: string | null) => qtyText(v) },
    { title: "超收 max(R−N,0)", dataIndex: "over_received_qty", width: 130, align: "right",
      render: (v: string | null) => qtyText(v) },
    { title: "基数状态", dataIndex: "basis_state", width: 100,
      render: (v: string) => <BasisStateTag state={v} /> },
    {
      title: "待核对原因", dataIndex: "reason_codes", width: 200,
      render: (codes: string[]) => (codes.length
        ? codes.map((code) => reasonText(code)).join("、")
        : "—"),
    },
    { title: "无业务日实收", dataIndex: "undated_receipt_qty", width: 110, align: "right",
      render: (v: string | null) => qtyText(v) },
    { title: "无返还备注", dataIndex: "no_return_note", width: 140, ellipsis: true,
      render: (v: string | null) => v || "—" },
  ], []);

  const cards = data?.cards;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {error ? (
        <Alert
          type="error" showIcon message="需求数量核对加载失败" description={error}
          action={<Button size="small" onClick={() => void load()}>重试</Button>}
        />
      ) : null}

      <Alert type="info" showIcon data-testid="checks-note"
        message={cards?.note ?? "当前累计（整单跨全部 PN/来源/件况）；不发布返还状态"} />
      <Text type="secondary" style={{ fontSize: 12 }} data-testid="checks-cumulative-note">
        全局日期仅按制单日期挑选需求单；PN 关键词命中含该 PN 的整张需求单（整单跨全部 PN/来源/件况），N/R 为整单当前累计，不因期间或 PN 条件截断；逐单差额与超收分别合计，不互相抵消。
      </Text>

      <Row gutter={12} style={{ display: "flex", flexWrap: "wrap" }} data-testid="checks-cards">
        <KpiCard label="需求量 N 合计" loading={loading}
          value={qtyText(cards?.checked_demand_qty)} />
        <KpiCard label="实收 R 合计" loading={loading}
          value={qtyText(cards?.received_qty)} />
        <KpiCard label="差额合计" loading={loading}
          value={qtyText(cards?.known_difference_qty)}
          sub="max(N−R,0) 逐单加总" />
        <KpiCard label="超收合计" loading={loading}
          value={qtyText(cards?.over_received_qty)}
          sub="max(R−N,0) 逐单加总" />
        <KpiCard label="待核对单数" loading={loading}
          value={cards ? qtyText(String(cards.pending_check_order_count)) : undefined}
          sub="基数不完整或归属冲突" />
        <KpiCard label="失效单数" loading={loading}
          value={cards ? qtyText(String(cards.inactive_order_count)) : undefined} />
        <KpiCard label="未关联需求单实收" loading={loading}
          value={qtyText(cards?.unassigned_receipt_qty)}
          sub="当前授权范围内未关联需求单的实收（不摊入需求单行）" />
      </Row>

      <Space wrap size={12}>
        <Select<string>
          aria-label="核对排序"
          value={sort}
          style={{ width: 160 }}
          options={sorts.map((key) => ({ label: SORT_LABELS[key] ?? key, value: key }))}
          onChange={onSortChange}
        />
        <DashboardExportButtons
          params={params}
          kind="quantity_checks"
          maxExportRows={maxExportRows}
          ownerToken={ownerToken}
          onSessionMismatch={onSessionMismatch}
        />
      </Space>

      <Card size="small" title="需求单数量核对（当前累计）">
        <Table<QuantityCheckRow>
          rowKey="source_order_id"
          size="small"
          loading={loading}
          dataSource={data?.rows ?? []}
          columns={columns}
          onChange={onTableChange}
          scroll={{ x: 1660 }}
          pagination={{
            current: page,
            pageSize,
            total: data?.pagination.total ?? 0,
            pageSizeOptions: PAGE_SIZE_OPTIONS,
            showSizeChanger: true,
            showTotal: (total) => `共 ${total} 张需求单`,
          }}
          locale={{ emptyText: "当前窗口没有需求单" }}
        />
      </Card>

      {data?.unassigned_receipts.length ? (
        <Card size="small" title="未关联需求单的实收（当前累计）">
          <Table<QuantityChecksResponse["unassigned_receipts"][number]>
            rowKey="project_id"
            size="small"
            loading={loading}
            dataSource={data.unassigned_receipts}
            pagination={false}
            columns={[
              { title: "项目", dataIndex: "display_name",
                render: (v: string, row) => `${v}（${row.project_code ?? "—"}）` },
              { title: "实收数量", dataIndex: "received_qty", width: 140, align: "right",
                render: (v: string) => qtyText(v) },
            ]}
            locale={{ emptyText: "无" }}
          />
        </Card>
      ) : null}
    </Space>
  );
}
