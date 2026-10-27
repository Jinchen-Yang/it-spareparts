import { useCallback, useEffect, useMemo, useRef, useState, type Key } from "react";
import { useNavigate } from "react-router-dom";
import { Alert, App, Button, DatePicker, Input, Drawer, Select, Segmented, Space, Table, Tabs, Tag } from "antd";
import type { Dayjs } from "dayjs";
import { presales, type Inquiry, type InquiryLine, type Offer } from "../../api/presales";
import { listWorkCandidates, type WorkCandidate } from "../../api/workItems";
import { presalesWorkbooks } from "../../api/presalesWorkbooks";
import { WorkbookExportButton } from "../../components/workbook";
import WorkbookDialog from "./WorkbookDialog";
import { readPermissionMap } from "../../nav";
import type { ColumnsType } from "antd/es/table";
import PageHeader from "../../components/PageHeader";
type InquirySort = { key: string; dir: "asc" | "desc" };
import { isOfferActive, useNow } from "./shared";
import { moneyExact } from "../../utils/format";
import "./presales.css";

const statuses: Record<string, string> = { draft: "草稿", submitted: "已提交", revising: "撤回修改中", closed: "已结束" };
const workStatuses: Record<string, string> = { pending: "待处理", in_progress: "处理中", done: "已回复", declined: "已退回", cancelled: "已取消" };
const defaultSort: InquirySort = { key: "updated_at", dir: "desc" };
const serverKeys = new Set(["inquiry_no", "line_count", "status", "updated_at", "created_at"]);

function lineOffers(detail: Inquiry | undefined, line: InquiryLine): Offer[] {
  // 本行已有引用时只使用钉住快照，不能用当前报价的续期或降价改写摘要。
  const pinned = new Map([...(line.linked_offers || []), ...(line.ref_offer ? [line.ref_offer] : []),
    ...(detail?.line_responses || []).filter((response) => !response.is_draft && response.line_id === line.id && response.mode === "reuse" && response.offer)
      .map((response) => response.offer!)].map((offer) => [offer.id, offer]));
  const current = (detail?.offers || []).filter((offer) => offer.line_id === line.id && !pinned.has(offer.id));
  return [...pinned.values(), ...current];
}

export default function InquiryListPage() {
  const navigate = useNavigate();
  const { message } = App.useApp();
  const permissions = readPermissionMap();
  const supervisor = !!permissions.action_presales_supervise || ["admin", "boss"].includes(localStorage.getItem("role") || "");
  const [scope, setScope] = useState<"mine" | "shared" | "all">("mine");
  const [kind, setKind] = useState<Inquiry["kind"]>();
  const [status, setStatus] = useState<Inquiry["status"]>();
  const [stage, setStage] = useState("");
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [range, setRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);
  const [detailId, setDetailId] = useState<number>();
  const [items, setItems] = useState<Inquiry[]>([]);
  const [details, setDetails] = useState<Record<number, Inquiry>>({});
  const [candidates, setCandidates] = useState<WorkCandidate[]>([]);
  const [selected, setSelected] = useState<Key[]>([]);
  const [importOpen, setImportOpen] = useState(false);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [sort, setSort] = useState<InquirySort | null>(defaultSort);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState(false);
  const [reload, setReload] = useState(0);
  const conflictRetried = useRef(false);
  const now = useNow(Object.values(details).flatMap((detail) => (detail.lines || []).flatMap((line) => lineOffers(detail, line)
    .flatMap((offer) => [offer.valid_from, offer.valid_until]))));
  useEffect(() => {
    const timer = setTimeout(() => { setSearch(query.trim()); setPage(1); }, 300);
    return () => clearTimeout(timer);
  }, [query]);
  const dateFrom = range?.[0]?.format("YYYY-MM-DD");
  const dateTo = range?.[1]?.format("YYYY-MM-DD");
  useEffect(() => { conflictRetried.current = false; setSelected([]); }, [scope, kind, status, page, sort, search, dateFrom, dateTo]);
  useEffect(() => {
    let disposed = false;
    setLoading(true); setError(""); setItems([]); setDetails({}); setDetailError(false);
    const selected = sort && serverKeys.has(sort.key) ? sort : defaultSort;
    void presales.listInquiries({ scope, kind, status, page, page_size: 20,
      q: search || undefined, date_from: dateFrom, date_to: dateTo,
      sort: selected.key as "updated_at", dir: selected.dir }).then(async (data) => {
      if (disposed) return;
      conflictRetried.current = false;
      setItems(data.items); setTotal(data.total);
      const rows = await Promise.allSettled(data.items.map((item) => presales.getInquiry(item.id)));
      if (disposed) return;
      const result: Record<number, Inquiry> = {};
      rows.forEach((row) => { if (row.status === "fulfilled") result[row.value.id] = row.value; });
      setDetails(result); setDetailError(rows.some((row) => row.status === "rejected"));
    }).catch((cause) => {
      if (disposed) return;
      const conflict = cause?.response?.status === 409;
      setError(conflict ? "已被他人修改/领取，请刷新" : "询价单列表加载失败");
      if (conflict && !conflictRetried.current) {
        conflictRetried.current = true;
        void message.warning("已被他人修改/领取，已自动刷新"); setReload((value) => value + 1);
      }
    }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [scope, kind, status, page, reload, message, sort, search, dateFrom, dateTo]);
  useEffect(() => {
    let disposed = false;
    void listWorkCandidates("presales_inquiry").then((data) => { if (!disposed) setCandidates(data.items); }).catch(() => {});
    return () => { disposed = true; };
  }, []);
  const progress = useCallback((row: Inquiry) => {
    if (row.status !== "submitted") return statuses[row.status] || row.status;
    const detail = details[row.id];
    if (!detail) return "已提交";
    const work = (detail.work_items || []).filter((item) => !["cancelled", "declined"].includes(item.status));
    const replied = work.filter((item) => item.status === "done").length;
    if (work.length && replied === work.length) return "已回复";
    if (replied || work.some((item) => item.status === "in_progress")) return `处理中「已回复 ${replied}/${work.length}」`;
    if (!work.length && (detail.lines || []).length && (detail.lines || []).every((line) => line.ref_offer_id)) return "已回复";
    return "待处理";
  }, [details]);
  const filtered = useMemo(() => items.filter((item) => !stage || progress(item).startsWith(stage)), [items, stage, progress]);
  const exportIds = filtered.filter((item) => scope === "mine" && item.is_owner && item.kind === "sales_inquiry" && selected.includes(item.id)).map((item) => item.id);
  const columns: ColumnsType<Inquiry> = [
    { key: "inquiry_no", title: "单号 / 客户", sorter: true, render: (_, row) => <><Button type="link" onClick={() => navigate(`/presales/inquiries/${row.id}`)}>{row.inquiry_no}</Button>
      <div className="presales-muted">{row.customer_name}{row.kind === "proactive_offer" && <Tag>主动报价</Tag>}</div></> },
    { key: "pns", title: "PN（前 3 个）", render: (_, row) => (details[row.id]?.lines ?? []).slice(0, 3).map((line) => line.pn_raw).join(" · ") },
    { key: "line_count", title: "行数", width: 80, sorter: true, render: (_, row) => row.line_count },
    { key: "status", title: "进度 / 处理人", width: 180, sorter: true, render: (_, row) => <><Tag>{progress(row)}</Tag><div>{(details[row.id]?.work_items ?? []).map((work) => {
      const person = work.claimed_by ?? work.assignee_id;
      return <div className="presales-muted" key={work.id}>{person == null ? "公共池" : candidates.find((candidate) => candidate.id === person)?.display_name ?? `账号 #${person}`} · {work.paused ? "已暂停" : workStatuses[work.status]}</div>;
    })}</div></> },
    { key: "updated_at", title: "最近动态", width: 170, sorter: true, defaultSortOrder: "descend", render: (_, row) => new Date(row.updated_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) },
    { key: "actions", title: "操作", width: 170, render: (_, row) => <Space size={0} wrap><Button type="link" onClick={() => navigate(`/presales/inquiries/${row.id}`)}>打开</Button><Button type="link" onClick={() => setDetailId(row.id)}>报价概览</Button></Space> },
  ];
  const detail = detailId ? details[detailId] : undefined;
  const minimums = (detail?.lines ?? []).map((line) => lineOffers(detail, line).filter((offer) => isOfferActive(offer, now) && offer.is_tax_inclusive === true).map((offer) => Number(offer.unit_price)));
  return <section className="presales-page">
    <PageHeader title="询价单" subtitle="创建询价、跟进采购回复与查看报价" extra={<Space><Button onClick={() => setReload((value) => value + 1)}>刷新</Button>
      <Button onClick={() => setImportOpen(true)}>从 Excel 新建</Button><Button type="primary" onClick={() => navigate("/presales/inquiries/new")}>新建询价</Button></Space>} />
    <div className="presales-panel">
      <Tabs activeKey={scope} onChange={(value) => { setScope(value as typeof scope); setPage(1); }} items={[
        { key: "mine", label: "我的询价" }, { key: "shared", label: "共享给我" }, ...(supervisor ? [{ key: "all", label: "全部" }] : []),
      ]} />
      <div className="presales-toolbar">
        <Segmented value={stage} options={[{ value: "", label: "全部状态" }, ...["草稿", "待处理", "处理中", "已回复", "已结束"].map((value) => ({ value, label: value }))]}
          onChange={(value) => { setStage(value); setStatus(value === "草稿" ? "draft" : value === "已结束" ? "closed" : value ? "submitted" : undefined); setPage(1); }} />
        <Input aria-label="搜索单号、客户或 PN" placeholder="单号 / 客户 / PN / 描述" allowClear value={query} style={{ width: 240 }} onChange={(event) => setQuery(event.target.value)} />
        <Select aria-label="询价类型" allowClear placeholder="类型" style={{ width: 120 }} value={kind} options={[{ value: "sales_inquiry", label: "销售询价" }, { value: "proactive_offer", label: "主动报价" }]}
          onChange={(value) => { setKind(value); setPage(1); }} />
        <Select aria-label="询价状态" allowClear placeholder="单据状态" style={{ width: 125 }} value={status} options={Object.entries(statuses).map(([value, label]) => ({ value, label }))}
          onChange={(value) => { setStatus(value); setStage(""); setPage(1); }} />
        <DatePicker.RangePicker aria-label="最近动态日期" value={range} onChange={(value) => { setRange(value); setPage(1); }} />
      </div>
      <div className="presales-toolbar"><WorkbookExportButton label="合并下载" schemaName="presales_sales_summary" disabled={!exportIds.length || loading}
        fetchSchema={() => presalesWorkbooks.getSalesSummarySchema()} onExport={(visible) => presalesWorkbooks.exportSalesSummary(exportIds, visible)} filename="销售询价汇总.xlsx" />
        <span className="presales-muted">选择本人销售询价后按 PN 汇总下载。处理进度筛选当前页。</span></div>
      {error && <Alert type="error" message={error} action={<Button onClick={() => setReload((value) => value + 1)}>重试</Button>} />}
      {detailError && <Alert type="warning" message="部分处理人状态加载失败，请刷新后查看。" />}
      <Table aria-label="询价单列表" rowKey="id" columns={columns} dataSource={filtered} loading={loading} tableLayout="fixed"
        rowSelection={scope === "mine" ? { selectedRowKeys: selected, onChange: setSelected, getCheckboxProps: (row) => ({ "aria-label": `选择 ${row.inquiry_no}`, disabled: !row.is_owner || row.kind !== "sales_inquiry" }) } : undefined}
        pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, showTotal: () => `本页显示 ${filtered.length} / ${items.length} 张 · 范围内共 ${total} 张` }}
        onChange={(pagination, _filters, sorter) => {
          const value = Array.isArray(sorter) ? sorter[0] : sorter;
          setSort(value.order && value.columnKey ? { key: String(value.columnKey), dir: value.order === "ascend" ? "asc" : "desc" } : defaultSort);
          setPage(value.columnKey !== sort?.key || (value.order === "ascend" ? "asc" : "desc") !== sort?.dir ? 1 : pagination.current ?? 1);
        }} />
    </div>
    <Drawer title={detail ? `${detail.inquiry_no} · 报价概览` : "报价概览"} open={!!detailId} width={640} onClose={() => setDetailId(undefined)} destroyOnHidden>
      {detail && <><Table aria-label="PN 报价状态" rowKey="id" dataSource={detail.lines} tableLayout="fixed" pagination={{ pageSize: 10 }} columns={[
        { title: "PN / 描述", key: "pn", render: (_, line) => <>{line.pn_raw}<div className="presales-muted">{line.description}</div></> },
        { title: "数量", dataIndex: "qty", width: 70 },
        { title: "报价状态", key: "state", render: (_, line) => {
          const count = lineOffers(detail, line).filter((offer) => isOfferActive(offer, now)).length;
          const responses = (detail.line_responses ?? []).filter((response) => response.line_id === line.id);
          const skipped = responses.filter((response) => !response.is_draft && response.mode === "skip");
          return <>{responses.some((response) => response.is_draft) && <Tag>草稿（未回复）</Tag>}{count ? `${line.ref_offer_id ? "已引用 · " : ""}${count} 条有效报价` : skipped.length ? `不报 · ${skipped.map((response) => response.reason).join("；")}` : "待报价"}</>;
        } },
      ]} /><p>最低报价合计（含税）：{minimums.length && minimums.every((values) => values.length) ? moneyExact(minimums.reduce((sum, values, index) => sum + Math.min(...values) * Number(detail.lines![index].qty), 0)) : "—"}</p></>}
    </Drawer>
    {importOpen && <WorkbookDialog mode="inquiry" onClose={() => setImportOpen(false)} onApplied={(response) => {
      const id = (response as { inquiry_id?: number })?.inquiry_id;
      if (typeof id === "number" && id > 0) navigate(`/presales/inquiries/${id}/edit`);
      else throw new Error("草稿编号未返回");
    }} />}
  </section>;
}
