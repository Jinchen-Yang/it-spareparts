import { Alert, Button, Empty, Space, Tag } from "antd";
import { Link } from "react-router-dom";

import {
  formatDecimalAmount,
  type CollectionFollowUpAction,
  type CollectionMilestoneRow,
  type CollectionProjectDetailResponse,
  type CollectionReminderState,
} from "../../api/maintenanceCollectionReminders";
import ResizableTable from "../ResizableTable";
import {
  COLLECTION_OPERATION_LABELS,
  COLLECTION_PAGE,
  COLLECTION_STATE_LABELS,
} from "./maintenanceLanguage";
import type { MaintenanceCapabilities } from "./maintenancePermissions";

function amountText(value: string | null): string {
  const formatted = formatDecimalAmount(value);
  return formatted ? `¥${formatted}` : COLLECTION_PAGE.amountRestricted;
}

function stateTag(state: CollectionReminderState) {
  const item = COLLECTION_STATE_LABELS[state] ?? { label: state };
  return <Tag color={item.color}>{item.label}</Tag>;
}

function managerName(detail: CollectionProjectDetailResponse): string {
  const { display_name, username } = detail.project.manager_assignment;
  return display_name || username || "—";
}

function servicePeriodText(detail: CollectionProjectDetailResponse): string {
  const { service_start, service_end } = detail.project.service_period;
  if (!service_start && !service_end) return "—";
  return `${service_start ?? "—"} ~ ${service_end ?? "—"}`;
}

function contractText(detail: CollectionProjectDetailResponse): string {
  const nos = detail.project.contracts
    .map((contract) => contract.contract_no)
    .filter((no): no is string => Boolean(no));
  return nos.length > 0 ? nos.join("、") : "—";
}

function lastOperationText(row: CollectionMilestoneRow): string {
  if (!row.last_operation) return "—";
  const label = COLLECTION_OPERATION_LABELS[row.last_operation.action]
    ?? row.last_operation.action;
  return `${label} · ${row.last_operation.actor_display_name}`;
}

interface CollectionReminderDetailProps {
  detail: CollectionProjectDetailResponse | null;
  loading: boolean;
  error: boolean;
  selected: boolean;
  capabilities: MaintenanceCapabilities;
  /** 操作弹窗打开时隐藏行操作按钮，避免与弹窗内同名文案冲突。 */
  actionsHidden?: boolean;
  onFollowUp: (
    row: CollectionMilestoneRow,
    action: CollectionFollowUpAction,
    event: React.MouseEvent<HTMLElement>,
  ) => void;
  onImportPlan: () => void;
  onRetry: () => void;
}

export default function CollectionReminderDetail({
  detail,
  loading,
  error,
  selected,
  capabilities,
  actionsHidden = false,
  onFollowUp,
  onImportPlan,
  onRetry,
}: CollectionReminderDetailProps) {
  const canFollowUp = capabilities.canFollowUpCollection;
  const summary = detail?.summary;

  const actionButtons = (row: CollectionMilestoneRow) => {
    if (actionsHidden) return null;
    const state = row.reminder_state;
    if (state === "needs_review") {
      return (
        <Space direction="vertical" size={4}>
          <Space wrap>
            {canFollowUp && (
              <Button size="small" onClick={(event) => onFollowUp(row, "reopen", event)}>
                {COLLECTION_PAGE.actionReopen}
              </Button>
            )}
          </Space>
          <span style={{ fontSize: 12, color: "#722ed1" }}>{COLLECTION_PAGE.needsReviewHint}</span>
        </Space>
      );
    }
    if (state === "incomplete") {
      return (
        <span style={{ fontSize: 12, color: "#d46b08" }}>{COLLECTION_PAGE.incompleteHint}</span>
      );
    }
    if (state === "handled") {
      return canFollowUp ? (
        <Button size="small" onClick={(event) => onFollowUp(row, "reopen", event)}>
          {COLLECTION_PAGE.actionReopen}
        </Button>
      ) : null;
    }
    if (!canFollowUp) return null;
    return (
      <Space wrap>
        <Button size="small" onClick={(event) => onFollowUp(row, "handle", event)}>
          {COLLECTION_PAGE.actionHandle}
        </Button>
        <Button size="small" onClick={(event) => onFollowUp(row, "reschedule", event)}>
          {COLLECTION_PAGE.actionReschedule}
        </Button>
      </Space>
    );
  };

  const columns = [
    { key: "contract", title: COLLECTION_PAGE.colContract, render: (_: unknown, row: CollectionMilestoneRow) => row.contract_no ?? "—" },
    { key: "sequence", title: COLLECTION_PAGE.colSequence, render: (_: unknown, row: CollectionMilestoneRow) => COLLECTION_PAGE.sequenceOf(row.sequence) },
    { key: "planned_month", title: COLLECTION_PAGE.colPlannedMonth, render: (_: unknown, row: CollectionMilestoneRow) => row.planned_month ?? "—" },
    { key: "planned_amount", title: COLLECTION_PAGE.colPlannedAmount, render: (_: unknown, row: CollectionMilestoneRow) => amountText(row.planned_amount) },
    { key: "state", title: COLLECTION_PAGE.colState, render: (_: unknown, row: CollectionMilestoneRow) => stateTag(row.reminder_state) },
    { key: "last_operation", title: COLLECTION_PAGE.colLastOperation, render: (_: unknown, row: CollectionMilestoneRow) => lastOperationText(row) },
    { key: "actions", title: COLLECTION_PAGE.colActions, render: (_: unknown, row: CollectionMilestoneRow) => actionButtons(row) },
  ];

  return (
    <div data-testid="mcr-detail-pane" className="mcr-detail-pane" style={{ minWidth: 0 }}>
      {!selected && <Empty description={COLLECTION_PAGE.emptyDetail} />}
      {selected && error && (
        <Space direction="vertical" size={8}>
          <Alert type="error" showIcon message={COLLECTION_PAGE.detailLoadFailed} />
          <Button onClick={onRetry}>{COLLECTION_PAGE.retry}</Button>
        </Space>
      )}
      {selected && !error && !detail && <Empty description={COLLECTION_PAGE.emptyDetail} />}
      {selected && !error && detail && (
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <div>
            <Space align="center" wrap style={{ width: "100%", justifyContent: "space-between" }}>
              <h3 style={{ margin: 0, fontSize: 17, fontWeight: 600 }}>
                {detail.project.display_name}
              </h3>
              {detail.project.project_id && (
                <Link to={`/maintenance/beta/projects/${encodeURIComponent(detail.project.project_id)}`}>
                  {COLLECTION_PAGE.viewFullProject}
                </Link>
              )}
            </Space>
            <div style={{ fontSize: 13, color: "var(--mb-text-2)", marginTop: 4 }}>
              {detail.project.project_code}
            </div>
            <div style={{ fontSize: 13, color: "var(--mb-text-2)", marginTop: 4 }}>
              {COLLECTION_PAGE.managerLabel}：{managerName(detail)}
            </div>
            <div style={{ fontSize: 13, color: "var(--mb-text-2)", marginTop: 4 }}>
              {COLLECTION_PAGE.servicePeriodLabel}：{servicePeriodText(detail)}
            </div>
            <div style={{ fontSize: 13, color: "var(--mb-text-2)", marginTop: 4 }}>
              {COLLECTION_PAGE.contractsLabel}：{contractText(detail)}
            </div>
          </div>
          <Alert type="info" showIcon message={COLLECTION_PAGE.disclaimer} />
          {summary && (
            <div className="mcr-metrics" style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              <div className="mcr-metric">
                <div className="mcr-metric-value">{summary.total}</div>
                <div className="mcr-metric-label">{COLLECTION_PAGE.metricMilestones}</div>
              </div>
              <div className={`mcr-metric${summary.needs_review > 0 ? " is-emphasized" : ""}`}>
                <div className="mcr-metric-value">{summary.needs_review}</div>
                <div className="mcr-metric-label">{COLLECTION_PAGE.metricNeedsReview}</div>
              </div>
              <div className="mcr-metric">
                <div className="mcr-metric-value">{summary.due_this_month}</div>
                <div className="mcr-metric-label">{COLLECTION_PAGE.metricDueThisMonth}</div>
              </div>
              <div className="mcr-metric">
                <div className="mcr-metric-value">{summary.overdue}</div>
                <div className="mcr-metric-label">{COLLECTION_PAGE.metricOverdue}</div>
              </div>
              <div className="mcr-metric">
                <div className="mcr-metric-value">{summary.handled}</div>
                <div className="mcr-metric-label">{COLLECTION_PAGE.metricHandled}</div>
              </div>
            </div>
          )}
          {detail.rows.length === 0 ? (
            <Space direction="vertical" size={8}>
              <Empty description={COLLECTION_PAGE.noPlanHint} />
              {capabilities.canImportCollectionPlan && (
                <Button onClick={onImportPlan}>{COLLECTION_PAGE.importPlan}</Button>
              )}
            </Space>
          ) : (
            <ResizableTable<CollectionMilestoneRow>
              storageKey="maintenance-collection-reminders-detail"
              rowKey="milestone_id"
              size="small"
              loading={loading}
              dataSource={detail.rows}
              columns={columns}
              pagination={false}
            />
          )}
        </Space>
      )}
    </div>
  );
}
