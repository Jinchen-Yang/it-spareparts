import { useCallback, useEffect, useState } from "react";
import { Alert, App, Button, Drawer, Input, InputNumber, Modal, Space, Spin, Tag, Typography } from "antd";
import { Link, Navigate, useLocation, useNavigate, useParams } from "react-router-dom";
import { presales, type ExistingChoice, type Inquiry, type InquiryLine, type LineResponseInput, type Offer, type OfferInput } from "../../api/presales";
import { releaseWorkItem, type WorkItem } from "../../api/workItems";
import GalleryPicker from "../../components/gallery/GalleryPicker";
import Timeline from "../../components/workflow/Timeline";
import { refreshWorkBadges } from "../../components/workflow/useWorkBadge";
import { offerInput, quoteComplete } from "./QuoteEditor";
import { usePresalesError, isOfferActive, useNow, flattenOffers } from "./shared";
import QuoteGrid, { type QuoteGridRow } from "./QuoteGrid";
import { existingOfferConflict, useExistingOfferChoice } from "./ExistingOfferDialog";
import { WorkbookExportButton } from "../../components/workbook";
import { presalesWorkbooks } from "../../api/presalesWorkbooks";
import WorkbookDialog from "./WorkbookDialog";
import PartBinding from "./PartBinding";
import PageHeader from "../../components/PageHeader";
import { findOwnTask, loadInquiry } from "./quoteData";
import "./presales.css";
import { answerSentence, lineAnswer } from "./answers";

export default function ProcessPage() {
  const { id } = useParams();
  return <ProcessContent key={id} inquiryId={Number(id)} />;
}

export function ProcessContent({ inquiryId, onTaskChange, onDirtyChange }: { inquiryId: number; onTaskChange?: () => void; onDirtyChange?: (dirty: boolean) => void }) {
  const location = useLocation();
  const navigationTask = (location.state as { workItem?: WorkItem } | null)?.workItem;
  if (navigationTask?.subject_type === "presales_offer_notice") return <Navigate replace to={`/presales/inquiries/${inquiryId}`} state={location.state} />;
  return <QuoteProcessContent inquiryId={inquiryId} onTaskChange={onTaskChange} onDirtyChange={onDirtyChange} />;
}

function QuoteProcessContent({ inquiryId, onTaskChange, onDirtyChange }: { inquiryId: number; onTaskChange?: () => void; onDirtyChange?: (dirty: boolean) => void }) {
  const handlePresalesError = usePresalesError();
  const { choose, dialog } = useExistingOfferChoice();
  const location = useLocation();
  const navigationTask = (location.state as { workItem?: WorkItem } | null)?.workItem;
  const { message, modal } = App.useApp();
  const navigate = useNavigate();
  const [inquiry, setInquiry] = useState<Inquiry>();
  const [task, setTask] = useState<WorkItem | undefined>(navigationTask?.subject_id === inquiryId ? navigationTask : undefined);
  const [loadVersion, setLoadVersion] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refreshRequired, setRefreshRequired] = useState(false);
  const [gallery, setGallery] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [values, setValues] = useState<Record<number, { key: string; value: OfferInput }>>({});
  const [withdrawOffer, setWithdrawOffer] = useState<Offer>();
  const [releaseOpen, setReleaseOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [timeline, setTimeline] = useState(false);
  const [dirty, setDirty] = useState<Record<number, boolean>>({});
  const [timelineVersion, setTimelineVersion] = useState(0);
  const [library, setLibrary] = useState<Record<number, Offer[]>>({});
  const [libraryError, setLibraryError] = useState(false);
  const [responses, setResponses] = useState<Record<number, LineResponseInput>>({});
  const hasChanges = Object.values(dirty).some(Boolean) || Object.keys(responses).length > 0;
  useEffect(() => {
    if (!hasChanges) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [hasChanges]);
  const back = () => {
    if (!hasChanges) { navigate("/presales/workbench"); return; }
    modal.confirm({ title: "当前单据有未保存的修改", content: "返回列表会放弃尚未保存的报价、沿用选择和不报原因。",
      okText: "放弃修改并返回", cancelText: "继续填写", onOk: () => navigate("/presales/workbench") });
  };
  useEffect(() => { onDirtyChange?.(Object.values(dirty).some(Boolean) || Object.keys(responses).length > 0); }, [dirty, responses, onDirtyChange]);
  const now = useNow([...Object.values(library).flatMap(flattenOffers), ...(inquiry?.line_responses ?? []).flatMap((row) => row.offer ? [row.offer] : [])]
    .map((offer) => offer.valid_from));
  const partKey = (inquiry?.lines ?? []).filter((line) => line.part_id).map((line) => line.part_id).join(",");
  const refresh = useCallback(async () => {
    const latest = await loadInquiry(inquiryId);
    setInquiry(latest); setRefreshRequired(false); setTimelineVersion((value) => value + 1);
  }, [inquiryId]);
  useEffect(() => {
    let cancelled = false;
    setLibraryError(false);
    if (!partKey) { setLibrary({}); return; }
    presales.reference({ inquiry_id: inquiryId }).then((data) => {
      if (!cancelled) setLibrary(data.offers);
    }).catch(() => { if (!cancelled) { setLibrary({}); setLibraryError(true); } });
    return () => { cancelled = true; };
  }, [partKey, inquiry?.version, inquiryId]);
  useEffect(() => {
    let cancelled = false;
    setLoading(true); setLoadError(false); setInquiry(undefined); setDirty({}); setValues({}); setResponses({});
    const currentTask = navigationTask?.subject_id === inquiryId ? navigationTask : undefined;
    setTask(currentTask);
    loadInquiry(inquiryId).then(async (data) => {
      const ownTask = currentTask ?? (data.kind === "proactive_offer" ? undefined : await findOwnTask(inquiryId));
      if (!cancelled) { setInquiry(data); setTask(ownTask); }
    })
      .catch((error) => { if (!cancelled) { setLoadError(true); handlePresalesError(error); } })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [inquiryId, navigationTask, loadVersion]);

  if (loading) return <Spin />;
  if (!inquiry) return <Alert type="error" message={loadError ? "询价单加载失败" : "询价单不可用"}
    action={<Button onClick={() => setLoadVersion((version) => version + 1)}>重试</Button>} />;

  const liveTask = inquiry.work_items?.find((item) => item.id === task?.id);
  const taskStatus = liveTask?.status ?? task?.status;
  const active = taskStatus === "pending" || taskStatus === "in_progress";
  const paused = liveTask?.paused ?? task?.paused;
  const proactive = inquiry.kind === "proactive_offer";
  const actorId = proactive ? inquiry.created_by : task?.claimed_by ?? task?.assignee_id;
  const ownOffers = actorId == null ? [] : (inquiry.offers ?? []).filter((offer) => offer.reporter?.id === actorId);
  const lines = inquiry.lines ?? [];
  const required = lines.filter((line) => !line.ref_offer_id);
  const savedResponse = (lineId: number) => inquiry.line_responses?.find((row) => row.line_id === lineId && row.created_by === actorId);
  const responseFor = (lineId: number): LineResponseInput => responses[lineId] ?? savedResponse(lineId) ?? { line_id: lineId, mode: "quote" };
  const reusedOffer = (line: InquiryLine) => {
    const response = responseFor(line.id);
    if (response.mode !== "reuse") return undefined;
    return (savedResponse(line.id)?.ref_offer_id === response.ref_offer_id ? savedResponse(line.id)?.offer : undefined)
      ?? flattenOffers(library[line.part_id!] ?? []).find((offer) => offer.id === response.ref_offer_id);
  };
  const complete = required.length > 0 && required.every((line) => {
    const response = responseFor(line.id);
    if (response.mode === "skip") return !!response.reason?.trim();
    if (response.mode === "reuse") {
      const offer = reusedOffer(line);
      return !!offer && isOfferActive(offer, now);
    }
    return !dirty[line.id] && ownOffers.some((offer) => offer.line_id === line.id && ["draft", "published"].includes(offer.status)
      && quoteComplete(offer) && !offer.no_stock && !!line.part_id);
  });
  const canProcess = inquiry.status === "submitted" && (proactive || (!!task && actorId != null)) && !paused;
  const editable = canProcess && !refreshRequired;
  const refreshAfterSuccess = (error: unknown, action: string) => {
    setRefreshRequired(true);
    handlePresalesError.afterSuccess(error, action);
  };
  const reply = async () => {
    if (!complete || !task) return;
    setBusy(true);
    let completed = false;
    try {
      const lineResponses = required.map((line) => {
        const row = responseFor(line.id);
        return { line_id: row.line_id, mode: row.mode, ...(row.mode === "reuse" ? { ref_offer_id: row.ref_offer_id } : {}),
          ...(row.mode === "skip" ? { reason: row.reason?.trim() } : {}) };
      });
      const choices: Record<number, ExistingChoice> = {};
      for (;;) {
        try {
          await presales.replyInquiry(inquiry.id, { expected_version: inquiry.version, work_item_id: task.id,
            expected_work_item_version: liveTask?.version ?? task.version, line_responses: lineResponses,
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
      completed = true;
      message.success("已回复给销售"); setResponses({}); setDirty({}); refreshWorkBadges(); onTaskChange?.(); await refresh();
    } catch (error) {
      if (completed) refreshAfterSuccess(error, "回复销售");
      else await handlePresalesError(error, refresh);
    }
    finally { setBusy(false); }
  };

  const currentFor = (line: InquiryLine) => {
    const referenceId = proactive ? line.ref_offer_id ?? savedResponse(line.id)?.ref_offer_id ?? line.linked_offer_ids?.[0] : undefined;
    return (referenceId ? ownOffers.find((offer) => offer.id === referenceId) : undefined)
      ?? ownOffers.find((offer) => offer.line_id === line.id && offer.status !== "withdrawn" && offer.status !== "stale")
      ?? ownOffers.find((offer) => offer.line_id === line.id);
  };
  const valueKey = (line: InquiryLine) => `${currentFor(line)?.id ?? "new"}:${currentFor(line)?.version ?? 0}`;
  const valueFor = (line: InquiryLine) => {
    // 冲突刷新可更新版本，但不能静默丢弃该行尚未保存的填写内容。
    if (values[line.id] && (dirty[line.id] || values[line.id].key === valueKey(line))) return values[line.id].value;
    const current = currentFor(line);
    return offerInput(!proactive && (current?.status === "stale" || current?.status === "withdrawn") ? undefined : current);
  };
  const changeValue = (lineId: number, value: OfferInput) => {
    const line = lines.find((item) => item.id === lineId)!;
    setValues((previous) => ({ ...previous, [lineId]: { key: valueKey(line), value } }));
    setDirty((previous) => ({ ...previous, [lineId]: true }));
  };
  const saveLine = async (line: InquiryLine) => {
    const current = currentFor(line);
    const value = valueFor(line);
    const restart = !proactive && (current?.status === "stale" || current?.status === "withdrawn");
    if (current?.status === "published") await presales.updateOffer(current.id, { ...value, expected_version: current.version });
    else await presales.saveOffer(inquiry.id, line.id, { ...value,
      expected_version: current && !restart ? current.version : inquiry.version,
      ...(current && !restart ? { offer_id: current.id } : {}), ...(task ? { work_item_id: task.id } : {}) });
    setDirty((previous) => ({ ...previous, [line.id]: false }));
  };
  const save = async (line?: InquiryLine) => {
    setBusy(true);
    let completed = false;
    try {
      const selected = line ? [line] : required.filter((item) => responseFor(item.id).mode === "quote" && quoteComplete(valueFor(item)) && !!item.part_id);
      for (const item of selected) await saveLine(item);
      completed = true;
      message.success(line && currentFor(line)?.status === "published" ? "报价已更新，已保存历史版本" : "报价草稿已保存；沿用与不报将在回复时提交");
      await refresh();
    } catch (error) {
      if (completed) refreshAfterSuccess(error, "保存报价");
      else await handlePresalesError(error, refresh);
    }
    finally { setBusy(false); }
  };
  const withdraw = async () => {
    if (!withdrawOffer || !reason.trim()) return;
    setBusy(true);
    let completed = false;
    try {
      await presales.withdrawOffer(withdrawOffer.id, { expected_version: withdrawOffer.version, reason: reason.trim() });
      completed = true;
      setWithdrawOffer(undefined); setReason(""); message.success("报价已撤销"); await refresh();
    } catch (error) {
      if (completed) refreshAfterSuccess(error, "撤销报价");
      else await handlePresalesError(error, refresh);
    }
    finally { setBusy(false); }
  };
  const release = async () => {
    if (!task || !reason.trim()) return;
    setBusy(true);
    let completed = false;
    try {
      await releaseWorkItem(task.id, { expected_version: liveTask?.version ?? task.version, reason: reason.trim() });
      completed = true;
      setReleaseOpen(false); setTask(undefined); refreshWorkBadges(); onTaskChange?.(); await refresh();
    } catch (error) {
      if (completed) refreshAfterSuccess(error, "退回待办");
      else await handlePresalesError(error, refresh);
    }
    finally { setBusy(false); }
  };
  const rows: QuoteGridRow[] = lines.map((line) => {
    const current = currentFor(line);
    const response = responseFor(line.id);
    const canEdit = !refreshRequired && (current?.status === "published"
      || (editable && !proactive && !line.ref_offer_id && active));
    return { line, current, value: valueFor(line), response, reused: reusedOffer(line),
      responseDraft: !!responses[line.id] || !!savedResponse(line.id)?.is_draft,
      reporterLabel: savedResponse(line.id)?.ref_offer_id === response.ref_offer_id ? savedResponse(line.id)?.reporter_label : undefined,
      existing: library[line.part_id!] ?? [],
      disabled: !canEdit || busy, responseDisabled: !editable || !active || busy,
      referenced: !!line.ref_offer_id && !proactive,
      binding: !line.part_id && canProcess && active ? <PartBinding line={line} onBound={setInquiry} refresh={refresh} disabled={busy || refreshRequired} /> : undefined,
      details: <>
        {inquiry.line_answers && <p>当前答复：{answerSentence(line, lineAnswer(inquiry, line))}</p>}
        {proactive && (!current || current.status !== "published") && <Alert type="info" message="当前报价不可更新，可新建主动报价"
          action={<Link to="/presales/offers/new">新建主动报价</Link>} />}
      </>,
      actions: canEdit && (proactive || response.mode === "quote") && <span className="presales-toolbar">
        <Button size="small" loading={busy} disabled={!quoteComplete(valueFor(line)) || (!line.part_id && !valueFor(line).no_stock)}
          onClick={() => void save(line)}>{current?.status === "published" ? "更新报价" : "保存该行报价"}</Button>
        {current?.status === "published" && <Button size="small" danger disabled={busy} onClick={() => { setReason(""); setWithdrawOffer(current); }}>撤销报价</Button>}
      </span> };
  });
  const handledRows = required.filter((line) => {
    const response = responseFor(line.id);
    if (response.mode === "skip") return !!response.reason?.trim();
    if (response.mode === "reuse") return !!reusedOffer(line) && isOfferActive(reusedOffer(line)!, now);
    return !dirty[line.id] && ownOffers.some((offer) => offer.line_id === line.id && ["draft", "published"].includes(offer.status)
      && quoteComplete(offer) && !offer.no_stock && !!line.part_id);
  });
  const count = (mode: LineResponseInput["mode"]) => handledRows.filter((line) => responseFor(line.id).mode === mode).length;


  return <section className="presales-page">
    <PageHeader title={`询价人：${inquiry.created_by_name ?? "业务人员"}`} subtitle="逐行填写报价，保存草稿后回复给销售" extra={<Button onClick={back}>返回列表</Button>} />
    <div className="presales-panel">
    <div className="presales-toolbar">
      {"notes" in inquiry && <span className="presales-muted" title={inquiry.notes ?? undefined}>{inquiry.notes || "无客户补充要求"}</span>}
      <span className="presales-muted">报价不设有效期，一直有效，直到更新或撤回</span>
      {!proactive && <><WorkbookExportButton size="small" label="下载报价表" schemaName="presales_quote_sheet" disabled={busy || !editable || !active}
        fetchSchema={() => presalesWorkbooks.getQuoteSheetSchema()} onExport={(visible) => presalesWorkbooks.exportQuoteSheet([inquiry.id], visible)}
        filename="采购报价表.xlsx" />
        <Button size="small" disabled={busy || !editable || !active} onClick={() => setImportOpen(true)}>上传报价表</Button></>}
      <Button size="small" onClick={() => setGallery(true)} disabled={!lines.some((line) => line.part_id)}>查看图片 / 多选下载</Button>
    </div>
    {inquiry.field_values?.map((field, index) => <div key={index}>{field.name ?? field.new_name}: {String(field.value ?? "")}</div>)}
    {!task && !proactive && <Alert type="info" message="请从自己的采购待办打开本单，领取或接受后即可处理" />}
    {paused && <Alert type="warning" message="询价已撤回，待办暂停" />}
    {refreshRequired && <Alert type="warning" message="操作已成功，页面尚未更新，请刷新确认后继续。"
      action={<Button loading={busy} onClick={async () => {
        setBusy(true);
        try { await refresh(); } catch (error) { refreshAfterSuccess(error, "之前的操作"); }
        finally { setBusy(false); }
      }}>刷新结果</Button>} />}
    {libraryError && <Alert type="warning" message="已有有效报价加载失败，请刷新后重试；仍可报新价或不报" />}
    <QuoteGrid id="presales-workbench-lines" rows={rows} now={now} proactive={proactive} onChange={changeValue}
      onResponse={(lineId, patch) => {
        setResponses((previous) => ({ ...previous, [lineId]: { ...(previous[lineId] ?? savedResponse(lineId) ?? { line_id: lineId, mode: "quote" as const }), ...patch } }));
      }} />
    </div>
    <footer className="presales-footer"><span>填写完成 <b>{handledRows.length}</b> / {required.length} 行（报价 {count("quote")} · 沿用 {count("reuse")} · 不报 {count("skip")}）</span>
      <div className="presales-toolbar"><Button size="small" onClick={() => setTimeline(!timeline)}>{timeline ? "隐藏动态" : "查看动态"}</Button>
        {active && <><Button disabled={busy || !editable || !task?.allowed_actions.includes("release")} onClick={() => { setReason(""); setReleaseOpen(true); }}>退回公共池</Button>
          <Button loading={busy} disabled={!editable || !required.some((line) => responseFor(line.id).mode === "quote" && quoteComplete(valueFor(line)) && !!line.part_id)} onClick={() => void save()}>保存草稿</Button>
          <Button type="primary" loading={busy} disabled={!complete || !editable} onClick={() => void reply()}>回复给销售</Button></>}
      </div>
    </footer>
    {active && !complete && <div className="presales-muted">每行保存新报价、沿用有效报价或填写不报原因后，才能回复给销售。</div>}
    {dialog}
    <Modal title="撤销报价" open={!!withdrawOffer} okText="确认撤销" cancelText="取消" confirmLoading={busy}
      okButtonProps={{ disabled: !reason.trim() }} onCancel={() => setWithdrawOffer(undefined)} onOk={() => void withdraw()}>
      <Input.TextArea aria-label="撤销原因" placeholder="必须填写撤销原因" value={reason} onChange={(event) => setReason(event.target.value)} />
    </Modal>
    <Modal title="退回公共池" open={releaseOpen} okText="确认退回" cancelText="取消" confirmLoading={busy}
      okButtonProps={{ disabled: !reason.trim() }} onCancel={() => setReleaseOpen(false)} onOk={() => void release()}>
      <Input.TextArea aria-label="退回原因" placeholder="必须填写退回原因" value={reason} onChange={(event) => setReason(event.target.value)} />
    </Modal>
    <Drawer title="询价动态" open={timeline} onClose={() => setTimeline(false)} width={480} destroyOnHidden>
      {timeline && <Timeline key={`timeline-${timelineVersion}`} subjectType="presales_inquiry" subjectId={inquiry.id} />}
    </Drawer>
    {gallery && <GalleryPicker mode="modal" items={lines.filter((line) => line.part_id).map((line) => ({ partId: line.part_id!, pn: line.pn_raw }))} onClose={() => setGallery(false)} />}
    {importOpen && <WorkbookDialog mode="quote" inquiryId={inquiry.id} onClose={() => setImportOpen(false)} onApplied={() => {
      message.success("已保存为草稿，请在页面确认后回复"); setImportOpen(false); setLoadVersion((version) => version + 1); onTaskChange?.();
    }} />}
  </section>;
}
