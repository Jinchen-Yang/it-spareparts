import { Card, Col, Row, Typography } from "antd";
import type { BoardSummary } from "../../../api/maintenanceBossBoard";
import KnownCostCell from "./KnownCostCell";
import StatCell from "./StatCell";

const { Text } = Typography;

/**
 * 本期变化（plan v1.3 §5.1 首屏第二段）：orders_ytd / lines_ytd / 成本五件套 + 环比。
 * 字段名由后端窗口参数决定，**不写死年份**。
 */
function delta(current?: number | null, previous?: number | null): string {
  if (current === null || current === undefined) return "";
  if (previous === null || previous === undefined) return "";
  const diff = current - previous;
  if (diff === 0) return "持平";
  return `${diff > 0 ? "+" : ""}${diff} 环比`;
}

export function PeriodDeltaCards({ summary }: { summary?: BoardSummary | null }) {
  if (!summary) return null;
  const prev = summary.prev_window;
  return (
    <Row gutter={12} data-testid="period-delta-cards">
      <Col xs={24} md={8}>
        <Card size="small" title="本期需求单">
          <StatCell stat={summary.orders_ytd} />
          <div>
            <Text type="secondary" style={{ fontSize: 11.5 }}>
              {delta(summary.orders_ytd.value, prev.orders_ytd.value)}
            </Text>
          </div>
        </Card>
      </Col>
      <Col xs={24} md={8}>
        <Card size="small" title="本期需求明细行">
          <StatCell stat={summary.lines_ytd} />
          <div>
            <Text type="secondary" style={{ fontSize: 11.5 }}>
              {delta(summary.lines_ytd.value, prev.lines_ytd.value)}
            </Text>
          </div>
        </Card>
      </Col>
      <Col xs={24} md={8}>
        <Card size="small" title="已知申请估算成本（含税）">
          <KnownCostCell stat={summary.known_apply_cost_inc_tax} />
        </Card>
      </Col>
    </Row>
  );
}

export default PeriodDeltaCards;
