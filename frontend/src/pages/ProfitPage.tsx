import { useEffect, useState } from "react";
import {
  Card, Segmented, DatePicker, Switch, Button, Space, Tag, message, Statistic, Row, Col,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import ResizableTable from "../components/ResizableTable";
import PageHeader from "../components/PageHeader";
import type { Dayjs } from "dayjs";
import api from "../api";

interface ProfitRow {
  dimension: string;
  revenue: number | null;
  revenue_costed: number | null;
  cost_moving_avg: number | null;
  gross_profit_moving: number | null;
  gross_margin_moving: number | null;
  cost_fifo: number | null;
  gross_profit_fifo: number | null;
  gross_margin_fifo: number | null;
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

  const numCol = (v: number | null) => (
    <span style={{ color: v != null && v < 0 ? "var(--mb-danger)" : undefined }}>{money(v)}</span>
  );
  const pctCol = (v: number | null) => (
    <span style={{ color: v != null && v < 0 ? "var(--mb-danger)" : undefined }}>{pct(v)}</span>
  );

  const cols: ColumnsType<ProfitRow> = [
    { title: DIM_LABEL[dimension], dataIndex: "dimension", width: 180, fixed: "left", ellipsis: true },
    { title: "营收(总)", dataIndex: "revenue", width: 130, align: "right", render: money,
      defaultSortOrder: "descend", sorter: (a, b) => (a.revenue ?? 0) - (b.revenue ?? 0) },
    { title: "可比营收", dataIndex: "revenue_costed", width: 120, align: "right", render: money },
    {
      title: "移动加权",
      children: [
        { title: "成本", dataIndex: "cost_moving_avg", width: 120, align: "right", render: money },
        { title: "毛利", dataIndex: "gross_profit_moving", width: 120, align: "right",
          sorter: (a, b) => (a.gross_profit_moving ?? 0) - (b.gross_profit_moving ?? 0), render: numCol },
        { title: "毛利率", dataIndex: "gross_margin_moving", width: 90, align: "right", render: pctCol },
      ],
    },
    {
      title: "先进先出 FIFO",
      children: [
        { title: "成本", dataIndex: "cost_fifo", width: 120, align: "right", render: money },
        { title: "毛利", dataIndex: "gross_profit_fifo", width: 120, align: "right",
          sorter: (a, b) => (a.gross_profit_fifo ?? 0) - (b.gross_profit_fifo ?? 0), render: numCol },
        { title: "毛利率", dataIndex: "gross_margin_fifo", width: 90, align: "right", render: pctCol },
      ],
    },
    { title: "行数", dataIndex: "lines", width: 70, align: "right" },
    { title: "无成本", dataIndex: "no_cost", width: 80, align: "right",
      render: (v: number) => (v ? <Tag color="orange">{v}</Tag> : v) },
  ];

  const sum = (k: keyof ProfitRow) => rows.reduce((s, r) => s + ((r[k] as number) ?? 0), 0);
  const totalRev = sum("revenue");
  const totalRevCosted = sum("revenue_costed");
  const totalGpMa = sum("gross_profit_moving");
  const totalGpFifo = sum("gross_profit_fifo");

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title="利润分析"
        subtitle="按型号 / 销售员 / 客户维度聚合营收与毛利（移动加权 + FIFO 两种成本法并列）"
      />
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
          <Tag color="blue">移动加权 + FIFO 并排</Tag>
          <Button type="primary" loading={recomputing} onClick={recompute}>重算</Button>
          <Button onClick={exportCsv} disabled={!rows.length}>导出 CSV</Button>
        </Space>
      </Card>

      <Row gutter={16}>
        <Col span={8}><Card size="small"><Statistic title="合计营收(不含税)" value={totalRev} precision={2} prefix="¥" /></Card></Col>
        <Col span={8}><Card size="small">
          <Statistic title="移动加权 · 毛利" value={totalGpMa} precision={2} prefix="¥" valueStyle={{ color: totalGpMa < 0 ? "var(--mb-danger)" : undefined }} />
          <span style={{ color: "var(--mb-text-3)" }}>毛利率 {totalRevCosted ? ((totalGpMa / totalRevCosted) * 100).toFixed(2) : "-"}%</span>
        </Card></Col>
        <Col span={8}><Card size="small">
          <Statistic title="先进先出 FIFO · 毛利" value={totalGpFifo} precision={2} prefix="¥" valueStyle={{ color: totalGpFifo < 0 ? "var(--mb-danger)" : undefined }} />
          <span style={{ color: "var(--mb-text-3)" }}>毛利率 {totalRevCosted ? ((totalGpFifo / totalRevCosted) * 100).toFixed(2) : "-"}%</span>
        </Card></Col>
      </Row>

      <Card title={`利润 · ${DIM_LABEL[dimension]}维度`}>
        <ResizableTable
          storageKey="profit"
          rowKey="dimension"
          size="small"
          loading={loading}
          columns={cols}
          dataSource={rows}
          scroll={{ x: 1320 }}
          pagination={{ pageSize: 20, showSizeChanger: true }}
        />
      </Card>
    </Space>
  );
}
