import { useCallback, useEffect, useMemo, useRef, useState, type Key } from "react";
import { useNavigate } from "react-router-dom";
import { Alert, App, Button, DatePicker, Input, Pagination, Select, Tabs, Tag } from "antd";
import type { Dayjs } from "dayjs";
import { presales, type Inquiry, type InquiryLine, type Offer } from "../../api/presales";
import { listWorkCandidates, type WorkCandidate } from "../../api/workItems";
import { presalesWorkbooks } from "../../api/presalesWorkbooks";
import { WorkbookExportButton } from "../../components/workbook";
import WorkbookDialog from "./WorkbookDialog";
import { readPermissionMap } from "../../nav";
import DataGrid, { type DataGridColumn, type DataGridSort } from "../../components/datagrid/DataGrid";
import { isOfferActive, useNow } from "./shared";
import { moneyExact } from "../../utils/format";
import "./presales.css";

const statuses: Record<string, string> = { draft: "草稿", submitted: "已提交", revising: "撤回修改中", closed: "已结束" };
const workStatuses: Record<string, string> = { pending: "待处理", in_progress: "处理中", done: "已回复", declined: "已退回", cancelled: "已取消" };
const defaultSort: DataGridSort = { key: "updated_at", dir: "desc" };
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
  const [items, setItems] = useState<Inquiry[]>([]);
  const [details, setDetails] = useState<Record<number, Inquiry>>({});
  const [candidates, setCandidates] = useState<WorkCandidate[]>([]);
  const [selected, setSelected] = useState<Key[]>([]);
  const [importOpen, setImportOpen] = useState(false);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [sort, setSort] = useState<DataGridSort | null>(defaultSort);
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
  const onSortChange = useCallback((value: DataGridSort | null) => { setSort(value); setPage(1); }, []);
  const columns: DataGridColumn<Inquiry>[] = [
    { key: "inquiry_no", title: "单号", width: 180, identity: true, type: "code", value: (r) => r.inquiry_no,
      render: (r) => <><button className="presales-cell-link presales-mono" onClick={() => navigate(`/presales/inquiries/${r.id}`)}>{r.inquiry_no}</button>{r.kind === "proactive_offer" && <span className="presales-pill presales-pill-purple">主动报价</span>}</> },
    { key: "customer", title: "客户", width: 180, sortable: false, value: (r) => r.customer_name },
    { key: "pns", title: "PN（前 3 个）", width: 240, sortable: false, value: (r) => {
      const lines = details[r.id]?.lines || [];
      return lines.slice(0, 3).map((l) => l.pn_raw).join(" · ") + (lines.length > 3 ? ` +${lines.length - 3}` : "");
    } },
    { key: "line_count", title: "行数", width: 64, type: "number", align: "right", value: (r) => r.line_count },
    { key: "status", title: "进度", width: 160, type: "rank", value: (r) => r.status, rank: ["draft", "revising", "submitted", "closed"],
      render: (r) => <Tag title="排序按单据阶段：草稿、撤回修改、已提交、已结束">{progress(r)}</Tag> },
    { key: "handlers", title: "处理人状态", width: 240, sortable: false, render: (row) => <span>{(details[row.id]?.work_items || []).map((item) => {
      const userId = item.claimed_by ?? item.assignee_id;
      const person = candidates.find((candidate) => candidate.id === userId);
      return <Tag key={item.id}>{userId != null ? `${person?.display_name || `账号 #${userId}`} · ` : item.assignee_id === null && item.claimed_by === null ? "公共池 · " : ""}
        {item.paused ? "已暂停" : workStatuses[item.status] || item.status}</Tag>;
    })}</span> },
    { key: "amount", title: "最低报价合计（含税）", width: 165, hidden: true, sortable: false, render: (r) => {
      const detail = details[r.id]; const lines = detail?.lines || [];
      const values = lines.map((line) => lineOffers(detail, line).filter((o) => isOfferActive(o, now) && o.is_tax_inclusive === true).map((o) => Number(o.unit_price)));
      return lines.length && values.every((v) => v.length) ? moneyExact(values.reduce((sum, v, i) => sum + Math.min(...v) * Number(lines[i].qty), 0)) : "—";
    } },
    { key: "updated_at", title: "最近动态", width: 170, type: "time", value: (r) => r.updated_at,
      render: (r) => new Date(r.updated_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) },
  ];
  return <section className="presales-page presales-inquiry-list">
    <div className="presales-heading"><h1>询价单</h1>
      <Tabs activeKey={scope} onChange={(value) => { setScope(value as typeof scope); setPage(1); }} items={[
        { key: "mine", label: "我的询价" }, { key: "shared", label: "共享给我" }, ...(supervisor ? [{ key: "all", label: "全部" }] : []),
      ]} /><span className="presales-spacer" />
      <Input size="small" className="presales-search" aria-label="搜索单号、客户或 PN" placeholder="单号 / 客户 / PN / 描述" allowClear value={query} onChange={(e) => setQuery(e.target.value)} />
      <Button size="small" onClick={() => setReload((v) => v + 1)}>刷新</Button>
      <Button size="small" onClick={() => setImportOpen(true)}>从 Excel 新建</Button>
      <Button size="small" type="primary" onClick={() => navigate("/presales/inquiries/new")}>新建询价</Button>
    </div>
    <div className="presales-filters">
      {["", "草稿", "待处理", "处理中", "已回复", "已结束"].map((name) => <Button size="small" shape="round" key={name} type={stage === name ? "primary" : "default"}
        onClick={() => { setStage(name); setStatus(name === "草稿" ? "draft" : name === "已结束" ? "closed" : name ? "submitted" : undefined); setPage(1); }}>{name || "全部状态"}</Button>)}
      <Select size="small" aria-label="询价类型" allowClear placeholder="类型" style={{ width: 110 }} value={kind}
        options={[{ value: "sales_inquiry", label: "销售询价" }, { value: "proactive_offer", label: "主动报价" }]} onChange={(value) => { setKind(value); setPage(1); }} />
      <Select size="small" aria-label="询价状态" allowClear placeholder="单据状态" style={{ width: 115 }} value={status}
        options={Object.entries(statuses).map(([value, label]) => ({ value, label }))} onChange={(value) => { setStatus(value); setStage(""); setPage(1); }} />
      <DatePicker.RangePicker size="small" aria-label="最近动态日期" value={range} onChange={(value) => { setRange(value); setPage(1); }} />
    </div>
    <div className="presales-filters"><WorkbookExportButton size="small" label="合并下载" schemaName="presales_sales_summary"
      disabled={!exportIds.length || loading} fetchSchema={() => presalesWorkbooks.getSalesSummarySchema()}
      onExport={(visible) => presalesWorkbooks.exportSalesSummary(exportIds, visible)} filename="销售询价汇总.xlsx" />
      <span className="presales-filter-note">在「我的询价」选择销售询价后按 PN 汇总下载。处理进度筛选当前页；搜索、日期和排序由服务端处理。</span></div>
    {detailError && <Alert type="warning" message="部分处理人状态加载失败，请刷新后查看。" />}
    <DataGrid id="presales-inquiries" rows={filtered} columns={columns} rowKey={(r) => r.id} loading={loading} error={error}
      selectable={scope === "mine"} selectedRowKeys={selected} onSelectedRowKeysChange={setSelected} isRowSelectable={(row) => !!row.is_owner && row.kind === "sales_inquiry"}
      onRetry={() => setReload((v) => v + 1)} defaultSort={defaultSort} serverSort onSortChange={onSortChange} ariaLabel="询价单列表"
      renderExpanded={(row) => <DataGrid id="presales-inquiry-summary" rows={details[row.id]?.lines || []} rowKey={(l) => l.id} ariaLabel="PN 报价状态" columns={[
        { key: "pn", title: "PN", identity: true, type: "code", width: 180, value: (l) => l.pn_raw },
        { key: "description", title: "描述", type: "zh", width: 350, value: (l) => l.description },
        { key: "qty", title: "数量", type: "number", width: 70, value: (l) => l.qty },
        { key: "quote", title: "报价状态", width: 170, render: (l) => {
          const count = lineOffers(details[row.id], l).filter((o) => isOfferActive(o, now)).length;
          const skipped = (details[row.id]?.line_responses || []).filter((response) => !response.is_draft && response.line_id === l.id && response.mode === "skip");
          const drafts = (details[row.id]?.line_responses || []).filter((response) => response.line_id === l.id && response.is_draft);
          return <>{drafts.length > 0 && <Tag>草稿（未回复）</Tag>}{count ? <span className="presales-pill presales-pill-success">{l.ref_offer_id ? "已引用 · " : ""}{count} 条有效报价</span>
            : skipped.length ? <span className="presales-pill presales-pill-warning" title={skipped.map((response) => response.reason).filter(Boolean).join("；")}>不报 · {skipped.map((response) => response.reason).filter(Boolean).join("；")}</span>
              : <span className="presales-pill presales-pill-warning">待报价</span>}</>;
        } },
      ]} />} />
    <div className="presales-list-footer"><span>本页显示 {filtered.length} / {items.length} 张 · 范围内共 {total} 张</span>
      <Pagination size="small" current={page} pageSize={20} total={total} showSizeChanger={false} onChange={setPage} /></div>
    {importOpen && <WorkbookDialog mode="inquiry" onClose={() => setImportOpen(false)} onApplied={(response) => {
      const id = (response as { inquiry_id?: number })?.inquiry_id;
      if (typeof id === "number" && id > 0) navigate(`/presales/inquiries/${id}/edit`);
      else throw new Error("草稿编号未返回");
    }} />}
  </section>;
}
