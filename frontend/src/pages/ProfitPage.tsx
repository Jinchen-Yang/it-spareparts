import { useEffect, useState } from "react";
import {
  Card, Table, Segmented, DatePicker, Switch, Button, Space, Tag, message, Statistic, Row, Col,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import type { Dayjs } from "dayjs";
import api from "../api";

interface ProfitRow {
  dimension: string;
  revenue: number | null;
  cost: number | null;
  gross_profit: number | null;
  gross_margin: number | null;
  lines: number;
  no_cost: number;
  excluded_revenue: number | null;
}

const money = (v: number | null) => (v == null ? "-" : `¥${v.toLocaleString()}`);
const pct = (v: number | null) => (v == null ? "-" : `${(v * 100).toFixed(2)}%`);

const DIM_LABEL: Record<string, string> = { part: "型号", salesperson: "销售员", customer: "客户" };

export default function ProfitPage() {
  const [dimension, setDimension] = useState("salesperson");
  const [range, setRange] = useState<[Dayjs, Dayjs] | null>(null);
  const [onlyAnomaly, setOnlyAnomaly] = useState(false);
  const [rows, setRows] = useState<ProfitRow[]>([]);
  const [method, setMethod] = useState("");
  const [loading, setLoading] = useState(false);
  const [recomputing, setRecomputing] = useState(false);

  const params = () => ({
    dimension,
    only_anomaly: onlyAnomaly,
    date_from: range?.[0]?.format("YYYY-MM-DD"),
    date_to: range?.[1]?.format("YYYY-MM-DD"),
  });

  const load = async () => {
    setLoading(true);
    try {
      const { data } = await api.get("/profit", { params: params() });
      setRows(data.rows);
      setMethod(data.cost_method);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dimension, onlyAnomaly, range]);

  const recompute = async () => {
    setRecomputing(true);
    try {
      const { data } = await api.post("/profit/recompute");
      message.success(`重算完成：${data.sales_lines} 行，无成本 ${data.no_cost}，负毛利 ${data.neg_margin}`);
      load();
    } catch {
      message.error("重算失败（需要管理员权限）");
    } finally {
      setRecomputing(false);
    }
  };

  const exportCsv = async () => {
    const res = await api.get("/profit/export", { params: params(), responseType: "blob" });
    const url = URL.createObjectURL(res.data);
    const a = document.createElement("a");
    a.href = url;
    a.download = `profit_${dimension}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const cols: ColumnsType<ProfitRow> = [
    { title: DIM_LABEL[dimension], dataIndex: "dimension", width: 200, fixed: "left", ellipsis: true },
    { title: "营收(不含税)", dataIndex: "revenue", width: 130, align: "right", render: money,
      sorter: (a, b) => (a.revenue ?? 0) - (b.revenue ?? 0) },
    { title: "成本", dataIndex: "cost", width: 130, align: "right", render: money },
    { title: "毛利", dataIndex: "gross_profit", width: 130, align: "right",
      sorter: (a, b) => (a.gross_profit ?? 0) - (b.gross_profit ?? 0),
      render: (v: number | null) => <span style={{ color: v != null && v < 0 ? "#cf1322" : undefined }}>{money(v)}</span> },
    { title: "毛利率", dataIndex: "gross_margin", width: 100, align: "right",
      sorter: (a, b) => (a.gross_margin ?? 0) - (b.gross_margin ?? 0),
      render: (v: number | null) => <span style={{ color: v != null && v < 0 ? "#cf1322" : undefined }}>{pct(v)}</span> },
    { title: "行数", dataIndex: "lines", width: 70, align: "right" },
    { title: "无成本", dataIndex: "no_cost", width: 80, align: "right",
      render: (v: number) => (v ? <Tag color="orange">{v}</Tag> : v) },
    { title: "被排除营收", dataIndex: "excluded_revenue", width: 130, align: "right", render: money },
  ];

  const totalRev = rows.reduce((s, r) => s + (r.revenue ?? 0), 0);
  const totalGp = rows.reduce((s, r) => s + (r.gross_profit ?? 0), 0);

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card>
        <Space wrap size="large">
          <Segmented
            value={dimension}
            onChange={(v) => setDimension(v as string)}
            options={[
              { label: "按型号", value: "part" },
              { label: "按销售员", value: "salesperson" },
              { label: "按客户", value: "customer" },
            ]}
          />
          <DatePicker.RangePicker onChange={(v) => setRange(v as [Dayjs, Dayjs] | null)} />
          <Space>
            仅看异常
            <Switch checked={onlyAnomaly} onChange={setOnlyAnomaly} />
          </Space>
          <Tag color="blue">成本法：{method === "moving_avg" ? "移动加权" : method === "fifo" ? "先进先出" : method}</Tag>
          <Button type="primary" loading={recomputing} onClick={recompute}>重算</Button>
          <Button onClick={exportCsv} disabled={!rows.length}>导出 CSV</Button>
        </Space>
      </Card>

      <Row gutter={16}>
        <Col span={8}><Card size="small"><Statistic title="合计营收(不含税)" value={totalRev} precision={2} prefix="¥" /></Card></Col>
        <Col span={8}><Card size="small"><Statistic title="合计毛利" value={totalGp} precision={2} prefix="¥" valueStyle={{ color: totalGp < 0 ? "#cf1322" : undefined }} /></Card></Col>
        <Col span={8}><Card size="small"><Statistic title="整体毛利率" value={totalRev ? (totalGp / totalRev) * 100 : 0} precision={2} suffix="%" /></Card></Col>
      </Row>

      <Card title={`利润 · ${DIM_LABEL[dimension]}维度`}>
        <Table
          rowKey="dimension"
          size="small"
          loading={loading}
          columns={cols}
          dataSource={rows}
          scroll={{ x: 1000 }}
          pagination={{ pageSize: 20, showSizeChanger: true }}
        />
      </Card>
    </Space>
  );
}
