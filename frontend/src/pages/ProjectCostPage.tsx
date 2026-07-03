import { useEffect, useRef, useState } from "react";
import {
  Card, DatePicker, Input, Button, Space, Tag, Tooltip, message, Statistic, Row, Col,
  Drawer, Progress, Alert, Empty,
} from "antd";
import { InfoCircleOutlined } from "@ant-design/icons";
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
  contract_incomplete: boolean;
}

interface LineRow {
  id: number;
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
const SOURCE_ORDER = ["direct", "month_avg", "trace_avg", "sales_ref", "none"];
const COVERAGE_WARN_PCT = 80;   // 覆盖率预警线（经验值，非验收线；<此值标红提示核对无成本行）

function SourceTag({ source, trace }: { source: string | null; trace?: number | null }) {
  if (!source) return <span style={{ color: "var(--mb-text-3)" }}>-</span>;
  const m = SOURCE_META[source] || { label: source, color: "default" };
  const suffix = trace && trace >= 1 ? `·追溯${trace}月` : "";
  return <Tag color={m.color}>{m.label}{suffix}</Tag>;
}

// 成本来源图例（列头 Tooltip 用，避免用户逐个 hover 才懂颜色）
const SourceLegend = (
  <div>
    {SOURCE_ORDER.map((k) => (
      <div key={k} style={{ whiteSpace: "nowrap" }}>
        <Tag color={SOURCE_META[k].color}>■</Tag>{SOURCE_META[k].label}
      </div>
    ))}
  </div>
);

export default function ProjectCostPage() {
  const [range, setRange] = useState<[Dayjs, Dayjs] | null>(null);
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<ProjectRow[]>([]);
  const [startDate, setStartDate] = useState("");
  const [loading, setLoading] = useState(false);
  const [recomputing, setRecomputing] = useState(false);
  const [exporting, setExporting] = useState(false);
  // 明细抽屉
  const [detailProject, setDetailProject] = useState<string | null>(null);
  const [lines, setLines] = useState<LineRow[]>([]);
  const [linesTotal, setLinesTotal] = useState(0);
  const [linesPage, setLinesPage] = useState(1);
  const [linesMonth, setLinesMonth] = useState<string | undefined>(undefined);
  const [linesLoading, setLinesLoading] = useState(false);
  // 请求序号守卫：抽屉快速翻页/切项目时，迟到响应不得覆盖新结果
  const linesSeq = useRef(0);
  const detailRef = useRef<string | null>(null);

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

  const loadLines = async (project: string, page: number, month?: string) => {
    const seq = ++linesSeq.current;
    setLinesLoading(true);
    try {
      const { data } = await api.get("/maintenance/lines", {
        params: { project, page, page_size: 50, month, ...params() },
      });
      // 仅当仍是最新请求且抽屉未切项目时才落地（防竞态覆盖）
      if (seq !== linesSeq.current || detailRef.current !== project) return;
      setLines(data.rows);
      setLinesTotal(data.total);
      setLinesPage(page);
    } catch {
      if (seq === linesSeq.current) message.error("明细加载失败");
    } finally {
      if (seq === linesSeq.current) setLinesLoading(false);
    }
  };

  const openDetail = (project: string) => {
    detailRef.current = project;
    setDetailProject(project);
    setLines([]);
    setLinesTotal(0);
    setLinesPage(1);
    setLinesMonth(undefined);
    loadLines(project, 1);
  };

  const closeDetail = () => {
    detailRef.current = null;
    setDetailProject(null);
  };

  const recompute = async () => {
    setRecomputing(true);
    const hide = message.loading("正在重算项目成本，约需 1 分钟，请勿刷新页面…", 0);
    try {
      const { data } = await api.post("/maintenance/recompute");
      message.success(
        `重算完成：${data.lines_in_scope} 行 · 专属采购 ${data.direct} · 当月均价 ${data.month_avg}` +
        ` · 追溯 ${data.trace_avg} · 销售参考 ${data.sales_ref} · 无成本 ${data.none}`);
      load();
    } catch {
      message.error("重算失败（需要管理员权限）");
    } finally {
      hide();
      setRecomputing(false);
    }
  };

  const download = (path: string, filename: string, extra?: Record<string, unknown>) =>
    api.get(path, { params: { ...params(), ...extra }, responseType: "blob" })
      .then((res) => {
        const url = URL.createObjectURL(res.data);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        a.click();
        URL.revokeObjectURL(url);
      });

  const exportCsv = async () => {
    setExporting(true);
    try {
      await download("/maintenance/export", "maintenance_projects.csv");
    } catch {
      message.error("导出失败，请稍后重试或检查权限");
    } finally {
      setExporting(false);
    }
  };

  const exportLines = () => {
    if (!detailProject) return;
    download("/maintenance/lines/export", `项目备件明细.csv`,
      { project: detailProject, month: linesMonth })
      .catch(() => message.error("明细导出失败，请稍后重试"));
  };

  const projectCols: ColumnsType<ProjectRow> = [
    { title: "项目", dataIndex: "project", width: 300, fixed: "left", ellipsis: true },
    { title: "出库行", dataIndex: "lines", width: 80, align: "right" },
    { title: "数量", dataIndex: "qty", width: 80, align: "right" },
    { title: "备件成本(含税)", dataIndex: "cost_inc", width: 130, align: "right", render: money,
      sorter: (a, b) => (a.cost_inc ?? 0) - (b.cost_inc ?? 0) },
    { title: "备件成本(不含税)", dataIndex: "cost_ex", width: 130, align: "right", render: money,
      defaultSortOrder: "descend",
      sorter: (a, b) => (a.cost_ex ?? 0) - (b.cost_ex ?? 0) },
    { title: (
        <Tooltip title="有成本来源的出库行占比（按行数；销售参考价也计入已覆盖）。低于阈值提示核对无成本行。">
          覆盖率 <InfoCircleOutlined style={{ color: "var(--mb-text-3)" }} />
        </Tooltip>
      ), dataIndex: "coverage_pct", width: 120,
      render: (v: number | null) => v == null ? "-" :
        <Progress percent={v} size="small" status={v < COVERAGE_WARN_PCT ? "exception" : "normal"} /> },
    { title: (
        <Tooltip title={SourceLegend}>
          成本来源分布 <InfoCircleOutlined style={{ color: "var(--mb-text-3)" }} />
        </Tooltip>
      ), dataIndex: "by_source", width: 240,
      render: (bs: Record<string, number>) => (
        <Space size={2} wrap>
          {SOURCE_ORDER.map((k) =>
            bs?.[k] ? <Tooltip key={k} title={SOURCE_META[k].label}>
              <Tag color={SOURCE_META[k].color}>{bs[k]}</Tag></Tooltip> : null)}
        </Space>
      ) },
    { title: "合同额(参考)", dataIndex: "contract_amount", width: 150, align: "right",
      render: (v: number | null, r) => (
        <span>
          {money(v)}
          {r.contract_shared && (
            <Tooltip title="该合同被多个项目共同引用，金额跨项目重复，仅作参考（本期不计项目毛利）">
              <Tag color="warning" style={{ marginLeft: 4 }}>共用</Tag>
            </Tooltip>
          )}
          {r.contract_incomplete && (
            <Tooltip title="部分关联销售订单未在系统中（合同额被低估），请补导对应销售数据">
              <Tag color="default" style={{ marginLeft: 4 }}>不全</Tag>
            </Tooltip>
          )}
        </span>
      ) },
    { title: "月份数", dataIndex: "months", width: 80, align: "right" },
    { title: "", width: 70, fixed: "right", render: (_, r) => <a onClick={() => openDetail(r.project)}>明细</a> },
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

  // 成本被权限脱敏时（page_maintenance 开但无采购成本权限），合计显示为隐藏而非误导的 ¥0
  const costsMasked = rows.length > 0 && rows.every((r) => r.cost_inc == null && r.cost_ex == null);
  const totalInc = rows.reduce((s, r) => s + (r.cost_inc ?? 0), 0);
  const totalEx = rows.reduce((s, r) => s + (r.cost_ex ?? 0), 0);
  const totalNone = rows.reduce((s, r) => s + (r.by_source?.none ?? 0), 0);
  const costStat = (v: number) => costsMasked
    ? { value: "—" as const }
    : { value: v, precision: 2, prefix: "¥" };
  // 抽屉月份下拉：由当前项目行的 months 数不便直接取月份列表，简单用近 24 个月占位由后端过滤
  const monthOptions = detailProject
    ? (rows.find((r) => r.project === detailProject)?.months || 0)
    : 0;

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
          <Button loading={exporting} onClick={exportCsv} disabled={!rows.length}>导出 CSV</Button>
        </Space>
      </Card>

      <Row gutter={16}>
        <Col span={6}><Card size="small"><Statistic title="项目数" value={rows.length} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic
          title={<Space size={4}>备件成本 · 含税小计
            <Tooltip title="含税/不含税为分列口径，请勿直接相加对账"><InfoCircleOutlined /></Tooltip></Space>}
          {...costStat(totalInc)} /></Card></Col>
        <Col span={6}><Card size="small"><Statistic
          title={<Space size={4}>备件成本 · 不含税小计
            <Tooltip title="含税/不含税为分列口径，请勿直接相加对账"><InfoCircleOutlined /></Tooltip></Space>}
          {...costStat(totalEx)} /></Card></Col>
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
          scroll={{ x: 1520 }}
          pagination={{ pageSize: 20, showSizeChanger: true }}
          locale={{ emptyText: (q || range)
            ? "当前筛选无结果，请调整搜索或日期范围"
            : <Empty description="尚未导入维保出库数据，请到【数据导入】上传氚云维保订单 Excel" /> }}
        />
      </Card>

      <Drawer
        open={!!detailProject}
        width={1100}
        onClose={closeDetail}
        title={detailProject ? `${detailProject} · 出库明细` : ""}
        extra={
          <Space>
            {monthOptions > 1 && (
              <DatePicker
                picker="month" placeholder="按月筛选" allowClear
                onChange={(d) => {
                  const m = d ? d.format("YYYY-MM") : undefined;
                  setLinesMonth(m);
                  if (detailProject) loadLines(detailProject, 1, m);
                }}
              />
            )}
            <Button size="small" onClick={exportLines} disabled={!linesTotal}>导出明细 CSV</Button>
          </Space>
        }
      >
        <ResizableTable
          storageKey="maint-lines"
          rowKey="id"
          size="small"
          loading={linesLoading}
          columns={lineCols}
          dataSource={lines}
          scroll={{ x: 1400 }}
          pagination={{
            current: linesPage, pageSize: 50, total: linesTotal,
            showSizeChanger: false, showTotal: (t) => `共 ${t} 行`,
            onChange: (p: number) => detailProject && loadLines(detailProject, p, linesMonth),
          }}
        />
      </Drawer>
    </Space>
  );
}
