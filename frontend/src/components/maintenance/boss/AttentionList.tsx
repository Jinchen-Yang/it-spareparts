import { Alert, Card, List, Typography } from "antd";
import type { BoardAttention } from "../../../api/maintenanceBossBoard";

const { Text } = Typography;

/**
 * 需关注事项（plan v1.3 §5.1 首屏第三段，≤10 条）。
 *
 * M0-A（老板据此做什么决定）未拍板前：队列为空并明示待确认——不预置内容、
 * 不替业务拍板（计划 §2.1）。
 */
export function AttentionList({ attention }: { attention?: BoardAttention | null }) {
  if (!attention) return null;
  if (attention.pending_decision) {
    return (
      <Card size="small" title="需关注事项" data-testid="attention-pending">
        <Alert
          type="info"
          showIcon
          message="待业务确认「每周/每月据此做什么决定」后启用"
          description={`该队列的内容口径属 ${attention.pending_decision} 待拍板项；在书面确认前不预置任何条目。`}
        />
      </Card>
    );
  }
  return (
    <Card size="small" title="需关注事项" data-testid="attention-list">
      <List
        size="small"
        dataSource={attention.items}
        locale={{ emptyText: "本期没有需要关注的事项" }}
        renderItem={(item) => (
          <List.Item>
            <Text>{item.kind}</Text>
          </List.Item>
        )}
      />
    </Card>
  );
}

export default AttentionList;
