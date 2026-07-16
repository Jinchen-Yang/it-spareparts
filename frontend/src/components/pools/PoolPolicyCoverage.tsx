import { Button, Tag } from "antd";
import type { CSSProperties } from "react";
import type { PoolPolicyCoverage as Coverage, PoolPolicyMissing } from "../../api/pools";

const FILTER_LABEL: Record<PoolPolicyMissing, string> = {
  purchase: "未设采购上限",
  sales: "未设销售下限",
  either: "任一侧未设置",
  both: "两侧均未设置",
};

interface Props {
  coverage: Coverage | null | undefined;
  restricted?: boolean;
  selected?: PoolPolicyMissing | null;
  /** 缺省表示只读：仍展示数字，但不生成无权限页面的死链接。 */
  onSelect?: (filter: "purchase" | "sales") => void;
  onClear?: () => void;
  scopeNote?: string;
  readOnlyHint?: string;
}

/**
 * 池约束价覆盖率的唯一展示组件。
 *
 * 两个页面只负责取同一个 /api/pools 响应和决定点击后的导航，数字口径、文案、
 * 权限隐藏和窄屏换行全部在这里收口，避免经营看板与池管理页逐步漂移。
 */
export default function PoolPolicyCoverage({
  coverage, restricted = false, selected = null, onSelect, onClear, scopeNote, readOnlyHint,
}: Props) {
  if (restricted || !coverage) return null;

  const cardStyle: CSSProperties = {
    flex: "1 1 150px",
    border: "1px solid var(--mb-border, #e8e8e8)",
    borderRadius: 8,
    background: "var(--mb-surface, #fff)",
    padding: "10px 12px",
    textAlign: "left",
  };
  const metric = (label: string, value: number) => (
    <>
      <div style={{ color: "var(--mb-text-3, #777)", fontSize: 12.5 }}>{label}</div>
      <div style={{ marginTop: 2, fontSize: 22, lineHeight: 1.2, fontWeight: 600 }}>{value}</div>
    </>
  );
  const missingMetric = (side: "purchase" | "sales", label: string, value: number) => {
    const active = selected === side;
    const style: CSSProperties = {
      ...cardStyle,
      border: `1px solid ${active
        ? "var(--ant-color-primary, #1677ff)" : "var(--mb-border, #e8e8e8)"}`,
      background: active ? "var(--ant-color-primary-bg, #e6f4ff)" : cardStyle.background,
    };
    if (!onSelect) return <div key={side} style={style}>{metric(label, value)}</div>;
    return (
      <button key={side} type="button" aria-label={`筛选${label}的互通池`}
        aria-pressed={active}
        onClick={() => onSelect(side)}
        style={{ ...style, cursor: "pointer", color: "inherit", font: "inherit" }}>
        {metric(label, value)}
      </button>
    );
  };

  return (
    <section aria-label="互通池约束价覆盖" style={{ marginBottom: 12 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 8 }}>
        <span style={{ fontWeight: 600 }}>约束价覆盖</span>
        {scopeNote && <Tag color="blue">{scopeNote}</Tag>}
      </div>
      <div data-testid="pool-policy-coverage-grid"
        style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        <div style={cardStyle}>
          {metric("有效池", coverage.active_pool_count)}
        </div>
        {missingMetric("purchase", "未设采购上限", coverage.purchase_missing_count)}
        {missingMetric("sales", "未设销售下限", coverage.sales_missing_count)}
      </div>
      {readOnlyHint && <div style={{ marginTop: 8, color: "var(--mb-text-3, #777)", fontSize: 12.5 }}>
        {readOnlyHint}
      </div>}
      {selected && (
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginTop: 8 }}>
          <Tag color="blue">当前筛选：{FILTER_LABEL[selected]}</Tag>
          {onClear && <Button type="link" size="small" onClick={onClear}>清除缺失筛选</Button>}
        </div>
      )}
    </section>
  );
}
