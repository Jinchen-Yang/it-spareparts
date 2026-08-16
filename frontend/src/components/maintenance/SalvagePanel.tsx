import { useEffect, useRef, useState } from "react";
import { Alert, Card, Space, Table, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  listMaintenanceSalvages,
  type MaintenanceSalvageDirectory,
  type MaintenanceSalvageRow,
} from "../../api/maintenanceOperations";
import { moneyIncTax, qty } from "../../utils/format";

const { Text } = Typography;

const NOT_READY = "尚未接入";

function errorText(error: unknown): string {
  const payload = error as { response?: { data?: { detail?: string | { message?: string } } } };
  const detail = payload.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  return "坏件变卖清单加载失败，请重试";
}

const columns: ColumnsType<MaintenanceSalvageRow> = [
  { title: "变卖日期", dataIndex: "salvage_date", width: 110, render: (value) => value || NOT_READY },
  { title: "PN", dataIndex: "pn", width: 160, render: (value) => value || NOT_READY },
  {
    title: "数量",
    dataIndex: "qty",
    width: 90,
    align: "right",
    render: (value: number | null) => (value == null ? NOT_READY : qty(value)),
  },
  {
    title: "收入（含税）",
    dataIndex: "revenue",
    width: 130,
    align: "right",
    render: (value: number | null) => (value == null ? NOT_READY : moneyIncTax(value)),
  },
  {
    title: "贡献毛利（含税）",
    dataIndex: "margin",
    width: 140,
    align: "right",
    render: (value: number | null) => (value == null ? NOT_READY : moneyIncTax(value)),
  },
  { title: "买家备注", dataIndex: "buyer_note", width: 160, render: (value) => value || "—" },
  {
    title: "状态",
    dataIndex: "is_active",
    width: 90,
    render: (value: boolean) => (value ? <Tag color="green">有效</Tag> : <Tag>已作废</Tag>),
  },
];

export default function SalvagePanel({ projectId }: { projectId: string }) {
  const [data, setData] = useState<MaintenanceSalvageDirectory | null>(null);
  const [loading, setLoading] = useState(true);
  const [permissionDenied, setPermissionDenied] = useState(false);
  const generation = useRef(0);

  useEffect(() => {
    const request = ++generation.current;
    setLoading(true);
    setPermissionDenied(false);
    listMaintenanceSalvages(projectId)
      .then(({ data: response }) => {
        if (request === generation.current) setData(response);
      })
      .catch((error: unknown) => {
        if (request === generation.current) {
          setData(null);
          const status = (error as { response?: { status?: unknown } } | null)?.response?.status;
          setPermissionDenied(status === 403);
          // 403 = 缺 data_profit，面板内已明确说明；其它失败才弹错误提示。
          if (status !== 403) message.error(errorText(error));
        }
      })
      .finally(() => {
        if (request === generation.current) setLoading(false);
      });
    return () => { generation.current += 1; };
  }, [projectId]);

  return (
    <Card
      id="salvages"
      title="坏件变卖清单"
      extra={data ? <Tag>{`有效 ${data.active_count} 笔`}</Tag> : <Tag>尚未接入</Tag>}
    >
      {!data && !loading && permissionDenied && (
        <Alert
          showIcon
          type="info"
          style={{ marginBottom: 12 }}
          message="坏件变卖清单含成本与毛利，需要利润数据可见权限；当前账号暂无该权限。"
        />
      )}
      {data && data.margin_completeness === "incomplete" && (
        <Alert
          showIcon
          type="warning"
          style={{ marginBottom: 12 }}
          message="部分变卖缺少冻结成本，贡献毛利汇总不完整，不按 0 估算。"
        />
      )}
      <Space wrap size={[18, 8]} style={{ marginBottom: 12 }}>
        <Text strong>变卖收入合计（含税）：{data ? moneyIncTax(data.total_revenue) : NOT_READY}</Text>
        <Text strong>
          贡献毛利合计（含税）：
          {data && data.total_margin != null ? moneyIncTax(data.total_margin) : NOT_READY}
        </Text>
      </Space>
      <Table
        rowKey="salvage_id"
        size="small"
        columns={columns}
        dataSource={data?.rows ?? []}
        loading={loading}
        scroll={{ x: 880 }}
        pagination={false}
        locale={{ emptyText: loading ? "正在读取坏件变卖清单…" : "暂无坏件变卖记录" }}
      />
    </Card>
  );
}
