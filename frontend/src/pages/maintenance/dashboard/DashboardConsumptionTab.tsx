/** M01 期间下单 + M03 每日每项目已计消耗（R34 / D-0024）。
 *
 * - M01：制单日期入期的有效需求单原 qty + 去重单数（不扣退货/免返）；纯数量域，
 *   任何角色都无金额列。
 * - M03：数量全计（实际领用，未采集件况；未扣退库）；金额只读既有已计成本
 *   （可能含估算），只报已知小计，另列未计成本量/覆盖率/来源，绝不把部分已知
 *   成本当完整总成本。金额键仅金额可见账号渲染。
 * - 日表可下钻：每行「领用明细」按 issue_date × project_id 拉取行级领用与
 *   （有权时）成本证据，可核对；导出走完整筛选集（不下钻参数）。
 * - 该页签不吃关键词 q（orders/consumption 端点无此参数）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert, Button, Card, Drawer, Row, Space, Table, Tag, Typography,
} from "antd";
import type { ColumnsType, TableProps } from "antd/es/table";
import { Link } from "react-router-dom";
import { TaxMoney } from "../../../context/TaxBasis";
import {
  fetchDashboardConsumption,
  fetchDashboardConsumptionIssues,
  fetchDashboardOrders,
  type ConsumptionDailyRow,
  type ConsumptionIssueRow,
  type ConsumptionResponse,
  type OrdersResponse,
} from "../../../api/maintenanceDashboard";
import {
  DashboardExportButtons,
  KpiCard,
  apiErrorMessage,
  costSourceText,
  moneyText,
  qtyOnlyParams,
  qtyText,
  rateText,
  qtyNumber,
  sessionToken,
  sessionUnchanged,
  type DashboardTabProps,
} from "./dashboardShared";

const { Text } = Typography;

const PAGE_SIZE_OPTIONS = [20, 50, 100];

const DRAWER_PAGE_SIZE = 50;

export default function DashboardConsumptionTab({
  params, amountsVisible, page, pageSize, refreshKey, onPageChange, maxExportRows,
}: DashboardTabProps) {
  // orders/consumption/issues 都是数量域端点：契约不收关键词 q 与试算比例，显式剥离。
  const baseParams = useMemo(() => qtyOnlyParams(params), [params]);

  const [orders, setOrders] = useState<OrdersResponse | null>(null);
  const [consumption, setConsumption] = useState<ConsumptionResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = seqRef.current + 1;
    seqRef.current = seq;
    const token = sessionToken();
    setLoading(true);
    setError(null);
    try {
      const [ordersResp, consumptionResp] = await Promise.all([
        fetchDashboardOrders(baseParams),
        fetchDashboardConsumption({ ...baseParams, page, page_size: pageSize }),
      ]);
      if (seqRef.current !== seq || !sessionUnchanged(token)) return;
      setOrders(ordersResp);
      setConsumption(consumptionResp);
    } catch (err) {
      if (seqRef.current !== seq || !sessionUnchanged(token)) return;
      setOrders(null);
      setConsumption(null);
      setError(apiErrorMessage(err));
    } finally {
      if (seqRef.current === seq && sessionUnchanged(token)) setLoading(false);
    }
  }, [baseParams, page, pageSize]);

  useEffect(() => {
    void load();
    return () => { seqRef.current += 1; };
  }, [load, refreshKey]);

  const onTableChange: TableProps<ConsumptionDailyRow>["onChange"] = (pg) => {
    if (pg.pageSize && pg.pageSize !== pageSize) onPageChange(1, pg.pageSize);
    else if (pg.current && pg.current !== page) onPageChange(pg.current, pageSize);
  };

  // ---- M03 下钻：issue_date × project_id 行级领用明细（Drawer） ----
  const [drillTarget, setDrillTarget] = useState<ConsumptionDailyRow | null>(null);
  const [drillPage, setDrillPage] = useState(1);
  const [drillData, setDrillData] = useState<{
    rows: ConsumptionIssueRow[]; total: number;
  } | null>(null);
  const [drillLoading, setDrillLoading] = useState(false);
  const [drillError, setDrillError] = useState<string | null>(null);
  const drillSeqRef = useRef(0);

  const openDrill = useCallback((row: ConsumptionDailyRow) => {
    setDrillTarget(row);
    setDrillPage(1);
  }, []);

  const loadDrill = useCallback(async () => {
    if (!drillTarget) return;
    const seq = drillSeqRef.current + 1;
    drillSeqRef.current = seq;
    const token = sessionToken();
    setDrillLoading(true);
    setDrillError(null);
    try {
      const resp = await fetchDashboardConsumptionIssues({
        ...baseParams,
        issue_date: drillTarget.issue_date,
        project_id: drillTarget.project_id,
        page: drillPage,
        page_size: DRAWER_PAGE_SIZE,
      });
      if (drillSeqRef.current !== seq || !sessionUnchanged(token)) return;
      setDrillData({ rows: resp.rows ?? [], total: resp.pagination.total });
    } catch (err) {
      if (drillSeqRef.current !== seq || !sessionUnchanged(token)) return;
      setDrillData(null);
      setDrillError(apiErrorMessage(err));
    } finally {
      if (drillSeqRef.current === seq && sessionUnchanged(token)) setDrillLoading(false);
    }
  }, [baseParams, drillTarget, drillPage]);

  useEffect(() => {
    if (drillTarget) void loadDrill();
    return () => { drillSeqRef.current += 1; };
  }, [drillTarget, loadDrill]);

  const consumptionColumns: ColumnsType<ConsumptionDailyRow> = useMemo(() => {
    const base: ColumnsType<ConsumptionDailyRow> = [
      { title: "业务日", dataIndex: "issue_date", width: 110, fixed: "left" },
      {
        title: "项目", dataIndex: "display_name", width: 220,
        render: (v: string, row) => (
          <Space direction="vertical" size={0}>
            <Link to={`/maintenance/projects/${row.project_id}`}>{v}</Link>
            <Text type="secondary" style={{ fontSize: 12 }}>{row.project_code ?? "—"}</Text>
          </Space>
        ),
      },
      { title: "实际领用量", dataIndex: "issued_qty", width: 110, align: "right",
        render: (v: string) => qtyText(v) },
      { title: "领用行数", dataIndex: "line_count", width: 90, align: "right",
        render: (v: number) => qtyText(String(v)) },
      {
        title: "核对明细", key: "drill", width: 110,
        render: (_: unknown, row) => (
          <Button size="small" onClick={() => openDrill(row)}>领用明细</Button>
        ),
      },
    ];
    if (amountsVisible) {
      base.splice(3, 0,
        {
          title: "已计消耗成本（只读）", key: "cost", width: 190, align: "right",
          render: (_: unknown, row) => (row.cost_amount_inc_tax !== undefined
            ? (
              <TaxMoney scope="maintenance" stack exact
                inc={Number(row.cost_amount_inc_tax)}
                ex={Number(row.cost_amount_ex_tax ?? row.cost_amount_inc_tax)} />
            )
            : "—"),
        },
        {
          title: "未计成本量", dataIndex: "uncosted_qty", width: 100, align: "right",
          render: (v: string | undefined) => qtyText(v ?? null),
        },
        {
          title: "已计成本覆盖率", key: "coverage", width: 120, align: "right",
          render: (_: unknown, row) => rateText(row.coverage_pct ?? null),
        },
        {
          title: "成本来源", key: "sources", width: 150,
          // 来源码 → 中文：复用面板既有可信映射（与后端枚举一一对应）；
          // 未收录码保留原码并标注，不臆造含义。
          render: (_: unknown, row) => (row.sources?.length
            ? (
              <Space wrap size={4}>
                {row.sources.map((s) => <Tag key={s}>{costSourceText(s)}</Tag>)}
              </Space>
            )
            : "—"),
        },
      );
    }
    return base;
  }, [amountsVisible, openDrill]);

  const drillColumns: ColumnsType<ConsumptionIssueRow> = useMemo(() => {
    const base: ColumnsType<ConsumptionIssueRow> = [
      { title: "领用单号", dataIndex: "issue_no", width: 130,
        render: (v: string | null) => v || "—" },
      { title: "PN", dataIndex: "pn", width: 160, ellipsis: true },
      { title: "数量", dataIndex: "qty", width: 90, align: "right",
        render: (v: string) => qtyText(v) },
    ];
    if (amountsVisible) {
      base.push(
        { title: "成本来源", dataIndex: "cost_source", width: 110,
          // 同上：可信中文映射，未收录码保留原码并标注。
          render: (v: string | null | undefined) => costSourceText(v) },
        { title: "参考侧", dataIndex: "reference_side", width: 90,
          render: (v: string | null | undefined) => v || "—" },
        {
          title: "单价（不含税 / 含税）", key: "unit_cost", width: 170, align: "right",
          render: (_: unknown, row) => (row.unit_cost_ex_tax !== undefined
            ? `${moneyText(row.unit_cost_ex_tax)} / ${moneyText(row.unit_cost_inc_tax)}`
            : "未计成本"),
        },
        {
          title: "已计成本", key: "cost_amount", width: 170, align: "right",
          render: (_: unknown, row) => (row.cost_amount_ex_tax !== undefined
            ? (
              <TaxMoney scope="maintenance" stack exact
                inc={Number(row.cost_amount_inc_tax)}
                ex={Number(row.cost_amount_ex_tax)} />
            )
            : "未计成本"),
        },
      );
    }
    return base;
  }, [amountsVisible]);

  const orderCards = orders?.cards;
  const consumptionCards = consumption?.cards;
  const dailyHasData = !!consumption?.rows.some(
    (row) => (qtyNumber(row.issued_qty) ?? 0) !== 0,
  );

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {error ? (
        <Alert
          type="error" showIcon message="下单与消耗数据加载失败" description={error}
          action={<Button size="small" onClick={() => void load()}>重试</Button>}
        />
      ) : null}

      <Card size="small" title="期间下单（M01：制单日期入期，原数量；不扣退货/免返）">
        <Row gutter={12} style={{ display: "flex", flexWrap: "wrap" }} data-testid="orders-cards">
          <KpiCard label="期间下单数量" loading={loading} testId="kpi-order-qty"
            value={qtyText(orderCards?.order_qty)}
            sub="有效需求单有效行 Σ原qty；按制单日期入期" />
          <KpiCard label="期间下单单数" loading={loading} testId="kpi-order-count"
            value={orderCards ? qtyText(String(orderCards.order_count)) : undefined}
            sub="需求单去重单数" />
        </Row>
        <div style={{ marginTop: 12 }}>
          <Space wrap size={12}>
            <DashboardExportButtons
              params={baseParams}
              kind="orders"
              maxExportRows={maxExportRows}
            />
          </Space>
        </div>
        <div style={{ marginTop: 12 }}>
          <Table<OrdersResponse["rows"][number]>
            rowKey="project_id"
            size="small"
            loading={loading}
            dataSource={orders?.rows ?? []}
            pagination={false}
            columns={[
              {
                title: "项目", dataIndex: "display_name", ellipsis: true,
                render: (v: string, row) => (
                  <Space direction="vertical" size={0}>
                    <Link to={`/maintenance/projects/${row.project_id}`}>{v}</Link>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {row.project_code ?? "—"}{row.business_type ? ` · ${row.business_type}` : ""}
                    </Text>
                  </Space>
                ),
              },
              { title: "期间下单量（原qty）", dataIndex: "order_qty", width: 160, align: "right",
                render: (v: string) => qtyText(v) },
              { title: "期间下单单数", dataIndex: "order_count", width: 130, align: "right",
                render: (v: number) => qtyText(String(v)) },
            ]}
            locale={{ emptyText: "当前窗口没有下单记录" }}
          />
        </div>
      </Card>

      <Card
        size="small"
        title="每日每项目实际领用与已计消耗成本（M03：未扣退库；不回算成本）"
      >
        <Row gutter={12} style={{ display: "flex", flexWrap: "wrap" }} data-testid="consumption-cards">
          <KpiCard label="实际领用合计" loading={loading} testId="kpi-consumption-qty"
            value={qtyText(consumptionCards?.issued_qty)}
            sub="实际领用量（未采集件况）；未扣退库" />
          {amountsVisible ? (
            <>
              <KpiCard label="已计消耗成本（已知小计）" loading={loading} testId="kpi-consumption-cost"
                value={consumptionCards?.cost_amount_inc_tax !== undefined ? (
                  <TaxMoney scope="maintenance" stack exact
                    inc={Number(consumptionCards.cost_amount_inc_tax)}
                    ex={Number(consumptionCards.cost_amount_ex_tax
                      ?? consumptionCards.cost_amount_inc_tax)} />
                ) : undefined}
                sub="只读既有已计成本（可能含估算）；已知小计不含未知" />
              <KpiCard label="未计成本量" loading={loading} testId="kpi-consumption-uncosted"
                value={qtyText(consumptionCards?.uncosted_qty ?? null)}
                sub="数量照计，金额未知不猜价" />
              <KpiCard label="已计成本覆盖率" loading={loading} testId="kpi-consumption-coverage"
                value={rateText(consumptionCards?.coverage_pct ?? null)}
                sub="已计成本领用数量 ÷ 同单位有效领用数量" />
              <KpiCard label="成本来源" loading={loading} testId="kpi-consumption-sources"
                value={consumptionCards?.sources?.length
                  ? (
                    <Space wrap size={4}>
                      {consumptionCards.sources.map((s) => <Tag key={s}>{costSourceText(s)}</Tag>)}
                    </Space>
                  )
                  : "—"}
                sub={consumptionCards?.note} />
            </>
          ) : null}
        </Row>
        {!amountsVisible ? (
          <Text type="secondary" style={{ fontSize: 12 }} data-testid="consumption-qty-only-note">
            当前账号无金额查看权限：仅展示数量口径（已计成本/覆盖率/来源需 data_profit 授权）
          </Text>
        ) : null}

        <div style={{ marginTop: 12 }}>
          <Space wrap size={12}>
            <DashboardExportButtons
              params={baseParams}
              kind="consumption_daily"
              maxExportRows={maxExportRows}
            />
            <DashboardExportButtons
              params={baseParams}
              kind="consumption_issues"
              maxExportRows={maxExportRows}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              领用明细导出为当前筛选集全量（不受下钻影响）
            </Text>
          </Space>
        </div>

        <div style={{ marginTop: 12 }}>
          <Table<ConsumptionDailyRow>
            rowKey={(row) => `${row.issue_date}:${row.project_id}`}
            size="small"
            loading={loading}
            dataSource={consumption?.rows ?? []}
            columns={consumptionColumns}
            onChange={onTableChange}
            scroll={{ x: amountsVisible ? 1180 : 700 }}
            pagination={{
              current: page,
              pageSize,
              total: consumption?.pagination.total ?? 0,
              pageSizeOptions: PAGE_SIZE_OPTIONS,
              showSizeChanger: true,
              showTotal: (total) => `共 ${total} 个日×项目`,
            }}
            locale={{ emptyText: dailyHasData ? "没有更多" : "当前窗口没有领用记录" }}
          />
        </div>
      </Card>

      <Drawer
        title={drillTarget
          ? `领用明细：${drillTarget.issue_date} · ${drillTarget.display_name}`
          : "领用明细"}
        width={Math.min(760, typeof window === "undefined" ? 760 : window.innerWidth - 32)}
        open={drillTarget !== null}
        destroyOnClose
        onClose={() => setDrillTarget(null)}
      >
        {drillError ? (
          <Alert
            type="error" showIcon message="领用明细加载失败" description={drillError}
            action={<Button size="small" onClick={() => void loadDrill()}>重试</Button>}
            style={{ marginBottom: 12 }}
          />
        ) : null}
        <Text type="secondary" style={{ fontSize: 12 }}>
          按领用日 × 项目的行级事实核对{amountsVisible ? "；成本列为只读既有已计成本证据" : ""}。
        </Text>
        <Table<ConsumptionIssueRow>
          rowKey="issue_line_id"
          size="small"
          style={{ marginTop: 12 }}
          loading={drillLoading}
          dataSource={drillData?.rows ?? []}
          columns={drillColumns}
          scroll={{ x: amountsVisible ? 760 : 400 }}
          pagination={{
            current: drillPage,
            pageSize: DRAWER_PAGE_SIZE,
            total: drillData?.total ?? 0,
            onChange: (next) => setDrillPage(next),
            showTotal: (total) => `共 ${total} 行`,
          }}
          locale={{ emptyText: "该日×项目没有领用行" }}
        />
      </Drawer>
    </Space>
  );
}
