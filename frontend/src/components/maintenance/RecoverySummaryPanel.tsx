import { useContextQuery } from "../../authz/contextQuery";
import { withContextSurface, contextSurfaces } from "../../authz/ContextSurface";
import { useModuleAccess } from "../../authz/access";
import { authorizationErrorText } from "../../authz/requestLifecycle";
import { useEffect, useRef, useState } from "react";
import { App, Alert, Card, Tag, Typography } from "antd";
import {
  getMaintenanceProjectRecoverySummary,
  type MaintenanceRecoverySummary,
} from "../../api/maintenanceOperations";
import { qty } from "../../utils/format";
import "./maintenanceOperations.css";

const { Text } = Typography;

const NOT_READY = "尚未接入";

function errorText(error: unknown): string {
  const payload = error as { response?: { data?: { detail?: string | { message?: string } } } };
  const detail = payload.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  return "收回清单加载失败，请重试";
}

function RecoverySummaryPanel({ projectId }: { projectId: string }) {
  const query = useContextQuery({
    entryId: "GET /api/maintenance/projects/stable/{project_id}/recovery-summary", rule: contextSurfaces("maintenance_recovery.summary"),
    queryKey: [projectId], legacyAllowed: true,
    query: async capture => (await getMaintenanceProjectRecoverySummary(projectId, capture)).data,
  });
  const data = query.data, loading = query.loading;
  const permissionDenied = (query.error as { response?: { status?: number } })?.response?.status === 403;
  const module = useModuleAccess("maintenance_recovery", query.context);

  return (
    <Card
      id="recovery-summary"
      title="收回清单"
      extra={loading ? <Tag>加载中</Tag> : <Tag>项目结束收回口径</Tag>}
    >
      {query.error && <Alert type="error" showIcon message={authorizationErrorText(query.error, "收回清单加载失败")} />}
      <div className="bad-return-metric-grid" aria-busy={loading}>
        <div>
          <span>好件收回</span>
          <Text strong>
            {data ? `${qty(data.good_returned_total_qty)} 件` : NOT_READY}
          </Text>
        </div>
        <div>
          <span>坏件返还</span>
          <Text strong>
            {data ? `${qty(data.bad_returned_total_qty)} 件` : NOT_READY}
          </Text>
        </div>
        <div>
          <span>未收回结存</span>
          <Text strong>
            {data ? `${qty(data.remaining_total_qty)} 件` : NOT_READY}
          </Text>
        </div>
      </div>
      {!data && !loading && (
        <Alert
          showIcon
          type="info"
          style={{ marginTop: 12 }}
          message="收回清单尚未接入当前项目事实，请确认项目已结束或有收回记录。"
        />
      )}
      {data && data.remaining_total_qty > 0 && (
        <Text type="secondary" style={{ display: "block", marginTop: 12, fontSize: 12 }}>
          未收回结存 = 前置库当前结存（发货单入账后尚未被返库单收回的部分）。
        </Text>
      )}
    </Card>
  );
}

export default withContextSurface(RecoverySummaryPanel, contextSurfaces("maintenance_recovery.summary"), props => props.projectId);
