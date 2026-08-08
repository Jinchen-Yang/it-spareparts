import { Progress, Space, Tag } from "antd";

import { money } from "../../utils/format";

export type CostWaterlineStatus = "normal" | "yellow" | "red" | "unknown" | "restricted" | "no_contract";

export interface ProjectFinancialMetrics {
  total_contract_amount: number | null;
  contract_amount_complete: boolean;
  received_amount: number | null;
  site_requisition_known_cost: number | null;
  approved_expense: number | null;
  actual_project_cost_known: number | null;
  cost_complete: boolean | null;
  missing_cost_lines: number | null;
}

export function classifyCostWaterline({
  totalContractAmount,
  actualProjectCostKnown,
  costComplete,
  contractAmountComplete = true,
}: {
  totalContractAmount: number | null;
  actualProjectCostKnown: number | null;
  costComplete: boolean | null;
  contractAmountComplete?: boolean;
}): { status: CostWaterlineStatus; percent: number | null } {
  if (costComplete === null) return { status: "restricted", percent: null };
  if (
    !contractAmountComplete
    || totalContractAmount == null
    || !Number.isFinite(totalContractAmount)
    || totalContractAmount <= 0
    || actualProjectCostKnown == null
    || !Number.isFinite(actualProjectCostKnown)
  ) {
    return { status: "no_contract", percent: null };
  }
  const rawPercent = (actualProjectCostKnown / totalContractAmount) * 100;
  const percent = Number(rawPercent.toFixed(2));
  if (rawPercent > 100) return { status: "red", percent };
  if (rawPercent >= 80) return { status: "yellow", percent };
  if (costComplete === false) return { status: "unknown", percent };
  return { status: "normal", percent };
}

const STATUS_META: Record<CostWaterlineStatus, { label: string; color: string; tag?: string }> = {
  normal: { label: "低于 80%", color: "var(--mb-success)", tag: "green" },
  yellow: { label: "80%–100%", color: "var(--mb-warning)", tag: "gold" },
  red: { label: "超过 100%", color: "var(--mb-danger)", tag: "red" },
  unknown: { label: "成本未完整，当前为下限", color: "var(--mb-text-3)", tag: "default" },
  restricted: { label: "成本不可见/无权限", color: "var(--mb-text-3)", tag: "default" },
  no_contract: { label: "合同额不足，无法计算", color: "var(--mb-text-3)", tag: "default" },
};

function numericPercent(numerator: number | null, denominator: number | null): number | null {
  if (
    numerator == null
    || denominator == null
    || denominator <= 0
    || !Number.isFinite(numerator)
    || !Number.isFinite(denominator)
  ) return null;
  return Number(((numerator / denominator) * 100).toFixed(2));
}

function percentLabel(value: number | null): string {
  if (value == null) return "—";
  return `${value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}%`;
}

function MetricProgress({
  label,
  numerator,
  denominator,
  percent,
  percentText,
  color,
  testId,
  children,
}: {
  label: string;
  numerator: number | null;
  denominator: number | null;
  percent: number | null;
  percentText?: string;
  color: string;
  testId: string;
  children?: React.ReactNode;
}) {
  return (
    <div data-testid={testId} style={{ minWidth: 0 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, marginBottom: 4 }}>
        <span style={{ fontWeight: 600 }}>{label}</span>
        <span style={{ fontVariantNumeric: "tabular-nums" }}>
          {percentText ?? percentLabel(percent)}
        </span>
      </div>
      <Progress
        aria-label={label}
        percent={percent == null ? 0 : Math.min(Math.max(percent, 0), 100)}
        strokeColor={color}
        showInfo={false}
        size="small"
      />
      <div style={{ color: "var(--mb-text-3)", fontSize: 12, lineHeight: 1.65 }}>
        {money(numerator)} / {money(denominator)}
      </div>
      {children}
    </div>
  );
}

export default function ProjectFinancialProgress({ metrics }: {
  metrics: ProjectFinancialMetrics;
}) {
  const costWaterline = classifyCostWaterline({
    totalContractAmount: metrics.total_contract_amount,
    actualProjectCostKnown: metrics.actual_project_cost_known,
    costComplete: metrics.cost_complete,
    contractAmountComplete: metrics.contract_amount_complete,
  });
  const sitePercent = metrics.contract_amount_complete
    ? numericPercent(metrics.site_requisition_known_cost, metrics.total_contract_amount)
    : null;
  const collectionPercent = metrics.contract_amount_complete
    ? numericPercent(metrics.received_amount, metrics.total_contract_amount)
    : null;
  const collectionColor = collectionPercent != null && collectionPercent > 100
    ? "var(--mb-warning)" : "var(--mb-accent)";
  const knownCostLowerBound = metrics.contract_amount_complete
    && metrics.cost_complete === false
    && costWaterline.percent != null
    ? `已知下限 ≥${percentLabel(costWaterline.percent)}`
    : null;

  return (
    <Space direction="vertical" size={14} style={{ width: "100%" }}>
      <MetricProgress
        label="回款 / 全部合同额"
        numerator={metrics.received_amount}
        denominator={metrics.total_contract_amount}
        percent={collectionPercent}
        color={collectionColor}
        testId="collection-progress"
      >
        {!metrics.contract_amount_complete && (
          <div>合同额证据不完整，暂不计算比例。</div>
        )}
      </MetricProgress>
      <MetricProgress
        label="项目实际成本 / 全部合同额"
        numerator={metrics.actual_project_cost_known}
        denominator={metrics.total_contract_amount}
        percent={costWaterline.percent}
        percentText={knownCostLowerBound || undefined}
        color={STATUS_META[costWaterline.status].color}
        testId="project-cost-progress"
      >
        <div>
          现场领用已知成本 {money(metrics.site_requisition_known_cost)}
          {" · "}审批通过报销 {money(metrics.approved_expense)}
        </div>
        <div>现场领用占合同额 {percentLabel(sitePercent)}</div>
        {metrics.cost_complete === null ? null : !metrics.contract_amount_complete ? (
          <>
            <div>合同额证据不完整，暂不计算比例。</div>
            {metrics.cost_complete === false && (
              <div style={{ color: "var(--mb-warning)" }}>
                缺 {metrics.missing_cost_lines} 行成本；合同额完整后再显示已知下限。
              </div>
            )}
          </>
        ) : metrics.cost_complete === false && (
          <div style={{ color: "var(--mb-warning)" }}>
            {knownCostLowerBound
              ? `缺 ${metrics.missing_cost_lines} 行成本；${knownCostLowerBound}，补齐后只会更高。`
              : `缺 ${metrics.missing_cost_lines} 行成本；当前无可计算合同额，暂不显示下限。`}
          </div>
        )}
        <Tag color={STATUS_META[costWaterline.status].tag} style={{ marginTop: 4 }}>
          {STATUS_META[costWaterline.status].label}
        </Tag>
      </MetricProgress>
    </Space>
  );
}
