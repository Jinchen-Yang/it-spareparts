import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Card, Col, Row, Space, Statistic, Table, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Link } from "react-router-dom";

import {
  listMaintenanceProjects,
  type MaintenanceProject,
} from "../../api/maintenanceProjects";
import {
  getMaintenanceExpenseReconcile,
  getMaintenanceProjectFrontStock,
  getMaintenanceProjectReturnRate,
  listMaintenanceSalvages,
  type MaintenanceExpenseReconcileDirectory,
  type MaintenanceFrontStockSummary,
  type MaintenanceReturnRate,
  type MaintenanceSalvageDirectory,
} from "../../api/maintenanceOperations";
import PageHeader from "../../components/PageHeader";
import { moneyIncTax } from "../../utils/format";

const { Text } = Typography;

const NOT_READY = "尚未接入";

/** 看板一次性拉取的项目上限（项目目录接口单页上限 200）。 */
const DIRECTORY_PAGE_SIZE = 200;

const RETURN_STATUS_META: Record<string, { label: string; color?: string }> = {
  not_ready: { label: "返还率未导入", color: "orange" },
  basis_incomplete: { label: "基础待完善", color: "orange" },
  available: { label: "已发布", color: "green" },
  no_return_required: { label: "无应返义务", color: "default" },
};

interface DashboardProjectRow {
  project: MaintenanceProject;
  returnRate: MaintenanceReturnRate | null;
  salvage: MaintenanceSalvageDirectory | null;
  frontStock: MaintenanceFrontStockSummary | null;
}

function errorText(error: unknown): string {
  const payload = error as { response?: { data?: { detail?: string | { message?: string } } } };
  const detail = payload.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  return "请求失败，请刷新后重试";
}

/** 权限类失败（403）静默降级为「尚未接入」，不逐项目弹错误（如无 data_profit 的变卖清单）。 */
function isPermissionError(error: unknown): boolean {
  return (error as { response?: { status?: unknown } } | null)?.response?.status === 403;
}

const projectColumns: ColumnsType<DashboardProjectRow> = [
  {
    title: "项目",
    key: "project",
    width: 260,
    render: (_, row) => (
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 12, color: "var(--mb-text-3)" }}>{row.project.project_code}</div>
        <Link to={`/maintenance/beta/projects/${encodeURIComponent(row.project.project_id)}`}>
          {row.project.display_name}
        </Link>
      </div>
    ),
  },
  {
    title: "官方返还率",
    key: "return-rate",
    width: 180,
    render: (_, row) => {
      const rate = row.returnRate;
      if (!rate) return <Text type="secondary">{NOT_READY}</Text>;
      const meta = RETURN_STATUS_META[rate.status] ?? { label: rate.status };
      return (
        <Space wrap size={[6, 6]}>
          <Tag color={meta.color}>{meta.label}</Tag>
          {rate.status === "available" && rate.official_rate_pct != null
            ? <Text strong>{rate.official_rate_pct}%</Text>
            : <Text type="secondary">{NOT_READY}</Text>}
        </Space>
      );
    },
  },
  {
    title: "坏件变卖收入（含税）",
    key: "salvage-revenue",
    width: 170,
    align: "right",
    render: (_, row) => (
      row.salvage ? moneyIncTax(row.salvage.total_revenue) : <Text type="secondary">{NOT_READY}</Text>
    ),
  },
  {
    title: "前置库金额（含税）",
    key: "front-stock-value",
    width: 170,
    align: "right",
    render: (_, row) => {
      const stock = row.frontStock;
      if (!stock) return <Text type="secondary">{NOT_READY}</Text>;
      return stock.cost_visible
        ? stock.total_value_inc_tax != null
          ? moneyIncTax(stock.total_value_inc_tax)
          : <Text type="secondary">{NOT_READY}</Text>
        : <Tag>金额不可见</Tag>;
    },
  },
];

export default function MaintenanceSalesDashboardPage() {
  const [rows, setRows] = useState<DashboardProjectRow[]>([]);
  const [directoryTotal, setDirectoryTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [reconcile, setReconcile] = useState<MaintenanceExpenseReconcileDirectory | null>(null);
  const generation = useRef(0);

  useEffect(() => {
    const request = ++generation.current;
    setLoading(true);
    setLoadError(false);

    const fetchProject = (project: MaintenanceProject) =>
      Promise.allSettled([
        getMaintenanceProjectReturnRate(project.project_id),
        listMaintenanceSalvages(project.project_id),
        getMaintenanceProjectFrontStock(project.project_id),
      ]).then(([rateResult, salvageResult, stockResult]) => {
        if (request !== generation.current) return;
        setRows((prev) => [
          ...prev,
          {
            project,
            returnRate: rateResult.status === "fulfilled" ? rateResult.value.data : null,
            salvage: salvageResult.status === "fulfilled" ? salvageResult.value.data : null,
            frontStock: stockResult.status === "fulfilled" ? stockResult.value.data : null,
          },
        ]);
        if (rateResult.status === "rejected") {
          message.error(`「${project.display_name}」返还率加载失败：${errorText(rateResult.reason)}`);
        }
        if (salvageResult.status === "rejected" && !isPermissionError(salvageResult.reason)) {
          message.error(`「${project.display_name}」变卖清单加载失败：${errorText(salvageResult.reason)}`);
        }
        if (stockResult.status === "rejected") {
          message.error(`「${project.display_name}」前置库加载失败：${errorText(stockResult.reason)}`);
        }
      });

    // 每项目 3 个端点：目录最多 200 个项目，用有界并发拉取，避免一次性打爆服务端。
    const fetchAllProjects = async (projects: MaintenanceProject[]) => {
      const queue = [...projects];
      const CONCURRENCY = 8;
      const worker = async () => {
        for (;;) {
          const project = queue.shift();
          if (!project) return;
          await fetchProject(project);
        }
      };
      await Promise.all(
        Array.from({ length: Math.min(CONCURRENCY, queue.length) }, () => worker()),
      );
    };

    void (async () => {
      try {
        const [{ data: directory }, reconcileResult] = await Promise.all([
          listMaintenanceProjects({
            page: 1,
            page_size: DIRECTORY_PAGE_SIZE,
            include_inactive: false,
          }),
          getMaintenanceExpenseReconcile({ limit: 500 }).catch((error: unknown) => {
            // 可选面板：无 data_profit 权限时静默降级为「尚未接入」，不打扰主看板。
            if (request === generation.current && !isPermissionError(error)) {
              message.error(`报销对账加载失败：${errorText(error)}`);
            }
            return null;
          }),
        ]);
        if (request !== generation.current) return;
        setDirectoryTotal(directory.total ?? 0);
        setReconcile(reconcileResult?.data ?? null);
        await fetchAllProjects(directory.rows ?? []);
      } catch (error) {
        if (request !== generation.current) return;
        setLoadError(true);
        message.error(`项目目录加载失败：${errorText(error)}`);
      } finally {
        if (request === generation.current) setLoading(false);
      }
    })();

    return () => { generation.current += 1; };
  }, []);

  const aggregate = useMemo(() => {
    const notReadyCount = rows.filter((row) => row.returnRate?.status === "not_ready").length;
    const available = rows.filter((row) => row.returnRate?.status === "available");
    const rateValues = available
      .map((row) => row.returnRate?.official_rate_pct)
      .filter((value): value is string => value != null && Number.isFinite(Number(value)))
      .map(Number);
    const avgOfficialRate = rateValues.length > 0
      ? rateValues.reduce((sum, value) => sum + value, 0) / rateValues.length
      : null;
    const salvageRevenue = rows.reduce(
      (sum, row) => sum + (row.salvage?.total_revenue ?? 0),
      0,
    );
    const salvageRows = rows.filter((row) => row.salvage != null);
    const salvageMargins = salvageRows
      .map((row) => row.salvage?.total_margin)
      .filter((value): value is number => value != null);
    const salvageMargin = salvageMargins.length > 0
      ? salvageMargins.reduce((sum, value) => sum + value, 0)
      : null;
    const stockRows = rows.filter((row) => row.frontStock != null);
    const stockValueRows = rows.filter(
      (row) => row.frontStock?.cost_visible === true
        && row.frontStock.total_value_inc_tax != null,
    );
    const frontStockValue = stockValueRows.reduce(
      (sum, row) => sum + (row.frontStock?.total_value_inc_tax ?? 0),
      0,
    );
    return {
      notReadyCount,
      availableCount: available.length,
      avgOfficialRate,
      salvageRevenue,
      salvageMargin,
      salvageRowCount: salvageRows.length,
      salvageMarginComplete: salvageMargins.length > 0
        && salvageMargins.length === salvageRows.length,
      stockRows: stockRows.length,
      stockValueRows: stockValueRows.length,
      frontStockValue,
    };
  }, [rows]);

  const totalChecked = rows.length;
  const scopeNote = directoryTotal > totalChecked
    ? `项目目录共 ${directoryTotal} 个，本页仅统计前 ${DIRECTORY_PAGE_SIZE} 个。`
    : null;

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title="维保销售看板"
        subtitle="仅管理员/老板可见：跨项目汇总官方返还率、坏件变卖收入与前置库金额；数据不足一律显示「尚未接入」。"
      />
      {scopeNote && <Alert showIcon type="info" message={scopeNote} />}
      {loadError ? (
        <Alert type="error" showIcon message="销售看板加载失败，请刷新后重试" />
      ) : (
        <>
          <Row gutter={[16, 16]}>
            <Col xs={24} sm={12} xl={6}>
              <Card>
                <Statistic
                  title="返还率未导入项目"
                  value={loading ? NOT_READY : aggregate.notReadyCount}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  available 项目 {aggregate.availableCount} 个
                </Text>
              </Card>
            </Col>
            <Col xs={24} sm={12} xl={6}>
              <Card>
                <Statistic
                  title="平均官方返还率"
                  value={loading || aggregate.avgOfficialRate == null
                    ? NOT_READY
                    : `${aggregate.avgOfficialRate.toFixed(2)}%`}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  仅统计已发布且官方返还率非空的项目
                </Text>
              </Card>
            </Col>
            <Col xs={24} sm={12} xl={6}>
              <Card>
                <Statistic
                  title="坏件变卖总收入（含税）"
                  value={loading || aggregate.salvageRowCount === 0
                    ? NOT_READY
                    : moneyIncTax(aggregate.salvageRevenue)}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  贡献毛利（含税）：{loading || aggregate.salvageMargin == null
                    ? NOT_READY
                    : moneyIncTax(aggregate.salvageMargin)}
                  {!loading && aggregate.salvageMargin != null && !aggregate.salvageMarginComplete
                    && "（部分缺成本）"}
                </Text>
              </Card>
            </Col>
            <Col xs={24} sm={12} xl={6}>
              <Card>
                <Statistic
                  title="前置库总金额（含税）"
                  value={loading || aggregate.stockValueRows === 0
                    ? NOT_READY
                    : moneyIncTax(aggregate.frontStockValue)}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  仅统计成本可见项目（{aggregate.stockValueRows}/{aggregate.stockRows}）
                </Text>
              </Card>
            </Col>
          </Row>

          <Card title="报销对账（可选）" extra={reconcile ? <Tag>admin/boss 专属</Tag> : <Tag>尚未接入</Tag>}>
            {reconcile ? (
              <Space wrap size={[16, 8]}>
                <Tag color="green">一致 {reconcile.matched}</Tag>
                <Tag color="red">不一致 {reconcile.mismatch}</Tag>
                <Tag color="orange">金额缺失 {reconcile.unresolved}</Tag>
                <Tag>仅台账 {reconcile.ledger_only}</Tag>
                <Tag>仅BXD {reconcile.bxd_only}</Tag>
                <Tag>仅正式 {reconcile.formal_only}</Tag>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  本页返回 {reconcile.rows.length} 条（limit {reconcile.limit}）
                </Text>
              </Space>
            ) : (
              <Text type="secondary">{NOT_READY}</Text>
            )}
          </Card>

          <Card
            title="各项目明细"
            extra={<Tag>{`已统计 ${totalChecked} 个项目`}</Tag>}
          >
            <Table
              rowKey={(row) => row.project.project_id}
              size="small"
              columns={projectColumns}
              dataSource={[...rows].sort((a, b) =>
                a.project.project_code.localeCompare(b.project.project_code))}
              loading={loading}
              scroll={{ x: 780 }}
              pagination={false}
              locale={{ emptyText: loading ? "正在汇总各项目…" : "暂无项目" }}
            />
          </Card>
        </>
      )}
    </Space>
  );
}
