import { useEffect, useState } from "react";
import { Alert, App, Button, Card, Form, Input, Select, Space, Spin, Typography } from "antd";
import { Link, useSearchParams } from "react-router-dom";
import { presales, type Inquiry, type Offer, type OfferInput } from "../../api/presales";
import { listWorkCandidates, type WorkCandidate } from "../../api/workItems";
import { getFieldTemplate, type CustomFieldValue, type FieldDefinition, type FieldLevel } from "../../api/fields";
import type { PartSuggestion } from "../../api/search";
import LivePartSearch from "../../components/parts/LivePartSearch";
import PartThumb from "../../components/gallery/PartThumb";
import PriceRefBadge from "../../components/pricing/PriceRefBadge";
import CustomFieldsEditor from "../../components/fields/CustomFieldsEditor";
import AudiencePicker, { PUBLIC_AUDIENCE } from "../../components/audience/AudiencePicker";
import AudienceLock from "../../components/audience/AudienceLock";
import type { AudienceRule } from "../../api/audience";
import { refreshWorkBadges } from "../../components/workflow/useWorkBadge";
import QuoteEditor, { offerInput, quoteComplete } from "./QuoteEditor";
import PartQuoteBadges from "./PartQuoteBadges";
import { usePresalesError, isOfferActive, useNow } from "./shared";

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
  const { message } = App.useApp();
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
  const now = useNow(Object.values(library).flat().flatMap((offer) => [offer.valid_from, offer.valid_until]));
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
      setValues(Object.fromEntries((draft.lines ?? []).map((line) => [line.id, offerInput()])));
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
      await presales.publishInquiry(inquiry.id, { expected_version: inquiry.version, notice_user_ids: noticeIds });
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

  return <Space direction="vertical" size="middle" style={{ width: "100%" }}>
    <Typography.Title level={3}>主动报价{inquiry ? ` · ${inquiry.inquiry_no}` : ""}</Typography.Title>
    {!inquiry ? <>
      <Card title="选择 PN（支持多选批量加入）">
        <LivePartSearch mode="multiple" value={selected} onChange={setSelected}
          inputProps={{ "aria-label": "主动报价检索 PN" }}
          renderThumb={(part) => <PartThumb partId={part.part_id} pn={part.pn_std} />}
          renderBadges={(part) => <PartQuoteBadges partId={part.part_id} />} />
      </Card>
      <Card title="单头信息">
        <Form layout="vertical" component="div">
          <Form.Item label={<Space>单头备注<AudienceLock label="单头备注" value={notesAudience} onChange={setNotesAudience} /></Space>}><Input.TextArea aria-label="主动报价单头备注" value={notes}
            onChange={(event) => setNotes(event.target.value)} /></Form.Item>
          <Form.Item label="本单报价可见性"><AudiencePicker value={audience} onChange={setAudience} /></Form.Item>
          <Form.Item label="单头自定义字段"><DraftFields level="header" value={headerFields} onChange={setHeaderFields} /></Form.Item>
        </Form>
      </Card>
      {selected.map((part) => <Card key={part.part_id} title={<Space><PartThumb partId={part.part_id} pn={part.pn_std} />{part.pn_std}
        <PriceRefBadge partId={part.part_id} /></Space>}>
        <DraftFields level="line" partId={part.part_id} value={lineFields[part.part_id] ?? []}
          onChange={(next) => setLineFields((previous) => ({ ...previous, [part.part_id]: next }))} />
        {(library[part.part_id] ?? []).some((offer) => isOfferActive(offer, now))
          && <Alert type="info" message="该 PN 存在有效报价；同一报价人、同一货源将更新已有报价" />}
      </Card>)}
      <Button type="primary" loading={busy} disabled={!selected.length || selected.length > 200} onClick={() => void create()}>生成报价草稿</Button>
    </> : (inquiry.lines ?? []).map((line) => {
      const existingOwn = (library[line.part_id!] ?? []).filter((offer) => isOfferActive(offer, now)
        && offer.reporter?.id === inquiry.created_by);
      return <Card key={line.id} title={<Space><PartThumb partId={line.part_id!} pn={line.pn_raw} />{line.pn_raw}
        <PriceRefBadge partId={line.part_id!} /></Space>}>
        {!!existingOwn.length && <Alert type="info" message="你已有该 PN 的有效报价，同货源将更新已有报价" style={{ marginBottom: 12 }} />}
        <QuoteEditor label={line.pn_raw} value={values[line.id] ?? offerInput()} existing={saved[line.id]} disabled={busy}
          onChange={(next) => setValues((previous) => ({ ...previous, [line.id]: next }))} />
      </Card>;
    })}
    {libraryError && <Alert type="warning" message="已有报价提示加载失败，发布时仍由服务端检查同 PN 同货源报价" />}
    <Card title="定向通知账号（可选）">
      <Select mode="multiple" aria-label="定向通知销售" placeholder="选择需要收到新报价通知的账号" style={{ width: "100%" }}
        value={noticeIds} disabled={busy} onChange={setNoticeIds} showSearch optionFilterProp="label"
        options={candidates.map((candidate) => ({ value: candidate.id, label: candidate.display_name }))} />
      {candidateError && <Alert type="warning" message="通知账号加载失败，可暂不选择通知对象" />}
    </Card>
    {inquiry && <Button type="primary" loading={busy} disabled={!complete} onClick={() => void publish()}>发布主动报价</Button>}
  </Space>;
}
