import { Typography } from "antd";
import type { MaintenanceOrderContact } from "../../api/maintenanceOrderContact";

const FIELDS = [
  ["receiver_address", "收货地址"],
  ["receiver", "联系人"],
  ["receiver_phone", "电话"],
] as const;

/** Per-WBDD contact block; trust the server's visibility state, never local role. */
export default function OrderContactInfo({ contact }: { contact: MaintenanceOrderContact }) {
  if (contact.contact_info_state !== "visible") {
    return <Typography.Text type="secondary">无权限查看联系信息</Typography.Text>;
  }
  return (
    <div style={{ minWidth: 220, maxWidth: 360, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
      {FIELDS.map(([field, label]) => {
        const value = contact[field];
        const hasValue = Boolean(value?.trim());
        return (
          <div key={field}>
            <Typography.Text type="secondary">{label}：</Typography.Text>
            <Typography.Text copyable={hasValue ? { text: value! } : false}>
              {hasValue ? value : "—"}
            </Typography.Text>
          </div>
        );
      })}
    </div>
  );
}
