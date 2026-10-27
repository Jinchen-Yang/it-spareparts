import { useEffect, useState, type Key } from "react";
import { Alert, App, Button, Input, InputNumber, Select, Space, Spin } from "antd";
import { Link, useSearchParams } from "react-router-dom";
import { presales, type ExistingChoice, type Inquiry, type InquiryLine, type Offer, type OfferInput } from "../../api/presales";
import { listWorkCandidates, type WorkCandidate } from "../../api/workItems";
import { getFieldTemplate, type CustomFieldValue, type FieldDefinition, type FieldLevel } from "../../api/fields";
import type { PartSuggestion } from "../../api/search";
import LivePartSearch from "../../components/parts/LivePartSearch";
import PartThumb from "../../components/gallery/PartThumb";
import CustomFieldsEditor from "../../components/fields/CustomFieldsEditor";
import AudiencePicker, { PUBLIC_AUDIENCE } from "../../components/audience/AudiencePicker";
import AudienceLock from "../../components/audience/AudienceLock";
import type { AudienceRule } from "../../api/audience";
import { refreshWorkBadges } from "../../components/workflow/useWorkBadge";
import { offerInput, quoteComplete } from "./QuoteEditor";
import PartQuoteBadges from "./PartQuoteBadges";
import { usePresalesError, isOfferActive, useNow, expiryLabel, flattenOffers } from "./shared";
import QuoteGrid from "./QuoteGrid";
import { existingOfferConflict, useExistingOfferChoice } from "./ExistingOfferDialog";

function DraftFields({ level, partId, value, onChange }: {
  level: FieldLevel; partId?: number; value: CustomFieldValue[]; onChange: (next: CustomFieldValue[]) => void;
}) {
  const [template, setTemplate] = useState<FieldDefinition[]>([]);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setFailed(false);
    getFieldTemplate("presales_inquiry", level, partId ? `part:${partId}` : undefined, controller.signal)
      .then((rows) => { if (!controller.signal.aborted) setTemplate(rows); })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [partId, level]);
  return <>
    {failed && <Alert type="warning" message="字段池加载失败，仍可手动添加字段" />}
    <CustomFieldsEditor key={`${level}:${partId ?? "header"}`} subjectType="presales_inquiry" level={level}
      template={template} value={value} onChange={onChange} />
  </>;
}

export default function OfferNewPage() {
  const [params] = useSearchParams();
  const draftId = Number(params.get("draft"));
  return <OfferNewContent key={params.get("draft") ?? "new"} draftId={draftId} />;
}

function OfferNewContent({ draftId }: { draftId: number }) {
  const handlePresalesError = usePresalesError();
  const { choose, dialog } = useExistingOfferChoice();
  const { message } = App.useApp();
  const [expanded, setExpanded] = useState<Key[]>([]);
  const [validity, setValidity] = useState(1);
  const [extra, setExtra] = useState(false);
  const [selected, setSelected] = useState<PartSuggestion[]>([]);
  const [notes, setNotes] = useState("");
  const [audience, setAudience] = useState<AudienceRule>(PUBLIC_AUDIENCE);
  const [notesAudience, setNotesAudience] = useState<AudienceRule>(PUBLIC_AUDIENCE);
  const [headerFields, setHeaderFields] = useState<CustomFieldValue[]>([]);
  const [lineFields, setLineFields] = useState<Record<number, CustomFieldValue[]>>({});
  const [inquiry, setInquiry] = useState<Inquiry>();
  const [values, setValues] = useState<Record<number, OfferInput>>({});
  const [saved, setSaved] = useState<Record<number, Offer>>({});
  const [library, setLibrary] = useState<Record<number, Offer[]>>({});
  const [noticeIds, setNoticeIds] = useState<number[]>([]);
  const [candidates, setCandidates] = useState<WorkCandidate[]>([]);
  const [candidateError, setCandidateError] = useState(false);
  const [libraryError, setLibraryError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [published, setPublished] = useState(false);
  const [draftLoading, setDraftLoading] = useState(!!draftId);
  const [draftError, setDraftError] = useState(false);
  const now = useNow(Object.values(library).flatMap(flattenOffers).flatMap((offer) => [offer.valid_from, offer.valid_until]));
  const partKey = selected.map((part) => part.part_id).join(",");

  useEffect(() => {
    if (!draftId) return;
    let cancelled = false;
    setDraftLoading(true); setDraftError(false);
    presales.getInquiry(draftId).then((draft) => {
      if (cancelled) return;
      if (draft.kind !== "proactive_offer") throw new Error("请选择主动报价草稿");
      setInquiry(draft);
      setSelected((draft.lines ?? []).filter((line) => line.part_id).map((line) => ({
        part_id: line.part_id!, pn_std: line.pn_raw, description: line.description,
        match_type: "exact_pn", match_reason: "报价草稿", matched_terms: [],
      })));
      const offers = draft.offers ?? [];
      setValidity(offers[0]?.validity_days ?? 1);
      setSaved(Object.fromEntries(offers.map((offer) => [offer.line_id, offer])));
      setValues(Object.fromEntries((draft.lines ?? []).map((line) => [line.id,
        offerInput(offers.find((offer) => offer.line_id === line.id))])));
      setPublished(draft.status === "submitted");
    }).catch((error) => { if (!cancelled) { setDraftError(true); void handlePresalesError(error); } })
      .finally(() => { if (!cancelled) setDraftLoading(false); });
    return () => { cancelled = true; };
  }, [draftId]);

  useEffect(() => {
    const controller = new AbortController();
    listWorkCandidates("presales_offer_notice", controller.signal).then((data) => {
      if (!controller.signal.aborted) setCandidates(data.items);
    }).catch(() => { if (!controller.signal.aborted) setCandidateError(true); });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    let cancelled = false;
    setLibraryError(false);
    if (!partKey) { setLibrary({}); return; }
    presales.library(partKey.split(",").map(Number)).then((data) => { if (!cancelled) setLibrary(data); })
      .catch(() => { if (!cancelled) { setLibrary({}); setLibraryError(true); } });
    return () => { cancelled = true; };
  }, [partKey]);

  const create = async () => {
    setBusy(true);
    try {
      const draft = await presales.createInquiry({ kind: "proactive_offer", notes, notes_audience: notesAudience, offer_audience: audience,
        field_values: headerFields, lines: selected.map((part) => ({ part_id: part.part_id, pn_raw: part.pn_std,
          description: part.description, qty: 1, field_values: lineFields[part.part_id] ?? [] })) });
      setInquiry(draft);
      setValues(Object.fromEntries((draft.lines ?? []).map((line) => [line.id, values[-line.part_id!] ?? { ...offerInput(), validity_days: validity }])));
      setExpanded([]);
      message.success("报价草稿已生成，请逐行填写后发布");
    } catch (error) { await handlePresalesError(error); }
    finally { setBusy(false); }
  };
  const refresh = async () => {
    if (!inquiry) return;
    const latest = await presales.getInquiry(inquiry.id);
    setInquiry(latest);
    const offers = latest.offers ?? [];
    setSaved(Object.fromEntries(offers.map((offer) => [offer.line_id, offer])));
    setValues(Object.fromEntries((latest.lines ?? []).map((line) => [line.id,
      offerInput(offers.find((offer) => offer.line_id === line.id))])));
    if (latest.status === "submitted") setPublished(true);
  };
  const saveDraft = async () => {
    if (!inquiry) return;
    setBusy(true);
    try {
      for (const line of inquiry.lines ?? []) {
        const existing = saved[line.id];
        if (!quoteComplete(values[line.id] ?? {})) continue;
        const offer = await presales.saveOffer(inquiry.id, line.id, { ...values[line.id],
          expected_version: existing?.version ?? inquiry.version, ...(existing ? { offer_id: existing.id } : {}) });
        setSaved((previous) => ({ ...previous, [line.id]: offer }));
      }
      message.success("报价草稿已保存");
    } catch (error) { await handlePresalesError(error, refresh); }
    finally { setBusy(false); }
  };
  const publish = async () => {
    if (!inquiry) return;
    setBusy(true);
    try {
      for (const line of inquiry.lines ?? []) {
        const existing = saved[line.id];
        const offer = await presales.saveOffer(inquiry.id, line.id, { ...values[line.id],
          expected_version: existing?.version ?? inquiry.version, ...(existing ? { offer_id: existing.id } : {}) });
        setSaved((previous) => ({ ...previous, [line.id]: offer }));
      }
      const choices: Record<number, ExistingChoice> = {};
      for (;;) {
        try {
          await presales.publishInquiry(inquiry.id, { expected_version: inquiry.version, notice_user_ids: noticeIds,
            ...(Object.keys(choices).length ? { existing_choices: choices } : {}) });
          break;
        } catch (error) {
          const conflict = existingOfferConflict(error);
          if (!conflict) throw error;
          const choice = await choose(conflict);
          if (!choice) return;
          choices[conflict.draft_offer_id] = choice;
        }
      }
      setPublished(true); refreshWorkBadges(); message.success("主动报价已发布");
    } catch (error) { await handlePresalesError(error, refresh); }
    finally { setBusy(false); }
  };
  const complete = !!inquiry?.lines?.length && inquiry.lines.every((line) => quoteComplete(values[line.id] ?? {}));

  if (draftLoading) return <Spin />;
  if (draftError) return <Alert type="error" message="主动报价草稿加载失败，请返回询价单列表重试" />;
  if (published && inquiry) return <Alert type="success" message="主动报价已发布" description={<Space>
    <Link to={`/presales/inquiries/${inquiry.id}`}>查看报价单</Link>
    <Link to={`/presales/tasks/${inquiry.id}`}>更新 / 撤销报价</Link><Link to="/presales/library">查看报价库</Link>
  </Space>} />;

  const lines: InquiryLine[] = inquiry?.lines ?? selected.map((part, index) => ({
    id: -part.part_id, inquiry_id: 0, line_key: `part-${part.part_id}`, seq: index + 1, part_id: part.part_id,
    pn_raw: part.pn_std, description: part.description, qty: 1, status: "active", version: 0,
  }));
  const changeValue = (lineId: number, value: OfferInput) => setValues((previous) => ({ ...previous, [lineId]: value }));
  const dayValues = lines.map((line) => values[line.id]?.validity_days ?? validity);
  const uniformDays = dayValues.length ? dayValues.every((days) => days === dayValues[0]) ? dayValues[0] : undefined : validity;
  return <section className="quote-offer-page quote-document">
    <header className="quote-heading"><h2>主动报价{inquiry ? ` · ${inquiry.inquiry_no}` : ""}</h2><Link to="/presales/workbench">报价工作台</Link>
      <div className="quote-validity"><span>统一有效期</span><InputNumber size="small" aria-label="统一有效期" min={1} max={36500} precision={0}
        value={uniformDays ?? null} placeholder="多值" title="修改后应用到所有报价行" disabled={busy} onChange={(days) => {
          const next = days ?? 1; setValidity(next);
          setValues((previous) => ({ ...previous, ...Object.fromEntries(lines.map((line) => [line.id,
            { ...(previous[line.id] ?? offerInput()), validity_days: next }])) }));
        }} /><span role="status">{uniformDays === undefined ? "各行有效期不同；修改后统一应用" : expiryLabel(uniformDays, now)}</span></div>
      <Button size="small" onClick={() => setExtra(!extra)}>{extra ? "收起单头信息" : "单头备注 / 自定义字段"}</Button>
    </header>
    {!inquiry && <div className="quote-offer-tools"><LivePartSearch mode="multiple" value={selected}
      onChange={(parts) => { setSelected(parts); setExpanded([]); }} inputProps={{ "aria-label": "主动报价检索 PN" }}
      renderThumb={(part) => <PartThumb partId={part.part_id} pn={part.pn_std} size={24} />}
      renderBadges={(part) => <PartQuoteBadges partId={part.part_id} />} />
      <div><span className="quote-muted">本单报价可见性</span><AudiencePicker value={audience} onChange={setAudience} /></div>
    </div>}
    {extra && !inquiry && <div className="quote-offer-extra"><Space>单头备注<AudienceLock label="单头备注" value={notesAudience} onChange={setNotesAudience} /></Space>
      <Input.TextArea aria-label="主动报价单头备注" value={notes} onChange={(event) => setNotes(event.target.value)} />
      <DraftFields level="header" value={headerFields} onChange={setHeaderFields} /></div>}
    <QuoteGrid id="presales-proactive-lines" proactive rows={lines.map((line) => ({ line,
      value: values[line.id] ?? { ...offerInput(), validity_days: validity }, current: saved[line.id], disabled: busy,
      existing: library[line.part_id!] ?? [],
      details: <>{!inquiry && <DraftFields level="line" partId={line.part_id!} value={lineFields[line.part_id!] ?? []}
        onChange={(next) => setLineFields((previous) => ({ ...previous, [line.part_id!]: next }))} />}
        {flattenOffers(library[line.part_id!] ?? []).some((offer) => isOfferActive(offer, now)) && <Alert type="info"
          message={inquiry && flattenOffers(library[line.part_id!] ?? []).some((offer) => isOfferActive(offer, now) && offer.reporter?.id === inquiry.created_by)
            ? "你已有该 PN 的有效报价，发布时请选择更新或另报一条" : "该 PN 存在有效报价；再次报价时可选择更新或另报一条"} />}</>,
      actions: !inquiry && <Button size="small" danger aria-label={`删除 ${line.pn_raw}`} onClick={() => setSelected((previous) => previous.filter((part) => part.part_id !== line.part_id))}>删除</Button>,
    }))} now={now} onChange={changeValue} expandedRowKeys={expanded} onExpandedRowKeysChange={setExpanded} />
    {libraryError && <Alert type="warning" message="已有报价提示加载失败，发布时仍由服务端检查同 PN 有效报价" />}
    <div className="quote-offer-tools"><span>定向通知账号（可选）</span><Select mode="multiple" aria-label="定向通知销售" placeholder="选择需要收到新报价通知的账号"
      value={noticeIds} disabled={busy} onChange={setNoticeIds} showSearch optionFilterProp="label" style={{ minWidth: 250 }}
      options={candidates.map((candidate) => ({ value: candidate.id, label: candidate.display_name }))} /></div>
    {candidateError && <Alert type="warning" message="通知账号加载失败，可暂不选择通知对象" />}
    <footer className="quote-footer"><span>{lines.length} 行 · 已填写 {lines.filter((line) => quoteComplete(values[line.id] ?? {})).length} 行</span>
      <div className="quote-footer-actions">{!inquiry ? <Button type="primary" loading={busy} disabled={!selected.length || selected.length > 200} onClick={() => void create()}>生成报价草稿</Button>
        : <><Button loading={busy} disabled={!lines.some((line) => quoteComplete(values[line.id] ?? {}))} onClick={() => void saveDraft()}>保存草稿</Button>
          <Button type="primary" loading={busy} disabled={!complete} onClick={() => void publish()}>发布主动报价</Button></>}</div>
    </footer>{dialog}
  </section>;
}
