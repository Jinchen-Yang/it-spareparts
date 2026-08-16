import { useEffect, useRef, useState } from "react";
import { Alert, Card, Space, Table, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  getMaintenanceProjectFrontStock,
  type MaintenanceFrontStockRow,
  type MaintenanceFrontStockSummary,
} from "../../api/maintenanceOperations";
import { moneyIncTax, qty } from "../../utils/format";

const { Text } = Typography;

const NOT_READY = "尚未接入";

function errorText(error: unknown): string {
  const payload = error as { response?: { data?: { detail?: string | { message?: string } } } };
  const detail = payload.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  return "前置库数据加载失败，请重试";
}

const columns: ColumnsType<MaintenanceFrontStockRow> = [
  { title: "PN", dataIndex: "pn", width: 160, render: (value) => value || NOT_READY },
  { title: "描述", dataIndex: "description", width: 200, render: (value) => value || "—" },
  { title: "仓库", dataIndex: "warehouse_name", width: 120, render: (value) => value || "—" },
  {
    title: "数量",
    dataIndex: "qty",
    width: 100,
    align: "right",
    render: (value: number | null) => (value == null ? NOT_READY : qty(value)),
  },
  {
    title: "库龄（天）",
    dataIndex: "age_days",
    width: 110,
    align: "right",
    render: (value: number | null) => (value == null ? NOT_READY : String(value)),
  },
  {
    title: "超90天未领用",
    dataIndex: "stale_90d",
    width: 130,
    render: (value: boolean) => (value
      ? <Tag color="red">超90天未领用</Tag>
      : <Tag>正常</Tag>),
  },
  {
    title: "金额（含税）",
    dataIndex: "value_inc_tax",
    width: 140,
    align: "right",
    render: (value: number | null) => (value == null ? NOT_READY : moneyIncTax(value)),
  },
];

export default function FrontStockPanel({ projectId }: { projectId: string }) {
  const [data, setData] = useState<MaintenanceFrontStockSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const generation = useRef(0);

  useEffect(() => {
    const request = ++generation.current;
    setLoading(true);
    getMaintenanceProjectFrontStock(projectId)
      .then(({ data: response }) => {
        if (request === generation.current) setData(response);
      })
      .catch((error: unknown) => {
        if (request === generation.current) {
          setData(null);
          message.error(errorText(error));
        }
      })
      .finally(() => {
        if (request === generation.current) setLoading(false);
      });
    return () => { generation.current += 1; };
  }, [projectId]);

  const amountVisible = data?.cost_visible === true;

  return (
    <Card
      id="front-stock"
      title="前置库结存"
      extra={data
        ? <Tag>{`${data.rows.length} 行 · 超90天未领用 ${data.stale_90d_count}`}</Tag>
        : <Tag>尚未接入</Tag>}
    >
      {data && !amountVisible && (
        <Alert
          showIcon
          type="info"
          style={{ marginBottom: 12 }}
          message="当前账号没有采购成本数据权限，金额已脱敏；数量与库龄仍完整展示。"
        />
      )}
      {data && data.value_completeness === "incomplete" && (
        <Alert
          showIcon
          type="warning"
          style={{ marginBottom: 12 }}
          message="部分行缺少成本依据，金额估值不完整，不按 0 估算。"
        />
      )}
      <Space wrap size={[18, 8]} style={{ marginBottom: 12 }}>
        <Text strong>结存数量合计：{data ? qty(data.total_qty) : NOT_READY}</Text>
        <Text strong>
          结存金额合计（含税）：
          {data && amountVisible
            ? data.total_value_inc_tax != null
              ? moneyIncTax(data.total_value_inc_tax)
              : NOT_READY
            : NOT_READY}
        </Text>
      </Space>
      <Table
        rowKey="stock_id"
        size="small"
        columns={columns}
        dataSource={data?.rows ?? []}
        loading={loading}
        scroll={{ x: 960 }}
        pagination={false}
        locale={{ emptyText: loading ? "正在读取前置库…" : "暂无前置库结存" }}
      />
    </Card>
  );
}
