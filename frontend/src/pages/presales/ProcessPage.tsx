import { useCallback, useEffect, useState, type Key } from "react";
import { Alert, App, Button, Input, InputNumber, Modal, Space, Spin, Tag, Typography } from "antd";
import { Link, useLocation, useParams } from "react-router-dom";
import { presales, type ExistingChoice, type Inquiry, type InquiryLine, type LineResponseInput, type NearDuplicate, type Offer, type OfferInput } from "../../api/presales";
import { listWorkItems, releaseWorkItem, type WorkItem, type WorkTab } from "../../api/workItems";
import type { PartSuggestion } from "../../api/search";
import LivePartSearch from "../../components/parts/LivePartSearch";
import PartThumb from "../../components/gallery/PartThumb";
import GalleryPicker from "../../components/gallery/GalleryPicker";
import Timeline from "../../components/workflow/Timeline";
import { refreshWorkBadges } from "../../components/workflow/useWorkBadge";
import { offerInput, quoteComplete } from "./QuoteEditor";
import PartQuoteBadges from "./PartQuoteBadges";
import { usePresalesError, isOfferActive, useNow, flattenOffers, expiryLabel } from "./shared";
import QuoteGrid, { type QuoteGridRow } from "./QuoteGrid";
import { existingOfferConflict, useExistingOfferChoice } from "./ExistingOfferDialog";
import { WorkbookExportButton } from "../../components/workbook";
import { presalesWorkbooks } from "../../api/presalesWorkbooks";
import WorkbookDialog from "./WorkbookDialog";

async function findOwnTask(inquiryId: number): Promise<WorkItem | undefined> {
  // 待办接口的这三个页签均按服务端当前账号过滤，不以显示名称推断身份。
  for (const tab of ["assigned", "claimed", "done"] as WorkTab[]) {
    for (let page = 1; ; page += 1) {
      const result = await listWorkItems({ tab, subject_type: "presales_inquiry", page, page_size: 100 });
      const found = result.items.find((item) => item.subject_id === inquiryId);
      if (found) return found;
      if (page * 100 >= result.total) break;
    }
  }
  return undefined;
}

async function loadInquiry(inquiryId: number) {
  const inquiry = await presales.getInquiry(inquiryId);
  const references = (inquiry.lines ?? []).flatMap((line) => [...new Set([
    ...(line.ref_offer_id ? [line.ref_offer_id] : []), ...(line.linked_offer_ids ?? []), ...(line.linked_offers ?? []).map((offer) => offer.id),
    ...(inquiry.line_responses ?? []).filter((row) => row.line_id === line.id && row.mode === "reuse" && row.ref_offer_id)
      .map((row) => row.ref_offer_id!),
  ])].map((ref_offer_id) => ({ ref_offer_id, part_id: line.part_id })));
  if (inquiry.kind !== "proactive_offer" || !references.length) return inquiry;
  const library = await presales.library([...new Set(references.flatMap((row) => row.part_id ? [row.part_id] : []))]);
  // 处理页编辑当前报价；原单关联中的钉版本只在销售详情显示，不能当作并发更新版本。
  const offers = new Map((inquiry.offers ?? []).filter((offer) => !references.some((row) => row.ref_offer_id === offer.id))
    .map((offer) => [offer.id, offer]));
  for (const offer of Object.values(library).flatMap(flattenOffers)) {
    if (references.some((line) => line.ref_offer_id === offer.id)) offers.set(offer.id, offer);
  }
  // 过期报价已从报价库移除。历史只用于定位原单，再读当前记录获取最新并发版本。
  await Promise.allSettled(references.filter((line) => !offers.has(line.ref_offer_id!)).map(async (line) => {
    const revisions = await presales.revisions(line.ref_offer_id!);
    const originId = revisions[revisions.length - 1]?.snapshot.inquiry_id;
    if (!originId) return;
    const origin = await presales.getInquiry(originId);
    const current = origin.offers?.find((offer) => offer.id === line.ref_offer_id);
    if (current) offers.set(current.id, current);
  }));
  return { ...inquiry, offers: Array.from(offers.values()) };
}

function PartBinding({ line, onBound, refresh }: { line: InquiryLine; onBound: (inquiry: Inquiry) => void; refresh: () => Promise<void> }) {
  const handlePresalesError = usePresalesError();
  const [pn, setPn] = useState(line.pn_raw);
  const [description, setDescription] = useState(line.description ?? "");
  const [brand, setBrand] = useState("");
  const [duplicates, setDuplicates] = useState<NearDuplicate[]>();
  const [selected, setSelected] = useState<PartSuggestion[]>([]);
  const [busy, setBusy] = useState(false);
  const check = async () => {
    setBusy(true);
    try { setDuplicates((await presales.partCheck(line.id, pn)).near_duplicates); }
    catch (error) { handlePresalesError(error, refresh); }
    finally { setBusy(false); }
  };
  const bind = async (create: boolean) => {
    if (!duplicates || (!create && !selected[0])) return;
    setBusy(true);
    try {
      onBound(create
        ? await presales.createPart(line.id, { expected_version: line.version, pn_std: pn,
          description: description || null, brand: brand || null })
        : await presales.bindPart(line.id, { expected_version: line.version, part_id: selected[0].part_id }));
    } catch (error) { setDuplicates(undefined); handlePresalesError(error, refresh); }
    finally { setBusy(false); }
  };
  return <Space direction="vertical" style={{ width: "100%" }}>
    <Tag color="warning">待建档：先查重，再绑定已有 PN 或新建</Tag>
    <Space wrap>
      <Input aria-label={`${line.pn_raw}建档 PN`} value={pn} disabled={busy} maxLength={256}
        onChange={(event) => { setPn(event.target.value); setDuplicates(undefined); setSelected([]); }} />
      <Button loading={busy} disabled={!pn.trim()} onClick={() => void check()}>查重</Button>
    </Space>
    {duplicates && (duplicates.length ? <>
      <Alert type="info" message="发现已有或近似型号，请在实时检索中选定后绑定" />
      {duplicates.map((item) => <Typography.Text key={item.pn_std}>{item.pn_std} · {item.reason}</Typography.Text>)}
      <LivePartSearch inputProps={{ "aria-label": `${line.pn_raw}绑定已有 PN` }} value={selected} onChange={setSelected}
        renderThumb={(item) => <PartThumb partId={item.part_id} pn={item.pn_std} />}
        renderBadges={(item) => <PartQuoteBadges partId={item.part_id} />} />
      <Button disabled={!selected.length || busy} onClick={() => void bind(false)}>绑定已有 PN</Button>
    </> : <>
      <Typography.Text>查重无命中，可新建主数据。</Typography.Text>
      <Input aria-label={`${line.pn_raw}建档描述`} placeholder="产品描述" value={description} disabled={busy}
        onChange={(event) => setDescription(event.target.value)} />
      <Input aria-label={`${line.pn_raw}建档品牌`} placeholder="品牌（可选）" value={brand} disabled={busy}
        onChange={(event) => setBrand(event.target.value)} />
      <Button loading={busy} onClick={() => void bind(true)}>新建并绑定 PN</Button>
    </>)}
  </Space>;
}

export default function ProcessPage() {
  const { id } = useParams();
  return <ProcessContent key={id} inquiryId={Number(id)} />;
}

export function ProcessContent({ inquiryId, onTaskChange, onDirtyChange }: { inquiryId: number; onTaskChange?: () => void; onDirtyChange?: (dirty: boolean) => void }) {
  const handlePresalesError = usePresalesError();
  const { choose, dialog } = useExistingOfferChoice();
  const location = useLocation();
  const navigationTask = (location.state as { workItem?: WorkItem } | null)?.workItem;
  const { message } = App.useApp();
  const [inquiry, setInquiry] = useState<Inquiry>();
  const [task, setTask] = useState<WorkItem | undefined>(navigationTask?.subject_id === inquiryId ? navigationTask : undefined);
  const [loadVersion, setLoadVersion] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [gallery, setGallery] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [values, setValues] = useState<Record<number, { key: string; value: OfferInput }>>({});
  const [expanded, setExpanded] = useState<Key[]>([]);
  const [validity, setValidity] = useState(1);
  const [withdrawOffer, setWithdrawOffer] = useState<Offer>();
  const [releaseOpen, setReleaseOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [timeline, setTimeline] = useState(false);
  const [dirty, setDirty] = useState<Record<number, boolean>>({});
  const [timelineVersion, setTimelineVersion] = useState(0);
  const [library, setLibrary] = useState<Record<number, Offer[]>>({});
  const [libraryError, setLibraryError] = useState(false);
  const [responses, setResponses] = useState<Record<number, LineResponseInput>>({});
  useEffect(() => { onDirtyChange?.(Object.values(dirty).some(Boolean) || Object.keys(responses).length > 0); }, [dirty, responses, onDirtyChange]);
  const now = useNow([...Object.values(library).flatMap(flattenOffers), ...(inquiry?.line_responses ?? []).flatMap((row) => row.offer ? [row.offer] : [])]
    .flatMap((offer) => [offer.valid_from, offer.valid_until]));
  const partKey = (inquiry?.lines ?? []).filter((line) => line.part_id).map((line) => line.part_id).join(",");
  const refresh = useCallback(async () => {
    const latest = await loadInquiry(inquiryId);
    setInquiry(latest); setTimelineVersion((value) => value + 1);
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
      if (!cancelled) { setInquiry(data); setTask(ownTask); setValidity(data.offers?.[0]?.validity_days ?? 1); setExpanded((data.lines ?? []).filter((line) => !line.part_id).map((line) => line.id)); }
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
  const editable = inquiry.status === "submitted" && (proactive || (!!task && actorId != null)) && !paused;
  const reply = async () => {
    if (!complete || !task) return;
    setBusy(true);
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
      message.success("已回复给销售"); setResponses({}); setDirty({}); refreshWorkBadges(); onTaskChange?.(); await refresh();
    } catch (error) { handlePresalesError(error, refresh); }
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
    if (values[line.id]?.key === valueKey(line)) return values[line.id].value;
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
    try {
      const selected = line ? [line] : required.filter((item) => responseFor(item.id).mode === "quote" && quoteComplete(valueFor(item)) && !!item.part_id);
      for (const item of selected) await saveLine(item);
      message.success(line && currentFor(line)?.status === "published" ? "报价已更新，已保存历史版本" : "报价草稿已保存；沿用与不报将在回复时提交");
      await refresh();
    } catch (error) { await handlePresalesError(error, refresh); }
    finally { setBusy(false); }
  };
  const withdraw = async () => {
    if (!withdrawOffer || !reason.trim()) return;
    setBusy(true);
    try {
      await presales.withdrawOffer(withdrawOffer.id, { expected_version: withdrawOffer.version, reason: reason.trim() });
      setWithdrawOffer(undefined); setReason(""); message.success("报价已撤销"); await refresh();
    } catch (error) { await handlePresalesError(error, refresh); }
    finally { setBusy(false); }
  };
  const release = async () => {
    if (!task || !reason.trim()) return;
    setBusy(true);
    try {
      await releaseWorkItem(task.id, { expected_version: liveTask?.version ?? task.version, reason: reason.trim() });
      setReleaseOpen(false); setTask(undefined); refreshWorkBadges(); onTaskChange?.(); await refresh();
    } catch (error) { await handlePresalesError(error, refresh); }
    finally { setBusy(false); }
  };
  const rows: QuoteGridRow[] = lines.map((line) => {
    const current = currentFor(line);
    const response = responseFor(line.id);
    const canEdit = editable && (proactive ? current?.status === "published"
      : !line.ref_offer_id && (active || current?.status === "published"));
    return { line, current, value: valueFor(line), response, reused: reusedOffer(line),
      reporterLabel: savedResponse(line.id)?.ref_offer_id === response.ref_offer_id ? savedResponse(line.id)?.reporter_label : undefined,
      existing: library[line.part_id!] ?? [],
      disabled: !canEdit || busy, responseDisabled: !editable || !active || busy,
      referenced: !!line.ref_offer_id && !proactive,
      details: <>
        {!line.part_id && editable && active && <PartBinding line={line} onBound={setInquiry} refresh={refresh} />}
        {proactive && (!current || current.status !== "published") && <Alert type="info" message="当前报价不可更新，可新建主动报价"
          action={<Link to="/presales/offers/new">新建主动报价</Link>} />}
      </>,
      actions: canEdit && (proactive || response.mode === "quote") && <span className="quote-row-actions">
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

  const dayValues = (proactive ? lines : required).map((line) => valueFor(line).validity_days ?? 1);
  const uniformDays = dayValues.length ? dayValues.every((days) => days === dayValues[0]) ? dayValues[0] : undefined : validity;

  return <section className="quote-document">
    <header className="quote-heading"><span className="quote-number">{inquiry.inquiry_no}</span>
      {"notes" in inquiry && <span className="quote-header-note" title={inquiry.notes ?? undefined}>{inquiry.notes || "无客户补充要求"}</span>}
      <div className="quote-validity"><span>统一有效期</span><InputNumber size="small" aria-label="统一有效期" min={1} max={36500} precision={0}
        value={uniformDays ?? null} placeholder="多值" title="修改后应用到本单所有待处理报价行" disabled={!editable || busy} onChange={(days) => {
          const next = days ?? 1; setValidity(next);
          for (const line of proactive ? lines : required) changeValue(line.id, { ...valueFor(line), validity_days: next });
        }} /><span role="status">{uniformDays === undefined ? "各行有效期不同；修改后统一应用" : expiryLabel(uniformDays, now)}</span></div>
      {!proactive && <><WorkbookExportButton size="small" label="下载报价表" schemaName="presales_quote_sheet" disabled={busy || !editable || !active}
        fetchSchema={() => presalesWorkbooks.getQuoteSheetSchema()} onExport={(visible) => presalesWorkbooks.exportQuoteSheet([inquiry.id], visible)}
        filename={`${inquiry.inquiry_no}-报价表.xlsx`} />
        <Button size="small" disabled={busy || !editable || !active} onClick={() => setImportOpen(true)}>上传报价表</Button></>}
      <Button size="small" onClick={() => setGallery(true)} disabled={!lines.some((line) => line.part_id)}>查看图片 / 多选下载</Button>
    </header>
    {inquiry.field_values?.map((field, index) => <div key={index}>{field.name ?? field.new_name}: {String(field.value ?? "")}</div>)}
    {!task && !proactive && <Alert type="info" message="请从自己的采购待办打开本单，领取或接受后即可处理" />}
    {paused && <Alert type="warning" message="询价已撤回，待办暂停" />}
    {libraryError && <Alert type="warning" message="已有有效报价加载失败，请刷新后重试；仍可报新价或不报" />}
    <QuoteGrid id="presales-workbench-lines" rows={rows} now={now} proactive={proactive} onChange={changeValue}
      expandedRowKeys={expanded} onExpandedRowKeysChange={setExpanded} onResponse={(lineId, patch) => {
        setResponses((previous) => ({ ...previous, [lineId]: { ...responseFor(lineId), ...patch } }));
        if (patch.mode === "reuse") setExpanded((previous) => [...new Set([...previous, lineId])]);
      }} />
    <footer className="quote-footer"><span>已处理 <b>{handledRows.length}</b> / {required.length} 行（报价 {count("quote")} · 沿用 {count("reuse")} · 不报 {count("skip")}）</span>
      <div className="quote-footer-actions"><Button size="small" onClick={() => setTimeline(!timeline)}>{timeline ? "隐藏动态" : "查看动态"}</Button>
        {active && <><Button disabled={busy || !editable || !task?.allowed_actions.includes("release")} onClick={() => { setReason(""); setReleaseOpen(true); }}>退回公共池</Button>
          <Button loading={busy} disabled={!editable || !required.some((line) => responseFor(line.id).mode === "quote" && quoteComplete(valueFor(line)) && !!line.part_id)} onClick={() => void save()}>保存草稿</Button>
          <Button type="primary" loading={busy} disabled={!complete || !editable} onClick={() => void reply()}>回复给销售</Button></>}
      </div>
    </footer>
    {active && !complete && <div className="quote-muted">每行保存新报价、沿用有效报价或填写不报原因后，才能回复给销售。</div>}
    {dialog}
    <Modal title="撤销报价" open={!!withdrawOffer} okText="确认撤销" cancelText="取消" confirmLoading={busy}
      okButtonProps={{ disabled: !reason.trim() }} onCancel={() => setWithdrawOffer(undefined)} onOk={() => void withdraw()}>
      <Input.TextArea aria-label="撤销原因" placeholder="必须填写撤销原因" value={reason} onChange={(event) => setReason(event.target.value)} />
    </Modal>
    <Modal title="退回公共池" open={releaseOpen} okText="确认退回" cancelText="取消" confirmLoading={busy}
      okButtonProps={{ disabled: !reason.trim() }} onCancel={() => setReleaseOpen(false)} onOk={() => void release()}>
      <Input.TextArea aria-label="退回原因" placeholder="必须填写退回原因" value={reason} onChange={(event) => setReason(event.target.value)} />
    </Modal>
    {timeline && <Timeline key={`timeline-${timelineVersion}`} subjectType="presales_inquiry" subjectId={inquiry.id} />}
    {gallery && <GalleryPicker mode="modal" items={lines.filter((line) => line.part_id).map((line) => ({ partId: line.part_id!, pn: line.pn_raw }))} onClose={() => setGallery(false)} />}
    {importOpen && <WorkbookDialog mode="quote" inquiryId={inquiry.id} onClose={() => setImportOpen(false)} onApplied={() => {
      message.success("已保存为草稿，请在页面确认后回复"); setImportOpen(false); setLoadVersion((version) => version + 1); onTaskChange?.();
    }} />}
  </section>;
}
