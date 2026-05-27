import { useEffect, useState } from "react";
import {
  Card, Row, Col, Statistic, Segmented, Table, Tag, Button, Modal, Input, message, Space, Tooltip,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import api from "../api";

const money = (v: number | null) => (v == null ? "-" : `¥${v.toLocaleString()}`);
const pct = (v: number | null) => (v == null ? "-" : `${(v * 100).toFixed(1)}%`);

interface PartRow {
  pn_std: string;
  description: string | null;
  needs_review: boolean;
  is_excluded: boolean;
  exclude_reason: string | null;
  sales_lines: number;
  revenue: number | null;
  gross_margin: number | null;
}

export default function GovernancePage() {
  const [sum, setSum] = useState<any>(null);
  const [kind, setKind] = useState("nonstd");
  const [rows, setRows] = useState<PartRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [recomputing, setRecomputing] = useState(false);
  const [excluding, setExcluding] = useState<PartRow | null>(null);
  const [reason, setReason] = useState("");

  const loadSummary = () => api.get("/governance/summary").then((r) => setSum(r.data));
  const load = async (p = 1) => {
    setLoading(true);
    try {
      const { data } = await api.get("/governance/parts", { params: { kind, page: p, page_size: 20 } });
      setRows(data.items);
      setTotal(data.total);
      setPage(p);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadSummary(); }, []);
  useEffect(() => { load(1); /* eslint-disable-next-line */ }, [kind]);

  const doExclude = async () => {
    if (!excluding) return;
    await api.put("/governance/exclude", { pn_std: excluding.pn_std, excluded: true, reason });
    message.success("已排除，建议点「重算利润」生效");
    setExcluding(null); setReason("");
    load(page); loadSummary();
  };

  const unExclude = async (pn: string) => {
    await api.put("/governance/exclude", { pn_std: pn, excluded: false });
    message.success("已恢复，建议点「重算利润」生效");
    load(page); loadSummary();
  };

  const recompute = async () => {
    setRecomputing(true);
    try {
      const { data } = await api.post("/profit/recompute");
      message.success(`重算完成：排除型号行 ${data.excluded_part ?? 0}，负毛利 ${data.neg_margin}`);
      loadSummary();
    } finally {
      setRecomputing(false);
    }
  };

  const cols: ColumnsType<PartRow> = [
    { title: "型号 (PN)", dataIndex: "pn_std", width: 220, fixed: "left",
      render: (v, r) => <span>{v} {r.needs_review && <Tag color="orange">待复核</Tag>}{r.is_excluded && <Tag color="red">已排除</Tag>}</span> },
    { title: "描述", dataIndex: "description", ellipsis: true },
    { title: "销售行", dataIndex: "sales_lines", width: 80, align: "right" },
    { title: "营收", dataIndex: "revenue", width: 130, align: "right", render: money },
    { title: "毛利率", dataIndex: "gross_margin", width: 100, align: "right",
      render: (v: number | null) => <span style={{ color: v != null && v < 0 ? "#cf1322" : undefined }}>{pct(v)}</span> },
    { title: "操作", width: 90, fixed: "right",
      render: (_, r) => r.is_excluded
        ? <a onClick={() => unExclude(r.pn_std)}>恢复</a>
        : <a style={{ color: "#cf1322" }} onClick={() => { setExcluding(r); setReason(""); }}>排除</a> },
  ];

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card title="数据治理"
        extra={<Button type="primary" loading={recomputing} onClick={recompute}>重算利润</Button>}>
        {sum && (
          <Row gutter={[16, 16]}>
            <Col span={4}><Statistic title="非标候选型号" value={sum.nonstd_candidates} valueStyle={{ color: "#fa8c16" }} /></Col>
            <Col span={4}><Statistic title="已排除" value={sum.excluded} valueStyle={{ color: "#cf1322" }} /></Col>
            <Col span={4}><Statistic title="PN 待复核" value={sum.needs_review} /></Col>
            <Col span={4}><Tooltip title="销售行未匹配到成本"><Statistic title="无成本行" value={sum.sales_no_cost} /></Tooltip></Col>
            <Col span={4}><Tooltip title="账面亏本行"><Statistic title="负毛利行" value={sum.sales_neg_margin} valueStyle={{ color: "#cf1322" }} /></Tooltip></Col>
            <Col span={4}><Tooltip title="成本为估算(兜底),非真实匹配"><Statistic title="估算成本行" value={sum.sales_fallback_cost} /></Tooltip></Col>
          </Row>
        )}
      </Card>

      <Card>
        <Segmented
          value={kind} onChange={(v) => setKind(v as string)} style={{ marginBottom: 16 }}
          options={[
            { label: "非标候选型号", value: "nonstd" },
            { label: "待复核 PN", value: "needs_review" },
            { label: "已排除", value: "excluded" },
          ]}
        />
        <Table rowKey="pn_std" size="small" loading={loading} columns={cols} dataSource={rows}
          scroll={{ x: 800 }}
          pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: (p) => load(p) }} />
      </Card>

      <Modal open={!!excluding} title={`排除型号：${excluding?.pn_std}`} okText="确认排除" okButtonProps={{ danger: true }}
        onCancel={() => setExcluding(null)} onOk={doExclude}>
        <p style={{ color: "#888" }}>排除后，该型号的销售行不再计入营收/利润统计（如笼统打包型号"一批备件"）。</p>
        <Input.TextArea rows={2} placeholder="排除原因（写入审计）" value={reason} onChange={(e) => setReason(e.target.value)} />
      </Modal>
    </Space>
  );
}
