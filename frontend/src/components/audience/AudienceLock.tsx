import { useState } from "react";
import { Button, Popover } from "antd";
import { GlobalOutlined, LockOutlined, TeamOutlined, UserDeleteOutlined } from "@ant-design/icons";
import AudiencePicker, { PUBLIC_AUDIENCE, type AudiencePickerProps } from "./AudiencePicker";
import type { AudienceMode } from "../../api/audience";

export const AUDIENCE_LABELS: Record<AudienceMode, string> = {
  public: "公开", private: "私密", include: "部分可见", exclude: "不给谁看",
};
const ICONS = { public: GlobalOutlined, private: LockOutlined, include: TeamOutlined, exclude: UserDeleteOutlined };

export interface AudienceLockProps extends AudiencePickerProps { label?: string }

export function AudienceLock({ label = "字段", value, onChange, ...props }: AudienceLockProps) {
  const [innerValue, setInnerValue] = useState(PUBLIC_AUDIENCE);
  const current = value ?? innerValue;
  const Icon = ICONS[current.mode];
  return <Popover trigger="click" title={`${label}可见性`}
    content={<AudiencePicker {...props} value={current} onChange={(next) => {
      setInnerValue(next); onChange?.(next);
    }} />}>
    <Button type="text" size="small" disabled={props.disabled} data-mode={current.mode}
      aria-label={`${label}可见性：${AUDIENCE_LABELS[current.mode]}`} title={AUDIENCE_LABELS[current.mode]}
      icon={<Icon />} />
  </Popover>;
}

export default AudienceLock;
