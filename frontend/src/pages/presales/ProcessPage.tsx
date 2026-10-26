import { useCallback, useEffect, useState } from "react";
import { Alert, App, Button, Card, Descriptions, Empty, Input, Modal, Space, Spin, Tag, Typography } from "antd";
import { Link, useLocation, useParams } from "react-router-dom";
import { presales, type Inquiry, type InquiryLine, type NearDuplicate, type Offer } from "../../api/presales";
import { listWorkItems, type WorkItem, type WorkTab } from "../../api/workItems";
import type { PartSuggestion } from "../../api/search";
import LivePartSearch from "../../components/parts/LivePartSearch";
import PartThumb from "../../components/gallery/PartThumb";
import GalleryPicker from "../../components/gallery/GalleryPicker";
import PriceRefBadge from "../../components/pricing/PriceRefBadge";
import Timeline from "../../components/workflow/Timeline";
import { refreshWorkBadges } from "../../components/workflow/useWorkBadge";
import QuoteEditor, { offerInput, quoteComplete } from "./QuoteEditor";
import PartQuoteBadges from "./PartQuoteBadges";
import { usePresalesError, OfferBadge } from "./shared";

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
  const references = (inquiry.lines ?? []).filter((line) => line.ref_offer_id && line.part_id);
  if (inquiry.kind !== "proactive_offer" || !references.length) return inquiry;
  const library = await presales.library(references.map((line) => line.part_id!));
  const offers = new Map((inquiry.offers ?? []).map((offer) => [offer.id, offer]));
  for (const offer of Object.values(library).flat()) {
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

function QuoteLine({ inquiry, line, offer, taskId, editable, refresh, onDirty }: {
  inquiry: Inquiry; line: InquiryLine; offer?: Offer; taskId?: number;
  editable: boolean; refresh: () => Promise<void>; onDirty: (lineId: number, dirty: boolean) => void;
}) {
  const handlePresalesError = usePresalesError();
  const { message } = App.useApp();
  const restart = editable && inquiry.kind === "sales_inquiry" && (offer?.status === "stale" || offer?.status === "withdrawn");
  const newQuote = !offer || restart;
  const [value, setValue] = useState(() => offerInput(newQuote ? undefined : offer));
  const [busy, setBusy] = useState(false);
  const [withdrawOpen, setWithdrawOpen] = useState(false);
  const [reason, setReason] = useState("");
  const published = offer?.status === "published";
  const canEdit = editable;
  const save = async () => {
    setBusy(true);
    try {
      if (published) await presales.updateOffer(offer.id, { ...value, expected_version: offer.version });
      else await presales.saveOffer(inquiry.id, line.id, { ...value,
        expected_version: !newQuote && offer ? offer.version : inquiry.version,
        ...(!newQuote && offer ? { offer_id: offer.id } : {}),
        ...(taskId ? { work_item_id: taskId } : {}) });
      onDirty(line.id, false);
      message.success(published ? "报价已更新，已保存历史版本" : "该行报价已保存");
      await refresh();
    } catch (error) { handlePresalesError(error, refresh); }
    finally { setBusy(false); }
  };
  const withdraw = async () => {
    if (!offer || !reason.trim()) return;
    setBusy(true);
    try {
      await presales.withdrawOffer(offer.id, { expected_version: offer.version, reason: reason.trim() });
      onDirty(line.id, false); setWithdrawOpen(false); setReason("");
      message.success("报价已撤销"); await refresh();
    } catch (error) { handlePresalesError(error, refresh); }
    finally { setBusy(false); }
  };

  return <>
    {offer && <Space style={{ marginBottom: 12 }}>{offer.status === "published" && <OfferBadge offer={offer} />}
      {offer.status === "draft" && <Tag>已保存草稿</Tag>}
      {offer.status === "withdrawn" && <Tag>已撤销</Tag>}
      {offer.status === "stale" && <Tag>基于旧版本</Tag>}
    </Space>}
    <QuoteEditor label={line.pn_raw} value={value} existing={newQuote ? undefined : offer} disabled={!canEdit || busy}
      onChange={(next) => { setValue(next); onDirty(line.id, true); }} />
    {canEdit && <Space>
      <Button type="primary" loading={busy} disabled={!quoteComplete(value) || (!line.part_id && !value.no_stock)}
        onClick={() => void save()}>{published ? "更新报价" : "保存该行报价"}</Button>
      {published && <Button danger disabled={busy} onClick={() => { setReason(""); setWithdrawOpen(true); }}>撤销报价</Button>}
    </Space>}
    <Modal title="撤销报价" open={withdrawOpen} okText="确认撤销" cancelText="取消" confirmLoading={busy}
      okButtonProps={{ disabled: !reason.trim() }} onCancel={() => setWithdrawOpen(false)} onOk={() => void withdraw()}>
      <Input.TextArea aria-label="撤销原因" placeholder="必须填写撤销原因" value={reason}
        onChange={(event) => setReason(event.target.value)} />
    </Modal>
  </>;
}

export default function ProcessPage() {
  const { id } = useParams();
  return <ProcessContent key={id} inquiryId={Number(id)} />;
}

function ProcessContent({ inquiryId }: { inquiryId: number }) {
  const handlePresalesError = usePresalesError();
  const location = useLocation();
  const navigationTask = (location.state as { workItem?: WorkItem } | null)?.workItem;
  const { message } = App.useApp();
  const [inquiry, setInquiry] = useState<Inquiry>();
  const [task, setTask] = useState<WorkItem | undefined>(navigationTask?.subject_id === inquiryId ? navigationTask : undefined);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [gallery, setGallery] = useState(false);
  const [dirty, setDirty] = useState<Record<number, boolean>>({});
  const [timelineVersion, setTimelineVersion] = useState(0);
  const refresh = useCallback(async () => {
    const latest = await loadInquiry(inquiryId);
    setInquiry(latest); setTimelineVersion((value) => value + 1);
  }, [inquiryId]);
  useEffect(() => {
    let cancelled = false;
    setLoading(true); setLoadError(false); setInquiry(undefined); setDirty({});
    const currentTask = navigationTask?.subject_id === inquiryId ? navigationTask : undefined;
    setTask(currentTask);
    loadInquiry(inquiryId).then(async (data) => {
      const ownTask = currentTask ?? (data.kind === "proactive_offer" ? undefined : await findOwnTask(inquiryId));
      if (!cancelled) { setInquiry(data); setTask(ownTask); }
    })
      .catch((error) => { if (!cancelled) { setLoadError(true); handlePresalesError(error); } })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [inquiryId, navigationTask]);

  if (loading) return <Spin />;
  if (!inquiry) return <Alert type="error" message={loadError ? "询价单加载失败" : "询价单不可用"}
    action={<Button onClick={() => void refresh().catch((error) => handlePresalesError(error))}>重试</Button>} />;

  const liveTask = inquiry.work_items?.find((item) => item.id === task?.id);
  const taskStatus = liveTask?.status ?? task?.status;
  const active = taskStatus === "pending" || taskStatus === "in_progress";
  const paused = liveTask?.paused ?? task?.paused;
  const proactive = inquiry.kind === "proactive_offer";
  const actorId = proactive ? inquiry.created_by : task?.assignee_id;
  const ownOffers = actorId == null ? [] : (inquiry.offers ?? []).filter((offer) => offer.reporter?.id === actorId);
  const lines = inquiry.lines ?? [];
  const required = lines.filter((line) => !line.ref_offer_id);
  const complete = required.length > 0 && required.every((line) => !dirty[line.id]
    && ownOffers.some((offer) => offer.line_id === line.id && ["draft", "published"].includes(offer.status)
      && quoteComplete(offer) && (!!line.part_id || offer.no_stock)));
  const editable = inquiry.status === "submitted" && (proactive || !!task) && !paused;
  const reply = async () => {
    if (!complete || !task) return;
    setBusy(true);
    try {
      await presales.replyInquiry(inquiry.id, { expected_version: inquiry.version, work_item_id: task.id,
        expected_work_item_version: liveTask?.version ?? task.version });
      message.success("已回复给销售"); refreshWorkBadges(); await refresh();
    } catch (error) { handlePresalesError(error, refresh); }
    finally { setBusy(false); }
  };

  return <Space direction="vertical" size="middle" style={{ width: "100%" }}>
    <Space wrap><Typography.Title level={3} style={{ margin: 0 }}>处理询价 · {inquiry.inquiry_no}</Typography.Title>
      <Link to="/presales/tasks">返回采购待办</Link>
      <Button onClick={() => setGallery(true)} disabled={!lines.some((line) => line.part_id)}>查看图片 / 多选下载</Button>
    </Space>
    <Descriptions size="small" bordered column={2}>
      <Descriptions.Item label="询价行数">{lines.length}</Descriptions.Item>
      {"notes" in inquiry && <Descriptions.Item label="客户要求">{inquiry.notes}</Descriptions.Item>}
    </Descriptions>
    {inquiry.field_values?.map((field, index) => <Typography.Paragraph key={`header-field-${index}`}>
      {field.name ?? field.new_name}: {String(field.value ?? "")}
    </Typography.Paragraph>)}
    {!task && !proactive && <Alert type="info" message="请从自己的采购待办打开本单，领取或接受后即可处理" />}
    {paused && <Alert type="warning" message="询价已撤回，待办暂停" />}
    {!lines.length && <Empty description="暂无询价行" />}
    {lines.map((line) => {
      const current = (proactive && line.ref_offer_id ? ownOffers.find((offer) => offer.id === line.ref_offer_id) : undefined)
        ?? ownOffers.find((offer) => offer.line_id === line.id && offer.status !== "withdrawn" && offer.status !== "stale")
        ?? ownOffers.find((offer) => offer.line_id === line.id);
      return <Card key={`line-${line.id}`} title={<Space wrap>
        {line.part_id && <PartThumb partId={line.part_id} pn={line.pn_raw} />}
        <span>{line.seq}. {line.pn_raw}</span><Tag>需求 {line.qty}</Tag>
        {line.require_exact_pn && <Tag>要求 PN 一致</Tag>}
        {line.part_id && <PriceRefBadge partId={line.part_id} />}
      </Space>}>
        {line.description && <Typography.Paragraph>{line.description}</Typography.Paragraph>}
        {"notes" in line && line.notes && <Typography.Paragraph>{line.notes}</Typography.Paragraph>}
        {line.field_values?.map((field, index) => <Typography.Paragraph key={index}>{field.name ?? field.new_name}: {String(field.value ?? "")}</Typography.Paragraph>)}
        {line.ref_offer_id && !proactive ? <Tag color="success">已引用有效报价，无需再次处理</Tag> : <>
          {!line.part_id && editable && active && <PartBinding line={line} onBound={setInquiry} refresh={refresh} />}
          {(!proactive || current) && <QuoteLine key={`${current?.id ?? "new"}:${current?.version ?? 0}`} inquiry={inquiry} line={line} offer={current}
            taskId={task?.id} editable={editable && (proactive ? current?.status === "published" : active || current?.status === "published")} refresh={refresh}
            onDirty={(lineId, isDirty) => setDirty((previous) => ({ ...previous, [lineId]: isDirty }))} />}
        </>}
        {proactive && (!current || current.status !== "published") && <Alert type="info"
          message={line.ref_offer_id && !current ? "引用报价当前不可用，请回原来源单查看，或新建主动报价" : "当前报价不可更新，可新建主动报价"}
          action={<Link to="/presales/offers/new">新建主动报价</Link>} />}
      </Card>;
    })}
    {active && <Button type="primary" loading={busy} disabled={!complete || !editable} onClick={() => void reply()}>回复给销售</Button>}
    {active && !complete && <Typography.Text type="secondary">每行保存报价或标记无货后，才能回复给销售。</Typography.Text>}
    <Timeline key={`timeline-${timelineVersion}`} subjectType="presales_inquiry" subjectId={inquiry.id} />
    {gallery && <GalleryPicker mode="modal" items={lines.filter((line) => line.part_id)
      .map((line) => ({ partId: line.part_id!, pn: line.pn_raw }))} onClose={() => setGallery(false)} />}
  </Space>;
}
