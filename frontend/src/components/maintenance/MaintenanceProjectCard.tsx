import { Badge, Card, Space, Tag } from "antd";
import { Link } from "react-router-dom";

import type { MaintenanceProjectOperationsSummary } from "../../api/maintenanceOperations";
import ContractPortfolio from "./ContractPortfolio";
import ProjectFinancialProgress from "./ProjectFinancialProgress";
import type { ProjectFinancialVisibility } from "./ProjectFinancialProgress";
import ProjectManagerAssignmentControl from "./ProjectManagerAssignmentControl";

const LIFECYCLE_META: Record<string, { label: string; color?: string }> = {
  ongoing: { label: "服务中", color: "blue" },
  ended: { label: "已结项" },
  missing: { label: "期限待确认", color: "orange" },
};

function ProjectReturnStatus({ project }: { project: MaintenanceProjectOperationsSummary }) {
  const rate = project.return_rate;
  if (!rate) return null;
  return (
    <Space data-testid="project-return-status" wrap size={[6, 6]}>
      {rate.status === "available" && rate.warehouse_confirmed_rate_pct != null ? (
        <Tag color="blue">返还率试算 {rate.warehouse_confirmed_rate_pct}%</Tag>
      ) : rate.status === "basis_incomplete" ? (
        <Tag color="orange">返还率待判定</Tag>
      ) : (
        <Tag color="green">无应返项</Tag>
      )}
      <Tag>应返 {rate.required_quantity}</Tag>
      <Tag>已登记 {rate.registered_quantity}</Tag>
      <Tag>仓库确认 {rate.warehouse_confirmed_quantity}</Tag>
      {rate.status !== "no_return_required" && (
        <Tag>待仓库确认 {rate.outstanding_quantity}</Tag>
      )}
      {Number(rate.exempt_quantity) > 0 && (
        <Tag color="green">硬盘免返 {rate.exempt_quantity}</Tag>
      )}
      {rate.pending_count > 0 && (
        <Tag color="orange">品类待判定 {rate.pending_quantity}</Tag>
      )}
    </Space>
  );
}

export default function MaintenanceProjectCard({
  project,
  visibility,
  canManageAssignment = false,
  onAssignmentChanged,
}: {
  project: MaintenanceProjectOperationsSummary;
  visibility: ProjectFinancialVisibility;
  canManageAssignment?: boolean;
  onAssignmentChanged?: () => void;
}) {
  const lifecycle = LIFECYCLE_META[project.lifecycle_status]
    ?? { label: "期限待确认", color: "orange" };
  const assignment = project.manager_assignment;
  const primaryTask = project.task_summary?.primary;
  const missingLabels = project.missing_data_labels ?? [];
  // 2026-08-25 客户拍板：「项目经理月度更新」任务随月度全量表入口退役，
  // 「上传月度全量表」卡片链接（目标路由已重定向回维保主页）一并去掉。
  const tracking = project.manager_tracking;
  return (
    <Card
      data-testid={`maintenance-project-card-${project.project_id}`}
      className="maintenance-project-card"
      title={(
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 12, color: "var(--mb-text-3)", marginBottom: 2 }}>
            {project.project_code}
          </div>
          <div title={project.display_name} style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
            {project.display_name}
          </div>
        </div>
      )}
      extra={<Tag color={lifecycle.color}>{lifecycle.label}</Tag>}
      actions={[
        <Link key="detail" to={`/maintenance/beta/projects/${encodeURIComponent(project.project_id)}`}>
          进入项目
        </Link>,
        ...(canManageAssignment ? [
          <ProjectManagerAssignmentControl
            key="manager"
            project={project}
            canManage
            onChanged={onAssignmentChanged ?? (() => undefined)}
          />,
        ] : []),
      ]}
      styles={{ body: { display: "flex", flexDirection: "column", gap: 14 } }}
    >
      <div className="maintenance-project-card-meta">
        <div>
          维保负责人：
          {assignment
            ? `${assignment.display_name || assignment.username} · ${assignment.username}`
            : "未映射系统账号"}
          {assignment?.account_status === "inactive" && (
            <Tag color="red" style={{ marginInlineStart: 6 }}>负责人账号失效</Tag>
          )}
        </div>
        <div>
          来源负责人原文：{project.project_manager_id || "未提供"}
          <span style={{ marginInline: 8 }}>·</span>
          数据更新至：{project.as_of || "—"}
        </div>
      </div>
      {primaryTask && (
        <div className={`maintenance-project-task ${primaryTask.is_overdue ? "is-overdue" : ""}`}>
          <Space wrap size={[6, 6]}>
            <Tag color={primaryTask.is_overdue ? "red" : primaryTask.severity === "critical" ? "red" : "gold"}>
              {primaryTask.task_type}
            </Tag>
            <Tag>{primaryTask.status === "completed" ? "已完成" : "待处理"}</Tag>
            {primaryTask.is_overdue && <Tag color="red">已逾期</Tag>}
          </Space>
          <div className="maintenance-project-task-title">{primaryTask.title}</div>
          <div>
            截止：{primaryTask.due_date || "无固定日期"}
            <span style={{ marginInline: 8 }}>·</span>
            待办 {project.task_summary.open_count} 项
            {project.task_summary.overdue_count > 0
              ? `，逾期 ${project.task_summary.overdue_count} 项`
              : ""}
          </div>
          <div>完成依据：{primaryTask.close_basis}</div>
        </div>
      )}
      <div>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 7 }}>
          关联合同
        </div>
        <ContractPortfolio contracts={project.contracts} compact />
      </div>
      <ProjectFinancialProgress metrics={project.metrics} visibility={visibility} />
      <ProjectReturnStatus project={project} />
      {tracking && (
        <div className="maintenance-project-task" data-testid="manager-tracking-summary">
          <Space wrap size={[6, 6]}>
            <Tag>
              维保：{tracking.service_period.service_start || "待补"}
              {" ～ "}{tracking.service_period.service_end || "待补"}
            </Tag>
            {tracking.next_collection_milestone ? (
              <Tag color={tracking.next_collection_milestone.is_overdue ? "red" : "blue"}>
                下一回款：{tracking.next_collection_milestone.contract_no || "未标合同"}
                {` 第 ${tracking.next_collection_milestone.sequence} 期`}
                {tracking.next_collection_milestone.is_overdue
                  ? `，逾期 ${tracking.next_collection_milestone.overdue_days} 天`
                  : ""}
              </Tag>
            ) : <Tag color="orange">回款计划待补</Tag>}
            {/* 2026-08-25 客户拍板：验收无截止日概念、只是个上传的地方——
                验收标签改挂提交状态，不再渲染截止日/逾期（旧口径死胡同）。 */}
            <Tag color={tracking.acceptance.submission_status === "submitted" ? "green" : "gold"}>
              验收：{tracking.acceptance.submission_status === "submitted" ? "已提交" : "待提交"}
            </Tag>
          </Space>
        </div>
      )}
      <Space wrap size={[6, 6]}>
        {missingLabels.map((label) => (
          <Tag key={label} color="orange">{label}</Tag>
        ))}
        {project.reminder_count > 0
          ? <Badge count={project.reminder_count}><Tag color="orange">待办</Tag></Badge>
          : <Tag color="green">无待办</Tag>}
        {missingLabels.length === 0 && (!visibility.canViewCost
          ? <Tag>无权限查看</Tag>
          : project.metrics.missing_cost_lines != null
            && project.metrics.missing_cost_lines > 0
            ? <Tag color="orange">部分领用缺成本</Tag>
            : project.metrics.cost_complete === null
              ? <Tag>项目总成本状态不可判定</Tag>
              : null)}
      </Space>
    </Card>
  );
}
