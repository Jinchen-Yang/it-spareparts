import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Alert, App, Button, Checkbox, Descriptions, Drawer, Form, Input, InputNumber, Modal, Popover, Select, Space, Spin, Table, Tag } from "antd";
import { presales, type Inquiry, type InquiryLine, type Offer } from "../../api/presales";
import type { AudienceRule, AudienceRuleRead } from "../../api/audience";
import { type CustomFieldValue } from "../../api/fields";
import { listWorkCandidates, type WorkCandidate } from "../../api/workItems";
import type { PartSuggestion } from "../../api/search";
import LivePartSearch from "../../components/parts/LivePartSearch";
import CreatePartModal from "../../components/parts/CreatePartModal";
import PartThumb from "../../components/gallery/PartThumb";
import PriceRefBadge from "../../components/pricing/PriceRefBadge";
import AudiencePicker, { PUBLIC_AUDIENCE } from "../../components/audience/AudiencePicker";
import AudienceLock from "../../components/audience/AudienceLock";
import PartQuoteBadges from "./PartQuoteBadges";
import { isOfferActive as activeOffer, OfferBadge, useNow, flattenOffers, usePresalesError } from "./shared";
import { formatMoney } from "../../utils/format";
import CandidateSelect from "./CandidateSelect";
import { changedAudience } from "../../components/audience/rules";
import { readPermissionMap } from "../../nav";
import PageHeader from "../../components/PageHeader";
import { type DraftLine, type ReadField, editableFields, fieldKey, DELIVERY_FIELD, errorText, Fields, EffectiveOffers } from "./InquiryFields";
import { WorkbookExportButton } from "../../components/workbook";
import { presalesWorkbooks } from "../../api/presalesWorkbooks";
import WorkbookDialog from "./WorkbookDialog";
import "./presales.css";
import { parseApiError } from "../../utils/apiError";

function fromLine(line: InquiryLine): DraftLine {
  const fields = (line.field_values || []) as ReadField[];
  return { key: line.line_key, line_key: line.line_key, part_id: line.part_id ?? null, pn_raw: line.pn_raw,
    description: line.description, qty: Number(line.qty), require_exact_pn: !!line.require_exact_pn,
    ref_offer_id: line.ref_offer_id, ref_offer_revision_no: line.ref_offer_revision_no, ref_offer: line.ref_offer, notes: line.notes, notesVisible: "notes" in line, field_values: editableFields(fields, true), originalFields: fields,
    savedNotesAudience: line.notes_audience, expectedDelivery: line.expected_delivery ?? "" };
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
  const { message, modal } = App.useApp();
  const handleError = usePresalesError();
  const [importOpen, setImportOpen] = useState(false);
  const [creatingPart, setCreatingPart] = useState<{ pn: string; lineKey?: string; description?: string | null }>();
  const [versionConflict, setVersionConflict] = useState(false);
  const canCreatePart = localStorage.getItem("role") === "admin" || readPermissionMap().page_master_data === true;
  const customerVisible = localStorage.getItem("role") === "admin" || readPermissionMap().data_customer !== false;
  const [inquiry, setInquiry] = useState<Inquiry>();
  const [lines, setLines] = useState<DraftLine[]>([]);
  const [editing, setEditing] = useState<string>();
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
    setVersionConflict(false);
    currentInquiry.current = data;
    const next = append ? [] : (data.lines || []).map(fromLine);
    setInquiry(data); setLines(next);
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
    expected_delivery: line.expectedDelivery || null,
    field_values: serializeFields(line.field_values, line.originalFields, true),
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
      // 冲突时保留草稿，不用服务端内容覆盖尚未保存的行与字段。
      if (parseApiError(error).kind === "version_conflict") setVersionConflict(true);
      await handleError(error);
    } finally { if (mounted.current) setBusy(false); }
  };

  if (loading) return <Spin aria-label="加载询价单" />;
  if (loadError) return <Alert type="error" message={loadError} action={<Button onClick={() => void refresh()}>重试</Button>} />;
  const line = lines.find((item) => item.key === editing);
  return <section className="presales-page">
    <PageHeader title={append ? "追加询价行" : inquiry ? `编辑询价 ${inquiry.inquiry_no}` : "新建询价"} subtitle="选择型号并填写需求，提交后进入采购待办" extra={<Button onClick={() => navigate(inquiry ? `/presales/inquiries/${inquiry.id}` : "/presales/inquiries")}>返回</Button>} />
    <div className="presales-panel">
      {versionConflict && <Alert type="warning" showIcon message="记录版本已变化，当前填写内容仍保留。"
        description="请先复制需要保留的修改，再重新加载最新单据。"
        action={<Button onClick={() => modal.confirm({ title: "放弃当前未保存修改并重新加载？", okText: "重新加载", cancelText: "继续保留",
          onOk: async () => {
            if (id) await refresh();
            else if (currentInquiry.current) {
              try { apply(await presales.getInquiry(currentInquiry.current.id)); }
              catch (error) { await handleError(error); }
            }
          } })}>重新加载最新单据</Button>} />}
      {blocked && <Alert type="warning" message="当前单据不可在此编辑；已提交的销售询价请先撤回修改，关单后请复制为新单。" />}
      {!append && (blocked ? <Descriptions column={1} items={[
        ...("customer_id" in (inquiry ?? {}) ? [{ key: "customer", label: "客户", children: inquiry?.customer_name ?? customerId ?? "—" }] : []),
        ...("notes" in (inquiry ?? {}) ? [{ key: "notes", label: "客户要求", children: notes }] : []),
      ]} /> : <Form layout="vertical" disabled={busy}>
        <div className="presales-form">
          {customerVisible && (!inquiry || "customer_id" in inquiry) && <Form.Item label="客户（可选）"><CandidateSelect label="客户" load={presales.customers} value={customerId} onChange={setCustomerId} disabled={busy} /></Form.Item>}
          {(!inquiry || "assignee_ids" in inquiry) && <Form.Item label="处理人（留空进公共池）"><Select aria-label="处理人" mode="multiple" value={assigneeIds} onChange={setAssigneeIds}
            options={candidates.map((candidate) => ({ value: candidate.id, label: candidate.display_name }))} /></Form.Item>}
          {(!inquiry || inquiry.offer_audience) && <Form.Item label="本单报价可见范围"><Popover trigger="click" content={<AudiencePicker key={inquiry?.version ?? "new"} savedRule={inquiry?.offer_audience} value={offerAudience} onChange={setOfferAudience} disabled={busy} />}>
            <Button aria-label="设置本单报价可见范围">{({ public: "公开（全体销售）", private: "私密", include: "部分可见", exclude: "不给谁看" })[offerAudience?.mode ?? inquiry?.offer_audience?.mode ?? "public"]}</Button>
          </Popover></Form.Item>}
        </div>
        <Button type="link" aria-expanded={headOpen} onClick={() => setHeadOpen(!headOpen)}>{headOpen ? "收起客户要求 / 自定义字段" : "客户要求 / 自定义字段"}</Button>
        {candidateError && <Alert type="warning" message="处理人候选加载失败，请刷新后再指定；留空仍表示公共池。" />}
        {headOpen && <>
          {(!inquiry || "notes" in inquiry) && <Form.Item label={<Space>客户要求{(!inquiry || inquiry.notes_audience) && <AudienceLock key={inquiry?.version ?? "new"} label="客户要求" savedRule={inquiry?.notes_audience} value={notesAudience} onChange={setNotesAudience} />}</Space>}>
            <Input.TextArea aria-label="客户要求" value={notes} onChange={(event) => setNotes(event.target.value)} /></Form.Item>}
          <Form.Item label="单头自定义字段"><Fields key={inquiry?.version ?? "new"} level="header" savedValues={inquiry?.field_values} value={fields} onChange={setFields} disabled={busy} /></Form.Item>
        </>}
      </Form>)}
      <div className="presales-toolbar">
        {append && <span className="presales-muted">新增行（原有行不变）</span>}
        {!blocked && <LivePartSearch mode="multiple" value={[]} onChange={addParts} inputProps={{ "aria-label": "搜索并批量加入 PN", placeholder: "PN / 品牌 / 参数 / 描述，支持多选", disabled: busy }}
          renderThumb={(part) => <PartThumb partId={part.part_id} pn={part.pn_std} size={24} />} renderBadges={(part) => <PartQuoteBadges partId={part.part_id} />} onAddRaw={(text) => addRawParts([text])}
          onCreate={canCreatePart ? (pn) => setCreatingPart({ pn }) : undefined} />}
        <Button disabled={busy || blocked} onClick={() => setPasteOpen(true)}>批量粘贴 PN</Button>
        {!append && !blocked && <><WorkbookExportButton label="下载本单 Excel" schemaName="presales_inquiry_lines" disabled={busy}
          fetchSchema={() => presalesWorkbooks.getInquirySchema(undefined, inquiry?.id)} filename={`${inquiry?.inquiry_no || "询价空模板"}.xlsx`}
          onExport={(visible) => inquiry ? presalesWorkbooks.exportInquiry(inquiry.id, visible) : presalesWorkbooks.exportInquiryTemplate(visible)} />
          <Button disabled={busy} onClick={() => setImportOpen(true)}>上传 Excel</Button><span className="presales-muted">下载使用已保存的数据</span></>}
      </div>
      <Table aria-label="询价行" rowKey="key" dataSource={lines} tableLayout="fixed" scroll={{ x: 920 }} pagination={{ defaultPageSize: 20 }} columns={[
        { title: "PN / 描述", key: "pn", render: (_, item) => <><Space>{item.part_id && <PartThumb partId={item.part_id} pn={item.pn_raw} size={24} />}{item.pn_raw}{!item.part_id && <span>待建档</span>}</Space><div className="presales-muted">{item.description}</div></> },
        { title: "数量", dataIndex: "qty", width: 90 },
        { title: "期望交期", key: "delivery", width: 130, render: (_, item) => item.expectedDelivery || "—" },
        { title: "PN 一致", key: "exact", width: 100, render: (_, item) => item.require_exact_pn ? "一致" : "—" },
        { title: "报价引用", key: "reference", width: 200, render: (_, item) => item.ref_offer_id ? <>{`已引用报价 #${item.ref_offer_id} · 本行不再派单`}{(!referencedOffer(item) || !activeOffer(referencedOffer(item)!, now)) && <Tag>引用已失效</Tag>}</> : item.part_id ? `${flattenOffers(library[item.part_id] ?? []).filter((offer) => activeOffer(offer, now)).length} 条有效报价` : "—" },
        { title: "操作", key: "actions", width: 150, render: (_, item) => <Space><Button type="link" aria-label={`编辑 ${item.pn_raw}`} onClick={() => setEditing(item.key)}>{blocked ? "查看" : "编辑"}</Button>
          {!blocked && <Button type="link" disabled={busy} aria-label={`移除${item.pn_raw}`} onClick={() => setLines((previous) => previous.filter((other) => other.key !== item.key))}>删除</Button>}</Space> },
      ]} locale={{ emptyText: "搜索加入多个 PN，或按原文添加待建档行" }} />
    </div>
    <footer className="presales-footer"><span>{lines.length} 行 · {lines.filter((item) => item.ref_offer_id).length} 行引用报价 · {lines.filter((item) => !item.part_id).length} 行待建档</span>
      {!blocked && <Space>{!append && <Button loading={busy} onClick={() => void save(false)}>存草稿</Button>}<Button type="primary" aria-label={append ? "提交追加行" : inquiry?.status === "revising" ? "重新提交" : "提交"}
        loading={busy} onClick={() => void save(true)}>{append ? "提交追加行" : inquiry?.status === "revising" ? "重新提交" : "提交"}</Button></Space>}
    </footer>
    <Drawer title={line ? `${line.pn_raw} · 询价需求` : "询价需求"} open={!!line} width={640} onClose={() => setEditing(undefined)} destroyOnHidden>
      {line && (blocked ? <Descriptions column={1} items={[{ key: "description", label: "描述", children: line.description }, { key: "qty", label: "数量", children: line.qty },
        { key: "delivery", label: "期望交期", children: line.expectedDelivery }, ...(line.notesVisible ? [{ key: "notes", label: "行备注", children: line.notes }] : []),
        ...line.originalFields.map((field, index) => ({ key: `field-${index}`, label: field.name ?? field.new_name, children: String(field.value ?? "") })),
      ]} /> : <Form layout="vertical" disabled={busy}>
        {!line.part_id && canCreatePart && <Button style={{ marginBottom: 16 }} onClick={() => setCreatingPart({ pn: line.pn_raw, lineKey: line.key, description: line.description })}>为此 PN 建档</Button>}
        <Form.Item label="完整描述">{line.part_id ? <p>{line.description || "—"}</p> : <Input.TextArea aria-label={`${line.pn_raw} 描述`} value={line.description ?? ""} onChange={(event) => updateLine(line.key, { description: event.target.value })} />}</Form.Item>
        <div className="presales-form"><Form.Item label="数量" required><InputNumber aria-label={`${line.pn_raw} 数量`} min={0.001} max={99999999999.999} precision={3} value={line.qty} onChange={(qty) => updateLine(line.key, { qty: qty ?? 0 })} /></Form.Item>
          <Form.Item label="期望交期"><Input type="date" aria-label={`${line.pn_raw} 期望交期`} value={line.expectedDelivery} onChange={(event) => updateLine(line.key, { expectedDelivery: event.target.value })} /></Form.Item>
          <Form.Item><Checkbox aria-label="要求 PN 一致" checked={line.require_exact_pn} onChange={(event) => updateLine(line.key, { require_exact_pn: event.target.checked })}>要求 PN 一致</Checkbox></Form.Item></div>
        {line.notesVisible && <Form.Item label={<Space>行备注{(!line.line_key || line.savedNotesAudience) && <AudienceLock label={`${line.pn_raw} 行备注`} savedRule={line.savedNotesAudience} value={line.notes_audience} onChange={(rule) => updateLine(line.key, { notes_audience: rule })} />}</Space>}>
          <Input.TextArea aria-label={`${line.pn_raw} 行备注`} value={line.notes ?? ""} onChange={(event) => updateLine(line.key, { notes: event.target.value })} /></Form.Item>}
        <Form.Item label="行自定义字段"><Fields key={`${line.key}:${inquiry?.version ?? "new"}`} level="line" partId={line.part_id} savedValues={line.originalFields} value={line.field_values} onChange={(values) => updateLine(line.key, { field_values: values })} disabled={busy} /></Form.Item>
        {line.part_id && <><h3>参考价（30 天）</h3><PriceRefBadge partId={line.part_id} /><h3>有效报价</h3>{line.ref_offer_id ? <><p>已引用报价 #{line.ref_offer_id}</p>{referencedOffer(line) && <OfferBadge offer={referencedOffer(line)!} />}<Button onClick={() => updateLine(line.key, { ref_offer_id: null, ref_offer_revision_no: null, ref_offer: undefined })}>取消引用</Button></>
          : <EffectiveOffers offers={library[line.part_id] ?? []} now={now} onReference={(offer) => updateLine(line.key, { ref_offer_id: offer.id })} />}</>}
      </Form>)}
    </Drawer>
    {creatingPart && canCreatePart && <CreatePartModal pn={creatingPart.pn} description={creatingPart.description}
      onClose={() => setCreatingPart(undefined)} onSelected={(part) => {
        if (creatingPart.lineKey) updateLine(creatingPart.lineKey, { part_id: part.part_id, pn_raw: part.pn_std,
          description: part.description ?? part.summary, brand: part.brand });
        else addParts([part]);
      }} />}
    <Modal open={pasteOpen} title="批量粘贴 PN" okText="按原文加入" onCancel={() => setPasteOpen(false)} onOk={() => { addRawParts(pasteText.split(/[\n\r\t,，;；]+/)); setPasteText(""); setPasteOpen(false); }}>
      <p>每行一个 PN，也支持逗号或分号分隔；相同 PN 不重复加入。</p><Input.TextArea aria-label="批量 PN" rows={6} value={pasteText} onChange={(event) => setPasteText(event.target.value)} />
    </Modal>
    {importOpen && !blocked && <WorkbookDialog mode="inquiry" inquiryId={inquiry?.id} onClose={() => setImportOpen(false)} onApplied={async (response) => {
      const savedId = (response as { inquiry_id?: number })?.inquiry_id;
      if (inquiry) { await refresh(); setImportOpen(false); }
      else if (typeof savedId === "number" && savedId > 0) navigate(`/presales/inquiries/${savedId}/edit`);
      else throw new Error("草稿编号未返回");
    }} />}
  </section>;
}
