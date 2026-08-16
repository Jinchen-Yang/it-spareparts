import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Alert, Button, Card, Col, Row, Space, Typography } from "antd";
import {
  getBoardAttention,
  getBoardHealth,
  getBoardProjects,
  getBoardSummary,
  type BoardAttention,
  type BoardHealth,
  type BoardProjectRow,
  type BoardSummary,
} from "../../../api/maintenanceBossBoard";
import AttentionList from "../../../components/maintenance/boss/AttentionList";
import KnownCostCell from "../../../components/maintenance/boss/KnownCostCell";
import PeriodDeltaCards from "../../../components/maintenance/boss/PeriodDeltaCards";
import SourceHealthBar from "../../../components/maintenance/boss/SourceHealthBar";
import StatCell from "../../../components/maintenance/boss/StatCell";

const { Title, Text } = Typography;

const FOCUS_CARD_LIMIT = 12;

/**
 * 维保展示板首屏（plan v1.3 §5.1）。
 * 五段固定顺序：来源健康 → 本期变化 → 需关注事项 → 重点项目卡 → 查看全部。
 * 「数据更新至」只在右上角出现一次（§5.0 硬规则）。
 */
export default function BossOverviewPage() {
  const [health, setHealth] = useState<BoardHealth | null>(null);
  const [summary, setSummary] = useState<BoardSummary | null>(null);
  const [attention, setAttention] = useState<BoardAttention | null>(null);
  const [projects, setProjects] = useState<BoardProjectRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all([
      getBoardHealth(),
      getBoardSummary(),
      getBoardAttention(),
      getBoardProjects({ page: 1, page_size: FOCUS_CARD_LIMIT }),
    ])
      .then(([h, s, a, p]) => {
        if (!alive) return;
        setHealth(h.data);
        setSummary(s.data);
        setAttention(a.data);
        setProjects(p.data.rows);
        setError(null);
      })
      .catch(() => {
        if (alive) setError("展示板数据加载失败，请稍后重试");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const asOf = health?.sources.wbdd.as_of ?? null;

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Row justify="space-between" align="middle">
        <Col>
          <Title level={4} style={{ margin: 0 }}>
            维保展示板
          </Title>
          <Text type="secondary" style={{ fontSize: 11.5 }}>
            后事实展示板：数据来自定期上传的氚云导出，系统只归集与展示，不做流程闭环。
          </Text>
        </Col>
        <Col>
          <Text type="secondary">数据更新至 {asOf ?? "尚未导入"}</Text>
        </Col>
      </Row>

      {error ? <Alert type="error" showIcon message={error} /> : null}

      <SourceHealthBar health={health} />
      <PeriodDeltaCards summary={summary} />
      <AttentionList attention={attention} />

      <Card
        size="small"
        title="重点项目"
        loading={loading}
        extra={<Link to="/maintenance/boss/projects">查看全部项目</Link>}
      >
        <Row gutter={[12, 12]}>
          {projects.map((row) => (
            <Col xs={24} md={12} xl={8} key={row.project_id}>
              <Card size="small" data-testid={`project-card-${row.project_id}`}>
                <Space direction="vertical" size={2} style={{ width: "100%" }}>
                  <Link to={`/maintenance/boss/projects/${encodeURIComponent(row.project_id)}`}>
                    {row.display_name}
                  </Link>
                  <KnownCostCell stat={row.known_apply_cost_inc_tax} compact />
                  <Space size={16} wrap>
                    <span>
                      <Text type="secondary" style={{ fontSize: 11.5 }}>
                        本期需求单{" "}
                      </Text>
                      <StatCell stat={row.orders_ytd} />
                    </span>
                    <span>
                      <Text type="secondary" style={{ fontSize: 11.5 }}>
                        实发{" "}
                      </Text>
                      <StatCell stat={row.shipped_qty} />
                    </span>
                  </Space>
                </Space>
              </Card>
            </Col>
          ))}
          {!loading && projects.length === 0 ? (
            <Col span={24}>
              <Text type="secondary">暂无项目数据</Text>
            </Col>
          ) : null}
        </Row>
      </Card>
    </Space>
  );
}
