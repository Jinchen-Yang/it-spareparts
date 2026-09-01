import { Progress, Space, Tooltip, Typography } from "antd";
import { LockOutlined } from "@ant-design/icons";
import type { KnownCostStat } from "../../../api/maintenanceBossBoard";
import { RESTRICTED_TEXT } from "./StatCell";

const { Text } = Typography;

/**
 * 「已知申请估算成本（含税）」五件套（plan v1.3 §4.3）。
 *
 * 缺价语义（需求定义 §3.2）：quality=incomplete 时必须显示「不完整/已知下限」，
 * **绝不按 0 计**；无成本权限时整块显示受限，不占位为 0（§5.1 指标槽规则）。
 */
const QUALITY_TEXT: Record<string, string> = {
  actual_only: "全部实际价",
  contains_estimate: "含参照价估算",
  incomplete: "不完整 · 已知下限",
};

function money(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const num = typeof value === "string" ? Number(value) : value;
  if (Number.isNaN(num)) return String(value);
  return num.toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function KnownCostCell({ stat, compact }: { stat?: KnownCostStat | null; compact?: boolean }) {
  if (!stat || stat.state === "restricted") {
    return (
      <Tooltip title="无成本数据权限">
        <Text type="secondary" data-testid="cost-restricted">
          <LockOutlined /> {RESTRICTED_TEXT}
        </Text>
      </Tooltip>
    );
  }
  if (stat.state === "not_imported" || stat.state === "error" || !stat.value) {
    return (
      <Text style={{ color: "#4b3fbb" }} data-testid="cost-blocked">
        ● 尚未导入
      </Text>
    );
  }
  const value = stat.value;
  const incomplete = value.quality === "incomplete";
  const noLines = incomplete && value.known_amount == null;
  const allMissing = incomplete && !noLines
    && value.missing_lines > 0 && Number(value.coverage_pct ?? 0) === 0;
  return (
    <Space direction="vertical" size={0} data-testid="cost-ready">
      <Text strong style={{ fontSize: compact ? 14 : 19 }}>
        {allMissing || noLines ? "暂无可计算成本" : money(value.known_amount)}
        {incomplete && !allMissing && !noLines ? " ≥" : ""}
      </Text>
      <Text type={incomplete ? "warning" : "secondary"} style={{ fontSize: 11.5 }}>
        {noLines
          ? "暂无有效需求明细"
          : allMissing
            ? "全部缺价 · 待补价"
            : QUALITY_TEXT[value.quality] ?? value.quality}
        {value.missing_lines > 0 ? ` · 缺价 ${value.missing_lines} 行` : ""}
      </Text>
      {!compact && value.coverage_pct !== null ? (
        <Tooltip
          title={`实际价 ${money(value.actual_amount)} · 参照价 ${money(value.estimated_amount)}`}
        >
          <Progress
            percent={value.coverage_pct}
            size="small"
            showInfo={false}
            strokeColor={incomplete ? "#faad14" : "#52c41a"}
            style={{ width: 120 }}
          />
        </Tooltip>
      ) : null}
    </Space>
  );
}

export default KnownCostCell;
