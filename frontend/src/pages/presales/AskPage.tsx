import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, App, AutoComplete, Button, Checkbox, Collapse, Input, InputNumber, Pagination, Popover, Radio, Select, Space, Spin, Table, Tag } from "antd";
import { DeleteOutlined } from "@ant-design/icons";
import { Link } from "react-router-dom";
import { presales, type Inquiry, type InquiryLine, type InquiryPriority, type Offer } from "../../api/presales";
import type { AudienceRule } from "../../api/audience";
import { listWorkCandidates, type WorkCandidate } from "../../api/workItems";
import { suggestParts, type PartSuggestion } from "../../api/search";
import AudiencePicker, { PUBLIC_AUDIENCE } from "../../components/audience/AudiencePicker";
import PageHeader from "../../components/PageHeader";
import { readPermissionMap } from "../../nav";
import { formatBusinessDateTime } from "../../utils/date";
import CandidateSelect from "./CandidateSelect";
import { answerSentence, basisLabel, lineAnswer, priceText, requiredLines } from "./answers";
import { LatestPriceTag, latestActiveOffer, usePresalesError } from "./shared";
import "./presales.css";

const EXACT_KEY = "presales:exact-pn";
const PAGE_SIZE = 10;

interface Row { key: number; pn: string; part?: PartSuggestion; latest?: Offer; qty: number; exact: boolean; returnDefective: boolean; note: string }

function readExact() {
  try { return localStorage.getItem(EXACT_KEY) !== "0"; } catch { return true; }
}
function writeExact(value: boolean) {
  try { localStorage.setItem(EXACT_KEY, value ? "1" : "0"); } catch { /* 偏好不可写不影响填写 */ }
}
const normalize = (text: string) => text.trim().toUpperCase();

/** PN 输入：联想已建档型号；未选中时按 PN 精确匹配自动带出描述；粘贴多行拆成多行。 */
function PnInput({ row, onChange, onPasteMany }: { row: Row; onChange: (patch: Partial<Row>) => void; onPasteMany: (pns: string[]) => void }) {
  const [options, setOptions] = useState<PartSuggestion[]>([]);
  const [latest, setLatest] = useState<Record<number, Offer | undefined>>({});
  useEffect(() => {
    const query = row.pn.trim();
    if (!query || row.part?.pn_std === row.pn) { setOptions([]); return; }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      suggestParts(query, 8, controller.signal).then(async (items) => {
        if (controller.signal.aborted) return;
        setOptions(items);
        if (!items.length) return;
        // 候选一次批量读报价库（已按受众过滤），每个 PN 只标最新一条有效价。
        // 精确匹配要等价格回来再回写本行：先回写会触发本 effect 清理，取消价格请求。
        const library = await presales.library(items.map((item) => item.part_id)).catch(() => ({} as Record<number, Offer[]>));
        if (controller.signal.aborted) return;
        const prices = Object.fromEntries(items.map((item) => [item.part_id, latestActiveOffer(library[item.part_id] ?? [])]));
        setLatest(prices);
        const exact = items.find((item) => normalize(item.pn_std) === normalize(query));
        if (exact && !row.part) onChange({ part: exact, latest: prices[exact.part_id] });
      }).catch(() => undefined);
    }, 250);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [row.pn, row.part]);
  return <AutoComplete aria-label={`第 ${row.key} 行 PN`} value={row.pn} placeholder="输入或粘贴 PN" style={{ width: "100%" }} popupMatchSelectWidth={480}
    onChange={(value) => onChange({ pn: value, ...(row.part && normalize(value) !== normalize(row.part.pn_std) ? { part: undefined, latest: undefined } : {}) })}
    onSelect={(_, option) => { const part = options.find((item) => String(item.part_id) === option.key); if (part) onChange({ pn: part.pn_std, part, latest: latest[part.part_id] }); }}
    options={options.map((item) => ({ key: String(item.part_id), value: item.pn_std, label: <span className="presales-option">
      <b>{item.pn_std}</b><span className="presales-muted">{item.summary || item.description}</span><LatestPriceTag offer={latest[item.part_id]} />
    </span> }))}>
    <Input onPaste={(event) => {
      const parts = event.clipboardData.getData("text").split(/[\n\r\t,，;；]+/).map((text) => text.trim()).filter(Boolean);
      if (parts.length > 1) { event.preventDefault(); onPasteMany(parts); }
    }} />
  </AutoComplete>;
}

function NewInquiry({ onCreated }: { onCreated: () => void }) {
  const { message } = App.useApp();
  const handleError = usePresalesError();
  const customerVisible = localStorage.getItem("role") === "admin" || readPermissionMap().data_customer !== false;
  const nextKey = useRef(2);
  const [exactPref, setExactPref] = useState(readExact);
  const blank = (key: number): Row => ({ key, pn: "", qty: 1, exact: exactPref, returnDefective: true, note: "" });
  const [rows, setRows] = useState<Row[]>([blank(1)]);
  const [customerId, setCustomerId] = useState<number>();
  const [priority, setPriority] = useState<InquiryPriority>("normal");
  const [notes, setNotes] = useState("");
  const [delivery, setDelivery] = useState("");
  const [assigneeIds, setAssigneeIds] = useState<number[]>([]);
  const [audience, setAudience] = useState<AudienceRule>(PUBLIC_AUDIENCE);
  const [candidates, setCandidates] = useState<WorkCandidate[]>([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    listWorkCandidates("presales_inquiry", controller.signal).then((data) => { if (!controller.signal.aborted) setCandidates(data.items); }).catch(() => undefined);
    return () => controller.abort();
  }, []);
  const update = (key: number, patch: Partial<Row>) => {
    if (patch.exact != null) { setExactPref(patch.exact); writeExact(patch.exact); }
    setRows((previous) => previous.map((row) => row.key === key ? { ...row, ...patch } : row));
  };
  const pasteMany = (key: number, pns: string[]) => setRows((previous) => {
    const index = previous.findIndex((row) => row.key === key);
    const added = pns.map((pn) => ({ ...blank(nextKey.current++), pn }));
    return [...previous.slice(0, index), { ...previous[index], pn: pns[0], part: undefined }, ...added.slice(1), ...previous.slice(index + 1)];
  });
  const filled = rows.filter((row) => row.pn.trim());
  const save = async (submit: boolean) => {
    if (!filled.length) { void message.warning("请至少填写一行 PN"); return; }
    if (filled.some((row) => !(row.qty > 0))) { void message.warning("数量必须大于 0"); return; }
    setBusy(true);
    try {
      const created = await presales.createInquiry({ kind: "sales_inquiry", ...(customerVisible ? { customer_id: customerId ?? null } : {}),
        notes, assignee_ids: assigneeIds, offer_audience: audience, priority,
        lines: filled.map((row) => ({ part_id: row.part?.part_id ?? null, pn_raw: row.part?.pn_std ?? row.pn.trim(),
          description: row.part ? row.part.summary || row.part.description || null : null, qty: row.qty, require_exact_pn: row.exact,
          return_defective: row.returnDefective,
          notes: row.note.trim() || null, expected_delivery: delivery || null })) });
      if (submit) await presales.submitInquiry(created.id, { expected_version: created.version });
      void message.success(submit ? `询价 ${created.inquiry_no} 已提交，进入采购报价` : `草稿 ${created.inquiry_no} 已保存`);
      setRows([blank(nextKey.current++)]); setCustomerId(undefined); setNotes(""); setDelivery(""); setAssigneeIds([]); setAudience(PUBLIC_AUDIENCE); setPriority("normal");
      onCreated();
    } catch (error) { await handleError(error); }
    finally { setBusy(false); }
  };
  return <div className="presales-panel presales-stack">
    <div className="presales-toolbar">
      <h2 className="presales-h2">新建询价</h2>
      {customerVisible && <span className="presales-inline-field">客户<CandidateSelect label="客户" load={presales.customers} value={customerId} onChange={setCustomerId} disabled={busy} /></span>}
      <span className="presales-inline-field presales-inline-auto">优先级<Radio.Group aria-label="优先级" optionType="button" size="small" value={priority}
        onChange={(event) => setPriority(event.target.value)} options={[{ value: "normal", label: "普通" }, { value: "urgent", label: "紧急" }]} /></span>
      <span className="presales-muted" style={{ marginLeft: "auto" }}>数量默认 1；「PN 一致」按你上次的习惯预选</span>
    </div>
    <Table aria-label="新建询价行" rowKey="key" size="small" pagination={false} dataSource={rows} tableLayout="fixed" scroll={{ x: 900 }} columns={[
      { title: "#", key: "no", width: 44, render: (_, __, index) => <span className="presales-muted">{index + 1}</span> },
      { title: "PN / 型号", key: "pn", width: 240, render: (_, row) => <PnInput row={row} onChange={(patch) => update(row.key, patch)} onPasteMany={(pns) => pasteMany(row.key, pns)} /> },
      { title: "描述（自动带出）", key: "desc", render: (_, row) => <span className="presales-option">
        <span className="presales-muted" title={row.part?.description ?? undefined}>
          {row.part ? row.part.summary || row.part.description || row.part.pn_std : row.pn.trim() ? "未建档 · 提交后由采购补全" : ""}</span>
        {row.part && <LatestPriceTag offer={row.latest} />}</span> },
      { title: "数量", key: "qty", width: 96, render: (_, row) => <InputNumber aria-label={`第 ${row.key} 行数量`} min={0.001} max={99999999999.999} style={{ width: "100%" }}
        value={row.qty} onChange={(qty) => update(row.key, { qty: qty ?? 0 })} /> },
      { title: "PN 一致", key: "exact", width: 80, align: "center", render: (_, row) => <Checkbox aria-label={`第 ${row.key} 行要求 PN 一致`} checked={row.exact} onChange={(event) => update(row.key, { exact: event.target.checked })} /> },
      { title: "坏件返回", key: "return", width: 84, align: "center", render: (_, row) => <Checkbox aria-label={`第 ${row.key} 行坏件返回`} checked={row.returnDefective} onChange={(event) => update(row.key, { returnDefective: event.target.checked })} /> },
      { title: "备注", key: "note", width: 200, render: (_, row) => <Input aria-label={`第 ${row.key} 行备注`} placeholder="可选" value={row.note} onChange={(event) => update(row.key, { note: event.target.value })} /> },
      { title: "", key: "remove", width: 48, render: (_, row) => <Button type="text" danger aria-label={`删除第 ${row.key} 行`} icon={<DeleteOutlined />}
        disabled={rows.length === 1} onClick={() => setRows((previous) => previous.filter((other) => other.key !== row.key))} /> },
    ]} />
    <Collapse ghost size="small" items={[{ key: "more", label: "更多选项（很少用，默认收起）", children: <div className="presales-more">
      <label>期望交期（全部行）<Input type="date" aria-label="期望交期" value={delivery} onChange={(event) => setDelivery(event.target.value)} /></label>
      <label>指定处理人（留空进公共池）<Select aria-label="处理人" mode="multiple" value={assigneeIds} onChange={setAssigneeIds}
        options={candidates.map((candidate) => ({ value: candidate.id, label: candidate.display_name }))} /></label>
      <label>报价可见范围<Popover trigger="click" content={<AudiencePicker value={audience} onChange={setAudience} disabled={busy} />}>
        <Button aria-label="设置报价可见范围">{({ public: "公开（全体销售）", private: "私密", include: "部分可见", exclude: "不给谁看" })[audience.mode]}</Button>
      </Popover></label>
      <label className="presales-wide">客户要求<Input.TextArea aria-label="客户要求" autoSize={{ minRows: 1, maxRows: 4 }} value={notes} onChange={(event) => setNotes(event.target.value)} /></label>
    </div> }]} />
    <div className="presales-toolbar">
      <Button onClick={() => setRows((previous) => [...previous, blank(nextKey.current++)])}>+ 添加一行</Button>
      <span className="presales-muted">{filled.length} 行 · 可从 Excel 直接粘贴多行 PN</span>
      <Space style={{ marginLeft: "auto" }}>
        <Button loading={busy} onClick={() => void save(false)}>存草稿</Button>
        <Button type="primary" loading={busy} onClick={() => void save(true)}>提交询价</Button>
      </Space>
    </div>
  </div>;
}

function statusChip(inquiry: Inquiry, lines: InquiryLine[]) {
  if (inquiry.status === "draft") return <Tag>草稿</Tag>;
  if (inquiry.status === "revising") return <Tag color="warning">修改中</Tag>;
  if (inquiry.status === "closed") return <Tag>{({ won: "已成交", lost: "未成交", cancelled: "已取消" })[inquiry.closed_outcome ?? "cancelled"]}</Tag>;
  const answered = lines.filter((line) => lineAnswer(inquiry, line).state !== "pending").length;
  if (!answered) return <Tag color="orange">待报价</Tag>;
  return answered < lines.length ? <Tag color="blue">已答复 {answered}/{lines.length}</Tag> : <Tag color="green">全部已答复</Tag>;
}

function AnswerLine({ inquiry, line }: { inquiry: Inquiry; line: InquiryLine }) {
  const [copied, setCopied] = useState(false);
  const answer = lineAnswer(inquiry, line);
  const sentence = answerSentence(line, answer);
  return <div className="presales-answer">
    <span className="presales-answer-pn">{line.pn_raw} <span className="presales-muted">× {Number(line.qty)}</span></span>
    <b className={`presales-say is-${answer.state}`}>{answer.state === "quoted" ? priceText(answer.offer) : answer.state === "skip" ? `不报：${answer.reason}` : "待报价"}</b>
    {answer.state === "quoted" && <Tag color={answer.offer.price_basis === "cost" ? "purple" : undefined}>{basisLabel(answer.offer)}</Tag>}
    {!line.return_defective && <Tag>坏件不返回</Tag>}
    <span className="presales-muted">{answer.state === "quoted" ? `${answer.reporter} · ${formatBusinessDateTime(answer.at)}` : answer.state === "skip" ? answer.reporter
      : inquiry.status === "submitted" ? "已进采购报价" : ""}</span>
    {answer.state === "quoted" && <Button type="link" size="small" style={{ marginLeft: "auto" }} aria-label={`复制 ${line.pn_raw} 报价给客户`}
      onClick={() => { void navigator.clipboard?.writeText(sentence).catch(() => undefined); setCopied(true); }}>{copied ? "已复制" : "复制给客户"}</Button>}
  </div>;
}

/** D-0047：发起人催未回复的询价，30 分钟限一次（服务端校验，过早时提示还需等待）。 */
function UrgeButton({ inquiry, onUrged }: { inquiry: Inquiry; onUrged: (inquiry: Inquiry) => void }) {
  const { message } = App.useApp();
  const handleError = usePresalesError();
  const [busy, setBusy] = useState(false);
  return <Button size="small" danger loading={busy} onClick={async () => {
    setBusy(true);
    try {
      const urged = await presales.urgeInquiry(inquiry.id);
      void message.success(`已催办 ${inquiry.inquiry_no}，采购报价页会置顶标红`);
      onUrged(urged);
    } catch (error) { await handleError(error); }
    finally { setBusy(false); }
  }}>催办</Button>;
}

function MyInquiries({ refreshKey }: { refreshKey: number }) {
  const handleError = usePresalesError();
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<Inquiry[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const generation = useRef(0);
  const load = useCallback(async () => {
    const current = ++generation.current;
    setLoading(true); setFailed(false);
    try {
      const result = await presales.listInquiries({ kind: "sales_inquiry", scope: "mine", page, page_size: PAGE_SIZE, sort: "updated_at", dir: "desc", ...(query.trim() ? { q: query.trim() } : {}) });
      const details = await Promise.allSettled(result.items.map((item) => presales.getInquiry(item.id)));
      if (current !== generation.current) return;
      setItems(details.map((detail, index) => detail.status === "fulfilled" ? detail.value : result.items[index]));
      setTotal(result.total);
    } catch (error) {
      if (current === generation.current) { setFailed(true); await handleError(error); }
    } finally { if (current === generation.current) setLoading(false); }
  }, [page, query, handleError]);
  useEffect(() => { void load(); }, [load, refreshKey]);
  return <div className="presales-panel presales-stack">
    <div className="presales-toolbar">
      <h2 className="presales-h2">我的询价</h2>
      <span className="presales-muted">每行一句可以直接说给客户的话；报价一直有效，直到采购更新</span>
      <Input.Search aria-label="搜索我的询价" allowClear placeholder="单号 / PN / 客户" style={{ width: 220, marginLeft: "auto" }}
        onSearch={(value) => { setPage(1); setQuery(value); }} />
      <Link to="/presales/inquiries">全部询价单</Link>
    </div>
    {failed && <Alert type="error" message="询价加载失败" action={<Button onClick={() => void load()}>重试</Button>} />}
    <Spin spinning={loading}>
      {!items.length && !loading ? <div className="presales-empty">还没有询价</div> : items.map((inquiry) => {
        const lines = requiredLines(inquiry);
        return <div key={inquiry.id} className="presales-inquiry-card">
          <div className="presales-toolbar presales-tight">
            <Link to={`/presales/inquiries/${inquiry.id}`}><b>{inquiry.inquiry_no}</b></Link>
            {inquiry.customer_name && <span>{inquiry.customer_name}</span>}
            {inquiry.priority === "urgent" && <Tag color="red">紧急</Tag>}
            {statusChip(inquiry, lines)}
            <span className="presales-muted">{formatBusinessDateTime(inquiry.submitted_at ?? inquiry.updated_at)}</span>
            {!!inquiry.urge_count && <span className="presales-muted">已催 {inquiry.urge_count} 次 · 最近 {formatBusinessDateTime(inquiry.last_urged_at)}</span>}
            {["draft", "revising"].includes(inquiry.status) && <Link style={{ marginLeft: "auto" }} to={`/presales/inquiries/${inquiry.id}/edit`}>继续编辑</Link>}
            {inquiry.status === "submitted" && lines.some((line) => lineAnswer(inquiry, line).state === "pending") && <span style={{ marginLeft: "auto" }}>
              <UrgeButton inquiry={inquiry} onUrged={(urged) => setItems((previous) => previous.map((item) => item.id === urged.id ? urged : item))} /></span>}
          </div>
          {lines.length ? lines.map((line) => <AnswerLine key={line.id} inquiry={inquiry} line={line} />)
            : <span className="presales-muted">{inquiry.line_count} 行</span>}
        </div>;
      })}
    </Spin>
    {total > PAGE_SIZE && <Pagination size="small" current={page} pageSize={PAGE_SIZE} total={total} onChange={setPage} showSizeChanger={false} />}
  </div>;
}

export default function AskPage() {
  const [refreshKey, setRefreshKey] = useState(0);
  return <section className="presales-page presales-stack">
    <PageHeader title="询价" subtitle="直接填写型号和数量提交；采购报价后在下方「我的询价」看到价格" />
    <NewInquiry onCreated={() => setRefreshKey((value) => value + 1)} />
    <MyInquiries refreshKey={refreshKey} />
  </section>;
}
