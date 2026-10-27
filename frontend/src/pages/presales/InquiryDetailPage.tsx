import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Alert, App, Button, Card, Descriptions, Empty, Form, Modal, Radio, Select, Space, Spin, Tag, Typography } from "antd";
import { presales, type Inquiry, type InquiryLine, type Offer, type OfferRevision } from "../../api/presales";
import PartThumb from "../../components/gallery/PartThumb";
import GalleryPicker from "../../components/gallery/GalleryPicker";
import Timeline from "../../components/workflow/Timeline";
import PageHeader from "../../components/PageHeader";
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

/** 每个 PN 独立比价；含税、未税、未声明三种口径分别找最低价。 */
function Comparison({ line, offers, onHistory, now }: {
  line: InquiryLine; offers: Offer[]; onHistory: (offer: Offer) => void; now: number;
}) {
  const pinned = new Map([...(line.linked_offers ?? []), ...(line.ref_offer ? [line.ref_offer] : [])].map((offer) => [offer.id, offer]));
  const shown = [...pinned.values(), ...offers.filter((offer) => !pinned.has(offer.id))].filter((offer) => (offer.line_id === line.id
    || offer.id === line.ref_offer_id || pinned.has(offer.id) || line.linked_offer_ids?.includes(offer.id)) && offer.status !== "withdrawn");
  if (!shown.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无报价" />;
  const minima = new Map<string, number>();
  shown.filter((offer) => isOfferActive(offer, now) && !offer.no_stock && hasValue(offer.unit_price))
    .forEach((offer) => {
      const basis = String(offer.is_tax_inclusive);
      const price = Number(offer.unit_price);
      if (Number.isFinite(price)) minima.set(basis, Math.min(minima.get(basis) ?? Infinity, price));
    });
  const rows: { key: keyof Offer | "price"; label: string; render: (offer: Offer) => ReactNode }[] = [
    { key: "price", label: "单价 / 口径", render: (offer) => <OfferPrice offer={offer} /> },
    { key: "qty_available", label: "可供数量", render: (offer) => displayValue(offer.qty_available) },
    { key: "stock_location", label: "货物所在地", render: (offer) => displayValue(offer.stock_location) },
    { key: "lead_time", label: "交期", render: (offer) => displayValue(offer.lead_time) },
    { key: "valid_until", label: "有效截止（上海）", render: (offer) => hasValue(offer.valid_until) ? formatBusinessDateTime(offer.valid_until) : null },
    { key: "supplier_id", label: "供应商", render: (offer) => displayValue(offer.supplier_id) },
    { key: "source_key", label: "货源", render: (offer) => displayValue(offer.source_key) },
    { key: "notes", label: "采购备注", render: (offer) => displayValue(offer.notes) },
  ];
  return <div style={{ overflowX: "auto" }}>
    <table aria-label={`${line.pn_raw} 比价`} style={{ width: "100%", borderCollapse: "collapse", textAlign: "left" }}>
      <thead><tr><th scope="col" style={{ minWidth: 130, padding: 12 }}>报价信息</th>{shown.map((offer) => {
        const active = isOfferActive(offer, now);
        const lowest = active && !offer.no_stock && hasValue(offer.unit_price)
          && Number(offer.unit_price) === minima.get(String(offer.is_tax_inclusive));
        return <th key={offer.id} scope="col" style={{ minWidth: 220, padding: 12, verticalAlign: "top" }}>
          <Space direction="vertical" size={4}>
            <Typography.Text>{offer.reporter?.name || `报价 #${offer.id}`}</Typography.Text>
            <Space wrap>
              {lowest && <Tag color="success">{taxLabel(offer.is_tax_inclusive)}最低价</Tag>}
              {offer.status === "stale" && <Tag color="warning">基于旧版本</Tag>}
              {offer.status === "draft" && <Tag>报价草稿</Tag>}
              {offer.status === "published" && !offer.no_stock && !active && <Tag>已失效</Tag>}
            </Space>
            <OfferUpdates offer={offer} />
            <Button type="link" size="small" aria-label={`查看报价 ${offer.id} 历史`} onClick={() => onHistory(offer)}>报价历史</Button>
          </Space>
        </th>;
      })}</tr></thead>
      <tbody>{rows.filter((row) => row.key === "price" || shown.some((offer) => hasValue(offer[row.key as keyof Offer])))
        .map((row) => <tr key={row.key}>
          <th scope="row" style={{ padding: 12, borderTop: "1px solid var(--mb-border-soft)", fontWeight: 400 }}>{row.label}</th>
          {shown.map((offer) => <td key={offer.id} style={{ padding: 12, borderTop: "1px solid var(--mb-border-soft)",
            color: isOfferActive(offer, now) ? "var(--mb-text)" : "var(--mb-text-3)", verticalAlign: "top" }}>{row.render(offer)}</td>)}
        </tr>)}</tbody>
    </table>
  </div>;
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
  const comparisonOffers = offers;
  const canAmend = inquiry.kind === "sales_inquiry";
  const adopted = inquiry.won_offer;
  const lineForOffer = (offer: Offer) => lines.find((line) => line.id === offer.line_id || line.ref_offer_id === offer.id
    || line.linked_offer_ids?.includes(offer.id) || line.linked_offers?.some((linked) => linked.id === offer.id) || inquiry.line_responses?.some((row) => row.line_id === line.id && row.mode === "reuse" && row.ref_offer_id === offer.id));
  const closeOptions = [...new Map([...offers, ...lines.flatMap((line) => [...(line.linked_offers ?? []), ...(line.ref_offer ? [line.ref_offer] : [])]),
    ...(inquiry.line_responses ?? []).flatMap((row) => row.mode === "reuse" && row.offer ? [row.offer] : [])]
    .map((offer) => [offer.id, offer])).values()].filter((offer) => isOfferActive(offer, now) && !!lineForOffer(offer));
  const galleryItems = lines.filter((line) => line.part_id).map((line) => ({ partId: line.part_id!, pn: line.pn_raw }));

  return <Space direction="vertical" size="middle" style={{ width: "100%" }}>
    <PageHeader title={`询价单 ${inquiry.inquiry_no}`} subtitle={<Space wrap>
      <Tag>{inquiry.kind === "sales_inquiry" ? "销售询价" : "主动报价"}</Tag>
      <Tag>{statusLabels[inquiry.status] || inquiry.status}</Tag>
      <Typography.Text type="secondary">版本 {inquiry.revision}</Typography.Text>
    </Space>} extra={<Space wrap>
      <Button onClick={() => navigate("/presales/inquiries")}>返回列表</Button>
      {inquiry.status === "closed" ? <Button loading={busy} onClick={() => void copy()}>复制为新单</Button> : <>
        {inquiry.kind === "proactive_offer" && inquiry.status === "draft" && <Button disabled={busy}
          onClick={() => navigate(`/presales/offers/new?draft=${id}`)}>继续填写主动报价</Button>}
        {inquiry.kind === "proactive_offer" && inquiry.status === "submitted" && <Button disabled={busy}
          onClick={() => navigate(`/presales/tasks/${id}`)}>更新 / 撤销报价</Button>}
        {canAmend && ["draft", "revising"].includes(inquiry.status) && <Button disabled={busy} onClick={() => navigate(`/presales/inquiries/${id}/edit`)}>编辑询价</Button>}
        {canAmend && inquiry.status === "submitted" && <>
          <Button loading={busy} onClick={() => void withdraw()}>撤回修改</Button>
          <Button disabled={busy} onClick={() => navigate(`/presales/inquiries/${id}/edit?append=1`)}>追加行</Button>
        </>}
        {["submitted", "revising"].includes(inquiry.status) && <Button aria-label="关单" disabled={busy} onClick={() => { setOutcome("won"); setWonOfferId(undefined); setCloseOpen(true); }}>关单</Button>}
      </>}
    </Space>} />
    {error && <Alert type="error" message={error} action={<Button onClick={() => void load()}>重试</Button>} />}
    <Card title="单头信息" size="small" loading={loading}>
      <Descriptions column={{ xs: 1, sm: 2, lg: 3 }} items={[
        ...(hasValue(inquiry.customer_id) ? [{ key: "customer", label: "客户", children: String(inquiry.customer_id) }] : []),
        ...(hasValue(inquiry.notes) ? [{ key: "notes", label: "客户要求", children: inquiry.notes }] : []),
        { key: "created", label: "创建时间", children: formatBusinessDateTime(inquiry.created_at) },
        { key: "updated", label: "最近动态", children: formatBusinessDateTime(inquiry.updated_at) },
        ...(inquiry.closed_outcome ? [{ key: "outcome", label: "关单结果", children: outcomeLabels[inquiry.closed_outcome] || inquiry.closed_outcome }] : []),
        ...(adopted ? [{ key: "adopted", label: "采纳报价", children: <Space wrap><Button type="link" onClick={() => void showHistory(adopted)}>报价 #{adopted.id} · 第 {adopted.revision_no} 版</Button><OfferBadge offer={adopted} /></Space> }] : []),
      ]} />
      <FieldValues values={inquiry.field_values} />
      {!!inquiry.work_items?.length && <Space wrap>{inquiry.work_items.map((work) => <Tag key={work.id}>
        {hasValue(work.claimed_by) || hasValue(work.assignee_id) ? `处理人 ${work.claimed_by ?? work.assignee_id} · ` : ""}
        {workLabels[work.status] || work.status}{work.paused ? " · 已暂停" : ""}
      </Tag>)}</Space>}
    </Card>
    <Space wrap>
      <Typography.Title level={4} style={{ margin: 0 }}>询价明细与比价</Typography.Title>
      {!!galleryItems.length && <Button onClick={() => setGalleryOpen(true)}>图库多选下载</Button>}
      <Typography.Text type="secondary">最低价按同一含税口径比较，仅计入有效报价。</Typography.Text>
    </Space>
    {lines.map((line) => <Card key={line.id} size="small" title={<Space>
      {line.part_id ? <PartThumb partId={line.part_id} pn={line.pn_raw} /> : <Tag>待建档</Tag>}
      <Typography.Text strong>{line.seq}. {line.pn_raw}</Typography.Text>
    </Space>}>
      <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 3 }} items={[
        { key: "qty", label: "数量", children: String(line.qty) },
        { key: "exact", label: "要求 PN 一致", children: line.require_exact_pn ? "是" : "否" },
        ...(hasValue(line.description) ? [{ key: "description", label: "描述", children: line.description }] : []),
        ...(hasValue(line.notes) ? [{ key: "notes", label: "行要求", children: line.notes }] : []),
        ...(line.ref_offer_id ? [{ key: "reference", label: "引用报价", children: `#${line.ref_offer_id}` }] : []),
      ]} />
      <FieldValues values={line.field_values} />
      {(inquiry.line_responses ?? []).filter((response) => response.line_id === line.id && response.mode !== "quote")
        .map((response) => <div key={`${response.work_item_id}:${line.id}`}>
          {response.mode === "skip" ? <Typography.Paragraph>无货 / 不报：{response.reason}</Typography.Paragraph>
            : <Space wrap><Typography.Text>沿用报价 #{response.ref_offer_id}</Typography.Text>
              {response.offer ? <><OfferBadge offer={response.offer} />
                <span>原报价人：{response.reporter_label ?? response.offer.reporter?.name ?? "其他采购"}</span>
                {response.offer.valid_from && <span>原报价时间：{formatBusinessDateTime(response.offer.valid_from)}</span>}
              </> : <Tag>{response.expired ? "已失效" : "当前不可见"}</Tag>}
            </Space>}
        </div>)}
      <Comparison line={line} offers={comparisonOffers} now={now} onHistory={(offer) => void showHistory(offer)} />
    </Card>)}
    {!lines.length && <Empty description="暂无询价行" />}
    <Card title="时间线" size="small"><Timeline key={`${id}:${inquiry.version}`} subjectType="presales_inquiry" subjectId={id} /></Card>
    {galleryOpen && <GalleryPicker items={galleryItems} mode="modal" onClose={() => setGalleryOpen(false)} />}
    <Modal open={!!historyOffer} title={historyOffer ? `报价 #${historyOffer.id} 历史` : "报价历史"} footer={null} width={760}
      onCancel={() => { ++historyGeneration.current; setHistoryOffer(undefined); }}>
      {historyLoading && <Spin aria-label="报价历史加载中" />}
      {historyError && <Alert type="error" message={historyError} action={<Button onClick={() => historyOffer && void showHistory(historyOffer)}>重试</Button>} />}
      <Space direction="vertical" style={{ width: "100%" }}>{history.map((revision) => <Card size="small" key={revision.revision_no}
        title={`第 ${revision.revision_no} 版 · ${formatBusinessDateTime(revision.created_at)}`}>
        <Descriptions size="small" column={2} items={historyFields.filter((field) => hasValue(revision.snapshot[field.key]))
          .map((field) => ({ key: field.key, label: field.label, children: field.key === "is_tax_inclusive"
            ? taxLabel(revision.snapshot.is_tax_inclusive) : field.key === "valid_from" || field.key === "valid_until"
              ? formatBusinessDateTime(revision.snapshot[field.key]) : displayValue(revision.snapshot[field.key]) }))} />
        {hasValue(revision.snapshot.reporter?.name) && <div>报价人：{revision.snapshot.reporter?.name}</div>}
        {!!revision.changed_fields.length && <Typography.Text type="secondary">变更：{revision.changed_fields.map((field) => historyFields.find((entry) => entry.key === field)?.label ?? field).join("、")}</Typography.Text>}
        {hasValue(revision.reason) && <div>原因：{revision.reason}</div>}
      </Card>)}</Space>
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
  </Space>;
}
