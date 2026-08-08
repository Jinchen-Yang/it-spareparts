import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Empty, Space, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Link, useParams } from "react-router-dom";

import {
  getMaintenanceProjectWorkspace,
  type MaintenanceApprovedExpenseRow,
  type MaintenanceCollectionSnapshotRow,
  type MaintenanceProjectWorkspace,
  type MaintenanceSiteRequisitionRow,
} from "../../api/maintenanceOperations";
import ContractPortfolio from "../../components/maintenance/ContractPortfolio";
import ProjectFinancialProgress from "../../components/maintenance/ProjectFinancialProgress";
import ProjectWorkbookActions from "../../components/maintenance/ProjectWorkbookActions";
import WorkbookFourSheetPreview from "../../components/maintenance/WorkbookFourSheetPreview";
import { readMaintenanceCapabilities } from "../../components/maintenance/maintenancePermissions";
import "../../components/maintenance/maintenanceOperations.css";
import PageHeader from "../../components/PageHeader";
import { money } from "../../utils/format";

const requisitionColumns: ColumnsType<MaintenanceSiteRequisitionRow> = [
  { title: "领用日期", dataIndex: "order_date", width: 110, render: (value) => value || "—" },
  { title: "现场领用单", dataIndex: "order_no", width: 150 },
  { title: "合同", dataIndex: "contract_no", width: 140, render: (value) => value || "—" },
  { title: "PN", dataIndex: "pn", width: 150, render: (value) => value || "—" },
  { title: "描述", dataIndex: "description", width: 220, render: (value) => value || "—" },
  { title: "数量", dataIndex: "quantity", width: 80, align: "right" },
  { title: "单位成本", dataIndex: "unit_cost", width: 120, align: "right", render: money },
  { title: "已知成本", dataIndex: "cost_amount", width: 120, align: "right", render: money },
  {
    title: "成本状态",
    dataIndex: "cost_status",
    width: 120,
    render: (value) => value === "missing"
      ? <Tag color="orange">待回填成本</Tag>
      : value === "restricted"
        ? <Tag>成本不可见</Tag>
        : value === "not_counted"
          ? <Tag>未计入成本</Tag>
          : <Tag color="green">已有成本</Tag>,
  },
];

const expenseColumns: ColumnsType<MaintenanceApprovedExpenseRow> = [
  {
    title: "报销单号",
    width: 170,
    render: (_, row) => row.expense_no || row.expense_ref || "—",
  },
  { title: "报销日期", dataIndex: "expense_date", width: 110, render: (value) => value || "—" },
  { title: "合同", dataIndex: "contract_no", width: 140, render: (value) => value || "—" },
  { title: "申请人", dataIndex: "applicant", width: 110, render: (value) => value || "未提供" },
  { title: "费用分类", dataIndex: "category", width: 130, render: (value) => value || "未提供" },
  { title: "支出事由", dataIndex: "expense_reason", render: (value) => value || "未提供" },
  { title: "金额", dataIndex: "amount", width: 120, align: "right", render: money },
  { title: "审批状态", width: 110, render: () => <Tag color="green">审批通过</Tag> },
];

const collectionStatus = (status: string) => {
  if (status === "confirmed") return <Tag color="green">已确认</Tag>;
  if (status === "unconfirmed") return <Tag color="orange">待确认</Tag>;
  if (status === "void") return <Tag>已作废</Tag>;
  return <Tag>{status || "未知"}</Tag>;
};

const collectionColumns: ColumnsType<MaintenanceCollectionSnapshotRow> = [
  {
    title: "报告月",
    dataIndex: "report_month",
    width: 105,
    render: (value: string) => value ? value.slice(0, 7) : "—",
  },
  { title: "合同编号", dataIndex: "contract_no", width: 170, render: (value) => value || "—" },
  {
    title: "累计回款",
    dataIndex: "cumulative_amount",
    width: 130,
    align: "right",
    render: money,
  },
  {
    title: "凭证",
    dataIndex: "receipt_reference",
    width: 180,
    render: (value) => value || "—",
  },
  { title: "状态", dataIndex: "status", width: 100, render: collectionStatus },
  { title: "备注", dataIndex: "remark", render: (value) => value || "—" },
  { title: "版本", dataIndex: "version", width: 75, align: "right" },
];

export default function MaintenanceProjectWorkspacePage({ projectId }: {
  projectId?: string;
}) {
  const params = useParams<{ projectId: string }>();
  const resolvedProjectId = projectId ?? params.projectId ?? "";
  const [workspace, setWorkspace] = useState<MaintenanceProjectWorkspace | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const generation = useRef(0);
  const [{ canManageProject }] = useState(readMaintenanceCapabilities);

  const load = async () => {
    if (!resolvedProjectId) {
      setLoading(false);
      setLoadError(true);
      return;
    }
    const request = ++generation.current;
    setLoading(true);
    setLoadError(false);
    try {
      const { data } = await getMaintenanceProjectWorkspace(resolvedProjectId);
      if (request === generation.current) setWorkspace(data);
    } catch {
      if (request === generation.current) {
        setWorkspace(null);
        setLoadError(true);
      }
    } finally {
      if (request === generation.current) setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    return () => { generation.current += 1; };
    // resolvedProjectId is the complete request identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolvedProjectId]);

  if (loadError) {
    return (
      <Alert
        type="error"
        showIcon
        message="项目工作台加载失败"
        action={<Button size="small" danger onClick={() => void load()}>重试</Button>}
      />
    );
  }
  if (!workspace) {
    return <Card loading={loading}><Empty description="正在读取项目工作台" /></Card>;
  }

  const { project } = workspace;
  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title={project.display_name}
        subtitle={`${project.project_code} · 项目经理 ${project.project_manager_id || "待指定"} · 数据截止 ${workspace.as_of}`}
        extra={(
          <Space wrap>
            {canManageProject && (
              <Link to={`/maintenance/cost-refill?project_id=${encodeURIComponent(project.project_id)}`}>
                去人工回填成本
              </Link>
            )}
          </Space>
        )}
      />

      <div className="maintenance-workspace-two-column">
        <Card title="回款与项目实际成本">
          <ProjectFinancialProgress metrics={project.metrics} />
        </Card>
        <Card title="系统提醒">
          {workspace.reminders.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无系统提醒" />
          ) : (
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              {workspace.reminders.map((reminder) => (
                <Alert
                  key={reminder.reminder_id}
                  showIcon
                  type={reminder.severity === "critical"
                    ? "error" : reminder.severity === "warning" ? "warning" : "info"}
                  message={reminder.title}
                  description={reminder.detail || undefined}
                />
              ))}
            </Space>
          )}
        </Card>
      </div>

      <Card title="全部关联合同">
        <ContractPortfolio contracts={project.contracts} />
      </Card>

      <Card title="回款明细" extra={<Tag>{`截至 ${workspace.as_of}`}</Tag>}>
        <div data-testid="collection-snapshot-table">
          <Table
            rowKey="collection_id"
            size="small"
            columns={collectionColumns}
            dataSource={workspace.collection_snapshots.rows}
            scroll={{ x: 980 }}
            pagination={{ pageSize: 20, showSizeChanger: true }}
            locale={{ emptyText: "暂无回款记录" }}
          />
        </div>
      </Card>

      <Card
        title="现场领用全量明细"
        extra={project.metrics.cost_complete === null
          ? <Tag>成本明细不可见</Tag>
          : project.metrics.cost_complete === false
            ? <Tag color="orange">缺 {project.metrics.missing_cost_lines} 行成本，明细仍完整展示</Tag>
            : <Tag color="green">成本完整</Tag>}
      >
        <div data-testid="site-requisition-table">
          <Table
            rowKey="line_id"
            size="small"
            columns={requisitionColumns}
            dataSource={workspace.requisitions.rows}
            scroll={{ x: 1200 }}
            pagination={{ pageSize: 20, showSizeChanger: true }}
            locale={{ emptyText: "暂无现场领用记录" }}
          />
        </div>
      </Card>

      <Card title="审批通过报销">
        <div data-testid="approved-expense-table">
          <Table
            rowKey="expense_id"
            size="small"
            columns={expenseColumns}
            dataSource={workspace.approved_expenses.rows}
            scroll={{ x: 1120 }}
            pagination={{ pageSize: 20, showSizeChanger: true }}
            locale={{ emptyText: "暂无审批通过报销" }}
          />
        </div>
      </Card>

      <Card
        title="完整项目工作簿"
        extra={<Tag>{`协议 ${workspace.workbook_preview.protocol_version}`}</Tag>}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 14 }}
          message="下载的是当前项目全量四表；仅 01_总览的回款表尾可追加，备件消耗、报销单、项目经理追踪与提醒均由系统生成。"
        />
        <WorkbookFourSheetPreview preview={workspace.workbook_preview} />
        <div style={{ marginTop: 16 }}>
          <ProjectWorkbookActions
            projectId={project.project_id}
            projectCode={project.project_code}
            onApplied={load}
          />
        </div>
      </Card>
    </Space>
  );
}
