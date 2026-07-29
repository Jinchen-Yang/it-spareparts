import { Card, Tag } from "antd";
import { Link } from "react-router-dom";
import type {
  PoolAnalysisSide,
  PoolReference,
  PoolReferenceSide,
} from "../../api/poolAnalysis";
import {
  TaxMoneyByBasis,
  taxBasisCaption,
  useTaxBasis,
  type TaxBasis,
} from "../../context/TaxBasis";
import { moneyExact, splitFixed } from "../../utils/format";

export interface PoolReferenceCardProps {
  reference: PoolReference;
  side?: "both" | PoolAnalysisSide;
  compact?: boolean;
  forceRestricted?: boolean;
}

function deltaLabel(value: number | null, target: "池均价" | "人工约束") {
  if (value == null) return null;
  const direction = value > 0 ? "高于" : value < 0 ? "低于" : "等于";
  const amount = moneyExact(Math.abs(value));
  return `${direction}${target}${value === 0 ? "" : ` ${amount}（未税差额）`}`;
}

function PriceSide({ kind, value, basis, forceRestricted = false }: {
  kind: PoolAnalysisSide;
  value: PoolReferenceSide;
  basis: TaxBasis;
  forceRestricted?: boolean;
}) {
  const title = kind === "purchase" ? "采购参考" : "销售参考";
  const limitLabel = kind === "purchase" ? "人工上限" : "人工下限";

  if (forceRestricted || value.restricted || value.constraint.status === "restricted") {
    return (
      <div style={sidePanelStyle} aria-label={`${title}（无池价格权限）`}>
        <strong>{title}</strong>
        <Tag color="default" style={{ marginInlineStart: 8 }}>无池价格权限</Tag>
        <div style={mutedStyle}>价格、约束差额与越线状态已按权限隐藏</div>
      </div>
    );
  }

  const pool = value.pool_stats;
  const part = value.part_stats;
  const taxValue = (raw: number | null | undefined) => {
    const pair = splitFixed(raw, "ex");
    return raw == null
      ? "暂无样本"
      : <TaxMoneyByBasis basis={basis} inc={pair.inc} ex={pair.ex} exact />;
  };
  return (
    <div style={sidePanelStyle} aria-label={title}>
      <div style={{ fontWeight: 600, marginBottom: 8 }}>
        {title}（{taxBasisCaption(basis)}）
      </div>
      <div style={metricsStyle}>
        <span>池均价 <b>{taxValue(pool?.weighted_avg)}</b></span>
        <span>中位 <b>{taxValue(pool?.median)}</b></span>
        <span>{limitLabel}(未税) <b>{value.constraint.status === "unset"
          ? "未设置" : moneyExact(value.constraint.value)}</b></span>
      </div>
      <div style={{ ...metricsStyle, marginTop: 6 }}>
        <span>本型号均价 <b>{taxValue(part?.weighted_avg)}</b></span>
        {deltaLabel(value.delta_to_pool_avg, "池均价") && (
          <Tag color={value.delta_to_pool_avg != null && value.delta_to_pool_avg > 0 ? "volcano" : "green"}>
            {deltaLabel(value.delta_to_pool_avg, "池均价")}
          </Tag>
        )}
        {deltaLabel(value.delta_to_constraint, "人工约束") && (
          <Tag color={kind === "purchase"
            ? (value.relation_to_constraint === "above" ? "volcano" : "green")
            : (value.relation_to_constraint === "below" ? "volcano" : "green")}>
            {deltaLabel(value.delta_to_constraint, "人工约束")}
          </Tag>
        )}
      </div>
    </div>
  );
}

const sidePanelStyle: React.CSSProperties = {
  minWidth: 0,
  padding: "10px 12px",
  border: "1px solid var(--mb-border, #e5e2dc)",
  borderRadius: 8,
  background: "rgba(0,0,0,.015)",
};
const metricsStyle: React.CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  alignItems: "center",
  gap: "6px 18px",
  fontSize: 13,
};
const mutedStyle: React.CSSProperties = {
  color: "var(--mb-text-3, #8c8880)",
  fontSize: 12.5,
  marginTop: 8,
};

export default function PoolReferenceCard({
  reference,
  side = "both",
  compact = false,
  forceRestricted = false,
}: PoolReferenceCardProps) {
  const purchaseBasis = useTaxBasis("purchase");
  const salesBasis = useTaxBasis("sales");
  const label = `${reference.pn_std || "当前型号"} 的池价格参考`;
  if (!reference.pool) {
    return (
      <section role="region" aria-label={label}>
        <Card size="small"><span style={mutedStyle}>该型号尚未加入互通池</span></Card>
      </section>
    );
  }

  const qs = new URLSearchParams();
  if (reference.window.range) qs.set("range", reference.window.range);
  if (reference.window.range === "custom" && reference.window.date_from && reference.window.date_to) {
    qs.set("from", reference.window.date_from);
    qs.set("to", reference.window.date_to);
  }
  if (reference.pn_std) qs.set("pn", reference.pn_std);
  if (side !== "both") qs.set("side", side);
  const sides: PoolAnalysisSide[] = side === "both" ? ["purchase", "sales"] : [side];
  const purchaseSamples = forceRestricted || reference.purchase_reference.restricted
    || reference.purchase_reference.constraint.status === "restricted"
    ? "采购无池价格权限"
    : `采购 ${reference.purchase_reference.pool_stats?.order_count ?? 0} 单`;
  const salesSamples = forceRestricted || reference.sales_reference.restricted
    || reference.sales_reference.constraint.status === "restricted"
    ? "销售无池价格权限"
    : `销售 ${reference.sales_reference.pool_stats?.order_count ?? 0} 单`;
  const sampleText = [
    purchaseSamples,
    salesSamples,
    "业务价按管理员口径",
    "约束/差额为未税规则值",
    reference.window.range === "90d" ? "近 90 天" : reference.window.range,
  ].join(" · ");

  return (
    <section role="region" aria-label={label}>
      <Card
        size="small"
        styles={{ body: { padding: compact ? 10 : 14 } }}
        title={(
          <span style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span>所属互通池：</span>
            <span>{reference.pool.name}</span>
            <Tag color="blue">{reference.pool.member_count} 个 PN</Tag>
          </span>
        )}
        extra={(
          <Link to={`/pool-analysis/${reference.pool.group_id}?${qs.toString()}`} aria-label="查看互通池详情">
            查看池详情
          </Link>
        )}
      >
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 320px), 1fr))", gap: 10 }}>
          {sides.map((kind) => (
            <PriceSide
              key={kind}
              kind={kind}
              value={kind === "purchase" ? reference.purchase_reference : reference.sales_reference}
              basis={kind === "purchase" ? purchaseBasis : salesBasis}
              forceRestricted={forceRestricted}
            />
          ))}
        </div>
        <div style={mutedStyle}>{sampleText}</div>
      </Card>
    </section>
  );
}
