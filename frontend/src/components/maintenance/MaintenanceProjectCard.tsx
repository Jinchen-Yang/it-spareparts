import { Badge, Card, Space, Tag } from "antd";
import { Link } from "react-router-dom";

import type { MaintenanceProjectOperationsSummary } from "../../api/maintenanceOperations";
import ContractPortfolio from "./ContractPortfolio";
import ProjectFinancialProgress from "./ProjectFinancialProgress";
import type { ProjectFinancialVisibility } from "./ProjectFinancialProgress";

const LIFECYCLE_META: Record<string, { label: string; color?: string }> = {
  ongoing: { label: "进行中", color: "blue" },
  ended: { label: "已结束" },
  missing: { label: "期限缺失", color: "orange" },
};

export default function MaintenanceProjectCard({ project, visibility }: {
  project: MaintenanceProjectOperationsSummary;
  visibility: ProjectFinancialVisibility;
}) {
  const lifecycle = LIFECYCLE_META[project.lifecycle_status]
    ?? { label: "业务期限待确认", color: "orange" };
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
          查看项目
        </Link>,
      ]}
      styles={{ body: { display: "flex", flexDirection: "column", gap: 14 } }}
    >
      <div style={{ color: "var(--mb-text-2)", fontSize: 12.5 }}>
        项目经理：{project.project_manager_id || "待指定"}
        <span style={{ marginInline: 8 }}>·</span>
        数据截止：{project.as_of || "—"}
      </div>
      <div>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 7 }}>
          全部关联合同
        </div>
        <ContractPortfolio contracts={project.contracts} compact />
      </div>
      <ProjectFinancialProgress metrics={project.metrics} visibility={visibility} />
      <Space wrap size={[6, 6]}>
        {project.reminder_count > 0
          ? <Badge count={project.reminder_count}><Tag color="orange">系统提醒</Tag></Badge>
          : <Tag color="green">暂无提醒</Tag>}
        {!visibility.canViewCost
          ? <Tag>成本不可见</Tag>
          : project.metrics.missing_cost_lines != null
            && project.metrics.missing_cost_lines > 0
            ? <Tag color="orange">成本待补</Tag>
            : project.metrics.cost_complete === null
              ? <Tag>项目总成本状态不可判定</Tag>
              : null}
      </Space>
    </Card>
  );
}
