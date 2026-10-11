import { Typography } from "antd";

/** Actual place of use, kept separate from the demand order's shipping address. */
export default function SiteIssueAddress({
  value,
  showLabel = true,
}: {
  value: string | null | undefined;
  showLabel?: boolean;
}) {
  const hasValue = Boolean(value?.trim());
  return (
    <div style={{ minWidth: 0, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
      {showLabel && <Typography.Text type="secondary">领用地址：</Typography.Text>}
      <Typography.Text copyable={hasValue ? { text: value! } : false}>
        {hasValue ? value : "未填写"}
      </Typography.Text>
    </div>
  );
}
