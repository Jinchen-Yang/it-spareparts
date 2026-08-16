import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Alert, Card, Space, Typography } from "antd";
import {
  getBoardOrderLines,
  getBoardProjectOrders,
  UNASSIGNED_BUCKET,
  type BoardLineRow,
  type BoardOrderRow,
} from "../../../api/maintenanceBossBoard";
import {
  LineEvidenceTable,
  OrderEvidenceTable,
} from "../../../components/maintenance/boss/EvidenceTables";

const { Title, Text } = Typography;

/**
 * 证据下钻（plan v1.3 §5.1）：项目 → 单据 → PN 行，两级服务端分页。
 */
export default function BossProjectDrillPage() {
  const { projectId = "" } = useParams();
  const [orders, setOrders] = useState<BoardOrderRow[]>([]);
  const [orderTotal, setOrderTotal] = useState(0);
  const [orderPage, setOrderPage] = useState(1);
  const [orderPageSize, setOrderPageSize] = useState(20);
  const [selected, setSelected] = useState<string | null>(null);
  const [lines, setLines] = useState<BoardLineRow[]>([]);
  const [lineTotal, setLineTotal] = useState(0);
  const [linePage, setLinePage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [lineLoading, setLineLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  useEffect(() => {
    const ticket = ++seq.current;
    setLoading(true);
    getBoardProjectOrders(projectId, { page: orderPage, page_size: orderPageSize })
      .then((resp) => {
        if (ticket !== seq.current) return;
        setOrders(resp.data.rows);
        setOrderTotal(resp.data.total);
        setError(null);
      })
      .catch((err) => {
        if (ticket !== seq.current) return;
        setError(
          err?.response?.status === 404
            ? "项目不存在或不在你的可见范围内"
            : "单据列表加载失败，请稍后重试",
        );
      })
      .finally(() => {
        if (ticket === seq.current) setLoading(false);
      });
  }, [projectId, orderPage, orderPageSize]);

  useEffect(() => {
    if (!selected) {
      setLines([]);
      setLineTotal(0);
      return;
    }
    setLineLoading(true);
    getBoardOrderLines(selected, { page: linePage, page_size: 20 })
      .then((resp) => {
        setLines(resp.data.rows);
        setLineTotal(resp.data.total);
      })
      .finally(() => setLineLoading(false));
  }, [selected, linePage]);

  const isBucket = projectId === UNASSIGNED_BUCKET;

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Space direction="vertical" size={0}>
        <Title level={4} style={{ margin: 0 }}>
          {isBucket ? "未归属需求单" : "项目单据证据"}
        </Title>
        <Link to="/maintenance/boss/projects">返回项目列表</Link>
      </Space>
      {isBucket ? (
        <Alert
          type="info"
          showIcon
          message="这些需求单尚未人工确认项目归属"
          description="归属确认在「项目主数据维护」完成后，这些单据会并入对应项目；系统不按名称自动归属。"
        />
      ) : null}
      {error ? <Alert type="error" showIcon message={error} /> : null}
      <Card size="small" title="单据">
        <OrderEvidenceTable
          rows={orders}
          total={orderTotal}
          page={orderPage}
          pageSize={orderPageSize}
          loading={loading}
          selectedId={selected}
          onSelect={(id) => {
            setSelected(id);
            setLinePage(1);
          }}
          onChange={(page, size) => {
            setOrderPage(page);
            setOrderPageSize(size);
          }}
        />
        <Text type="secondary" style={{ fontSize: 11.5 }}>
          自报列（氚云头级汇总）与事实列（发货单/返库单/入库单）并排展示，系统不做差异判定。
        </Text>
      </Card>
      {selected ? (
        <Card size="small" title={`PN 证据行 · ${selected}`}>
          <LineEvidenceTable
            rows={lines}
            total={lineTotal}
            page={linePage}
            pageSize={20}
            loading={lineLoading}
            onChange={(page) => setLinePage(page)}
          />
        </Card>
      ) : null}
    </Space>
  );
}
