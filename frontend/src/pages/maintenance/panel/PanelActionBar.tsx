import type { ReactNode } from "react";
import { Space, Typography } from "antd";

const { Text } = Typography;

/**
 * 面板动作栏（v1.36）：统一各 tab 的「下载/上传/登记」入口位置。
 *
 * 布局约定（用户 2026-09-20 拍板）：
 * - 一律放在 tab 内容最顶部；
 * - 默认保持 Excel 往返在前；现场高频页可用 actionsFirst 将登记动作提到最前；
 * - 视图切换（含已作废等）靠右。
 * - 说明文字（hint）单独一行置于按钮行下方，字号 11.5 次要色。
 */
export default function PanelActionBar({
  workbook,
  actions,
  trailing,
  hint,
  actionsFirst = false,
}: {
  /** Excel 下载/上传或导入等批量动作。 */
  workbook?: ReactNode;
  /** 登记类主按钮组。 */
  actions?: ReactNode;
  /** 视图切换/次要控件（右对齐）。 */
  trailing?: ReactNode;
  /** 一行说明文字。 */
  hint?: string;
  /** 高频登记场景把 actions 排在 Excel/导入动作之前。 */
  actionsFirst?: boolean;
}) {
  const hasLeading = workbook != null || actions != null;
  return (
    <Space direction="vertical" size={2} style={{ width: "100%" }}>
      {hasLeading || trailing != null ? (
        <Space
          size={8}
          wrap
          style={
            trailing != null
              ? { width: "100%", justifyContent: "space-between" }
              : undefined
          }
        >
          <Space size={8} wrap>
            {actionsFirst ? actions : workbook}
            {actionsFirst ? workbook : actions}
          </Space>
          {trailing}
        </Space>
      ) : null}
      {hint ? (
        <Text type="secondary" style={{ fontSize: 11.5 }}>
          {hint}
        </Text>
      ) : null}
    </Space>
  );
}
