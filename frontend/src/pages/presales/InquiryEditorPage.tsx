import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Alert, App, Button, Card, Checkbox, Col, Empty, Form, Input, InputNumber, Row, Select, Space, Spin, Tag, Typography } from "antd";
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
import OfferGroups from "./OfferGroups";
import CandidateSelect from "./CandidateSelect";
import { changedAudience } from "../../components/audience/rules";
import { readPermissionMap } from "../../nav";

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
  return <OfferGroups offers={offers.filter((offer) => activeOffer(offer, now))} renderOffer={(offer) => <Space key={offer.id} size={2}>
    <OfferBadge offer={offer} />
    {onReference && <Button size="small" onClick={() => onReference(offer)}>直接引用</Button>}
  </Space>} />;
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
  const customerVisible = localStorage.getItem("role") === "admin" || readPermissionMap().data_customer !== false;
  const [inquiry, setInquiry] = useState<Inquiry>();
  const [lines, setLines] = useState<DraftLine[]>([]);
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
    setInquiry(data); setLines(append ? [] : (data.lines || []).map(fromLine));
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
  const addParts = (parts: PartSuggestion[]) => setLines((previous) => [...previous, ...parts
    .filter((part) => !previous.some((line) => line.part_id === part.part_id)).map((part) => ({
      key: `new-${++nextKey.current}`, part_id: part.part_id, pn_raw: part.pn_std, description: part.summary || part.description,
      brand: part.brand, qty: 1, require_exact_pn: false, notesVisible: true, field_values: [], originalFields: [], expectedDelivery: "",
    }))]);
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
  return <Space direction="vertical" size="large" style={{ width: "100%" }}>
    <Space wrap style={{ justifyContent: "space-between", width: "100%" }}>
      <Typography.Title level={3} style={{ margin: 0 }}>{append ? "追加询价行" : inquiry ? `编辑询价 ${inquiry.inquiry_no}` : "新建询价"}</Typography.Title>
      <Button onClick={() => navigate(inquiry ? `/presales/inquiries/${inquiry.id}` : "/presales/inquiries")}>返回</Button>
    </Space>
    {blocked && <Alert type="warning" message="当前单据不可在此编辑；已提交的销售询价请先撤回修改，关单后请复制为新单。" />}
    {!append && <Card title="询价信息"><Form layout="vertical" disabled={busy || blocked}>
      {customerVisible && (!inquiry || "customer_id" in inquiry) && <Form.Item label="客户（可选）">
        <CandidateSelect label="客户" load={presales.customers} value={customerId} onChange={setCustomerId} disabled={busy || blocked} />
      </Form.Item>}
      {(!inquiry || "notes" in inquiry) && <Form.Item label={<Space>客户要求{(!inquiry || inquiry.notes_audience) && <AudienceLock key={inquiry?.version ?? "new"} label="客户要求" savedRule={inquiry?.notes_audience} value={notesAudience} onChange={setNotesAudience} />}</Space>}>
        <Input.TextArea aria-label="客户要求" rows={3} value={notes} onChange={(event) => setNotes(event.target.value)} />
      </Form.Item>}
      <Form.Item label="单头自定义字段"><Fields key={inquiry?.version ?? "new"} level="header" savedValues={inquiry?.field_values} value={fields} onChange={setFields} disabled={busy || blocked} /></Form.Item>
      {(!inquiry || "assignee_ids" in inquiry) && <Form.Item label="处理人（多选）" extra="留空表示放入公共池，采购领取后处理。">
        <Select aria-label="处理人" mode="multiple" value={assigneeIds} onChange={setAssigneeIds}
          options={candidates.map((candidate) => ({ value: candidate.id, label: candidate.display_name }))} />
        {candidateError && <Alert type="warning" message="处理人候选加载失败，请刷新后再指定；留空仍表示公共池。" />}
      </Form.Item>}
      {(!inquiry || inquiry.offer_audience) && <Form.Item label="本单报价其他销售可见" extra={inquiry ? "未调整时保留本单原有可见性。" : "默认公开。"}>
        <AudiencePicker key={inquiry?.version ?? "new"} savedRule={inquiry?.offer_audience} value={offerAudience} onChange={setOfferAudience} disabled={busy || blocked} />
      </Form.Item>}
    </Form></Card>}
    <Card title={append ? "新增行（原有行不变）" : "询价行"}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {!blocked && !busy && <LivePartSearch mode="multiple" value={[]} onChange={addParts}
          inputProps={{ "aria-label": "搜索并批量加入 PN", placeholder: "搜索 PN、品牌、参数或描述，点击候选加入" }}
          renderThumb={(part) => <PartThumb partId={part.part_id} pn={part.pn_std} size={40} />}
          renderBadges={(part) => <PartQuoteBadges partId={part.part_id} />}
          onAddRaw={(text) => setLines((previous) => [...previous, { key: `new-${++nextKey.current}`, part_id: null,
            pn_raw: text, qty: 1, require_exact_pn: false, notesVisible: true, field_values: [], originalFields: [], expectedDelivery: "" }])} />}
        {!lines.length && <Empty description="搜索加入多个 PN，或按原文添加待建档行" />}
        {lines.map((line, index) => <Card size="small" key={line.key} title={<Space wrap>
          {line.part_id && <PartThumb partId={line.part_id} pn={line.pn_raw} />}
          <span>{index + 1}. {line.pn_raw}</span>{line.brand && <Tag>{line.brand}</Tag>}
          {!line.part_id && <Tag color="orange">待建档</Tag>}
          {line.part_id && <PriceRefBadge partId={line.part_id} />}
        </Space>} extra={<Button disabled={busy || blocked} type="text" danger aria-label={`移除${line.pn_raw}`}
          onClick={() => setLines((previous) => previous.filter((item) => item.key !== line.key))}>移除</Button>}>
          <Form layout="vertical" disabled={busy || blocked}>
            {line.description && <Typography.Paragraph type="secondary">{line.description}</Typography.Paragraph>}
            <Row gutter={16}>
              <Col xs={24} sm={8}><Form.Item label="数量"><InputNumber aria-label={`${line.pn_raw} 数量`} min={0.001} max={99999999999.999} precision={3}
                value={line.qty} onChange={(qty) => updateLine(line.key, { qty: qty ?? 0 })} style={{ width: "100%" }} /></Form.Item></Col>
              <Col xs={24} sm={8}><Form.Item label={<Space>期望交期（自定义字段）{(!line.deliveryFieldId || line.savedDeliveryAudience) && <AudienceLock label={`${line.pn_raw} 期望交期`}
                savedRule={line.savedDeliveryAudience} value={line.deliveryAudience} onChange={(rule) => updateLine(line.key, { deliveryAudience: rule })} />}</Space>}><Input aria-label={`${line.pn_raw} 期望交期`} type="date"
                value={line.expectedDelivery} onChange={(event) => updateLine(line.key, { expectedDelivery: event.target.value })} /></Form.Item></Col>
              <Col xs={24} sm={8}><Form.Item label="PN 要求"><Checkbox checked={line.require_exact_pn}
                onChange={(event) => updateLine(line.key, { require_exact_pn: event.target.checked })}>要求 PN 一致</Checkbox></Form.Item></Col>
            </Row>
            {line.part_id && <Form.Item label="有效报价">
              {line.ref_offer_id ? <Space wrap><Tag color="green">已引用报价 #{line.ref_offer_id} · 本行不再派单</Tag>
                {referencedOffer(line) && <OfferBadge offer={referencedOffer(line)!} />}
                {(!referencedOffer(line) || !activeOffer(referencedOffer(line)!, now)) && <Tag color="red">引用已失效</Tag>}
                <Button onClick={() => updateLine(line.key, { ref_offer_id: null, ref_offer_revision_no: null, ref_offer: undefined })}>取消引用</Button></Space>
                : <EffectiveOffers offers={library[line.part_id] || []} now={now} onReference={(offer) => updateLine(line.key, { ref_offer_id: offer.id })} />}
            </Form.Item>}
            {line.notesVisible && <Form.Item label={<Space>行备注{(!line.line_key || line.savedNotesAudience) && <AudienceLock label={`${line.pn_raw} 行备注`} savedRule={line.savedNotesAudience} value={line.notes_audience}
              onChange={(rule) => updateLine(line.key, { notes_audience: rule })} />}</Space>}>
              <Input.TextArea aria-label={`${line.pn_raw} 行备注`} value={line.notes || ""} onChange={(event) => updateLine(line.key, { notes: event.target.value })} />
            </Form.Item>}
            <Form.Item label="行自定义字段"><Fields key={`${line.key}:${inquiry?.version ?? "new"}`} level="line" partId={line.part_id} savedValues={line.originalFields}
              value={line.field_values} onChange={(values) => updateLine(line.key, { field_values: values })} disabled={busy || blocked} /></Form.Item>
          </Form>
        </Card>)}
      </Space>
    </Card>
    <Space wrap>
      {!append && <Button loading={busy} disabled={blocked} onClick={() => void save(false)}>存草稿</Button>}
      <Button type="primary" aria-label={append ? "提交追加行" : inquiry?.status === "revising" ? "重新提交" : "提交"}
        loading={busy} disabled={blocked} onClick={() => void save(true)}>{append ? "提交追加行" : inquiry?.status === "revising" ? "重新提交" : "提交"}</Button>
    </Space>
  </Space>;
}
