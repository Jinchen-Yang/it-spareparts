import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Alert, App, Button, Descriptions, Empty, Form, Modal, Radio, Select, Space, Spin, Tag, Typography } from "antd";
import { presales, type Inquiry, type InquiryLine, type Offer, type OfferRevision } from "../../api/presales";
import PartThumb from "../../components/gallery/PartThumb";
import GalleryPicker from "../../components/gallery/GalleryPicker";
import Timeline from "../../components/workflow/Timeline";
import DataGrid, { type DataGridColumn } from "../../components/datagrid/DataGrid";
import "./sales-pages.css";
import { formatBusinessDateTime } from "../../utils/date";
import { formatMoney } from "../../utils/format";
import { usePresalesError, isOfferActive, OfferBadge, taxLabel, useNow, OfferUpdates } from "./shared";

const statusLabels: Record<string, string> = { draft: "草稿", submitted: "已提交", revising: "撤回修改中", closed: "已结束" };
const outcomeLabels: Record<string, string> = { won: "成交", lost: "未成交", cancelled: "取消" };
const workLabels: Record<string, string> = { pending: "待处理", in_progress: "处理中", done: "已回复", declined: "已退回", cancelled: "已取消" };

function hasValue(value: unknown): boolean {
  return value !== undefined && value !== null && value !== "";
}

function displayValue(value: unknown): ReactNode {
  if (typeof value === "boolean") return value ? "是" : "否";
  return hasValue(value) ? String(value) : null;
}

function FieldValues({ values }: { values?: Inquiry["field_values"] }) {
  if (!values?.length) return null;
  return <Descriptions size="small" column={1} items={values.filter((field) => hasValue(field.value)).map((field) => ({
    key: String(field.field_def_id), label: field.name, children: displayValue(field.value),
  }))} />;
}

function OfferPrice({ offer }: { offer: Offer }) {
  if (offer.no_stock) return <Tag>无货</Tag>;
  return hasValue(offer.unit_price) ? <>{formatMoney(offer.unit_price, { prefix: "¥" })} · {taxLabel(offer.is_tax_inclusive)}</> : null;
}

/** 引用、沿用按本行钉住快照显示，绝不补查当前报价覆盖旧价格。 */
function offersForLine(line: InquiryLine, inquiry: Inquiry): Offer[] {
  const pinned = new Map([...(line.linked_offers ?? []), ...(line.ref_offer ? [line.ref_offer] : []),
    ...(inquiry.line_responses ?? []).flatMap((response) => response.line_id === line.id && response.mode === "reuse" && response.offer ? [response.offer] : [])]
    .map((offer) => [offer.id, offer]));
  return [...pinned.values(), ...(inquiry.offers ?? []).filter((offer) => !pinned.has(offer.id))].filter((offer) =>
    (offer.line_id === line.id || offer.id === line.ref_offer_id || pinned.has(offer.id) || line.linked_offer_ids?.includes(offer.id))
    && offer.status !== "withdrawn" && !offer.original_withdrawn);
}

function OfferDetails({ line, offers, onHistory }: { line: InquiryLine; offers: Offer[]; onHistory: (offer: Offer) => void }) {
  const columns: DataGridColumn<Offer>[] = [
    { key: "offer", title: "报价", width: 140, type: "code", identity: true, value: (offer) => offer.id,
      render: (offer) => <Button size="small" type="link" aria-label={`查看报价 ${offer.id} 历史`} onClick={() => onHistory(offer)}>报价 #{offer.id} 历史</Button> },
    { key: "price", title: "单价 / 口径", width: 180, type: "money", value: (offer) => offer.unit_price, render: (offer) => <OfferPrice offer={offer} /> },
    ...([{ key: "qty_available", title: "可供数量", type: "number" }, { key: "stock_location", title: "货物所在地", type: "zh" },
      { key: "lead_time", title: "交期", type: "zh" }, { key: "supplier_id", title: "供应商", type: "code" },
      { key: "source_key", title: "货源", type: "zh" }, { key: "notes", title: "采购备注", type: "zh" }] as const)
      .filter((field) => offers.some((offer) => hasValue(offer[field.key])))
      .map((field) => ({ ...field, width: 150, value: (offer: Offer) => offer[field.key] })),
    { key: "valid_until", title: "有效截止（上海）", width: 180, type: "time", value: (offer) => offer.valid_until,
      render: (offer) => hasValue(offer.valid_until) ? formatBusinessDateTime(offer.valid_until) : "—" },
  ];
  return <DataGrid id="presales-comparison-offers" ariaLabel={`${line.pn_raw} 报价详情`} rows={offers} columns={columns} rowKey={(offer) => offer.id}
    renderExpanded={(offer) => <div className="sales-full-description"><OfferBadge offer={offer} />{hasValue(offer.notes) && <p>采购备注：{offer.notes}</p>}</div>} />;
}

const historyFields: { key: keyof Offer; label: string }[] = [
  { key: "unit_price", label: "单价" }, { key: "is_tax_inclusive", label: "含税口径" },
  { key: "no_stock", label: "无货" }, { key: "qty_available", label: "可供数量" },
  { key: "stock_location", label: "货物所在地" }, { key: "lead_time", label: "交期" },
  { key: "supplier_id", label: "供应商" }, { key: "source_key", label: "货源" },
  { key: "notes", label: "采购备注" }, { key: "valid_from", label: "生效时间" }, { key: "valid_until", label: "有效截止" },
];

export default function InquiryDetailPage() {
  const { id = "" } = useParams();
  return <InquiryDetailContent key={id} id={Number(id)} />;
}

function InquiryDetailContent({ id }: { id: number }) {
  const handlePresalesError = usePresalesError();
  const navigate = useNavigate();
  const { message } = App.useApp();
  const [inquiry, setInquiry] = useState<Inquiry>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [galleryOpen, setGalleryOpen] = useState(false);
  const [galleryLineId, setGalleryLineId] = useState<number>();
  const [timelineOpen, setTimelineOpen] = useState(true);
  const [historyOffer, setHistoryOffer] = useState<Offer>();
  const [history, setHistory] = useState<OfferRevision[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [closeOpen, setCloseOpen] = useState(false);
  const [outcome, setOutcome] = useState<"won" | "lost" | "cancelled">("won");
  const [wonOfferId, setWonOfferId] = useState<number>();
  const generation = useRef(0);
  const historyGeneration = useRef(0);
  const now = useNow([...(inquiry?.offers ?? []), ...(inquiry?.lines ?? []).flatMap((line) => [...(line.linked_offers ?? []), ...(line.ref_offer ? [line.ref_offer] : [])]),
    ...(inquiry?.line_responses ?? []).flatMap((row) => row.offer ? [row.offer] : [])]
    .flatMap((offer) => [offer.valid_from, offer.valid_until]));

  const load = useCallback(async () => {
    const current = ++generation.current;
    setLoading(true); setError("");
    try {
      if (!Number.isInteger(id) || id <= 0) throw new Error("询价单编号无效");
      const data = await presales.getInquiry(id);
      if (current === generation.current) setInquiry(data);
    } catch (error) {
      if (current === generation.current) setError("询价单加载失败，请重试");
    } finally { if (current === generation.current) setLoading(false); }
  }, [id]);

  useEffect(() => {
    void load();
    return () => { ++generation.current; ++historyGeneration.current; };
  }, [load]);

  const showHistory = async (offer: Offer) => {
    const current = ++historyGeneration.current;
    setHistoryOffer(offer); setHistory([]); setHistoryLoading(true); setHistoryError("");
    try {
      const rows = await presales.revisions(offer.id);
      if (current === historyGeneration.current) setHistory(rows);
    } catch {
      if (current === historyGeneration.current) setHistoryError("报价历史加载失败，可能无权查看，请重试");
    } finally { if (current === historyGeneration.current) setHistoryLoading(false); }
  };

  const withdraw = async () => {
    if (!inquiry || busy) return;
    setBusy(true);
    try {
      await presales.withdrawInquiry(id, { expected_version: inquiry.version });
      navigate(`/presales/inquiries/${id}/edit`);
    } catch (error) { await handlePresalesError(error, load); }
    finally { setBusy(false); }
  };

  const copy = async () => {
    if (!inquiry || busy) return;
    setBusy(true);
    try {
      const copied = await presales.copyInquiry(id, { expected_version: inquiry.version });
      message.success("已复制为新草稿");
      navigate(copied.kind === "proactive_offer" ? `/presales/offers/new?draft=${copied.id}` : `/presales/inquiries/${copied.id}/edit`);
    } catch (error) { await handlePresalesError(error, load); }
    finally { setBusy(false); }
  };

  const close = async () => {
    if (!inquiry || busy) return;
    setBusy(true);
    try {
      const data = await presales.closeInquiry(id, { expected_version: inquiry.version, outcome,
        ...(outcome === "won" && wonOfferId ? { won_offer_id: wonOfferId } : {}) });
      setInquiry(data); setCloseOpen(false); message.success("已关单");
    } catch (error) { await handlePresalesError(error, load); }
    finally { setBusy(false); }
  };

  if (!inquiry) return <>{loading && <Spin aria-label="询价单加载中" />}{error && <Alert type="error" message={error}
    action={<Button onClick={() => void load()}>重试</Button>} />}</>;

  const lines = inquiry.lines ?? [];
  const offers = inquiry.offers ?? [];
  const canAmend = inquiry.kind === "sales_inquiry";
  const adopted = inquiry.won_offer;
  const lineForOffer = (offer: Offer) => lines.find((line) => line.id === offer.line_id || line.ref_offer_id === offer.id
    || line.linked_offer_ids?.includes(offer.id) || line.linked_offers?.some((linked) => linked.id === offer.id) || inquiry.line_responses?.some((row) => row.line_id === line.id && row.mode === "reuse" && row.ref_offer_id === offer.id));
  const closeOptions = [...new Map([...offers, ...lines.flatMap((line) => [...(line.linked_offers ?? []), ...(line.ref_offer ? [line.ref_offer] : [])]),
    ...(inquiry.line_responses ?? []).flatMap((row) => row.mode === "reuse" && row.offer ? [row.offer] : [])]
    .map((offer) => [offer.id, offer])).values()].filter((offer) => isOfferActive(offer, now) && !!lineForOffer(offer));
  const galleryItems = lines.filter((line) => line.part_id).map((line) => ({ partId: line.part_id!, pn: line.pn_raw }));

  const lineOffers = new Map(lines.map((line) => [line.id, offersForLine(line, inquiry)]));
  const referenceIds = (line: InquiryLine) => new Set([...(line.linked_offer_ids ?? []), ...(line.linked_offers ?? []).map((offer) => offer.id),
    ...(line.ref_offer_id ? [line.ref_offer_id] : []), ...(inquiry.line_responses ?? []).filter((response) => response.line_id === line.id && response.mode === "reuse")
      .flatMap((response) => response.ref_offer_id ? [response.ref_offer_id] : [])]);
  const reporters = new Map<string, string>();
  lines.forEach((line) => (lineOffers.get(line.id) ?? []).filter((offer) => !referenceIds(line).has(offer.id)).forEach((offer) =>
    reporters.set(offer.reporter ? `person:${offer.reporter.id}` : `offer:${offer.id}`, offer.reporter?.name || `报价 #${offer.id}`)));
  (inquiry.work_items ?? []).forEach((work) => {
    const person = work.claimed_by ?? work.assignee_id;
    if (person && !reporters.has(`person:${person}`)) reporters.set(`person:${person}`, `处理人 ${person}`);
  });
  const canClose = ["submitted", "revising"].includes(inquiry.status);
  const renderOffer = (line: InquiryLine, offer: Offer) => {
    const active = isOfferActive(offer, now);
    const candidates = (lineOffers.get(line.id) ?? []).filter((candidate) => isOfferActive(candidate, now) && !candidate.no_stock
      && hasValue(candidate.unit_price) && candidate.is_tax_inclusive === offer.is_tax_inclusive);
    const lowest = active && !offer.no_stock && typeof offer.is_tax_inclusive === "boolean" && hasValue(offer.unit_price)
      && Number(offer.unit_price) === Math.min(...candidates.map((candidate) => Number(candidate.unit_price)));
    const detail = [offer.reporter?.name || `报价 #${offer.id}`, offer.stock_location, offer.lead_time,
      offer.valid_until ? `有效至 ${formatBusinessDateTime(offer.valid_until)}` : ""].filter(Boolean).join(" · ");
    return <span key={offer.id} className={`sales-quote-cell ${offer.status === "stale" ? "sales-stale" : ""}`}>
      <button type="button" aria-label={`采纳 ${line.pn_raw} 报价 ${offer.id}`} aria-pressed={wonOfferId === offer.id}
        className={`sales-quote-choice ${wonOfferId === offer.id ? "is-selected" : lowest ? "is-lowest" : ""}`}
        disabled={!canClose || busy || !active || !!offer.no_stock} title={detail}
        onClick={() => setWonOfferId(wonOfferId === offer.id ? undefined : offer.id)}><OfferPrice offer={offer} />
        {lowest && <span className="sales-lowest">{taxLabel(offer.is_tax_inclusive)}最低价</span>}
        {offer.status === "stale" && <span>基于旧版本</span>}
        {offer.status === "draft" && <span>报价草稿</span>}
        {offer.status === "published" && !offer.no_stock && !active && <span>已失效</span>}
      </button>
      <span className="sales-quote-updates"><OfferUpdates offer={offer} /></span>
      <Button size="small" type="link" aria-label={`查看报价 ${offer.id} 历史`} onClick={() => void showHistory(offer)}>历史</Button>
    </span>;
  };
  const columns: DataGridColumn<InquiryLine>[] = [
    { key: "pn", title: "PN", width: 170, identity: true, type: "code", value: (line) => line.pn_raw,
      render: (line) => <span className="sales-inline">{line.part_id && <PartThumb partId={line.part_id} pn={line.pn_raw} size={24} />}
        <span className="sales-mono">{line.pn_raw}</span>{line.require_exact_pn && <span title="要求 PN 一致" className="sales-muted">一致</span>}</span> },
    { key: "description", title: "描述", width: 200, type: "zh", value: (line) => line.description },
    { key: "qty", title: "数量", width: 64, type: "number", value: (line) => line.qty },
    ...[...reporters].map(([key, title]): DataGridColumn<InquiryLine> => ({ key, title, width: 230, render: (line) => {
      const quoted = (lineOffers.get(line.id) ?? []).filter((offer) => !referenceIds(line).has(offer.id)
        && (offer.reporter ? `person:${offer.reporter.id}` : `offer:${offer.id}`) === key);
      if (quoted.length) return <span className="sales-inline">{quoted.map((offer) => renderOffer(line, offer))}</span>;
      const person = key.startsWith("person:") ? Number(key.slice(7)) : undefined;
      const response = inquiry.line_responses?.find((item) => item.line_id === line.id && item.created_by === person);
      if (response?.mode === "skip") return <span title={response.reason}>不报：{response.reason}</span>;
      if (response?.mode === "reuse") return <span>沿用报价 #{response.ref_offer_id}</span>;
      const work = inquiry.work_items?.find((item) => (item.claimed_by ?? item.assignee_id) === person);
      return work ? workLabels[work.status] || work.status : "—";
    } })),
    { key: "referenced", title: "引用 / 报价库", width: 260, render: (line) => <span className="sales-inline">{(lineOffers.get(line.id) ?? [])
      .filter((offer) => referenceIds(line).has(offer.id)).map((offer) => <span key={offer.id} className="sales-inline">
        {offer.reporter && <span>{offer.reporter.name}</span>}{renderOffer(line, offer)}</span>)}</span> },
    { key: "gallery", title: "操作", width: 120, render: (line) => line.part_id && <Button size="small"
      onClick={() => { setGalleryLineId(line.id); setGalleryOpen(true); }}>图库多选下载</Button> },
  ];

  return <div className="sales-page sales-detail">
    <header className="sales-heading"><Button size="small" onClick={() => navigate("/presales/inquiries")}>返回列表</Button>
      <h1 className="sales-mono">询价单 {inquiry.inquiry_no}</h1>
      <Tag>{inquiry.kind === "sales_inquiry" ? "销售询价" : "主动报价"}</Tag><Tag>{statusLabels[inquiry.status] || inquiry.status}</Tag>
      <span className="sales-muted">{lines.length} 行 · 版本 {inquiry.revision}</span><div className="sales-spacer" />
      {inquiry.status === "closed" ? <Button size="small" loading={busy} onClick={() => void copy()}>复制为新单</Button> : <>
        {inquiry.kind === "proactive_offer" && inquiry.status === "draft" && <Button size="small" disabled={busy}
          onClick={() => navigate(`/presales/offers/new?draft=${id}`)}>继续填写主动报价</Button>}
        {inquiry.kind === "proactive_offer" && inquiry.status === "submitted" && <Button size="small" disabled={busy}
          onClick={() => navigate(`/presales/tasks/${id}`)}>更新 / 撤销报价</Button>}
        {canAmend && ["draft", "revising"].includes(inquiry.status) && <Button size="small" disabled={busy} onClick={() => navigate(`/presales/inquiries/${id}/edit`)}>编辑询价</Button>}
        {canAmend && inquiry.status === "submitted" && <><Button size="small" loading={busy} onClick={() => void withdraw()}>撤回修改</Button>
          <Button size="small" disabled={busy} onClick={() => navigate(`/presales/inquiries/${id}/edit?append=1`)}>追加行</Button></>}
      </>}
      <Button size="small" onClick={() => setTimelineOpen(!timelineOpen)}>{timelineOpen ? "隐藏时间线" : "显示时间线"}</Button>
    </header>
    {error && <Alert type="error" message={error} action={<Button onClick={() => void load()}>重试</Button>} />}
    <section className="sales-detail-summary" aria-label="单头信息">
      {hasValue(inquiry.customer_id) && <span>客户：{inquiry.customer_id}</span>}
      {hasValue(inquiry.notes) && <span title={inquiry.notes || undefined} className="sales-summary-notes">客户要求：{inquiry.notes}</span>}
      <span className="sales-muted" title={`创建时间：${formatBusinessDateTime(inquiry.created_at)}`}>最近动态 {formatBusinessDateTime(inquiry.updated_at)}</span>
      {inquiry.closed_outcome && <span>关单结果：{outcomeLabels[inquiry.closed_outcome] || inquiry.closed_outcome}</span>}
      {adopted && <Space wrap><Button type="link" onClick={() => void showHistory(adopted)}>报价 #{adopted.id} · 第 {adopted.revision_no} 版</Button><OfferBadge offer={adopted} /></Space>}
      <FieldValues values={inquiry.field_values} />
    </section>
    <div className="sales-work-status">{inquiry.work_items?.map((work) => <span className={`sales-pill ${work.status === "done" ? "sales-success" : ""}`} key={work.id}>
      {hasValue(work.claimed_by) || hasValue(work.assignee_id) ? `处理人 ${work.claimed_by ?? work.assignee_id} · ` : "公共池 · "}
      {workLabels[work.status] || work.status}{work.paused ? " · 已暂停" : ""}</span>)}
      <span className="sales-muted">最低价按同一含税口径比较，仅计入有效报价。</span></div>
    <div className={`sales-detail-layout ${timelineOpen ? "with-timeline" : ""}`}>
      <main className="sales-matrix"><DataGrid id="presales-comparison-matrix" ariaLabel="询价比价矩阵" rows={lines} columns={columns}
        rowKey={(line) => line.id} loading={loading} emptyText="暂无询价行"
        renderExpanded={(line) => <div className="sales-detail-expanded">
          <div className="sales-full-description">{line.description}</div>
          {hasValue(line.notes) && <p>行要求：{line.notes}</p>}
          <FieldValues values={line.field_values} />
          {(inquiry.line_responses ?? []).filter((response) => response.line_id === line.id && response.mode !== "quote").map((response) => <div key={`${response.work_item_id}:${line.id}`}>
            {response.mode === "skip" ? <p>无货 / 不报：{response.reason}</p> : <Space wrap><span>沿用报价 #{response.ref_offer_id}</span>
              {response.offer ? <><span>原报价人：{response.reporter_label ?? response.offer.reporter?.name ?? "其他采购"}</span>
                {response.offer.valid_from && <span>原报价时间：{formatBusinessDateTime(response.offer.valid_from)}</span>}</>
                : <Tag>{response.expired ? "已失效" : "当前不可见"}</Tag>}</Space>}
          </div>)}
          <OfferDetails line={line} offers={lineOffers.get(line.id) ?? []} onHistory={(offer) => void showHistory(offer)} />
        </div>} /></main>
      {timelineOpen && <aside className="sales-timeline" aria-label="时间线"><h2>时间线</h2><Timeline key={`${id}:${inquiry.version}`} subjectType="presales_inquiry" subjectId={id} /></aside>}
    </div>
    <footer className="sales-footer"><span>{wonOfferId ? `已选择报价 #${wonOfferId}` : "点报价格子选择成交采纳报价"}</span><div className="sales-spacer" />
      {canClose && <><Button disabled={busy} onClick={() => { setOutcome("lost"); setCloseOpen(true); }}>未成交关单</Button>
        <Button type="primary" aria-label="关单" disabled={busy} onClick={() => { setOutcome("won"); setCloseOpen(true); }}>成交关单</Button></>}
    </footer>
    {galleryOpen && <GalleryPicker items={galleryLineId ? galleryItems.filter((item) => lines.find((line) => line.id === galleryLineId)?.part_id === item.partId) : galleryItems}
      mode="modal" onClose={() => setGalleryOpen(false)} />}
    <Modal open={!!historyOffer} title={historyOffer ? `报价 #${historyOffer.id} 历史` : "报价历史"} footer={null} width={760}
      onCancel={() => { ++historyGeneration.current; setHistoryOffer(undefined); }}>
      {historyLoading && <Spin aria-label="报价历史加载中" />}
      {historyError && <Alert type="error" message={historyError} action={<Button onClick={() => historyOffer && void showHistory(historyOffer)}>重试</Button>} />}
      <DataGrid id="presales-offer-history" ariaLabel="报价历史" rows={history} rowKey={(revision) => revision.revision_no} loading={historyLoading}
        columns={[
          { key: "revision", title: "版本", width: 90, type: "number", identity: true, value: (revision) => revision.revision_no,
            render: (revision) => `第 ${revision.revision_no} 版` },
          { key: "price", title: "单价", width: 110, type: "money", value: (revision) => revision.snapshot.unit_price },
          { key: "tax", title: "含税口径", width: 96, render: (revision) => taxLabel(revision.snapshot.is_tax_inclusive) },
          { key: "created", title: "更新时间", width: 180, type: "time", value: (revision) => revision.created_at,
            render: (revision) => formatBusinessDateTime(revision.created_at) },
          { key: "reason", title: "变更原因", width: 180, type: "zh", value: (revision) => revision.reason,
            render: (revision) => hasValue(revision.reason) ? `原因：${revision.reason}` : "—" },
        ]} renderExpanded={(revision) => <>
          <Descriptions size="small" column={2} items={historyFields.filter((field) => hasValue(revision.snapshot[field.key]))
            .map((field) => ({ key: field.key, label: field.label, children: field.key === "is_tax_inclusive"
              ? taxLabel(revision.snapshot.is_tax_inclusive) : field.key === "valid_from" || field.key === "valid_until"
                ? formatBusinessDateTime(revision.snapshot[field.key]) : displayValue(revision.snapshot[field.key]) }))} />
          {hasValue(revision.snapshot.reporter?.name) && <div>报价人：{revision.snapshot.reporter?.name}</div>}
          {!!revision.changed_fields.length && <Typography.Text type="secondary">变更：{revision.changed_fields.map((field) => historyFields.find((entry) => entry.key === field)?.label ?? field).join("、")}</Typography.Text>}
        </>} />
      {!historyLoading && !historyError && !history.length && <Empty description="暂无报价历史" />}
    </Modal>
    <Modal open={closeOpen} title="关单" onCancel={() => { if (!busy) setCloseOpen(false); }} onOk={() => void close()}
      confirmLoading={busy} okText="确认关单" cancelButtonProps={{ disabled: busy }} maskClosable={!busy} closable={!busy}>
      <Form layout="vertical">
        <Form.Item label="关单结果"><Radio.Group value={outcome} onChange={(event) => setOutcome(event.target.value)}>
          <Radio value="won">成交</Radio><Radio value="lost">未成交</Radio><Radio value="cancelled">取消</Radio>
        </Radio.Group></Form.Item>
        {outcome === "won" && <Form.Item label="采纳本单报价（可选）">
          <Select aria-label="采纳报价" allowClear value={wonOfferId} onChange={setWonOfferId} placeholder="选择采纳的本单报价（可选）" options={closeOptions.map((offer) => ({
            value: offer.id, label: `${lineForOffer(offer)?.pn_raw || ""} · 报价 #${offer.id}${offer.reporter ? ` · ${offer.reporter.name}` : ""} · ${offer.latest
              ? `当前${formatMoney(offer.latest.unit_price, { prefix: "¥" })} · ${taxLabel(offer.latest.is_tax_inclusive)} · 第 ${offer.latest.revision_no} 版`
              : `${formatMoney(offer.unit_price, { prefix: "¥" })} · ${taxLabel(offer.is_tax_inclusive)}`}`,
          }))} />
        </Form.Item>}
      </Form>
    </Modal>
  </div>;
}
