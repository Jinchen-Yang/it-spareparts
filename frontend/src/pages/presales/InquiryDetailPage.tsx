import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { Alert, App, Button, Descriptions, Drawer, Form, Modal, Radio, Select, Space, Spin, Table, Tag, Typography } from "antd";
import { presales, type Inquiry, type InquiryLine, type Offer, type OfferRevision } from "../../api/presales";
import type { WorkItem } from "../../api/workItems";
import PartThumb from "../../components/gallery/PartThumb";
import GalleryPicker from "../../components/gallery/GalleryPicker";
import Timeline from "../../components/workflow/Timeline";
import PageHeader from "../../components/PageHeader";
import { hasValue, FieldValues, OfferPrice, offersForLine, OfferDetails, historyFields, displayValue } from "./InquiryOffers";
import "./presales.css";
import { formatBusinessDateTime } from "../../utils/date";
import { formatMoney } from "../../utils/format";
import { usePresalesError, isOfferActive, OfferBadge, taxLabel, useNow, OfferUpdates } from "./shared";

const statusLabels: Record<string, string> = { draft: "草稿", submitted: "已提交", revising: "撤回修改中", closed: "已结束" };
const outcomeLabels: Record<string, string> = { won: "成交", lost: "未成交", cancelled: "取消" };
const workLabels: Record<string, string> = { pending: "待处理", in_progress: "处理中", done: "已回复", declined: "已退回", cancelled: "已取消" };

export default function InquiryDetailPage() {
  const { id = "" } = useParams();
  return <InquiryDetailContent key={id} id={Number(id)} />;
}

function InquiryDetailContent({ id }: { id: number }) {
  const handlePresalesError = usePresalesError();
  const location = useLocation();
  const readOnly = (location.state as { workItem?: WorkItem } | null)?.workItem?.subject_type === "presales_offer_notice";
  const navigate = useNavigate();
  const { message } = App.useApp();
  const [inquiry, setInquiry] = useState<Inquiry>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [galleryOpen, setGalleryOpen] = useState(false);
  const [galleryLineId, setGalleryLineId] = useState<number>();
  const [timelineOpen, setTimelineOpen] = useState(false);
  const [lineId, setLineId] = useState<number>();
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
    .map((offer) => offer.valid_from));

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
    || line.linked_offer_ids?.includes(offer.id) || line.linked_offers?.some((linked) => linked.id === offer.id) || inquiry.line_responses?.some((row) => !row.is_draft && row.line_id === line.id && row.mode === "reuse" && row.ref_offer_id === offer.id));
  const closeOptions = [...new Map([...offers, ...lines.flatMap((line) => [...(line.linked_offers ?? []), ...(line.ref_offer ? [line.ref_offer] : [])]),
    ...(inquiry.line_responses ?? []).flatMap((row) => !row.is_draft && row.mode === "reuse" && row.offer ? [row.offer] : [])]
    .map((offer) => [offer.id, offer])).values()].filter((offer) => isOfferActive(offer, now) && !!lineForOffer(offer));
  const galleryItems = lines.filter((line) => line.part_id).map((line) => ({ partId: line.part_id!, pn: line.pn_raw }));

  const lineOffers = new Map(lines.map((line) => [line.id, offersForLine(line, inquiry)]));
  const referenceIds = (line: InquiryLine) => new Set([...(line.linked_offer_ids ?? []), ...(line.linked_offers ?? []).map((offer) => offer.id),
    ...(line.ref_offer_id ? [line.ref_offer_id] : []), ...(inquiry.line_responses ?? []).filter((response) => !response.is_draft && response.line_id === line.id && response.mode === "reuse")
      .flatMap((response) => response.ref_offer_id ? [response.ref_offer_id] : [])]);
  const canClose = !readOnly && ["submitted", "revising"].includes(inquiry.status);
  const renderOffer = (line: InquiryLine, offer: Offer) => {
    const active = isOfferActive(offer, now);
    const candidates = (lineOffers.get(line.id) ?? []).filter((candidate) => isOfferActive(candidate, now) && !candidate.no_stock
      && hasValue(candidate.unit_price) && candidate.is_tax_inclusive === offer.is_tax_inclusive);
    const lowest = active && !offer.no_stock && typeof offer.is_tax_inclusive === "boolean" && hasValue(offer.unit_price)
      && Number(offer.unit_price) === Math.min(...candidates.map((candidate) => Number(candidate.unit_price)));
    const detail = [offer.reporter?.name || `报价 #${offer.id}`, offer.stock_location, offer.lead_time,
      offer.valid_from ? `报价于 ${formatBusinessDateTime(offer.valid_from)}` : ""].filter(Boolean).join(" · ");
    return <span key={offer.id} className="presales-quote-cell">
      <button type="button" aria-label={`采纳 ${line.pn_raw} 报价 ${offer.id}`} aria-pressed={wonOfferId === offer.id}
        className={`presales-offer-choice ${wonOfferId === offer.id ? "is-selected" : lowest ? "is-lowest" : ""}`}
        disabled={!canClose || busy || !active || !!offer.no_stock} title={detail}
        onClick={() => setWonOfferId(wonOfferId === offer.id ? undefined : offer.id)}><OfferPrice offer={offer} />
        {lowest && <span className="presales-muted">{taxLabel(offer.is_tax_inclusive)}最低价</span>}
        {active && <Tag color="green">有效</Tag>}
        {offer.status === "stale" && <span>基于旧版本</span>}
        {offer.status === "draft" && <span>报价草稿</span>}
        {offer.status === "published" && !offer.no_stock && !active && <span>已失效</span>}
      </button>
      <span className="presales-muted"><OfferUpdates offer={offer} /></span>
      <Button size="small" type="link" aria-label={`查看报价 ${offer.id} 历史`} onClick={() => void showHistory(offer)}>历史</Button>
    </span>;
  };
  const selectedLine = lines.find((line) => line.id === lineId);
  const responseDetails = (line: InquiryLine) => (inquiry.line_responses ?? []).filter((response) => response.line_id === line.id && response.mode !== "quote").map((response) => <div key={`${response.work_item_id}:${line.id}`}>
    {response.is_draft && <Tag>草稿（未回复）</Tag>}{response.mode === "skip" ? <p>无货 / 不报：{response.reason}</p> : <><div>沿用报价 #{response.ref_offer_id}</div>
      {response.offer ? <><div>原报价人：{response.reporter_label ?? response.offer.reporter?.name ?? "其他采购"}</div>{response.offer.valid_from && <div>原报价时间：{formatBusinessDateTime(response.offer.valid_from)}</div>}<OfferBadge offer={response.offer} /></>
        : <Tag>{response.expired ? "已失效" : "当前不可见"}</Tag>}</>}
  </div>);
  return <section className="presales-page">
    <PageHeader title={`询价单 ${inquiry.inquiry_no}`} subtitle={<><Tag>{statusLabels[inquiry.status]}</Tag>{lines.length} 行 · 版本 {inquiry.revision}</>} extra={<Space wrap>
      <Button onClick={() => navigate("/presales/inquiries")}>返回列表</Button>
      {!readOnly && (inquiry.status === "closed" ? <Button loading={busy} onClick={() => void copy()}>复制为新单</Button> : <>
        {inquiry.kind === "proactive_offer" && inquiry.status === "draft" && <Button disabled={busy} onClick={() => navigate(`/presales/offers/new?draft=${id}`)}>继续填写主动报价</Button>}
        {inquiry.kind === "proactive_offer" && inquiry.status === "submitted" && <Button disabled={busy} onClick={() => navigate(`/presales/workbench/${id}`)}>更新 / 撤销报价</Button>}
        {canAmend && ["draft", "revising"].includes(inquiry.status) && <Button disabled={busy} onClick={() => navigate(`/presales/inquiries/${id}/edit`)}>编辑询价</Button>}
        {canAmend && inquiry.status === "submitted" && <><Button loading={busy} onClick={() => void withdraw()}>撤回修改</Button><Button disabled={busy} onClick={() => navigate(`/presales/inquiries/${id}/edit?append=1`)}>追加行</Button></>}
      </>)}
      <Button onClick={() => setTimelineOpen(true)}>查看动态</Button>
    </Space>} />
    <div className="presales-panel">
      {error && <Alert type="error" message={error} action={<Button onClick={() => void load()}>重试</Button>} />}
      <Descriptions column={2} items={[
        ...(hasValue(inquiry.customer_id) ? [{ key: "customer", label: "客户", children: inquiry.customer_name ?? inquiry.customer_id }] : []),
        ...(hasValue(inquiry.notes) ? [{ key: "notes", label: "客户要求", children: inquiry.notes }] : []),
        { key: "updated", label: "最近动态", children: formatBusinessDateTime(inquiry.updated_at) },
        ...(inquiry.closed_outcome ? [{ key: "outcome", label: "关单结果", children: outcomeLabels[inquiry.closed_outcome] }] : []),
      ]} />
      <FieldValues values={inquiry.field_values} />
      {adopted && <Space wrap><Button type="link" onClick={() => void showHistory(adopted)}>报价 #{adopted.id} · 第 {adopted.revision_no} 版</Button><OfferBadge offer={adopted} /></Space>}
      <div className="presales-toolbar">{inquiry.work_items?.map((work) => <Tag key={work.id}>{hasValue(work.claimed_by) || hasValue(work.assignee_id) ? `处理人 ${work.claimed_by ?? work.assignee_id} · ` : "公共池 · "}{workLabels[work.status]}{work.paused ? " · 已暂停" : ""}</Tag>)}</div>
      <p className="presales-muted">最低价按同一含税口径比较，仅计入有效报价。</p>
      <Table aria-label="询价比价矩阵" rowKey="id" dataSource={lines} loading={loading} tableLayout="fixed" pagination={{ defaultPageSize: 20 }} columns={[
        { title: "PN / 描述", key: "pn", render: (_, line) => <><Space>{line.part_id && <PartThumb partId={line.part_id} pn={line.pn_raw} size={24} />}{line.pn_raw}{line.require_exact_pn && <Tag>一致</Tag>}</Space><div className="presales-muted">{line.description}</div></> },
        { title: "数量 / 期望交期", key: "quantity", width: 150, render: (_, line) => <>{line.qty}<div className="presales-muted">{line.expected_delivery || "—"}</div></> },
        { title: "报价对比", key: "offers", render: (_, line) => <>{(lineOffers.get(line.id) ?? []).filter((offer) => !referenceIds(line).has(offer.id)).map((offer) => <div key={offer.id}>{offer.reporter?.name}{renderOffer(line, offer)}</div>)}
          {(inquiry.line_responses ?? []).filter((response) => response.line_id === line.id && response.mode !== "quote").map((response) => <div key={`${response.created_by}:${response.line_id}`}>
            {response.is_draft ? `草稿（未回复） · ${response.mode === "skip" ? `不报：${response.reason}` : `沿用报价 #${response.ref_offer_id}`}` : response.mode === "skip" ? `不报：${response.reason}` : `沿用报价 #${response.ref_offer_id}`}
          </div>)}{![...(lineOffers.get(line.id) ?? [])].length && !inquiry.line_responses?.some((response) => response.line_id === line.id) && "待报价"}</> },
        { title: "引用 / 报价库", key: "referenced", render: (_, line) => (lineOffers.get(line.id) ?? []).filter((offer) => referenceIds(line).has(offer.id)).map((offer) => <div key={offer.id}>{offer.reporter?.name}{renderOffer(line, offer)}</div>) },
        { title: "操作", key: "actions", width: 120, render: (_, line) => <Space direction="vertical"><Button type="link" aria-label={`查看 ${line.pn_raw} 详情`} onClick={() => setLineId(line.id)}>查看详情</Button>{line.part_id && <Button type="link" onClick={() => { setGalleryLineId(line.id); setGalleryOpen(true); }}>图库多选下载</Button>}</Space> },
      ]} />
    </div>
    <footer className="presales-footer"><span>{readOnly ? "报价通知 · 只读" : wonOfferId ? `已选择报价 #${wonOfferId}` : "点报价格子选择成交采纳报价"}</span>
      {canClose && <Space><Button disabled={busy} onClick={() => { setOutcome("lost"); setCloseOpen(true); }}>未成交关单</Button><Button type="primary" aria-label="关单" disabled={busy} onClick={() => { setOutcome("won"); setCloseOpen(true); }}>成交关单</Button></Space>}
    </footer>
    <Drawer title="询价动态" open={timelineOpen} onClose={() => setTimelineOpen(false)} width={480} destroyOnHidden>
      {timelineOpen && <Timeline key={`${id}:${inquiry.version}`} subjectType="presales_inquiry" subjectId={id} />}
    </Drawer>
    <Drawer title={selectedLine ? `${selectedLine.pn_raw} · 详情` : "询价行详情"} open={!!selectedLine} width={720} onClose={() => setLineId(undefined)} destroyOnHidden>
      {selectedLine && <><p>{selectedLine.description}</p>{hasValue(selectedLine.notes) && <p>行要求：{selectedLine.notes}</p>}<FieldValues values={selectedLine.field_values} />
        {responseDetails(selectedLine)}<OfferDetails line={selectedLine} offers={lineOffers.get(selectedLine.id) ?? []} onHistory={(offer) => void showHistory(offer)} /></>}
    </Drawer>
    {galleryOpen && <GalleryPicker items={galleryLineId ? galleryItems.filter((item) => lines.find((line) => line.id === galleryLineId)?.part_id === item.partId) : galleryItems} mode="modal" onClose={() => setGalleryOpen(false)} />}
    <Drawer open={!!historyOffer} title={historyOffer ? `报价 #${historyOffer.id} 历史` : "报价历史"} width={720} onClose={() => { ++historyGeneration.current; setHistoryOffer(undefined); }} destroyOnHidden>
      {historyError && <Alert type="error" message={historyError} action={<Button onClick={() => historyOffer && void showHistory(historyOffer)}>重试</Button>} />}
      <Table aria-label="报价历史" rowKey="revision_no" dataSource={history} loading={historyLoading} tableLayout="fixed" pagination={{ pageSize: 5 }} columns={[
        { title: "版本 / 时间", key: "version", width: 160, render: (_, revision) => <>第 {revision.revision_no} 版<div>{formatBusinessDateTime(revision.created_at)}</div>{revision.reason && <p>原因：{revision.reason}</p>}</> },
        { title: "报价内容", key: "details", render: (_, revision) => <><Descriptions column={1} size="small" items={historyFields.filter((field) => hasValue(revision.snapshot[field.key])).map((field) => ({ key: field.key, label: field.label,
          children: field.key === "is_tax_inclusive" ? taxLabel(revision.snapshot.is_tax_inclusive) : field.key === "valid_from" ? formatBusinessDateTime(revision.snapshot[field.key]) : displayValue(revision.snapshot[field.key]),
        }))} />{revision.snapshot.reporter?.name && <p>报价人：{revision.snapshot.reporter.name}</p>}{!!revision.changed_fields.length && <Typography.Text type="secondary">变更：{revision.changed_fields.map((field) => historyFields.find((entry) => entry.key === field)?.label ?? field).join("、")}</Typography.Text>}</> },
      ]} locale={{ emptyText: "暂无报价历史" }} />
    </Drawer>
    <Modal open={closeOpen} title="关单" onCancel={() => { if (!busy) setCloseOpen(false); }} onOk={() => void close()} confirmLoading={busy} okText="确认关单" cancelButtonProps={{ disabled: busy }} maskClosable={!busy} closable={!busy}>
      <Form layout="vertical"><Form.Item label="关单结果"><Radio.Group value={outcome} onChange={(event) => setOutcome(event.target.value)}><Radio value="won">成交</Radio><Radio value="lost">未成交</Radio><Radio value="cancelled">取消</Radio></Radio.Group></Form.Item>
        {outcome === "won" && <Form.Item label="采纳本单报价（可选）"><Select aria-label="采纳报价" allowClear value={wonOfferId} onChange={setWonOfferId} placeholder="选择采纳的本单报价（可选）" options={closeOptions.map((offer) => ({ value: offer.id,
          label: `${lineForOffer(offer)?.pn_raw || ""} · 报价 #${offer.id}${offer.reporter ? ` · ${offer.reporter.name}` : ""} · ${offer.latest ? `当前${formatMoney(offer.latest.unit_price, { prefix: "¥" })} · ${taxLabel(offer.latest.is_tax_inclusive)} · 第 ${offer.latest.revision_no} 版` : `${formatMoney(offer.unit_price, { prefix: "¥" })} · ${taxLabel(offer.is_tax_inclusive)}`}`,
        }))} /></Form.Item>}
      </Form>
    </Modal>
  </section>;
}
