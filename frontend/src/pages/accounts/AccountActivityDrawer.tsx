import { Alert, Button, Descriptions, Drawer, Empty, List, Space, Spin, Tag } from "antd";
import { api } from "../../api";
import { useAuthz } from "../../authz/AuthzProvider";
import { accountAccess } from "../../authz/accountAccess";
import { useAuthorizedQuery } from "../../authz/access";
import { surfaceRule } from "../../authz/rules";
import { transportConfig } from "../../authz/transport";
import { formatBusinessDateTime } from "../../utils/date";

interface Activity {
  last_login_at: string | null; total_actions: number;
  recent: { action: string; resource: string | null; ip: string | null; at: string | null }[];
  changes: { action: string; by: string | null; at: string | null; reason: string | null; before: unknown; after: unknown }[];
}
function validateActivity(value: Activity, canonical: boolean) {
  if (!value || !Number.isSafeInteger(value.total_actions) || value.total_actions < 0 ||
      !(value.last_login_at === null || typeof value.last_login_at === "string") || !Array.isArray(value.recent) || !Array.isArray(value.changes)) throw new Error("账号活动协议无效");
  if (canonical && (value.recent.some(row => row.resource !== null) || value.changes.some(row => row.reason !== null || row.before !== null || row.after !== null)))
    throw new Error("账号活动包含未授权内容");
  return value;
}
export default function AccountActivityDrawer({ username, onClose }: { username: string; onClose: () => void }) {
  const { snapshot } = useAuthz();
  const access = accountAccess(snapshot);
  const query = useAuthorizedQuery({ rule: surfaceRule("account_directory.activity"), legacy: access.legacy,
    queryKey: [username], enabled: !!username, query: async capture => validateActivity((await api.get<Activity>(
      `/accounts/${encodeURIComponent(username)}/activity`, transportConfig(capture))).data,
      snapshot?.resources.account_directory?.runtime_state === "enabled"),
  });
  const value = query.data;
  return <Drawer open title={`活动 · ${username}`} width={480} onClose={onClose}>
    {!access.activity ? <Alert type="warning" message="没有账号活动读取权限" /> : query.error
      ? <Alert type="error" message="账号活动暂不可用" action={<Button onClick={query.retry}>重新读取</Button>} /> : !value ? <Spin /> : <>
        <Descriptions column={1} size="small">
          <Descriptions.Item label="最近登录">{formatBusinessDateTime(value.last_login_at)}</Descriptions.Item>
          <Descriptions.Item label="累计操作">{value.total_actions} 次</Descriptions.Item>
        </Descriptions>
        <List size="small" header="账号变更记录" dataSource={value.changes} renderItem={row => <List.Item><Space wrap>
          <Tag>{row.action}</Tag><span>{row.by || "—"}</span><span>{formatBusinessDateTime(row.at)}</span>
        </Space></List.Item>} />
        <List size="small" header="最近活动" dataSource={value.recent} locale={{ emptyText: <Empty description="暂无活动记录" /> }} renderItem={row => <List.Item><Space wrap>
          <Tag>{row.action}</Tag><span>{row.resource || "—"}</span><span>{row.ip || "—"}</span><span>{formatBusinessDateTime(row.at)}</span>
        </Space></List.Item>} />
      </>}
  </Drawer>;
}
