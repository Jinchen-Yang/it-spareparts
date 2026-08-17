import { Tag, Tooltip, Typography } from "antd";
import { LockOutlined } from "@ant-design/icons";
import type { Stat } from "../../../api/maintenanceBossBoard";

const { Text } = Typography;

/**
 * 六态信封的**唯一渲染入口**（plan v1.3 §4.6 / §5.0）。
 *
 * 硬规则：not_imported / restricted / error **绝不渲染 0 或任何占位数字**
 * （铁律 5：未导入不得伪装成 0）。状态语义映射（§5.0）：
 *   restricted → 受限（🔒 灰字无外框）
 *   not_imported / error → 阻塞（靛紫点；not_imported 文案「尚未导入」）
 *   stale → 正常值 + 依据层黄色小注（不占状态位）
 *   partial → 正常值 + 「部分」角标（数字为已知下限）
 */
export const NOT_IMPORTED_TEXT = "尚未导入";
export const RESTRICTED_TEXT = "受限";
export const ERROR_TEXT = "加载失败";

export interface StatCellProps {
  stat?: Stat<unknown> | null;
  /** 值格式化（仅在有值时调用）。 */
  format?: (value: unknown) => string;
  /** 无值状态下的额外说明（如 admin 侧「去上传」提示文案）。 */
  hint?: string;
}

function formatValue(value: unknown, format?: (v: unknown) => string): string {
  if (format) return format(value);
  if (value === null || value === undefined) return "";
  return String(value);
}

export function StatCell({ stat, format, hint }: StatCellProps) {
  if (!stat) {
    return (
      <Text type="secondary" style={{ opacity: 0.65 }}>
        {NOT_IMPORTED_TEXT}
      </Text>
    );
  }
  switch (stat.state) {
    case "restricted":
      return (
        <Tooltip title="无该数据组权限">
          <Text type="secondary" data-testid="stat-restricted">
            <LockOutlined /> {RESTRICTED_TEXT}
          </Text>
        </Tooltip>
      );
    case "not_imported":
      return (
        <Tooltip title={hint || "该来源尚未导入，系统不以 0 代替"}>
          <Text style={{ color: "#4b3fbb" }} data-testid="stat-not-imported">
            ● {NOT_IMPORTED_TEXT}
          </Text>
        </Tooltip>
      );
    case "error":
      return (
        <Text style={{ color: "#4b3fbb" }} data-testid="stat-error">
          ● {ERROR_TEXT}
        </Text>
      );
    case "partial":
      return (
        <span data-testid="stat-partial">
          <Text strong>{formatValue(stat.value, format)}</Text>{" "}
          <Tooltip
            title={`部分关联：${stat.unlinked ?? 0} 行未关联到项目，数字为已知下限`}
          >
            <Tag color="default" style={{ marginInlineStart: 4 }}>
              部分
            </Tag>
          </Tooltip>
        </span>
      );
    case "stale":
      return (
        <span data-testid="stat-stale">
          <Text strong>{formatValue(stat.value, format)}</Text>{" "}
          <Text type="warning" style={{ fontSize: 11.5 }}>
            截至 {stat.as_of ?? "—"}
          </Text>
        </span>
      );
    case "ready":
    default:
      return (
        <Text strong data-testid="stat-ready">
          {formatValue(stat.value, format)}
        </Text>
      );
  }
}

export default StatCell;
