import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, App, AutoComplete, Button, Input, InputNumber, Modal, Popover, Select, Space, Spin, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Link } from "react-router-dom";
import { presales, type ExistingChoice, type Inquiry, type InquiryLine, type LineResponse, type LineResponseInput, type Offer, type OfferInput } from "../../api/presales";
import { claimWorkItem, listWorkItems, type WorkItem, type WorkTab } from "../../api/workItems";
import { suggestParts, type PartSuggestion } from "../../api/search";
import { refreshWorkBadges } from "../../components/workflow/useWorkBadge";
import PageHeader from "../../components/PageHeader";
import PartThumb from "../../components/gallery/PartThumb";
import CreatePartModal from "../../components/parts/CreatePartModal";
import { readPermissionMap } from "../../nav";
import { formatBusinessDateTime } from "../../utils/date";
import { moneyExact } from "../../utils/format";
import { existingOfferConflict, useExistingOfferChoice } from "./ExistingOfferDialog";
import { activeOfferGroups, flattenOffers, taxLabel, usePresalesError } from "./shared";
import { basisLabel, lineAnswer, ownedOfferForEdit, priceText } from "./answers";
import PartBinding from "./PartBinding";
import "./presales.css";

type RowState = "pending" | "draft" | "quoted" | "skip" | "unavailable";
type Filter = RowState | "all";

interface Source { task: WorkItem; inquiry: Inquiry; pool: boolean }
interface QuoteRow {
  key: string;
  source: Source;
  line: InquiryLine;
  own?: Offer;
  response?: LineResponse;
  displayOffer?: Offer;
  answerReason?: string;
  state: RowState;
}
type RowValue = Pick<OfferInput, "unit_price" | "is_tax_inclusive" | "price_basis" | "lead_time" | "qty_available" | "stock_location" | "notes">;

const TABS: WorkTab[] = ["assigned", "claimed", "pool", "done"];
const STATE_LABEL: Record<RowState, string> = { pending: "待报价", draft: "已填，待回复", quoted: "已报价", skip: "不报", unavailable: "已回复，当前无有效报价" };
const STATE_COLOR: Record<RowState, string | undefined> = { pending: "orange", draft: "blue", quoted: "green", skip: undefined, unavailable: "warning" };

async function mapLimit<T, R>(items: T[], limit: number, fn: (item: T) => Promise<R>): Promise<PromiseSettledResult<R>[]> {
  const results: PromiseSettledResult<R>[] = new Array(items.length);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) {
      const index = next++;
      try { results[index] = { status: "fulfilled", value: await fn(items[index]) }; }
      catch (reason) { results[index] = { status: "rejected", reason }; }
    }
  }));
  return results;
}

/** 待办接口按当前账号过滤；本人 = 已领取或被指派的处理人。 */
function actorOf(source: Source) {
  return source.pool ? undefined : source.task.assignee_id ?? source.task.claimed_by ?? undefined;
}

function liveTask(source: Source) {
  const live = source.inquiry.work_items?.find((item) => item.id === source.task.id);
  return { status: live?.status ?? source.task.status, paused: live?.paused ?? source.task.paused, version: live?.version ?? source.task.version };
}

function rowsOf(source: Source, skips: Record<string, string>): QuoteRow[] {
  const actor = actorOf(source);
  return (source.inquiry.lines ?? []).filter((line) => line.status !== "deleted" && !line.ref_offer_id).map((line) => {
    const key = `${source.inquiry.id}:${line.id}`;
    const response = actor == null ? undefined : source.inquiry.line_responses?.find((row) => row.line_id === line.id && row.created_by === actor);
    const answer = lineAnswer(source.inquiry, line);
    const own = ownedOfferForEdit(source.inquiry, line, actor);
    const handled = own?.status === "published" || (response && !response.is_draft);
    const state: RowState = skips[key] != null ? "skip" : own?.status === "draft" ? "draft"
      : handled ? answer.state : "pending";
    return { key, source, line, own, response, state, displayOffer: answer.state === "quoted" ? answer.offer : undefined,
      answerReason: answer.state === "skip" || answer.state === "unavailable" ? answer.reason : undefined };
  });
}

function canAct(source: Source) {
  const task = liveTask(source);
  return source.inquiry.status === "submitted" && !task.paused && (source.pool ? task.status === "pending" : ["pending", "in_progress"].includes(task.status));
}

function valueOf(offer?: Offer): RowValue {
  return { unit_price: offer?.unit_price ?? null, is_tax_inclusive: offer ? offer.is_tax_inclusive ?? null : true, price_basis: offer?.price_basis ?? "sale", lead_time: offer?.lead_time ?? "",
    qty_available: offer?.qty_available ?? null, stock_location: offer?.stock_location ?? "", notes: offer?.notes ?? "" };
}

function SkipButton({ disabled, onConfirm }: { disabled: boolean; onConfirm: (reason: string) => void }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("无货源");
  return <Popover trigger="click" open={open} onOpenChange={setOpen} content={<Space direction="vertical">
    <Input aria-label="不报原因" value={reason} onChange={(event) => setReason(event.target.value)} style={{ width: 220 }} />
    <Button type="primary" size="small" disabled={!reason.trim()} onClick={() => { setOpen(false); onConfirm(reason.trim()); }}>确定不报</Button>
  </Space>}>
    <Button size="small" disabled={disabled}>不报</Button>
  </Popover>;
}

export function DirectQuote({ onDone, onClose }: { onDone: (text: string) => void; onClose: () => void }) {
  const handleError = usePresalesError();
  const [text, setText] = useState("");
  const [options, setOptions] = useState<PartSuggestion[]>([]);
  const [part, setPart] = useState<PartSuggestion>();
  const [price, setPrice] = useState<number | null>(null);
  const [tax, setTax] = useState<boolean | null>(true);
  const [basis, setBasis] = useState<"sale" | "cost">("sale");
  const taxChanged = useRef(false);
  const basisChanged = useRef(false);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const canCreatePart = localStorage.getItem("role") === "admin" || readPermissionMap().page_master_data === true;
  useEffect(() => {
    const query = text.trim();
    if (!query || part?.pn_std === text) { setOptions([]); return; }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      suggestParts(query, 10, controller.signal).then((rows) => { if (!controller.signal.aborted) setOptions(rows); }).catch(() => undefined);
    }, 200);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [text, part]);
  const save = async () => {
    if (!part || price == null) return;
    setBusy(true);
    try {
      const input = { part_id: part.part_id, unit_price: price, is_tax_inclusive: tax, price_basis: basis, no_stock: false };
      let published: Offer;
      try { published = await presales.directOffer(input); }
      catch (error) {
        const existing = existingOfferConflict(error)?.existing_offers[0];
        if (!existing) throw error;
        // 首次读取既有报价的版本后更新一次；版本再次冲突则保留输入交由错误恢复处理。
        published = await presales.directOffer({ ...input,
          is_tax_inclusive: taxChanged.current ? tax : existing.is_tax_inclusive ?? null,
          price_basis: basisChanged.current ? basis : existing.price_basis ?? "sale",
          on_existing: "update", existing_offer_id: existing.id, expected_version: existing.version });
      }
      setTax(published.is_tax_inclusive ?? null); setBasis(published.price_basis ?? "sale");
      onDone(`${part.pn_std} 已更新为 ${priceText(published)}，保存成功。`);
    } catch (error) { await handleError(error); }
    finally { setBusy(false); }
  };
  return <div className="presales-direct">
    <p className="presales-wide presales-muted">主动报价直接更新你对这个型号的价，保存后按原有可见范围展示。</p>
    <label>PN<AutoComplete aria-label="主动报价 PN" value={text} placeholder="输入 PN 选择型号" style={{ width: "100%" }}
      onChange={(value) => { setText(value); if (part && value !== part.pn_std) { setPart(undefined); taxChanged.current = false; basisChanged.current = false; setTax(true); setBasis("sale"); } }}
      onSelect={(_, option) => { const picked = options.find((row) => String(row.part_id) === option.key); if (picked) { setPart(picked); setText(picked.pn_std); } }}
      options={options.map((row) => ({ key: String(row.part_id), value: row.pn_std, label: <span>{row.pn_std} <span className="presales-muted">{row.summary || row.description}</span></span> }))} /></label>
    <label>单价（元）<InputNumber aria-label="主动报价单价" min={0} precision={2} style={{ width: "100%" }} value={price} onChange={(value) => setPrice(value)} /></label>
    <label>税<Select aria-label="主动报价含税" value={tax == null ? "unknown" : tax ? "inc" : "ex"} onChange={(value) => { taxChanged.current = true; setTax(value === "unknown" ? null : value === "inc"); }} options={[{ value: "inc", label: "含税" }, { value: "ex", label: "不含税" }, ...(tax == null ? [{ value: "unknown", label: "未声明" }] : [])]} /></label>
    <label>价格性质<Select aria-label="主动报价价格性质" value={basis} onChange={(value) => { basisChanged.current = true; setBasis(value); }} options={[{ value: "sale", label: "售价" }, { value: "cost", label: "成本价" }]} /></label>
    <Space><Button type="primary" loading={busy} disabled={!part || price == null} onClick={() => void save()}>保存</Button><Button onClick={onClose}>收起</Button></Space>
    {text && !part && <span className="presales-wide presales-muted">请从下拉中选择已建档的型号{canCreatePart && <Button type="link" disabled={busy} onClick={() => setCreating(true)}>新建 PN 档案</Button>}</span>}
    {creating && <CreatePartModal compact pn={text.trim()} onClose={() => setCreating(false)} onSelected={(selected) => { setPart(selected); setText(selected.pn_std); }} />}
  </div>;
}

export default function QuotePage() {
  const { message } = App.useApp();
  const handleError = usePresalesError();
  const { choose, dialog } = useExistingOfferChoice();
  const [sources, setSources] = useState<Source[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [refreshRequired, setRefreshRequired] = useState(false);
  const [library, setLibrary] = useState<Record<number, Offer[]>>({});
  const [filter, setFilter] = useState<Filter>("pending");
  const [query, setQuery] = useState("");
  const [values, setValues] = useState<Record<string, RowValue>>({});
  const [editing, setEditing] = useState<Record<string, boolean>>({});
  const [skips, setSkips] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string>();
  const [direct, setDirect] = useState(false);
  const [toast, setToast] = useState("");
  const [binding, setBinding] = useState<QuoteRow>();
  const generation = useRef(0);
  const hasChanges = Object.keys(values).length > 0 || Object.keys(skips).length > 0;
  useEffect(() => {
    if (!hasChanges) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [hasChanges]);

  const load = useCallback(async () => {
    const current = ++generation.current;
    setLoading(true); setLoadError(false);
    try {
      const pages = await Promise.all(TABS.map(async (tab) => {
        const items: WorkItem[] = [];
        for (let page = 1; ; page += 1) {
          const result = await listWorkItems({ tab, subject_type: "presales_inquiry", page, page_size: 100 });
          items.push(...result.items);
          if (page * result.page_size >= result.total) break;
        }
        return items.map((task) => ({ task, pool: tab === "pool" }));
      }));
      const seen = new Set<number>();
      const tasks = pages.flat().filter(({ task }) => {
        if (seen.has(task.subject_id) || (task.status !== "done" && !["pending", "in_progress"].includes(task.status))) return false;
        seen.add(task.subject_id); return true;
      });
      const loaded = await mapLimit(tasks, 6, async ({ task, pool }) => ({ task, pool, inquiry: await presales.getInquiry(task.subject_id) }));
      if (current !== generation.current) return;
      const partial = loaded.some((result) => result.status === "rejected");
      setLoadError(partial);
      setRefreshRequired(partial);
      setSources(loaded.flatMap((result) => result.status === "fulfilled" ? [result.value] : []));
    } catch (error) {
      if (current === generation.current) { setLoadError(true); await handleError(error); }
    } finally { if (current === generation.current) setLoading(false); }
  }, [handleError]);
  useEffect(() => { void load(); }, [load]);

  const partKey = [...new Set(sources.flatMap((source) => (source.inquiry.lines ?? []).flatMap((line) => line.part_id ? [line.part_id] : [])))].sort((a, b) => a - b).join(",");
  useEffect(() => {
    let cancelled = false;
    if (!partKey) { setLibrary({}); return; }
    presales.library(partKey.split(",").map(Number)).then((data) => { if (!cancelled) setLibrary(data); }).catch(() => { if (!cancelled) setLibrary({}); });
    return () => { cancelled = true; };
  }, [partKey, sources]);

  // D-0047：紧急单在前，其次被催办的单（催得多的在前），同档保持待办原有顺序。
  const rank = (source: Source) => (source.inquiry.priority === "urgent" ? 2 : 0) + (source.inquiry.urge_count ? 1 : 0);
  const allRows = useMemo(() => [...sources].map((source, index) => ({ source, index }))
    .sort((a, b) => rank(b.source) - rank(a.source) || (b.source.inquiry.urge_count ?? 0) - (a.source.inquiry.urge_count ?? 0) || a.index - b.index)
    .flatMap(({ source }) => rowsOf(source, skips)), [sources, skips]);
  const counts = allRows.reduce((result, row) => ({ ...result, [row.state]: result[row.state] + 1 }), { pending: 0, draft: 0, quoted: 0, skip: 0, unavailable: 0 } as Record<RowState, number>);
  const keyword = query.trim().toUpperCase();
  const rows = allRows.filter((row) => (filter === "all" || editing[row.key] || (filter === "pending" ? ["pending", "draft"].includes(row.state) : row.state === filter))
    && (!keyword || [row.source.inquiry.created_by_name, row.line.pn_raw, row.source.inquiry.customer_name, row.source.inquiry.inquiry_no, row.line.description]
      .some((text) => (text ?? "").toUpperCase().includes(keyword))));

  const replace = (inquiry: Inquiry, task?: WorkItem) => setSources((previous) => previous.map((source) => source.inquiry.id === inquiry.id
    ? { ...source, inquiry, ...(task ? { task, pool: false } : {}) } : source));
  const valueFor = (row: QuoteRow) => values[row.key] ?? valueOf(row.own);
  const change = (row: QuoteRow, patch: Partial<RowValue>) => setValues((previous) => ({ ...previous, [row.key]: { ...valueFor(row), ...patch } }));
  const done = (row: QuoteRow) => {
    setValues((previous) => { const next = { ...previous }; delete next[row.key]; return next; });
    setEditing((previous) => { const next = { ...previous }; delete next[row.key]; return next; });
  };

  /** 公共池的单先领取，报价接口要求本人未暂停的待办。 */
  const ensureTask = async (source: Source): Promise<Source> => {
    if (!source.pool) return source;
    const task = await claimWorkItem(source.task.id, { expected_version: liveTask(source).version });
    setRefreshRequired(true);
    const inquiry = await presales.getInquiry(source.inquiry.id);
    setRefreshRequired(false);
    const claimed = { task, inquiry, pool: false };
    replace(inquiry, task);
    refreshWorkBadges();
    return claimed;
  };

  /** 整单每行都已报价或不报时自动回复销售（现行后端以整单回复完成待办）。 */
  const replyIfComplete = async (source: Source, localSkips: Record<string, string>) => {
    const inquiry = await presales.getInquiry(source.inquiry.id);
    const latest = { ...source, inquiry };
    replace(inquiry);
    const lines = rowsOf(latest, localSkips);
    const left = lines.filter((row) => row.state === "pending" || (row.state === "draft" && !row.line.part_id));
    if (left.length || !lines.some((row) => row.state === "draft" || localSkips[row.key] != null) || liveTask(latest).status === "done") return left.length;
    const lineResponses = lines.flatMap((row): LineResponseInput[] => localSkips[row.key] != null
      ? [{ line_id: row.line.id, mode: "skip", reason: localSkips[row.key] }]
      : row.state === "draft" || row.own?.status === "published" ? [{ line_id: row.line.id, mode: "quote" }] : []);
    const choices: Record<number, ExistingChoice> = {};
    for (;;) {
      try {
        await presales.replyInquiry(inquiry.id, { expected_version: inquiry.version, work_item_id: source.task.id,
          expected_work_item_version: liveTask(latest).version, line_responses: lineResponses,
          ...(Object.keys(choices).length ? { existing_choices: choices } : {}) });
        break;
      } catch (error) {
        const conflict = existingOfferConflict(error);
        if (!conflict) throw error;
        const choice = await choose(conflict);
        if (!choice) return -1;
        choices[conflict.draft_offer_id] = choice;
      }
    }
    setRefreshRequired(true);
    setSkips((previous) => Object.fromEntries(Object.entries(previous).filter(([key]) => !key.startsWith(`${inquiry.id}:`))));
    replace(await presales.getInquiry(inquiry.id));
    setRefreshRequired(false);
    refreshWorkBadges();
    message.success(`${inquiry.inquiry_no} 已全部答复，已回复给 ${inquiry.created_by_name ?? "销售"}`);
    return 0;
  };

  const afterChange = async (source: Source, localSkips: Record<string, string>) => {
    const left = await replyIfComplete(source, localSkips);
    if (left > 0) message.success(`已保存；${source.inquiry.inquiry_no} 还有 ${left} 行未填，填完自动回复给销售`);
  };

  const save = async (row: QuoteRow) => {
    if (refreshRequired || loadError || busy) return;
    const value = valueFor(row);
    if (value.unit_price == null || value.unit_price === "") return;
    setBusy(row.key);
    try {
      if (row.own?.status === "published") {
        await presales.updateOffer(row.own.id, { ...value, no_stock: false, expected_version: row.own.version });
        setRefreshRequired(true);
        replace(await presales.getInquiry(row.source.inquiry.id));
        setRefreshRequired(false);
        done(row);
        message.success(`${row.line.pn_raw} 报价已更新，销售那边同步看到新价`);
        return;
      }
      const source = await ensureTask(row.source);
      const own = rowsOf(source, skips).find((item) => item.key === row.key)?.own;
      await presales.saveOffer(source.inquiry.id, row.line.id, { ...value, no_stock: false,
        expected_version: own ? own.version : source.inquiry.version, ...(own ? { offer_id: own.id } : {}), work_item_id: source.task.id });
      setRefreshRequired(true);
      await afterChange(source, skips);
      setRefreshRequired(false);
      done(row);
    } catch (error) { await handleError(error, load); }
    finally { setBusy(undefined); }
  };

  const skip = async (row: QuoteRow, reason: string) => {
    if (refreshRequired || loadError || busy) return;
    setBusy(row.key);
    const next = { ...skips, [row.key]: reason };
    try {
      const source = await ensureTask(row.source);
      setSkips(next);
      done(row);
      await afterChange(source, next);
    } catch (error) { await handleError(error, load); }
    finally { setBusy(undefined); }
  };

  const siblingOf = (row: QuoteRow) => {
    if (!row.line.part_id || row.state === "quoted" || row.state === "skip") return undefined;
    return activeOfferGroups(library[row.line.part_id] ?? [], Date.now()).flatMap((group) => flattenOffers([group]))
      .filter((offer) => offer.line_id !== row.line.id)
      .sort((a, b) => Date.parse(b.valid_from ?? "") - Date.parse(a.valid_from ?? ""))[0];
  };

  // D-0048：本人已发布报价的更新独立于询价和待办状态。
  const canUpdate = (row: QuoteRow) => row.own?.status === "published";
  const editable = (row: QuoteRow) => !refreshRequired && !loadError && ((canAct(row.source) && ["pending", "draft"].includes(row.state) && skips[row.key] == null) || (!!editing[row.key] && canUpdate(row)));
  const columns: ColumnsType<QuoteRow> = [
    { title: "询价人", key: "sales", width: 160, render: (_, row) => <div className="presales-cell">
      <b className="presales-sales">{row.source.inquiry.created_by_name ?? "业务人员"}</b>
      <Link className="presales-muted" to={`/presales/workbench/${row.source.inquiry.id}`} state={{ workItem: row.source.task }}>
        {row.source.inquiry.inquiry_no}{row.source.inquiry.customer_name ? ` · ${row.source.inquiry.customer_name}` : ""}</Link>
      <Space size={4} wrap>
        {row.source.inquiry.priority === "urgent" && <Tag color="red">紧急</Tag>}
        {!!row.source.inquiry.urge_count && <Tag color="volcano" title={`最近 ${formatBusinessDateTime(row.source.inquiry.last_urged_at)}`}>已催 {row.source.inquiry.urge_count} 次</Tag>}
        {row.source.pool && <Tag>公共池</Tag>}
      </Space>
    </div> },
    { title: "PN / 描述", key: "pn", render: (_, row) => <div className="presales-cell">
      <Space>{row.line.part_id && <PartThumb partId={row.line.part_id} pn={row.line.pn_raw} size={32} />}<b>{row.line.pn_raw}</b></Space>
      <span className="presales-muted presales-ellipsis" title={row.line.description ?? undefined}>{row.line.description || "—"}</span>
      {!row.line.part_id && <Space size={4}><Tag color="warning">待建档</Tag>{canAct(row.source) && <Button type="link" size="small" onClick={() => setBinding(row)}>建档</Button>}</Space>}
    </div> },
    { title: "数量", key: "qty", width: 70, render: (_, row) => Number(row.line.qty) },
    { title: "PN 一致", key: "exact", width: 84, render: (_, row) => row.line.require_exact_pn ? "要求一致" : "—" },
    { title: "坏件", key: "return", width: 72, render: (_, row) => row.line.return_defective === false ? <Tag>不返回</Tag> : "返回" },
    { title: "单价（元）", key: "price", width: 140, render: (_, row) => editable(row)
      ? <InputNumber aria-label={`${row.line.pn_raw} 单价`} min={0} max={999999999999.99} precision={2} style={{ width: "100%" }} placeholder="单价"
        value={valueFor(row).unit_price == null || valueFor(row).unit_price === "" ? null : Number(valueFor(row).unit_price)}
        onChange={(value) => change(row, { unit_price: value })} onPressEnter={() => void save(row)} />
      : <b>{row.state === "quoted" ? moneyExact(Number(row.displayOffer?.unit_price)) : "—"}</b> },
    { title: "税", key: "tax", width: 104, render: (_, row) => editable(row)
      ? <Select aria-label={`${row.line.pn_raw} 含税`} style={{ width: "100%" }} value={valueFor(row).is_tax_inclusive == null ? "unknown" : valueFor(row).is_tax_inclusive ? "inc" : "ex"}
        onChange={(value) => change(row, { is_tax_inclusive: value === "unknown" ? null : value === "inc" })} options={[{ value: "inc", label: "含税" }, { value: "ex", label: "不含税" }, ...(valueFor(row).is_tax_inclusive == null ? [{ value: "unknown", label: "未声明" }] : [])]} />
      : row.state === "quoted" ? taxLabel(row.displayOffer?.is_tax_inclusive) : "—" },
    { title: "价格性质", key: "basis", width: 100, render: (_, row) => editable(row)
      ? <Select aria-label={`${row.line.pn_raw} 价格性质`} style={{ width: "100%" }} value={valueFor(row).price_basis ?? "sale"}
        onChange={(value) => change(row, { price_basis: value })} options={[{ value: "sale", label: "售价" }, { value: "cost", label: "成本价" }]} />
      : row.state === "quoted" ? basisLabel(row.displayOffer ?? {}) : "—" },
    { title: "状态", key: "state", width: 130, render: (_, row) => {
      const offer = row.state === "quoted" ? row.displayOffer : undefined;
      return <div className="presales-cell">
        <Tag color={editing[row.key] ? "processing" : STATE_COLOR[row.state]}>{editing[row.key] ? "修改中" : STATE_LABEL[row.state]}</Tag>
        {offer && <span className="presales-muted">{offer.reporter?.name ?? "我"} {formatBusinessDateTime(offer.valid_from)}</span>}
        {row.state === "skip" && <span className="presales-muted">{skips[row.key] ?? row.answerReason ?? row.response?.reason}{skips[row.key] != null && "（随整单回复）"}</span>}
      </div>;
    } },
    { title: "操作", key: "actions", width: 180, render: (_, row) => {
      if (editable(row)) return <Space size={6} wrap>
        <Button type="primary" size="small" loading={busy === row.key} disabled={!!busy || valueFor(row).unit_price == null || !row.line.part_id}
          onClick={() => void save(row)}>保存</Button>
        {!editing[row.key] && <SkipButton disabled={!!busy} onConfirm={(reason) => void skip(row, reason)} />}
        {editing[row.key] && <Button size="small" onClick={() => done(row)}>取消</Button>}
      </Space>;
      if (canUpdate(row)) return <Button type="link" disabled={refreshRequired || loadError} onClick={() => setEditing((previous) => ({ ...previous, [row.key]: true }))}>更新报价</Button>;
      if (skips[row.key] != null) return <Button type="link" onClick={() => setSkips((previous) => { const next = { ...previous }; delete next[row.key]; return next; })}>改为报价</Button>;
      return null;
    } },
  ];

  return <section className="presales-page">
    <PageHeader title="报价" subtitle="逐行直接填价；整单填完自动回复给销售。报价一直有效，直到你更新" extra={<Space>
      <Link to="/presales/workbench">Excel 批量报价</Link>
      <Button onClick={() => { setDirect(!direct); setToast(""); }}>主动报价</Button>
    </Space>} />
    <div className="presales-panel presales-stack">
      <div className="presales-toolbar">
        {([["pending", `待报价 ${counts.pending + counts.draft}`], ["quoted", `已报价 ${counts.quoted}`], ["skip", `不报 ${counts.skip}`], ["unavailable", `暂无有效价 ${counts.unavailable}`], ["all", `全部 ${allRows.length}`]] as [Filter, string][])
          .map(([key, label]) => <Button key={key} aria-pressed={filter === key} type={filter === key ? "primary" : "default"} ghost={filter === key} onClick={() => setFilter(key)}>{label}</Button>)}
        <Input.Search aria-label="筛选报价行" allowClear placeholder="按询价人、PN、客户筛选" style={{ width: 260, marginLeft: "auto" }} value={query} onChange={(event) => setQuery(event.target.value)} />
        <Button onClick={() => void load()} disabled={loading}>刷新</Button>
      </div>
      {direct && <DirectQuote onClose={() => setDirect(false)} onDone={(text) => { setDirect(false); setToast(text); void load(); }} />}
      {toast && <Alert type="success" showIcon closable message={toast} onClose={() => setToast("")} />}
      {loadError && <Alert type="error" message="报价行加载失败" action={<Button onClick={() => void load()}>重试</Button>} />}
      {refreshRequired && !loadError && <Alert type="warning" message="保存已成功，状态刷新尚未完成。填写内容已保留，请先刷新后再操作。" action={<Button onClick={() => void load()}>刷新最新状态</Button>} />}
      {Object.keys(skips).length > 0 && <Typography.Text type="secondary">标为不报的行会在整单填完时一起回复给销售；离开本页前请填完该单其余行。</Typography.Text>}
      <Spin spinning={loading}>
        <Table aria-label="报价行" rowKey="key" size="middle" dataSource={rows} columns={columns} tableLayout="fixed" scroll={{ x: 1280 }}
          rowClassName={(row) => editable(row) ? "presales-row-editing" : ""}
          pagination={{ defaultPageSize: 50, showSizeChanger: true, hideOnSinglePage: true }}
          locale={{ emptyText: loading ? "加载中" : "没有符合条件的报价行" }}
          expandable={{
            expandedRowRender: (row) => {
              const value = valueFor(row);
              const on = editable(row);
              const sibling = siblingOf(row);
              return <div className="presales-stack">
                {sibling && <div className="presales-hint">同型号已有 {priceText(sibling)}（{sibling.reporter?.name ?? "其他采购"} · {formatBusinessDateTime(sibling.valid_from)}）
                  {on && <Button type="link" size="small" onClick={() => change(row, { unit_price: sibling.unit_price ?? null, is_tax_inclusive: sibling.is_tax_inclusive ?? null, price_basis: sibling.price_basis ?? "sale" })}>沿用</Button>}</div>}
                <div className="presales-more">
                  <label>交期<Input aria-label={`${row.line.pn_raw} 交期`} disabled={!on} maxLength={256} value={value.lead_time ?? ""} onChange={(event) => change(row, { lead_time: event.target.value })} /></label>
                  <label>可供数量<InputNumber aria-label={`${row.line.pn_raw} 可供数量`} disabled={!on} min={0} precision={3} style={{ width: "100%" }}
                    value={value.qty_available == null || value.qty_available === "" ? null : Number(value.qty_available)} onChange={(next) => change(row, { qty_available: next })} /></label>
                  <label>货物所在地<Input aria-label={`${row.line.pn_raw} 货物所在地`} disabled={!on} maxLength={256} value={value.stock_location ?? ""} onChange={(event) => change(row, { stock_location: event.target.value })} /></label>
                  <label className="presales-wide">采购备注<Input aria-label={`${row.line.pn_raw} 采购备注`} disabled={!on} value={value.notes ?? ""} onChange={(event) => change(row, { notes: event.target.value })} /></label>
                </div>
                {(row.line.notes || row.line.expected_delivery) && <span className="presales-muted">销售要求：{[row.line.expected_delivery && `期望交期 ${row.line.expected_delivery}`, row.line.notes].filter(Boolean).join("；")}</span>}
              </div>;
            },
          }} />
      </Spin>
    </div>
    {dialog}
    <Modal title={binding ? `${binding.line.pn_raw} · 建档` : "建档"} open={!!binding} footer={null} width={560} onCancel={() => setBinding(undefined)} destroyOnHidden>
      {binding && <PartBinding compact line={binding.line} refresh={load} onBound={async (inquiry) => {
        setBinding(undefined);
        try { replace(await presales.getInquiry(inquiry.id)); } catch { replace(inquiry); }
      }} />}
    </Modal>
  </section>;
}
