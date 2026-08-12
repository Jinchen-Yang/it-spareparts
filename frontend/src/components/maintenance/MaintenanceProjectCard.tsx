import { Badge, Card, Space, Tag } from "antd";
import { Link } from "react-router-dom";

import type { MaintenanceProjectOperationsSummary } from "../../api/maintenanceOperations";
import ContractPortfolio from "./ContractPortfolio";
import ProjectFinancialProgress from "./ProjectFinancialProgress";
import { LIFECYCLE_LABELS, TERM, HINTS, ACTIONS } from "./maintenanceLanguage";

export default function MaintenanceProjectCard({ project }: {
  project: MaintenanceProjectOperationsSummary;
}) {
  const lifecycle = LIFECYCLE_LABELS[project.lifecycle_status]
    ?? { label: "期限待确认", color: "orange" };
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
        <Link key="detail" to={`/maintenance/projects/${encodeURIComponent(project.project_id)}`}>
          {ACTIONS.openProject}
        </Link>,
      ]}
      styles={{ body: { display: "flex", flexDirection: "column", gap: 14 } }}
    >
      <div style={{ color: "var(--mb-text-2)", fontSize: 12.5 }}>
        {TERM.projectManager}：{project.project_manager_id || "待指定"}
        <span style={{ marginInline: 8 }}>·</span>
        {HINTS.dataAsOf(project.as_of || "—")}
      </div>
      <div>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 7 }}>
          关联合同
        </div>
        <ContractPortfolio contracts={project.contracts} compact />
      </div>
      <ProjectFinancialProgress metrics={project.metrics} />
      <Space wrap size={[6, 6]}>
        {project.reminder_count > 0
          ? <Badge count={project.reminder_count}><Tag color="orange">{project.reminder_count} 项待办</Tag></Badge>
          : <Tag color="green">无待办</Tag>}
        {project.metrics.cost_complete === false && (
          <Tag color="orange">{HINTS.costIncomplete(project.metrics.missing_cost_lines ?? 0)}</Tag>
        )}
        {project.metrics.cost_complete === null && <Tag>{TERM.hidden}</Tag>}
      </Space>
    </Card>
  );
}
