import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Descriptions, Empty, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Link, useParams } from "react-router-dom";

import {
  getMaintenanceProjectWorkspace,
  type MaintenanceApprovedExpenseRow,
  type MaintenanceCollectionSnapshotRow,
  type MaintenanceProjectWorkspace,
  type MaintenanceSiteRequisitionRow,
  type MaintenanceWorkspaceParams,
} from "../../api/maintenanceOperations";
import ContractPortfolio from "../../components/maintenance/ContractPortfolio";
import BadReturnPanel from "../../components/maintenance/BadReturnPanel";
import MaintenanceAcceptancePanel from "../../components/maintenance/MaintenanceAcceptancePanel";
import ProjectFinancialProgress from "../../components/maintenance/ProjectFinancialProgress";
import ProjectWorkbookActions from "../../components/maintenance/ProjectWorkbookActions";
import SiteIssueWorkflowPanel from "../../components/maintenance/SiteIssueWorkflowPanel";
import WorkbookFourSheetPreview from "../../components/maintenance/WorkbookFourSheetPreview";
import { readMaintenanceCapabilities } from "../../components/maintenance/maintenancePermissions";
import "../../components/maintenance/maintenanceOperations.css";
import PageHeader from "../../components/PageHeader";
import {
  taxSidesForBasis,
  useTaxBasis,
  type TaxBasis,
  type TaxSide,
} from "../../context/TaxBasis";
import { money } from "../../utils/format";

const taxSideLabel = (side: TaxSide) => side === "inc" ? "含税" : "不含税";

const requisitionBaseColumns: ColumnsType<MaintenanceSiteRequisitionRow> = [
  { title: "领用日期", dataIndex: "order_date", width: 110, render: (value) => value || "—" },
  { title: "现场领用单", dataIndex: "order_no", width: 150 },
  { title: "合同", dataIndex: "contract_no", width: 140, render: (value) => value || "—" },
  { title: "PN", dataIndex: "pn", width: 150, render: (value) => value || "—" },
  { title: "描述", dataIndex: "description", width: 220, render: (value) => value || "—" },
  { title: "数量", dataIndex: "quantity", width: 80, align: "right" },
];

const requisitionStatusColumn: ColumnsType<MaintenanceSiteRequisitionRow>[number] = {
  title: "成本状态",
  dataIndex: "cost_status",
  width: 120,
  render: (value, row) => value === "missing"
    ? <Tag color="orange">待回填成本</Tag>
    : value === "restricted"
      ? <Tag>成本不可见</Tag>
      : value === "not_counted"
        ? <Tag>未计入成本</Tag>
        : row.cost_is_estimate
          ? <Tag color="gold">已计入（估算）</Tag>
          : <Tag color="green">已计入成本</Tag>,
};

const requisitionEvidenceColumn: ColumnsType<MaintenanceSiteRequisitionRow>[number] = {
  title: "取价依据",
  dataIndex: "cost_source_label",
  width: 220,
  render: (value, row) => {
    if (row.cost_status === "restricted") return <Tag>不可见</Tag>;
    if (!value) return <Tag>待补价格</Tag>;
    if (row.cost_is_estimate) return <Tag color="gold">{value}</Tag>;
    if (row.cost_evidence_kind === "purchase_evidence") return <Tag color="blue">{value}</Tag>;
    if (row.cost_evidence_kind === "manual_confirmed") return <Tag color="green">{value}</Tag>;
    return <Tag>{value}</Tag>;
  },
};

function requisitionColumns(basis: TaxBasis): ColumnsType<MaintenanceSiteRequisitionRow> {
  const sides = taxSidesForBasis(basis);
  return [
    ...requisitionBaseColumns,
    ...sides.map((side) => ({
      title: `单位成本（${taxSideLabel(side)}）`,
      key: `unit_cost_${side}`,
      dataIndex: side === "inc" ? "unit_cost_inc_tax" : "unit_cost_ex_tax",
      width: 130,
      align: "right" as const,
      render: money,
    })),
    ...sides.map((side) => ({
      title: `已计成本（${taxSideLabel(side)}）`,
      key: `cost_amount_${side}`,
      dataIndex: side === "inc" ? "cost_amount_inc_tax" : "cost_amount_ex_tax",
      width: 130,
      align: "right" as const,
      render: money,
    })),
    requisitionEvidenceColumn,
    requisitionStatusColumn,
  ];
}

const expenseBaseColumns: ColumnsType<MaintenanceApprovedExpenseRow> = [
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
];

function expenseColumns(basis: TaxBasis): ColumnsType<MaintenanceApprovedExpenseRow> {
  return [
    ...expenseBaseColumns,
    ...taxSidesForBasis(basis).map((side) => ({
      title: `金额（${taxSideLabel(side)}）`,
      key: `amount_${side}`,
      dataIndex: side === "inc" ? "amount_inc_tax" : "amount_ex_tax",
      width: 130,
      align: "right" as const,
      render: money,
    })),
    { title: "审批状态", width: 110, render: () => <Tag color="green">审批通过</Tag> },
  ];
}

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

const INITIAL_DETAIL_PAGES: Required<MaintenanceWorkspaceParams> = {
  collection_page: 1,
  collection_page_size: 20,
  requisition_page: 1,
  requisition_page_size: 20,
  expense_page: 1,
  expense_page_size: 20,
};

export default function MaintenanceProjectWorkspacePage({ projectId }: {
  projectId?: string;
}) {
  const params = useParams<{ projectId: string }>();
  const maintenanceBasis = useTaxBasis("maintenance");
  const resolvedProjectId = projectId ?? params.projectId ?? "";
  const [workspace, setWorkspace] = useState<MaintenanceProjectWorkspace | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [detailPages, setDetailPages] = useState(INITIAL_DETAIL_PAGES);
  const generation = useRef(0);
  const [capabilities] = useState(readMaintenanceCapabilities);
  const {
    canManageBadReturns,
    canManageSiteIssues,
    canManageProject,
    canViewCost,
    canViewExpense,
  } = capabilities;

  const load = async (
    requestedPages: Required<MaintenanceWorkspaceParams> = detailPages,
  ) => {
    if (!resolvedProjectId) {
      setLoading(false);
      setLoadError(true);
      return;
    }
    const request = ++generation.current;
    setLoading(true);
    setLoadError(false);
    try {
      const { data } = await getMaintenanceProjectWorkspace(
        resolvedProjectId,
        requestedPages,
      );
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
    setDetailPages(INITIAL_DETAIL_PAGES);
    setWorkspace(null);
    void load(INITIAL_DETAIL_PAGES);
    return () => { generation.current += 1; };
    // resolvedProjectId is the complete request identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolvedProjectId]);

  const loadCollectionPage = (page: number, pageSize: number) => {
    const next = {
      ...detailPages,
      collection_page: page,
      collection_page_size: pageSize,
    };
    setDetailPages(next);
    void load(next);
  };

  const loadRequisitionPage = (page: number, pageSize: number) => {
    const next = {
      ...detailPages,
      requisition_page: page,
      requisition_page_size: pageSize,
    };
    setDetailPages(next);
    void load(next);
  };

  const loadExpensePage = (page: number, pageSize: number) => {
    const next = {
      ...detailPages,
      expense_page: page,
      expense_page_size: pageSize,
    };
    setDetailPages(next);
    void load(next);
  };

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
  const managerLabel = project.manager_assignment
    ? `${project.manager_assignment.display_name || project.manager_assignment.username} · ${project.manager_assignment.username}`
    : "未映射系统账号";
  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title={project.display_name}
        subtitle={`${project.project_code} · 项目经理 ${managerLabel} · 数据截止 ${workspace.as_of}`}
        extra={(
          <Space wrap>
            <Tag>{`已人工归属历史维保单 ${project.manual_source_order_count} 张`}</Tag>
            <Link to={`/maintenance/beta/project-master/source-orders?project_id=${encodeURIComponent(project.project_id)}`}>
              查看归属明细
            </Link>
            {canManageProject && (
              <Link to={`/maintenance/beta/cost-refill?project_id=${encodeURIComponent(project.project_id)}`}>
                去人工回填成本
              </Link>
            )}
          </Space>
        )}
      />

      <div className="maintenance-workspace-two-column">
        <Card title="回款与项目已计成本">
          <ProjectFinancialProgress metrics={project.metrics} visibility={capabilities} />
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

      <Card title="项目跟踪与验收" data-testid="manager-tracking-card">
        {project.manager_tracking ? (
          <Space direction="vertical" size={16} style={{ width: "100%" }}>
            <Descriptions bordered size="small" column={{ xs: 1, md: 3 }}>
              <Descriptions.Item label="维保开始">
                {project.manager_tracking.service_period.service_start || (
                  <Typography.Text type="secondary">待补</Typography.Text>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="维保结束">
                {project.manager_tracking.service_period.service_end || (
                  <Typography.Text type="secondary">待补</Typography.Text>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="下一回款计划">
                {project.manager_tracking.next_collection_milestone ? (
                  <Space wrap size={4}>
                    <span>
                      {project.manager_tracking.next_collection_milestone.contract_no || "未标合同"}
                      {` · 第 ${project.manager_tracking.next_collection_milestone.sequence} 期 · `}
                      {project.manager_tracking.next_collection_milestone.planned_date || "日期待补"}
                    </span>
                    {project.manager_tracking.next_collection_milestone.is_overdue && (
                      <Tag color="red">
                        已逾期 {project.manager_tracking.next_collection_milestone.overdue_days} 天
                      </Tag>
                    )}
                  </Space>
                ) : <Typography.Text type="secondary">计划节点待补</Typography.Text>}
              </Descriptions.Item>
            </Descriptions>
            <MaintenanceAcceptancePanel
              projectId={project.project_id}
              onChanged={() => void load(detailPages)}
            />
          </Space>
        ) : (
          <Alert type="warning" showIcon message="项目跟踪字段尚未生成，请刷新后重试。" />
        )}
      </Card>

      <Card title="全部关联合同">
        <ContractPortfolio contracts={project.contracts} />
      </Card>

      <SiteIssueWorkflowPanel
        projectId={project.project_id}
        canManage={canManageSiteIssues}
        onChanged={() => void load(detailPages)}
      />

      <BadReturnPanel
        projectId={project.project_id}
        returnRate={workspace.return_rate ?? project.return_rate}
        canManage={canManageBadReturns}
        onChanged={() => void load(detailPages)}
      />

      <Card title="回款明细" extra={<Tag>{`截至 ${workspace.as_of}`}</Tag>}>
        <div data-testid="collection-snapshot-table">
          <Table
            rowKey="collection_id"
            size="small"
            columns={collectionColumns}
            dataSource={workspace.collection_snapshots.rows}
            loading={loading}
            scroll={{ x: 980 }}
            pagination={{
              current: workspace.collection_snapshots.page,
              pageSize: workspace.collection_snapshots.page_size,
              total: workspace.collection_snapshots.total,
              showSizeChanger: true,
              onChange: loadCollectionPage,
            }}
            locale={{ emptyText: "暂无回款记录" }}
          />
        </div>
      </Card>

      <Card
        title="现场领用全量明细"
        extra={!canViewCost
          ? <Tag>成本明细不可见</Tag>
          : project.metrics.missing_cost_lines != null
            && project.metrics.missing_cost_lines > 0
            ? <Tag color="orange">缺 {project.metrics.missing_cost_lines} 行成本，明细仍完整展示</Tag>
            : project.metrics.cost_complete === true
              ? <Tag color="green">成本完整</Tag>
              : !canViewExpense
                ? <Tag>现场领用成本可见，报销费用不可见</Tag>
                : <Tag>项目总成本完整度待确认</Tag>}
      >
        <div data-testid="site-requisition-table">
          <Table
            rowKey="line_id"
            size="small"
            columns={requisitionColumns(maintenanceBasis)}
            dataSource={workspace.requisitions.rows}
            loading={loading}
            scroll={{ x: maintenanceBasis === "both" ? 1680 : 1420 }}
            pagination={{
              current: workspace.requisitions.page,
              pageSize: workspace.requisitions.page_size,
              total: workspace.requisitions.total,
              showSizeChanger: true,
              onChange: loadRequisitionPage,
            }}
            locale={{ emptyText: "暂无现场领用记录" }}
          />
        </div>
      </Card>

      <Card title="审批通过报销">
        <div data-testid="approved-expense-table">
          <Table
            rowKey="expense_id"
            size="small"
            columns={expenseColumns(maintenanceBasis)}
            dataSource={workspace.approved_expenses.rows}
            loading={loading}
            scroll={{ x: maintenanceBasis === "both" ? 1250 : 1120 }}
            pagination={{
              current: workspace.approved_expenses.page,
              pageSize: workspace.approved_expenses.page_size,
              total: workspace.approved_expenses.total,
              showSizeChanger: true,
              onChange: loadExpensePage,
            }}
            locale={{ emptyText: "暂无审批通过报销" }}
          />
        </div>
      </Card>

      <Card
        id="project-workbook"
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
            onApplied={() => load(detailPages)}
          />
        </div>
      </Card>
    </Space>
  );
}
