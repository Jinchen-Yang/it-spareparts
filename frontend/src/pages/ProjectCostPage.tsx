import { useEffect, useState } from "react";
import {
  Card, DatePicker, Input, Button, Space, Tag, Tooltip, message, Statistic, Row, Col,
  Drawer, Progress, Alert,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import ResizableTable from "../components/ResizableTable";
import PageHeader from "../components/PageHeader";
import type { Dayjs } from "dayjs";
import api from "../api";
import { money } from "../utils/format";

interface ProjectRow {
  project: string;
  lines: number;
  qty: number | null;
  cost_inc: number | null;
  cost_ex: number | null;
  cost_total: number | null;
  coverage_pct: number | null;
  by_source: Record<string, number>;
  months: number;
  sales_orders: string[];
  contract_amount: number | null;
  contract_shared: boolean;
}

interface LineRow {
  order_no: string; order_date: string | null; demand_type: string | null;
  business_type: string | null; warehouse: string | null;
  pn_std: string | null; description: string | null;
  qty: number | null; return_qty: number | null;
  unit_cost: number | null; cost_amount: number | null;
  cost_source: string | null; cost_tax_basis: string | null;
  price_month: string | null; trace_months: number | null;
  linked_purchase_order_no: string | null; anomaly_flags: string[];
}

// 成本来源五态（口径见开发方案 §4.2）；trace_avg 必须带追溯月数标注（客户要求）
const SOURCE_META: Record<string, { label: string; color: string }> = {
  direct: { label: "实际·专属采购", color: "green" },
  month_avg: { label: "实际·当月均价", color: "blue" },
  trace_avg: { label: "预估·追溯均价", color: "orange" },
  sales_ref: { label: "没有采购有销售", color: "purple" },
  none: { label: "无成本", color: "red" },
};

function SourceTag({ source, trace }: { source: string | null; trace?: number | null }) {
  if (!source) return <span style={{ color: "var(--mb-text-3)" }}>-</span>;
  const m = SOURCE_META[source] || { label: source, color: "default" };
  const suffix = trace && trace >= 1 ? `·追溯${trace}月` : "";
  return <Tag color={m.color}>{m.label}{suffix}</Tag>;
}

export default function ProjectCostPage() {
  const [range, setRange] = useState<[Dayjs, Dayjs] | null>(null);
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<ProjectRow[]>([]);
  const [startDate, setStartDate] = useState("");
  const [loading, setLoading] = useState(false);
  const [recomputing, setRecomputing] = useState(false);
  // 明细抽屉
  const [detailProject, setDetailProject] = useState<string | null>(null);
  const [lines, setLines] = useState<LineRow[]>([]);
  const [linesTotal, setLinesTotal] = useState(0);
  const [linesPage, setLinesPage] = useState(1);
  const [linesLoading, setLinesLoading] = useState(false);

  const params = () => ({
    q: q || undefined,
    date_from: range?.[0]?.format("YYYY-MM-DD"),
    date_to: range?.[1]?.format("YYYY-MM-DD"),
  });

  const load = async () => {
    setLoading(true);
    try {
      const { data } = await api.get("/maintenance/projects", { params: params() });
      setRows(data.rows);
      setStartDate(data.start_date);
    } catch {
      message.error("项目成本加载失败，请稍后重试或检查权限");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, range]);

  const loadLines = async (project: string, page: number) => {
    setLinesLoading(true);
    try {
      const { data } = await api.get("/maintenance/lines", {
        params: { project, page, page_size: 50 },
      });
      setLines(data.rows);
      setLinesTotal(data.total);
      setLinesPage(page);
    } catch {
      message.error("明细加载失败");
    } finally {
      setLinesLoading(false);
    }
  };

  const openDetail = (project: string) => {
    setDetailProject(project);
    setLines([]);
    loadLines(project, 1);
  };

  const recompute = async () => {
    setRecomputing(true);
    try {
      const { data } = await api.post("/maintenance/recompute");
      message.success(
        `重算完成：${data.lines_in_scope} 行 · 专属采购 ${data.direct} · 当月均价 ${data.month_avg}` +
        ` · 追溯 ${data.trace_avg} · 销售参考 ${data.sales_ref} · 无成本 ${data.none}`);
      load();
    } catch {
      message.error("重算失败");
    } finally {
      setRecomputing(false);
    }
  };

  const exportCsv = async () => {
    const res = await api.get("/maintenance/export", { params: params(), responseType: "blob" });
    const url = URL.createObjectURL(res.data);
    const a = document.createElement("a");
    a.href = url;
    a.download = "maintenance_projects.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  const projectCols: ColumnsType<ProjectRow> = [
    { title: "项目", dataIndex: "project", width: 300, fixed: "left", ellipsis: true },
    { title: "出库行", dataIndex: "lines", width: 80, align: "right" },
    { title: "数量", dataIndex: "qty", width: 80, align: "right" },
    { title: "备件成本(含税)", dataIndex: "cost_inc", width: 130, align: "right", render: money,
      defaultSortOrder: "descend",
      sorter: (a, b) => (a.cost_total ?? 0) - (b.cost_total ?? 0) },
    { title: "备件成本(不含税)", dataIndex: "cost_ex", width: 130, align: "right", render: money },
    { title: "覆盖率", dataIndex: "coverage_pct", width: 110,
      render: (v: number | null) => v == null ? "-" :
        <Progress percent={v} size="small" status={v < 80 ? "exception" : "normal"} /> },
    { title: "成本来源分布", dataIndex: "by_source", width: 240,
      render: (bs: Record<string, number>) => (
        <Space size={2} wrap>
          {Object.entries(SOURCE_META).map(([k, m]) =>
            bs?.[k] ? <Tooltip key={k} title={m.label}><Tag color={m.color}>{bs[k]}</Tag></Tooltip> : null)}
        </Space>
      ) },
    { title: "合同额(参考)", dataIndex: "contract_amount", width: 140, align: "right",
      render: (v: number | null, r) => (
        <span>
          {money(v)}
          {r.contract_shared && (
            <Tooltip title="该合同被多个项目共同引用，金额跨项目重复，仅作参考（本期不计项目毛利）">
              <Tag color="warning" style={{ marginLeft: 4 }}>共用</Tag>
            </Tooltip>
          )}
        </span>
      ) },
    { title: "月份数", dataIndex: "months", width: 80, align: "right" },
    { title: "", width: 70, render: (_, r) => <a onClick={() => openDetail(r.project)}>明细</a> },
  ];

  const lineCols: ColumnsType<LineRow> = [
    { title: "日期", dataIndex: "order_date", width: 100 },
    { title: "维保单号", dataIndex: "order_no", width: 160, ellipsis: true },
    { title: "需求类型", dataIndex: "demand_type", width: 90 },
    { title: "PN", dataIndex: "pn_std", width: 160, ellipsis: true },
    { title: "描述", dataIndex: "description", ellipsis: true },
    { title: "数量", dataIndex: "qty", width: 70, align: "right" },
    { title: "退货", dataIndex: "return_qty", width: 70, align: "right",
      render: (v: number | null) => (v ? <Tag color="orange">{v}</Tag> : "-") },
    { title: "单价", dataIndex: "unit_cost", width: 100, align: "right", render: money },
    { title: "金额", dataIndex: "cost_amount", width: 110, align: "right", render: money },
    { title: "成本来源", width: 170,
      render: (_, r) => <SourceTag source={r.cost_source} trace={r.trace_months} /> },
    { title: "口径", dataIndex: "cost_tax_basis", width: 70,
      render: (v: string | null) => v ? <Tag>{v === "inc" ? "含税" : "不含税"}</Tag> : "-" },
    { title: "取价月", dataIndex: "price_month", width: 90 },
    { title: "关联采购单", dataIndex: "linked_purchase_order_no", width: 160, ellipsis: true,
      render: (v: string | null) => v || <span style={{ color: "var(--mb-text-3)" }}>-</span> },
  ];

  const totalInc = rows.reduce((s, r) => s + (r.cost_inc ?? 0), 0);
  const totalEx = rows.reduce((s, r) => s + (r.cost_ex ?? 0), 0);
  const totalNone = rows.reduce((s, r) => s + (r.by_source?.none ?? 0), 0);

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title="项目成本"
        subtitle={`维保项目备件成本自动核算：已采购按真实采购价（专属采购/当月均价），未采购按追溯均价（≤3月，标注）或销售参考价，逐条标注来源与含税口径${startDate ? ` · 起算日 ${startDate}` : ""}`}
      />
      <Card>
        <Space wrap size="large">
          <DatePicker.RangePicker onChange={(v) => setRange(v as [Dayjs, Dayjs] | null)} />
          <Input.Search
            placeholder="搜索项目名"
            allowClear
            style={{ width: 260 }}
            onSearch={(v) => setQ(v.trim())}
          />
          <Button type="primary" loading={recomputing} onClick={recompute}>重算成本</Button>
          <Button onClick={exportCsv} disabled={!rows.length}>导出 CSV</Button>
        </Space>
      </Card>

      <Row gutter={16}>
        <Col span={6}><Card size="small"><Statistic title="项目数" value={rows.length} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="备件成本 · 含税小计" value={totalInc} precision={2} prefix="¥" /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="备件成本 · 不含税小计" value={totalEx} precision={2} prefix="¥" /></Card></Col>
        <Col span={6}><Card size="small"><Statistic title="无成本行(待处理)" value={totalNone}
          valueStyle={{ color: totalNone ? "var(--mb-danger)" : undefined }} /></Card></Col>
      </Row>

      <Card title="项目备件成本">
        <Alert
          type="info" showIcon style={{ marginBottom: 12 }}
          message="含税/不含税小计按采购原值口径分列（客户确认口径），请勿直接相加对账；「合同额」为关联销售订单金额，仅作参考，本期不计项目毛利。"
        />
        <ResizableTable
          storageKey="maint-projects"
          rowKey="project"
          size="small"
          loading={loading}
          columns={projectCols}
          dataSource={rows}
          scroll={{ x: 1480 }}
          pagination={{ pageSize: 20, showSizeChanger: true }}
        />
      </Card>

      <Drawer
        open={!!detailProject}
        width={1100}
        onClose={() => setDetailProject(null)}
        title={detailProject ? `${detailProject} · 出库明细` : ""}
      >
        <ResizableTable
          storageKey="maint-lines"
          rowKey={(r: LineRow, i?: number) => `${r.order_no}-${i}`}
          size="small"
          loading={linesLoading}
          columns={lineCols}
          dataSource={lines}
          scroll={{ x: 1400 }}
          pagination={{
            current: linesPage, pageSize: 50, total: linesTotal, showSizeChanger: false,
            onChange: (p: number) => detailProject && loadLines(detailProject, p),
          }}
        />
      </Drawer>
    </Space>
  );
}
