import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Input,
  Modal,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  assignMaintenanceSourceOrders,
  listMaintenanceSourceOrders,
  type MaintenanceSourceOrderRow,
} from "../../../api/maintenanceSourceAssignments";

const { Title, Text, Paragraph } = Typography;

function errorText(error: unknown): string {
  const payload = error as { response?: { data?: { detail?: string } } };
  return payload.response?.data?.detail || "操作失败，请重试";
}

/**
 * 项目主数据维护 · 归属确认（plan v1.3 §5.1 第五页）。
 *
 * 归属规则（ADR-0002 / 铁律 4）：名称只产生**候选**，必须人工确认才写入；
 * 「预交付-X」按剥前缀后的真实合同项目出候选（并入展示，不合并项目档案）。
 */
export default function BossProjectMasterPage() {
  const [rows, setRows] = useState<MaintenanceSourceOrderRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<{
    row: MaintenanceSourceOrderRow;
    projectId: string;
    projectName: string;
  } | null>(null);
  const [reason, setReason] = useState("");

  const load = (nextPage = page) => {
    setLoading(true);
    listMaintenanceSourceOrders({
      assignment_status: "unassigned",
      include_candidates: true,
      page: nextPage,
      page_size: 20,
    })
      .then((resp) => {
        setRows(resp.data.rows);
        setTotal(resp.data.total);
        setError(null);
      })
      .catch((err) => setError(errorText(err)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load(page);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page]);

  const confirmAssign = async () => {
    if (!pending) return;
    if (reason.trim().length === 0) {
      message.warning("请填写确认理由（审计留痕）");
      return;
    }
    try {
      await assignMaintenanceSourceOrders({
        project_id: pending.projectId,
        items: [{ source_order_id: pending.row.raw_order_id }],
        reason: reason.trim(),
      });
      message.success("归属已确认");
      setPending(null);
      setReason("");
      load(page);
    } catch (err) {
      message.error(errorText(err));
    }
  };

  const columns: ColumnsType<MaintenanceSourceOrderRow> = [
    {
      title: "需求单号",
      dataIndex: "order_no",
      width: 180,
      render: (value: string, row) => (
        <Space direction="vertical" size={0}>
          <Text strong>{value}</Text>
          {row.is_pre_delivery ? <Tag>预交付</Tag> : null}
        </Space>
      ),
    },
    { title: "制单日期", dataIndex: "order_date", width: 110 },
    {
      title: "单据项目名（原值）",
      dataIndex: "project_raw",
      width: 240,
      render: (value: string | null) => value || "—",
    },
    {
      title: "系统候选（需人工确认）",
      dataIndex: "candidates",
      render: (_: unknown, row) => {
        const candidates = row.candidates ?? [];
        if (candidates.length === 0) {
          return <Text type="secondary">无候选，请在项目主档中先建档</Text>;
        }
        return (
          <Space wrap>
            {candidates.map((candidate) => (
              <Button
                key={candidate.project_id}
                size="small"
                onClick={() =>
                  setPending({
                    row,
                    projectId: candidate.project_id,
                    projectName: candidate.display_name,
                  })
                }
              >
                {candidate.display_name}
                <Tag
                  color={candidate.match_type === "exact" ? "green" : "default"}
                  style={{ marginInlineStart: 6 }}
                >
                  {candidate.match_type === "exact"
                    ? "精确"
                    : `相似 ${Math.round(candidate.score * 100)}%`}
                </Tag>
              </Button>
            ))}
          </Space>
        );
      },
    },
  ];

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Title level={4} style={{ margin: 0 }}>
        项目归属确认
      </Title>
      <Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
        系统只按名称给出候选，绝不自动归属；确认后写入带版本与审计的归属关系。
        「预交付-」单据按剥前缀后的真实合同项目出候选，归入后仍保留预交付标记。
      </Paragraph>
      {error ? <Alert type="error" showIcon message={error} /> : null}
      <Card size="small" title={`未归属需求单（${total}）`}>
        <Table<MaintenanceSourceOrderRow>
          rowKey="raw_order_id"
          size="small"
          loading={loading}
          dataSource={rows}
          columns={columns}
          pagination={{
            current: page,
            pageSize: 20,
            total,
            showSizeChanger: false,
            onChange: setPage,
          }}
        />
      </Card>
      <Modal
        open={!!pending}
        title="确认项目归属"
        onCancel={() => setPending(null)}
        onOk={confirmAssign}
        okText="确认归属"
      >
        <Paragraph>
          将需求单 <Text strong>{pending?.row.order_no}</Text> 归属到项目{" "}
          <Text strong>{pending?.projectName}</Text>。
        </Paragraph>
        <Input.TextArea
          rows={3}
          value={reason}
          placeholder="确认理由（必填，进审计留痕）"
          onChange={(event) => setReason(event.target.value)}
        />
      </Modal>
    </Space>
  );
}
