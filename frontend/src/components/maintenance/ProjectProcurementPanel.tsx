import { useEffect, useState } from "react";
import { Alert, Card, Empty, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";

import {
  getProjectProcurement,
  type ProcurementOrder,
  type ProcurementLine,
} from "../../api/maintenanceProjectProcurement";
import { money } from "../../utils/format";
import { TERM } from "./maintenanceLanguage";

const orderColumns: ColumnsType<ProcurementOrder> = [
  {
    title: "采购单号",
    dataIndex: "purchase_order_no",
    width: 150,
  },
  {
    title: "采购日期",
    dataIndex: "purchase_date",
    width: 110,
    render: (value: string | null) => value || "—",
  },
  {
    title: "采购人",
    dataIndex: "purchaser",
    width: 100,
    render: (value: string | null) => value || "—",
  },
  {
    title: "关联维保单",
    dataIndex: "demand_order_no",
    width: 150,
    render: (value: string | null) => value || "—",
  },
  {
    title: "维保日期",
    dataIndex: "demand_date",
    width: 110,
    render: (value: string | null) => value || "—",
  },
  {
    title: "备件明细",
    dataIndex: "line_count",
    width: 90,
    align: "right",
    render: (count: number) => `${count} 行`,
  },
];

const lineColumns: ColumnsType<ProcurementLine> = [
  { title: TERM.pn, dataIndex: "pn", width: 150, render: (v: string | null) => v || "—" },
  {
    title: "描述",
    dataIndex: "description",
    width: 200,
    render: (v: string | null) => v || "—",
  },
  { title: "数量", dataIndex: "qty", width: 80, align: "right" },
  {
    title: `单价（${TERM.exTax}）`,
    dataIndex: "unit_price",
    width: 120,
    align: "right",
    render: money,
  },
];

export default function ProjectProcurementPanel({
  projectId,
}: {
  projectId: string;
}) {
  const [orders, setOrders] = useState<ProcurementOrder[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(false);
    getProjectProcurement(projectId)
      .then(({ data }) => {
        if (!cancelled) setOrders(data.purchases ?? []);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [projectId]);

  if (error) {
    return (
      <Alert
        type="warning"
        showIcon
        message="采购订单关联数据加载失败"
        description="采购订单关联数据暂时不可用，请稍后重试。"
      />
    );
  }

  return (
    <Card title="采购订单与维保需求" loading={loading}>
      {orders.length === 0 ? (
        <Empty description="尚未找到关联采购订单" />
      ) : (
        <Table
          rowKey="purchase_order_no"
          size="small"
          columns={orderColumns}
          dataSource={orders}
          expandable={{
            expandedRowRender: (order) => (
              <Table
                rowKey={(_, i) => `${order.purchase_order_no}-line-${i}`}
                size="small"
                columns={lineColumns}
                dataSource={order.lines}
                pagination={false}
              />
            ),
            rowExpandable: (order) => order.line_count > 0,
          }}
          pagination={{ pageSize: 10, showSizeChanger: false }}
        />
      )}
      <div style={{ marginTop: 8, fontSize: 12, color: "var(--mb-text-3)" }}>
        数据来源于采购订单与维保需求单的只读关联，不会自动猜测或创建关联。
      </div>
    </Card>
  );
}
