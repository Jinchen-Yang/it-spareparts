import { useEffect, useState } from "react";
import {
  Card, Row, Col, Statistic, Segmented, Tag, Button, Modal, Input, message, Space,
  Tooltip, Tabs, Select, Popconfirm, Descriptions,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import ResizableTable from "../components/ResizableTable";
import api from "../api";
import { money, pct, splitFixed } from "../utils/format";
import { useTaxBasis } from "../context/TaxBasis";
import DataQualityIssuesPanel from "./governance/DataQualityIssuesPanel";
import DataQualityCalibrationPanel from "./governance/DataQualityCalibrationPanel";
const pct100 = (v: number | null) => (v == null ? "-" : `${v.toFixed(1)}%`);

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

interface CandidateRow {
  id: number;
  source_pn: string | null;
  source_desc: string | null;
  candidate_pn: string | null;
  candidate_desc: string | null;
  match_score: number | null;
  match_reason: string | null;
  source_volume: number;
}

interface IssueRow {
  id: number;
  issue_type: string;
  pn_std: string | null;
  part_description: string | null;
  description: string | null;
}

interface AliasRow {
  id: number;
  pn_raw: string;
  pn_std: string;
  source: string;
}

interface MergeRow {
  merge_log_id: number;
  source_pn: string | null;
  target_pn: string | null;
  reason: string | null;
  created_by: string | null;
  created_at: string;
  reverted: boolean;
  affected_counts: Record<string, number>;
}

const ISSUE_LABELS: Record<string, string> = {
  missing_brand: "缺品牌", missing_category: "缺品类", missing_description: "缺描述",
  nonstd_pn: "非标型号", duplicate_suspected: "疑似重复",
};

/** 型号治理（原有页签：非标/待复核/已排除 + 排除操作） */
function PartsTab() {
  const { basis } = useTaxBasis();
  const [kind, setKind] = useState("nonstd");
  const [rows, setRows] = useState<PartRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [excluding, setExcluding] = useState<PartRow | null>(null);
  const [reason, setReason] = useState("");

  const load = async (p = 1) => {
    setLoading(true);
    try {
      const { data } = await api.get("/governance/parts", { params: { kind, page: p, page_size: 20 } });
      setRows(data.items); setTotal(data.total); setPage(p);
    } finally { setLoading(false); }
  };
  useEffect(() => { load(1); /* eslint-disable-next-line */ }, [kind]);

  const doExclude = async () => {
    if (!excluding) return;
    await api.put("/governance/exclude", { pn_std: excluding.pn_std, excluded: true, reason });
    message.success("已排除，建议点「重算利润」生效");
    setExcluding(null); setReason("");
    load(page);
  };
  const unExclude = async (pn: string) => {
    await api.put("/governance/exclude", { pn_std: pn, excluded: false });
    message.success("已恢复，建议点「重算利润」生效");
    load(page);
  };
  const confirmNoCandidates = async () => {
    const { data } = await api.post("/governance/parts/confirm-batch", { scope: "no_candidates" });
    message.success(`已批量确认 ${data.confirmed} 个无候选的待复核型号为独立型号`);
    load(page);
  };

  const cols: ColumnsType<PartRow> = [
    { title: "型号 (PN)", dataIndex: "pn_std", width: 220, fixed: "left",
      render: (v, r) => <span>{v} {r.needs_review && <Tag color="orange">待复核</Tag>}{r.is_excluded && <Tag color="red">已排除</Tag>}</span> },
    { title: "描述", dataIndex: "description", ellipsis: true },
    { title: "销售行", dataIndex: "sales_lines", width: 80, align: "right" },
    ...(basis !== "ex" ? [{ title: "营收(含税)", key: "revenue_inc", dataIndex: "revenue", width: 130, align: "right" as const,
      render: (v: number | null) => money(splitFixed(v, "ex").inc) }] as ColumnsType<PartRow> : []),
    ...(basis !== "inc" ? [{ title: "营收(不含税)", key: "revenue_ex", dataIndex: "revenue", width: 130, align: "right" as const,
      render: (v: number | null) => money(splitFixed(v, "ex").ex) }] as ColumnsType<PartRow> : []),
    { title: "毛利率", dataIndex: "gross_margin", width: 100, align: "right",
      render: (v: number | null) => <span style={{ color: v != null && v < 0 ? "var(--mb-danger)" : undefined }}>{pct(v)}</span> },
    { title: "操作", width: 90, fixed: "right",
      render: (_, r) => r.is_excluded
        ? <a onClick={() => unExclude(r.pn_std)}>恢复</a>
        : <a style={{ color: "var(--mb-danger)" }} onClick={() => { setExcluding(r); setReason(""); }}>排除</a> },
  ];

  return (
    <>
      <Space style={{ marginBottom: 16 }} wrap>
        <Segmented
          value={kind} onChange={(v) => setKind(v as string)}
          options={[
            { label: "非标候选型号", value: "nonstd" },
            { label: "待复核 PN", value: "needs_review" },
            { label: "已排除", value: "excluded" },
          ]}
        />
        {kind === "needs_review" && (
          <Popconfirm title="把所有「没有合并候选」的待复核型号确认为独立型号？" onConfirm={confirmNoCandidates}>
            <Button>批量确认无候选型号</Button>
          </Popconfirm>
        )}
      </Space>
      <ResizableTable storageKey="gov-overview" rowKey="pn_std" size="small" loading={loading} columns={cols} dataSource={rows}
        scroll={{ x: 800 }}
        pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: (p) => load(p) }} />
      <Modal open={!!excluding} title={`排除型号：${excluding?.pn_std}`} okText="确认排除" okButtonProps={{ danger: true }}
        onCancel={() => setExcluding(null)} onOk={doExclude}>
        <p style={{ color: "var(--mb-text-3)" }}>排除后，该型号的销售行不再计入营收/利润统计（如笼统打包型号"一批备件"）。</p>
        <Input.TextArea rows={2} placeholder="排除原因（写入审计）" value={reason} onChange={(e) => setReason(e.target.value)} />
      </Modal>
    </>
  );
}

/** 合并候选审核 */
function CandidatesTab({ onChanged }: { onChanged: () => void }) {
  const [rows, setRows] = useState<CandidateRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [order, setOrder] = useState("volume");
  const [loading, setLoading] = useState(false);
  const [acting, setActing] = useState<number | null>(null);

  const load = async (p = 1) => {
    setLoading(true);
    try {
      const { data } = await api.get("/governance/candidates",
        { params: { status: "pending", order, page: p, page_size: 20 } });
      setRows(data.items); setTotal(data.total); setPage(p);
    } finally { setLoading(false); }
  };
  useEffect(() => { load(1); /* eslint-disable-next-line */ }, [order]);

  const review = async (id: number, action: string) => {
    setActing(id);
    try {
      await api.post(`/governance/candidates/${id}/review`, { action });
      message.success(action === "merge" ? "已合并并重算利润" : action === "reject" ? "已否决" : "已确认为独立型号");
      load(page); onChanged();
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "操作失败");
    } finally { setActing(null); }
  };

  const cols: ColumnsType<CandidateRow> = [
    { title: "源型号（将被并入）", dataIndex: "source_pn", width: 200,
      render: (v, r) => <Tooltip title={r.source_desc}><span>{v}</span></Tooltip> },
    { title: "建议目标", dataIndex: "candidate_pn", width: 200,
      render: (v, r) => <Tooltip title={r.candidate_desc}><b>{v}</b></Tooltip> },
    { title: "分数", dataIndex: "match_score", width: 70, align: "right",
      render: (v: number | null) => v?.toFixed(2) ?? "-" },
    { title: "依据", dataIndex: "match_reason", ellipsis: true },
    { title: "业务行", dataIndex: "source_volume", width: 80, align: "right" },
    { title: "操作", width: 230, fixed: "right",
      render: (_, r) => (
        <Space>
          <Popconfirm title={`确认把 ${r.source_pn} 并入 ${r.candidate_pn}？历史采购/销售/库存将归并，并自动重算利润。`}
            onConfirm={() => review(r.id, "merge")}>
            <Button size="small" type="primary" loading={acting === r.id}>合并</Button>
          </Popconfirm>
          <Button size="small" onClick={() => review(r.id, "reject")}>否决</Button>
          <Tooltip title="源型号是独立型号，不是任何型号的重复">
            <Button size="small" onClick={() => review(r.id, "independent")}>独立型号</Button>
          </Tooltip>
        </Space>
      ) },
  ];

  return (
    <>
      <Space style={{ marginBottom: 16 }}>
        排序：
        <Select value={order} onChange={setOrder} style={{ width: 140 }}
          options={[
            { label: "业务量优先", value: "volume" },
            { label: "分数优先", value: "score" },
            { label: "最早优先", value: "age" },
          ]} />
      </Space>
      <ResizableTable storageKey="gov-candidates" rowKey="id" size="small" loading={loading} columns={cols} dataSource={rows}
        scroll={{ x: 1000 }}
        pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: (p) => load(p) }} />
    </>
  );
}

/** 质量问题队列 */
function IssuesTab() {
  const [rows, setRows] = useState<IssueRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [type, setType] = useState<string | undefined>(undefined);
  const [loading, setLoading] = useState(false);

  const load = async (p = 1) => {
    setLoading(true);
    try {
      const { data } = await api.get("/governance/issues",
        { params: { status: "open", issue_type: type, page: p, page_size: 20 } });
      setRows(data.items); setTotal(data.total); setPage(p);
    } finally { setLoading(false); }
  };
  useEffect(() => { load(1); /* eslint-disable-next-line */ }, [type]);

  const resolve = async (id: number, action: string) => {
    await api.post(`/governance/issues/${id}/resolve`, { action });
    message.success(action === "dismissed" ? "已驳回（不再提示）" : "已关闭");
    load(page);
  };

  const cols: ColumnsType<IssueRow> = [
    { title: "类型", dataIndex: "issue_type", width: 110,
      render: (v: string) => <Tag>{ISSUE_LABELS[v] || v}</Tag> },
    { title: "型号", dataIndex: "pn_std", width: 200 },
    { title: "型号描述", dataIndex: "part_description", ellipsis: true },
    { title: "问题", dataIndex: "description", ellipsis: true },
    { title: "操作", width: 160, fixed: "right",
      render: (_, r) => (
        <Space>
          <a onClick={() => resolve(r.id, "resolved")}>已处理</a>
          <Tooltip title="确认这不是问题，扫描不再重开">
            <a onClick={() => resolve(r.id, "dismissed")}>不是问题</a>
          </Tooltip>
        </Space>
      ) },
  ];

  return (
    <>
      <Space style={{ marginBottom: 16 }}>
        <Select allowClear placeholder="问题类型" value={type} onChange={setType} style={{ width: 160 }}
          options={Object.entries(ISSUE_LABELS).map(([value, label]) => ({ value, label }))} />
      </Space>
      <ResizableTable storageKey="gov-issues" rowKey="id" size="small" loading={loading} columns={cols} dataSource={rows}
        scroll={{ x: 900 }}
        pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: (p) => load(p) }} />
    </>
  );
}

/** 别名审核 */
function AliasesTab() {
  const [rows, setRows] = useState<AliasRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [reassigning, setReassigning] = useState<AliasRow | null>(null);
  const [targetPn, setTargetPn] = useState("");

  const load = async (p = 1) => {
    setLoading(true);
    try {
      const { data } = await api.get("/governance/aliases",
        { params: { status: "pending", page: p, page_size: 20 } });
      setRows(data.items); setTotal(data.total); setPage(p);
    } finally { setLoading(false); }
  };
  useEffect(() => { load(1); /* eslint-disable-next-line */ }, []);

  const review = async (id: number, action: string, target?: string) => {
    try {
      await api.post(`/governance/aliases/${id}/review`, { action, target_pn: target });
      message.success("已处理");
      setReassigning(null); setTargetPn("");
      load(page);
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "操作失败");
    }
  };

  const cols: ColumnsType<AliasRow> = [
    { title: "原始写法", dataIndex: "pn_raw", width: 260 },
    { title: "当前映射型号", dataIndex: "pn_std", width: 220 },
    { title: "来源", dataIndex: "source", width: 80 },
    { title: "操作", width: 200, fixed: "right",
      render: (_, r) => (
        <Space>
          <a onClick={() => review(r.id, "approve")}>确认</a>
          <a onClick={() => review(r.id, "reject")}>否决</a>
          <a onClick={() => { setReassigning(r); setTargetPn(""); }}>改指</a>
        </Space>
      ) },
  ];

  return (
    <>
      <ResizableTable storageKey="gov-aliases" rowKey="id" size="small" loading={loading} columns={cols} dataSource={rows}
        scroll={{ x: 800 }}
        pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: (p) => load(p) }} />
      <Modal open={!!reassigning} title={`改指别名：${reassigning?.pn_raw}`} okText="确认改指"
        onCancel={() => setReassigning(null)}
        onOk={() => reassigning && review(reassigning.id, "reassign", targetPn.trim())}
        okButtonProps={{ disabled: !targetPn.trim() }}>
        <p style={{ color: "var(--mb-text-3)" }}>该写法将映射到下方型号，其既有采购/销售行会同步重指。</p>
        <Input placeholder="目标型号 (PN)" value={targetPn} onChange={(e) => setTargetPn(e.target.value)} />
      </Modal>
    </>
  );
}

/** 合并历史 + 回滚 */
function MergesTab({ onChanged }: { onChanged: () => void }) {
  const [rows, setRows] = useState<MergeRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [acting, setActing] = useState<number | null>(null);

  const load = async (p = 1) => {
    setLoading(true);
    try {
      const { data } = await api.get("/governance/merges", { params: { page: p, page_size: 20 } });
      setRows(data.items); setTotal(data.total); setPage(p);
    } finally { setLoading(false); }
  };
  useEffect(() => { load(1); }, []);

  const unmerge = async (id: number) => {
    setActing(id);
    try {
      await api.post("/governance/parts/unmerge", { merge_log_id: id });
      message.success("已回滚合并并重算利润");
      load(page); onChanged();
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "回滚失败");
    } finally { setActing(null); }
  };

  const cols: ColumnsType<MergeRow> = [
    { title: "#", dataIndex: "merge_log_id", width: 60 },
    { title: "源型号（已并入）", dataIndex: "source_pn", width: 200 },
    { title: "目标型号", dataIndex: "target_pn", width: 200, render: (v) => <b>{v}</b> },
    { title: "受影响", key: "aff", width: 160,
      render: (_, r) => {
        const c = r.affected_counts || {};
        const parts = [
          c.f_sales_line_ids ? `销${c.f_sales_line_ids}` : "",
          c.f_purchase_line_ids ? `采${c.f_purchase_line_ids}` : "",
          c.inventory_ids ? `存${c.inventory_ids}` : "",
        ].filter(Boolean);
        return <span style={{ color: "var(--mb-text-3)" }}>{parts.join(" / ") || "-"}</span>;
      } },
    { title: "原因", dataIndex: "reason", ellipsis: true },
    { title: "操作人", dataIndex: "created_by", width: 90 },
    { title: "时间", dataIndex: "created_at", width: 160,
      render: (v: string) => v?.replace("T", " ").slice(0, 16) },
    { title: "操作", width: 110, fixed: "right",
      render: (_, r) => r.reverted
        ? <Tag>已回滚</Tag>
        : <Popconfirm title={`回滚「${r.source_pn} → ${r.target_pn}」？历史归属将还原并重算利润。`}
            onConfirm={() => unmerge(r.merge_log_id)}>
            <Button size="small" danger loading={acting === r.merge_log_id}>回滚</Button>
          </Popconfirm> },
  ];

  return (
    <>
      <p style={{ color: "var(--mb-text-3)", marginBottom: 12 }}>
        合并是可逆操作：回滚会按日志把历史采购/销售/库存的归属逐行还原到源型号并重算利润。
        建议仅回滚最近一次误操作。
      </p>
      <ResizableTable storageKey="gov-mergelog" rowKey="merge_log_id" size="small" loading={loading} columns={cols} dataSource={rows}
        scroll={{ x: 1000 }}
        pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: (p) => load(p) }} />
    </>
  );
}

/** 主数据指标（审核说明 §7） */
function MetricsTab({ metrics }: { metrics: any }) {
  if (!metrics) return null;
  const m = metrics;
  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Row gutter={[16, 16]}>
        <Col span={4}><Statistic title="在用型号" value={m.parts.total_active} /></Col>
        <Col span={4}><Statistic title="已合并" value={m.parts.merged} /></Col>
        <Col span={4}><Statistic title="人工已确认" value={m.parts.reviewed} suffix={`/ ${pct100(m.parts.reviewed_pct)}`} /></Col>
        <Col span={4}><Statistic title="待复核" value={m.parts.needs_review} valueStyle={{ color: "var(--mb-warning)" }} /></Col>
        <Col span={4}><Statistic title="待审候选" value={m.review_backlog.pending_candidates} /></Col>
        <Col span={4}><Statistic title={`超${m.review_backlog.stale_days}天未审`} value={m.review_backlog.stale_candidates} valueStyle={{ color: m.review_backlog.stale_candidates ? "var(--mb-danger)" : undefined }} /></Col>
      </Row>
      <Descriptions bordered size="small" column={3}>
        <Descriptions.Item label="采购行 part_id 覆盖率">{pct100(m.coverage.purchase_lines.part_id_pct)}</Descriptions.Item>
        <Descriptions.Item label="销售行 part_id 覆盖率">{pct100(m.coverage.sales_lines.part_id_pct)}</Descriptions.Item>
        <Descriptions.Item label="库存 part_id 覆盖率">{pct100(m.coverage.inventory.part_id_pct)}</Descriptions.Item>
        <Descriptions.Item label="原文保留率(采购)">{pct100(m.coverage.purchase_lines.raw_retained_pct)}</Descriptions.Item>
        <Descriptions.Item label="原文保留率(销售)">{pct100(m.coverage.sales_lines.raw_retained_pct)}</Descriptions.Item>
        <Descriptions.Item label="别名归一率">{pct100(m.alias.normalized_pct)}（待审 {m.alias.pending}）</Descriptions.Item>
        <Descriptions.Item label="资料完整率(全量)">{pct100(m.completeness.all_pct)}</Descriptions.Item>
        <Descriptions.Item label="资料完整率(高频)">{pct100(m.completeness.high_freq_pct)}（{m.completeness.high_freq_total} 个有售型号）</Descriptions.Item>
        <Descriptions.Item label="疑似重复型号">{m.duplicates.open_suspected_parts}（{pct100(m.duplicates.suspected_pct)}）</Descriptions.Item>
        <Descriptions.Item label="价格可追溯率">{pct100(m.price_traceability.traceable_pct)}</Descriptions.Item>
        <Descriptions.Item label="毛利可计算率">{pct100(m.price_traceability.margin_computable_pct)}</Descriptions.Item>
        <Descriptions.Item label="开放质量问题">
          {Object.entries(m.open_issues || {}).map(([k, v]) => `${ISSUE_LABELS[k] || k}:${v}`).join("  ") || "无"}
        </Descriptions.Item>
      </Descriptions>
    </Space>
  );
}

export default function GovernancePage() {
  const isAdmin = localStorage.getItem("role") === "admin";
  const [sum, setSum] = useState<any>(null);
  const [metrics, setMetrics] = useState<any>(null);
  const [recomputing, setRecomputing] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const loadTop = () => {
    api.get("/governance/summary").then((r) => setSum(r.data));
    api.get("/governance/metrics").then((r) => setMetrics(r.data));
  };
  useEffect(() => { loadTop(); }, []);

  const recompute = async () => {
    setRecomputing(true);
    try {
      const { data } = await api.post("/profit/recompute");
      message.success(`重算完成：排除型号行 ${data.excluded_part ?? 0}，负毛利 ${data.neg_margin}`);
      loadTop();
    } finally { setRecomputing(false); }
  };

  const refresh = async () => {
    setRefreshing(true);
    try {
      const { data } = await api.post("/governance/refresh?with_candidates=true");
      const c = data.candidates;
      message.success(`刷新完成：品牌 ${data.brands.brands}、规格 ${data.specs.spec_rows} 条、新候选 ${c?.candidates_inserted ?? 0}`);
      loadTop();
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "刷新失败");
    } finally { setRefreshing(false); }
  };

  const [classifying, setClassifying] = useState(false);
  const autoClassify = async () => {
    setClassifying(true);
    try {
      const { data } = await api.post("/governance/classify-backfill");
      message.success(`自动分类完成：新归类 ${data.parts_reclassified} 个型号（共扫描 ${data.scanned}）`);
      loadTop();
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "自动分类失败");
    } finally { setClassifying(false); }
  };

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card title="数据治理"
        extra={<Space>
          <Tooltip title="回填品牌/品类字典、别名、规格，扫描质量问题，生成合并候选（幂等，可随时重跑）">
            <Button loading={refreshing} onClick={refresh}>刷新主数据</Button>
          </Tooltip>
          <Tooltip title="按标准描述给型号自动打上标准品类（人工设过/锁定的分类不动，幂等可重跑）——型号查询「按品类筛选」的数据来源">
            <Button loading={classifying} onClick={autoClassify}>自动分类</Button>
          </Tooltip>
          {isAdmin && (
            <Button type="primary" loading={recomputing} onClick={recompute}>重算利润</Button>
          )}
        </Space>}>
        {sum && (
          <Row gutter={[16, 16]}>
            <Col xs={12} sm={8} md={4}><Statistic title="非标候选型号" value={sum.nonstd_candidates} valueStyle={{ color: "var(--mb-warning)" }} /></Col>
            <Col xs={12} sm={8} md={4}><Statistic title="已排除" value={sum.excluded} valueStyle={{ color: "var(--mb-danger)" }} /></Col>
            <Col xs={12} sm={8} md={4}><Statistic title="PN 待复核" value={sum.needs_review} /></Col>
            <Col xs={12} sm={8} md={4}><Tooltip title="销售行未匹配到成本"><Statistic title="无成本行" value={sum.sales_no_cost} /></Tooltip></Col>
            <Col xs={12} sm={8} md={4}><Tooltip title="账面亏本行"><Statistic title="负毛利行" value={sum.sales_neg_margin} valueStyle={{ color: "var(--mb-danger)" }} /></Tooltip></Col>
            <Col xs={12} sm={8} md={4}><Tooltip title="成本为估算(兜底),非真实匹配"><Statistic title="估算成本行" value={sum.sales_fallback_cost} /></Tooltip></Col>
          </Row>
        )}
      </Card>

      <Card>
        <Tabs
          defaultActiveKey="parts"
          items={[
            { key: "parts", label: "型号治理", children: <PartsTab /> },
            { key: "fact-data-quality", label: "价格与数量疑点", children: <DataQualityIssuesPanel /> },
            { key: "data-quality-calibration", label: "规则校准预览", children: <DataQualityCalibrationPanel /> },
            { key: "candidates", label: "合并候选审核", children: <CandidatesTab onChanged={loadTop} /> },
            { key: "merges", label: "合并历史", children: <MergesTab onChanged={loadTop} /> },
            { key: "issues", label: "质量问题", children: <IssuesTab /> },
            { key: "aliases", label: "别名审核", children: <AliasesTab /> },
            { key: "metrics", label: "主数据指标", children: <MetricsTab metrics={metrics} /> },
          ]}
        />
      </Card>
    </Space>
  );
}
