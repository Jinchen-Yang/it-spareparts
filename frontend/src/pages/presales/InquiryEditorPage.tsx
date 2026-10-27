import { useCallback, useEffect, useRef, useState, type Key } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Alert, App, Button, Checkbox, Form, Input, InputNumber, Modal, Popover, Select, Space, Spin, Tag } from "antd";
import { presales, type Inquiry, type InquiryLine, type Offer } from "../../api/presales";
import type { AudienceRule, AudienceRuleRead } from "../../api/audience";
import { getFieldTemplate, listFieldDefinitions, type CustomFieldValue, type FieldDefinition, type FieldLevel } from "../../api/fields";
import { listWorkCandidates, type WorkCandidate } from "../../api/workItems";
import type { PartSuggestion } from "../../api/search";
import LivePartSearch from "../../components/parts/LivePartSearch";
import PartThumb from "../../components/gallery/PartThumb";
import PriceRefBadge from "../../components/pricing/PriceRefBadge";
import AudiencePicker, { PUBLIC_AUDIENCE } from "../../components/audience/AudiencePicker";
import AudienceLock from "../../components/audience/AudienceLock";
import CustomFieldsEditor from "../../components/fields/CustomFieldsEditor";
import PartQuoteBadges from "./PartQuoteBadges";
import { isOfferActive as activeOffer, OfferBadge, useNow, flattenOffers } from "./shared";
import { formatMoney } from "../../utils/format";
import CandidateSelect from "./CandidateSelect";
import { changedAudience } from "../../components/audience/rules";
import { readPermissionMap } from "../../nav";
import DataGrid, { type DataGridColumn } from "../../components/datagrid/DataGrid";
import { WorkbookExportButton } from "../../components/workbook";
import { presalesWorkbooks } from "../../api/presalesWorkbooks";
import WorkbookDialog from "./WorkbookDialog";
import "./sales-pages.css";

type ReadField = { field_def_id?: number; new_name?: string; name?: string; value: unknown; rule?: AudienceRuleRead };
type DraftLine = {
  key: string; line_key?: string; part_id: number | null; pn_raw: string; description?: string | null;
  brand?: string | null; qty: number; require_exact_pn: boolean; ref_offer_id?: number | null;
  ref_offer_revision_no?: number | null; ref_offer?: Offer;
  notes?: string | null; notes_audience?: AudienceRule; notesVisible: boolean; field_values: CustomFieldValue[];
  savedNotesAudience?: AudienceRuleRead; savedDeliveryAudience?: AudienceRuleRead;
  originalFields: ReadField[]; expectedDelivery: string; deliveryFieldId?: number; deliveryAudience?: AudienceRule;
};
const DELIVERY_FIELD = "期望交期";
const fieldKey = (value: { field_def_id?: number; new_name?: string }) => value.field_def_id ? `id:${value.field_def_id}` : `new:${value.new_name}`;
const editableFields = (values: ReadField[] = [], excludeDelivery = false): CustomFieldValue[] => values.flatMap((value) => {
  if ((excludeDelivery && value.name === DELIVERY_FIELD) || value.value == null) return [];
  return [{ ...(value.field_def_id ? { field_def_id: value.field_def_id } : { new_name: value.new_name || value.name || "" }), value: String(value.value) } as CustomFieldValue];
});

function errorText(error: unknown, fallback: string) {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === "string" ? detail : detail && typeof detail === "object" && "message" in detail ? String(detail.message) : fallback;
}

/** 字段池只负责编辑；模板按 PN anchor 拉取，不携带上一张单据的值。 */
function Fields({ level, partId, value, savedValues, onChange, disabled = false }: {
  level: FieldLevel; partId?: number | null; value: CustomFieldValue[];
  savedValues?: ReadField[];
  onChange: (values: CustomFieldValue[]) => void; disabled?: boolean;
}) {
  const [template, setTemplate] = useState<FieldDefinition[]>([]);
  const [definitions, setDefinitions] = useState<FieldDefinition[]>([]);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setError(false);
    void Promise.all([getFieldTemplate("presales_inquiry", level, partId ? `part:${partId}` : undefined, controller.signal),
      listFieldDefinitions("presales_inquiry", level, controller.signal)]).then(([rows, pool]) => {
      if (!controller.signal.aborted) {
        setTemplate(rows.filter((field) => field.name !== DELIVERY_FIELD || level !== "line"));
        setDefinitions(pool.filter((field) => field.name !== DELIVERY_FIELD || level !== "line"));
      }
    }).catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [level, partId]);
  return <>
    {error && <Alert type="warning" message="字段池加载失败，请刷新后重试；已有填写内容仍会保留。" />}
    <CustomFieldsEditor subjectType="presales_inquiry" level={level} template={template} definitions={definitions}
      value={value} savedValues={savedValues} onChange={onChange} disabled={disabled || error} />
  </>;
}

function EffectiveOffers({ offers, now, onReference }: { offers: Offer[]; now: number; onReference?: (offer: Offer) => void }) {
  const active = flattenOffers(offers).filter((offer) => activeOffer(offer, now));
  if (!active.length) return <span className="sales-muted">无有效报价</span>;
  const first = active[0];
  const columns: DataGridColumn<Offer>[] = [
    { key: "id", title: "报价", type: "code", width: 96, identity: true, value: (offer) => offer.id },
    { key: "price", title: "单价", type: "money", width: 120, value: (offer) => offer.unit_price,
      render: (offer) => formatMoney(offer.unit_price, { prefix: "¥" }) },
    { key: "summary", title: "有效报价", width: 320, render: (offer) => <OfferBadge offer={offer} /> },
    { key: "reference", title: "操作", width: 100, render: (offer) => onReference && <Button size="small" onClick={() => onReference(offer)}>引用报价 #{offer.id}</Button> },
  ];
  return <span className="sales-inline"><span title={`共 ${active.length} 条有效报价`}>{formatMoney(first.unit_price, { prefix: "¥" })}</span>
    {onReference && <Button size="small" onClick={() => onReference(first)}>直接引用</Button>}
    <Popover trigger="click" placement="bottomRight" content={<div className="sales-offer-popover"><DataGrid id="presales-reference-offers"
      ariaLabel="可引用报价" rows={active} columns={columns} rowKey={(offer) => offer.id} /></div>}>
      <Button size="small" type="link">查看 {active.length} 条</Button>
    </Popover></span>;
}

function fromLine(line: InquiryLine): DraftLine {
  const fields = (line.field_values || []) as ReadField[];
  const delivery = fields.find((field) => field.name === DELIVERY_FIELD);
  return { key: line.line_key, line_key: line.line_key, part_id: line.part_id ?? null, pn_raw: line.pn_raw,
    description: line.description, qty: Number(line.qty), require_exact_pn: !!line.require_exact_pn,
    ref_offer_id: line.ref_offer_id, ref_offer_revision_no: line.ref_offer_revision_no, ref_offer: line.ref_offer, notes: line.notes, notesVisible: "notes" in line, field_values: editableFields(fields, true), originalFields: fields,
    savedNotesAudience: line.notes_audience, savedDeliveryAudience: delivery?.rule,
    expectedDelivery: delivery?.value == null ? "" : String(delivery.value), deliveryFieldId: delivery?.field_def_id };
}

export default function InquiryEditorPage() {
  const { id } = useParams();
  const [params] = useSearchParams();
  return <InquiryEditorContent key={`${id || "new"}:${params.get("append") || "edit"}`} />;
}

function InquiryEditorContent() {
  const { id } = useParams();
  const [params] = useSearchParams();
  const append = params.get("append") === "1";
  const navigate = useNavigate();
  const location = useLocation();
  const { message } = App.useApp();
  const [importOpen, setImportOpen] = useState(false);
  const customerVisible = localStorage.getItem("role") === "admin" || readPermissionMap().data_customer !== false;
  const [inquiry, setInquiry] = useState<Inquiry>();
  const [lines, setLines] = useState<DraftLine[]>([]);
  const [expanded, setExpanded] = useState<Key[]>([]);
  const [headOpen, setHeadOpen] = useState(false);
  const [pasteOpen, setPasteOpen] = useState(false);
  const [pasteText, setPasteText] = useState("");
  const [customerId, setCustomerId] = useState<number>();
  const [notes, setNotes] = useState("");
  const [notesAudience, setNotesAudience] = useState<AudienceRule>();
  const [offerAudience, setOfferAudience] = useState<AudienceRule>();
  const [fields, setFields] = useState<CustomFieldValue[]>([]);
  const [assigneeIds, setAssigneeIds] = useState<number[]>([]);
  const [candidates, setCandidates] = useState<WorkCandidate[]>([]);
  const [candidateError, setCandidateError] = useState(false);
  const [library, setLibrary] = useState<Record<number, Offer[]>>({});
  const now = useNow([...Object.values(library).flatMap(flattenOffers), ...lines.flatMap((line) => line.ref_offer ? [line.ref_offer] : [])]
    .flatMap((offer) => [offer.valid_from, offer.valid_until]));
  const referencedOffer = (line: DraftLine) => line.ref_offer_revision_no != null && line.ref_offer?.id === line.ref_offer_id
    ? line.ref_offer : flattenOffers(library[line.part_id || 0] ?? []).find((offer) => offer.id === line.ref_offer_id);
  const [loading, setLoading] = useState(!!id);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState("");
  const nextKey = useRef(0);
  const currentInquiry = useRef<Inquiry | undefined>(undefined);
  const mounted = useRef(true);
  const refreshGeneration = useRef(0);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; refreshGeneration.current += 1; };
  }, []);
  const apply = useCallback((data: Inquiry) => {
    currentInquiry.current = data;
    const next = append ? [] : (data.lines || []).map(fromLine);
    setInquiry(data); setLines(next);
    setExpanded(next.filter((line) => next.length === 1 || (!line.part_id && !line.description)).map((line) => line.key));
    setCustomerId(data.customer_id ?? undefined); setNotes(data.notes || "");
    setFields(editableFields((data.field_values || []) as ReadField[])); setAssigneeIds(data.assignee_ids || []);
    // 回读规则只用于显示；编辑草稿另存，未调整时不重复提交。
    setOfferAudience(undefined); setNotesAudience(undefined);
  }, [append]);
  const refresh = useCallback(async () => {
    if (!id) return;
    const generation = ++refreshGeneration.current;
    setLoading(true); setLoadError("");
    try {
      const data = await presales.getInquiry(Number(id));
      if (mounted.current && generation === refreshGeneration.current) apply(data);
    } catch (error) {
      if (mounted.current && generation === refreshGeneration.current) setLoadError(errorText(error, "询价单加载失败"));
    } finally { if (mounted.current && generation === refreshGeneration.current) setLoading(false); }
  }, [id, apply]);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    const controller = new AbortController();
    void listWorkCandidates("presales_inquiry", controller.signal).then((data) => {
      if (!controller.signal.aborted) setCandidates(data.items);
    }).catch(() => { if (!controller.signal.aborted) setCandidateError(true); });
    return () => controller.abort();
  }, []);
  const partIds = [...new Set(lines.flatMap((line) => line.part_id ? [line.part_id] : []))].sort((a, b) => a - b).join(",");
  useEffect(() => {
    let disposed = false;
    if (!partIds) { setLibrary({}); return; }
    void presales.library(partIds.split(",").map(Number)).then((data) => { if (!disposed) setLibrary(data); })
      .catch(() => { if (!disposed) setLibrary({}); });
    return () => { disposed = true; };
  }, [partIds]);
  useEffect(() => {
    if (id) return;
    const state = location.state as { referenceOffer?: Offer; part?: PartSuggestion } | null;
    if (!state?.referenceOffer || !state.part) return;
    const part = state.part;
    setLines([{ key: `new-${++nextKey.current}`, part_id: part.part_id, pn_raw: part.pn_std,
      description: part.summary || part.description, brand: part.brand, qty: 1, require_exact_pn: false,
      ref_offer_id: state.referenceOffer.id, notesVisible: true, field_values: [], originalFields: [], expectedDelivery: "" }]);
  }, [id, location.state]);

  const blocked = !!inquiry && (inquiry.kind !== "sales_inquiry" || (append ? inquiry.status !== "submitted" : !["draft", "revising"].includes(inquiry.status)));
  const updateLine = (key: string, change: Partial<DraftLine>) => setLines((previous) => previous.map((line) => line.key === key ? { ...line, ...change } : line));
  const addParts = (parts: PartSuggestion[]) => {
    const seen = new Set(lines.flatMap((line) => line.part_id ? [line.part_id] : []));
    const added = parts.filter((part) => {
      if (seen.has(part.part_id)) return false;
      seen.add(part.part_id); return true;
    }).map((part): DraftLine => ({ key: `new-${++nextKey.current}`, part_id: part.part_id, pn_raw: part.pn_std,
      description: part.summary || part.description, brand: part.brand, qty: 1, require_exact_pn: false,
      notesVisible: true, field_values: [], originalFields: [], expectedDelivery: "" }));
    if (!added.length) return;
    setLines((previous) => [...previous, ...added]);
    setExpanded([...lines, ...added].filter((line) => lines.length + added.length === 1 || (!line.part_id && !line.description?.trim()))
      .map((line) => line.key));
  };
  const addRawParts = (texts: string[]) => {
    const seen = new Set(lines.map((line) => line.pn_raw.toLocaleUpperCase()));
    const added = texts.map((text) => text.trim()).filter((text) => {
      if (!text || seen.has(text.toLocaleUpperCase())) return false;
      seen.add(text.toLocaleUpperCase()); return true;
    }).map((pn): DraftLine => ({ key: `new-${++nextKey.current}`, part_id: null, pn_raw: pn,
      qty: 1, require_exact_pn: false, notesVisible: true, field_values: [], originalFields: [], expectedDelivery: "" }));
    if (!added.length) return;
    setLines((previous) => [...previous, ...added]);
    setExpanded([...lines, ...added].filter((line) => !line.part_id && !line.description?.trim()).map((line) => line.key));
  };
  const serializeFields = (values: CustomFieldValue[], original: ReadField[] = [], excludeDelivery = false) => {
    const retained = new Set(values.map(fieldKey));
    return [...values, ...original.filter((field) => (!excludeDelivery || field.name !== DELIVERY_FIELD) && field.field_def_id && !retained.has(fieldKey(field)))
      .map((field) => ({ field_def_id: field.field_def_id!, value: null }))];
  };
  const serializeLines = () => lines.map((line) => ({
    ...(line.line_key ? { line_key: line.line_key } : {}), part_id: line.part_id, pn_raw: line.pn_raw,
    description: line.description ?? null, qty: line.qty, require_exact_pn: line.require_exact_pn,
    ref_offer_id: line.ref_offer_id ?? null, ...(line.notesVisible ? { notes: line.notes ?? null } : {}),
    ...(changedAudience(line.notes_audience, line.savedNotesAudience) ? { notes_audience: line.notes_audience } : {}),
    field_values: [...serializeFields(line.field_values, line.originalFields, true),
      ...(line.expectedDelivery || line.deliveryFieldId ? [{ ...(line.deliveryFieldId ? { field_def_id: line.deliveryFieldId } : { new_name: DELIVERY_FIELD }),
        value: line.expectedDelivery || null, ...(changedAudience(line.deliveryAudience, line.savedDeliveryAudience) ? { rule: line.deliveryAudience } : {}) }] : [])],
  }));
  const save = async (submit: boolean) => {
    if (busy || blocked) return;
    if (!lines.length && (submit || append)) { void message.warning("请至少加入一行询价"); return; }
    if (lines.some((line) => !Number.isFinite(line.qty) || line.qty <= 0)) { void message.warning("数量必须大于 0"); return; }
    if (lines.some((line) => line.ref_offer_id && (!referencedOffer(line) || !activeOffer(referencedOffer(line)!, Date.now())))) {
      void message.warning("引用报价已失效，请重新选择有效报价或取消引用"); return;
    }
    setBusy(true);
    try {
      let saved: Inquiry;
      if (append && inquiry) {
        saved = await presales.appendLines(inquiry.id, { expected_version: inquiry.version, lines: serializeLines() });
      } else {
        const payload = { ...(customerVisible && (!inquiry || "customer_id" in inquiry) ? { customer_id: customerId ?? null } : {}),
          ...(!inquiry || "notes" in inquiry ? { notes } : {}),
          ...(!inquiry || "assignee_ids" in inquiry ? { assignee_ids: assigneeIds } : {}),
          lines: serializeLines(), field_values: serializeFields(fields, (inquiry?.field_values || []) as ReadField[]),
          ...(changedAudience(offerAudience, inquiry?.offer_audience) ? { offer_audience: offerAudience } : !inquiry ? { offer_audience: PUBLIC_AUDIENCE } : {}),
          ...(changedAudience(notesAudience, inquiry?.notes_audience) ? { notes_audience: notesAudience } : {}) };
        saved = inquiry ? await presales.updateInquiry(inquiry.id, { ...payload, expected_version: inquiry.version })
          : await presales.createInquiry({ ...payload, kind: "sales_inquiry" });
        if (!mounted.current) return;
        // 保存成功即同步版本与 line_key；提交失败重试时继续使用已保存的草稿身份。
        apply(saved);
        if (submit) saved = saved.status === "revising"
          ? await presales.resubmitInquiry(saved.id, { expected_version: saved.version })
          : await presales.submitInquiry(saved.id, { expected_version: saved.version });
      }
      if (!mounted.current) return;
      void message.success(append ? "已追加询价行" : submit ? "询价已提交" : "草稿已保存");
      navigate(`/presales/inquiries/${saved.id}${submit || append ? "" : "/edit"}`, { replace: !submit && !append });
      if (!submit && !append) apply(saved);
    } catch (error) {
      if (!mounted.current) return;
      if ((error as { response?: { status?: number } })?.response?.status === 409) {
        void message.warning("已被他人修改/领取，已自动刷新");
        const saved = currentInquiry.current;
        if (id) await refresh();
        else if (saved) { try { apply(await presales.getInquiry(saved.id)); } catch { /* 保留已保存草稿身份 */ } }
      } else void message.error(errorText(error, "保存失败，请重试"));
    } finally { if (mounted.current) setBusy(false); }
  };

  if (loading) return <Spin aria-label="加载询价单" />;
  if (loadError) return <Alert type="error" message={loadError} action={<Button onClick={() => void refresh()}>重试</Button>} />;
  const referenceCell = (line: DraftLine) => line.part_id ? line.ref_offer_id ? <span className="sales-inline">
    <span className="sales-success" title={`已引用报价 #${line.ref_offer_id} · 本行不再派单`}>已引用报价 #{line.ref_offer_id} · 本行不再派单</span>
    {(!referencedOffer(line) || !activeOffer(referencedOffer(line)!, now)) && <span className="sales-warning">引用已失效</span>}
    <Popover content={referencedOffer(line) && <OfferBadge offer={referencedOffer(line)!} />}><Button size="small" disabled={busy || blocked}
      onClick={() => updateLine(line.key, { ref_offer_id: null, ref_offer_revision_no: null, ref_offer: undefined })}>取消引用</Button></Popover>
  </span> : <EffectiveOffers offers={library[line.part_id] || []} now={now}
    onReference={busy || blocked ? undefined : (offer) => updateLine(line.key, { ref_offer_id: offer.id })} /> : "—";
  const columns: DataGridColumn<DraftLine>[] = [
    { key: "thumb", title: "图片", width: 40, minWidth: 36, render: (line) => line.part_id ? <PartThumb partId={line.part_id} pn={line.pn_raw} size={24} /> : "—" },
    { key: "pn", title: "PN", width: 150, type: "code", identity: true, value: (line) => line.pn_raw,
      render: (line) => <span className="sales-inline"><span className="sales-mono">{line.pn_raw}</span>{!line.part_id && <span className="sales-warning">待建档</span>}</span> },
    { key: "description", title: "描述 / 规格", width: 240, type: "zh", value: (line) => line.description },
    { key: "qty", title: "数量", width: 84, type: "number", value: (line) => line.qty, render: (line) => <InputNumber size="small"
      aria-label={`${line.pn_raw} 数量`} min={0.001} max={99999999999.999} precision={3} value={line.qty} disabled={busy || blocked}
      onChange={(qty) => updateLine(line.key, { qty: qty ?? 0 })} style={{ width: "100%" }} /> },
    { key: "exact", title: "PN 一致", width: 88, render: (line) => <Checkbox aria-label="要求 PN 一致" checked={line.require_exact_pn}
      disabled={busy || blocked} onChange={(event) => updateLine(line.key, { require_exact_pn: event.target.checked })}>一致</Checkbox> },
    { key: "reference", title: "参考价（30 天）", width: 180, render: (line) => line.part_id ? <span className="sales-price-ref"><PriceRefBadge partId={line.part_id} /></span> : "—" },
    { key: "offers", title: "有效报价", width: 250, render: referenceCell },
    { key: "remove", title: "操作", width: 64, minWidth: 56, render: (line) => <Button size="small" disabled={busy || blocked} type="text"
      aria-label={`移除${line.pn_raw}`} onClick={() => { setLines((previous) => previous.filter((item) => item.key !== line.key));
        setExpanded((previous) => previous.filter((key) => key !== line.key)); }}>删除</Button> },
  ];
  return <div className="sales-page sales-editor">
    <header className="sales-heading"><Button size="small" onClick={() => navigate(inquiry ? `/presales/inquiries/${inquiry.id}` : "/presales/inquiries")}>返回</Button>
      <h1>{append ? "追加询价行" : inquiry ? `编辑询价 ${inquiry.inquiry_no}` : "新建询价"}</h1>
      <span className="sales-muted">{inquiry?.status === "revising" ? "撤回修改中" : "草稿"}</span>
      {!append && !blocked && <><WorkbookExportButton size="small" label="下载本单 Excel" schemaName="presales_inquiry_lines" disabled={busy}
        fetchSchema={() => presalesWorkbooks.getInquirySchema()} filename={`${inquiry?.inquiry_no || "询价空模板"}.xlsx`}
        onExport={(visible) => inquiry ? presalesWorkbooks.exportInquiry(inquiry.id, visible) : presalesWorkbooks.exportInquiryTemplate(visible)} />
        <Button size="small" disabled={busy} onClick={() => setImportOpen(true)}>上传 Excel</Button>
        <span className="sales-muted">下载使用已保存的数据</span></>}
    </header>
    {blocked && <Alert type="warning" message="当前单据不可在此编辑；已提交的销售询价请先撤回修改，关单后请复制为新单。" />}
    {!append && <section className="sales-head-fields" aria-label="询价信息"><Form layout="vertical" size="small" disabled={busy || blocked}>
      <div className="sales-head-row">
        {customerVisible && (!inquiry || "customer_id" in inquiry) && <Form.Item label="客户（可选）">
          <CandidateSelect label="客户" load={presales.customers} value={customerId} onChange={setCustomerId} disabled={busy || blocked} />
        </Form.Item>}
        {(!inquiry || "assignee_ids" in inquiry) && <Form.Item label="处理人（留空进公共池）">
          <Select aria-label="处理人" mode="multiple" maxTagCount="responsive" value={assigneeIds} onChange={setAssigneeIds}
            options={candidates.map((candidate) => ({ value: candidate.id, label: candidate.display_name }))} />
        </Form.Item>}
        {(!inquiry || inquiry.offer_audience) && <Form.Item label="本单报价可见范围"><Popover trigger="click" placement="bottomRight"
          content={<AudiencePicker key={inquiry?.version ?? "new"} savedRule={inquiry?.offer_audience} value={offerAudience} onChange={setOfferAudience} disabled={busy || blocked} />}>
          <Button disabled={busy || blocked} aria-label="设置本单报价可见范围">{({ public: "公开（全体销售）", private: "私密", include: "部分可见", exclude: "不给谁看" })[offerAudience?.mode ?? inquiry?.offer_audience?.mode ?? "public"]}</Button>
        </Popover></Form.Item>}
        <Button type="link" aria-expanded={headOpen} onClick={() => setHeadOpen(!headOpen)}>{headOpen ? "收起客户要求 / 自定义字段" : "客户要求 / 自定义字段"}</Button>
      </div>
      {candidateError && <Alert type="warning" message="处理人候选加载失败，请刷新后再指定；留空仍表示公共池。" />}
      {headOpen && <div className="sales-expanded-fields">
        {(!inquiry || "notes" in inquiry) && <Form.Item label={<Space>客户要求{(!inquiry || inquiry.notes_audience) && <AudienceLock key={inquiry?.version ?? "new"} label="客户要求" savedRule={inquiry?.notes_audience} value={notesAudience} onChange={setNotesAudience} />}</Space>}>
          <Input.TextArea aria-label="客户要求" rows={2} value={notes} onChange={(event) => setNotes(event.target.value)} />
        </Form.Item>}
        <Form.Item label="单头自定义字段"><Fields key={inquiry?.version ?? "new"} level="header" savedValues={inquiry?.field_values} value={fields} onChange={setFields} disabled={busy || blocked} /></Form.Item>
      </div>}
    </Form></section>}
    <div className="sales-search-row">
      {!blocked && !busy && <LivePartSearch mode="multiple" value={[]} onChange={addParts}
        inputProps={{ "aria-label": "搜索并批量加入 PN", placeholder: "输入即搜：PN / 品牌 / 参数 / 描述，支持多选" }}
        renderThumb={(part) => <PartThumb partId={part.part_id} pn={part.pn_std} size={24} />}
        renderBadges={(part) => <PartQuoteBadges partId={part.part_id} />} onAddRaw={(text) => addRawParts([text])} />}
      <Button disabled={busy || blocked} onClick={() => setPasteOpen(true)}>批量粘贴 PN</Button>
      <span className="sales-muted">{append ? "新增行（原有行不变）" : "多于 1 行时详情自动折叠"}</span>
    </div>
    <DataGrid id="presales-inquiry-editor" ariaLabel="询价行" rows={lines} columns={columns} rowKey={(line) => line.key}
      expandedRowKeys={expanded} onExpandedRowKeysChange={setExpanded} emptyText="搜索加入多个 PN，或按原文添加待建档行"
      renderExpanded={(line) => <Form className="sales-line-details" layout="vertical" size="small" disabled={busy || blocked}>
        <Form.Item className="sales-full-width" label="完整描述">{line.part_id ? <div className="sales-full-description">{line.description || "—"}</div>
          : <Input.TextArea rows={2} aria-label={`${line.pn_raw} 描述`} value={line.description || ""}
            placeholder="补充规格、品牌或其他描述，便于采购建档" onChange={(event) => updateLine(line.key, { description: event.target.value })} />}</Form.Item>
        <Form.Item label={<Space>期望交期{(!line.deliveryFieldId || line.savedDeliveryAudience) && <AudienceLock label={`${line.pn_raw} 期望交期`}
          savedRule={line.savedDeliveryAudience} value={line.deliveryAudience} onChange={(rule) => updateLine(line.key, { deliveryAudience: rule })} />}</Space>}>
          <Input aria-label={`${line.pn_raw} 期望交期`} type="date" value={line.expectedDelivery} onChange={(event) => updateLine(line.key, { expectedDelivery: event.target.value })} /></Form.Item>
        {line.notesVisible && <Form.Item label={<Space>行备注{(!line.line_key || line.savedNotesAudience) && <AudienceLock label={`${line.pn_raw} 行备注`} savedRule={line.savedNotesAudience} value={line.notes_audience}
          onChange={(rule) => updateLine(line.key, { notes_audience: rule })} />}</Space>}><Input.TextArea rows={1} aria-label={`${line.pn_raw} 行备注`} value={line.notes || ""} onChange={(event) => updateLine(line.key, { notes: event.target.value })} /></Form.Item>}
        <Form.Item className="sales-full-width sales-compact-fields" label="行自定义字段"><Fields key={`${line.key}:${inquiry?.version ?? "new"}`} level="line" partId={line.part_id} savedValues={line.originalFields}
          value={line.field_values} onChange={(values) => updateLine(line.key, { field_values: values })} disabled={busy || blocked} /></Form.Item>
      </Form>} />
    <footer className="sales-footer"><span><b>{lines.length}</b> 行 · <b>{lines.filter((line) => line.ref_offer_id).length}</b> 行引用报价 · <b className="sales-warning">{lines.filter((line) => !line.part_id).length}</b> 行待建档</span>
      <div className="sales-spacer" />
      {!append && <Button loading={busy} disabled={blocked} onClick={() => void save(false)}>存草稿</Button>}
      <Button type="primary" aria-label={append ? "提交追加行" : inquiry?.status === "revising" ? "重新提交" : "提交"}
        loading={busy} disabled={blocked} onClick={() => void save(true)}>{append ? "提交追加行" : inquiry?.status === "revising" ? "重新提交" : "提交"}</Button>
    </footer>
    <Modal open={pasteOpen} title="批量粘贴 PN" okText="按原文加入" onCancel={() => setPasteOpen(false)} onOk={() => {
      addRawParts(pasteText.split(/[\n\r\t,，;；]+/)); setPasteText(""); setPasteOpen(false);
    }}><p>每行一个 PN，也支持逗号或分号分隔；按原文加入待建档行，相同 PN 不重复加入。</p>
      <Input.TextArea aria-label="批量 PN" rows={6} value={pasteText} onChange={(event) => setPasteText(event.target.value)} /></Modal>
    {importOpen && !blocked && <WorkbookDialog mode="inquiry" inquiryId={inquiry?.id} onClose={() => setImportOpen(false)} onApplied={async (response) => {
      const savedId = (response as { inquiry_id?: number })?.inquiry_id;
      if (inquiry) { await refresh(); setImportOpen(false); }
      else if (typeof savedId === "number" && savedId > 0) navigate(`/presales/inquiries/${savedId}/edit`);
      else throw new Error("草稿编号未返回");
    }} />}
  </div>;
}
