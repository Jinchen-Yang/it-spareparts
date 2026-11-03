import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, App, Button, Checkbox, Collapse, Drawer, Input, InputNumber, Modal, Popover, Radio, Select, Space, Table, Tag, Timeline } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Link } from "react-router-dom";
import { presales, type InquiryLine } from "../../api/presales";
import {
  notifyPoolChanged, poolConflict, presalesPool, type PoolCounts, type PoolHistoryItem, type PoolLine, type PoolLineEdit, type PoolTab, type PriceBasis,
} from "../../api/presalesPool";
import { presalesWorkbooks } from "../../api/presalesWorkbooks";
import PageHeader from "../../components/PageHeader";
import PartThumb from "../../components/gallery/PartThumb";
import { WorkbookExportButton } from "../../components/workbook";
import { formatBusinessDateTime } from "../../utils/date";
import { moneyExact } from "../../utils/format";
import CandidateSelect from "./CandidateSelect";
import { DirectQuote } from "./DirectQuote";
import NewInquiryPanel from "./NewInquiryPanel";
import PartBinding, { canBindPresalesPart } from "./PartBinding";
import WorkbookDialog from "./WorkbookDialog";
import { isPresalesSupervisor, taxLabel, usePresalesError } from "./shared";
import "./presales.css";

const PAGE_SIZE = 50;
const STATE_LABEL = { pending: "待报价", quoted: "已报价", skip: "不报" } as const;
const STATE_COLOR = { pending: "orange", quoted: "green", skip: undefined } as const;
const CHANGED_LABEL: Record<string, string> = { qty: "数量", return_defective: "坏件返回", require_exact_pn: "PN 一致", notes: "备注", expected_delivery: "交期" };

interface Draft {
  unit_price?: number | null; is_tax_inclusive?: boolean | null; price_basis?: PriceBasis;
  lead_time?: string; stock_location?: string; qty_available?: number | null; notes?: string; supplier_id?: number | null;
}
type CloseOutcome = "won" | "lost" | "cancelled";

const priceOf = (row: PoolLine) => row.price?.unit_price == null ? null : Number(row.price.unit_price);
const basisText = (basis?: PriceBasis) => basis === "cost" ? "成本价" : "售价";

function sentence(row: PoolLine) {
  return `${row.pn} × ${Number(row.qty)}：${moneyExact(priceOf(row))} ${taxLabel(row.price?.is_tax_inclusive)}（${basisText(row.price?.price_basis)}，坏件${row.return_defective ? "返回" : "不返回"}）`;
}

function SkipButton({ disabled, onConfirm }: { disabled: boolean; onConfirm: (reason: string) => void }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("无货源");
  return <Popover trigger="click" open={open} onOpenChange={setOpen} content={<Space direction="vertical">
    <Input aria-label="不报原因" value={reason} onChange={(event) => setReason(event.target.value)} style={{ width: 220 }} />
    <Button type="primary" size="small" disabled={!reason.trim()} onClick={() => { setOpen(false); onConfirm(reason.trim()); }}>确定不报</Button>
  </Space>}>
    <Button size="small" disabled={disabled}>不报</Button>
  </Popover>;
}

function historyText(item: PoolHistoryItem) {
  if (item.kind === "line.skipped") return `标为不报${item.reason ? `：${item.reason}` : ""}`;
  if (item.kind === "line.edited") return `修改 ${(item.changed ?? []).map((key) => CHANGED_LABEL[key] ?? key).join("、")}`;
  if (item.status === "stale" || item.status === "withdrawn") return `价格作废${item.reason ? `：${item.reason}` : ""}`;
  return `${moneyExact(item.unit_price == null ? null : Number(item.unit_price))} ${taxLabel(item.is_tax_inclusive)} · ${basisText(item.price_basis)}`;
}

export default function PoolPage() {
  const { message, modal } = App.useApp();
  const handleError = usePresalesError();
  const canBind = canBindPresalesPart();
  const supervisor = isPresalesSupervisor();
  const [tab, setTab] = useState<PoolTab>("pending");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [rows, setRows] = useState<PoolLine[]>([]);
  const [total, setTotal] = useState(0);
  const [counts, setCounts] = useState<PoolCounts>();
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [drafts, setDrafts] = useState<Record<number, Draft & { baseVersion: number }>>({});
  const [busy, setBusy] = useState<number>();
  const [selected, setSelected] = useState<number[]>([]);
  const [binding, setBinding] = useState<PoolLine>();
  const [history, setHistory] = useState<{ row: PoolLine; items?: PoolHistoryItem[] }>();
  const [importOpen, setImportOpen] = useState(false);
  const [direct, setDirect] = useState(false);
  const [closing, setClosing] = useState<{ row: PoolLine; outcome: CloseOutcome }>();
  const [closeBusy, setCloseBusy] = useState(false);
  const [qtyNonce, setQtyNonce] = useState(0);
  const resetQty = () => setQtyNonce((value) => value + 1);
  const generation = useRef(0);

  const load = useCallback(async () => {
    const current = ++generation.current;
    setLoading(true); setFailed(false);
    try {
      const result = await presalesPool.lines({ tab, page, page_size: PAGE_SIZE, ...(query.trim() ? { q: query.trim() } : {}) });
      if (current !== generation.current) return;
      setRows(result.items); setTotal(result.total); setCounts(result.counts);
    } catch (error) {
      if (current === generation.current) { setFailed(true); await handleError(error); }
    } finally { if (current === generation.current) setLoading(false); }
  }, [tab, page, query, handleError]);
  useEffect(() => { void load(); return () => { generation.current += 1; }; }, [load]);

  const replace = (row: PoolLine) => setRows((previous) => previous.map((item) => item.line_id === row.line_id ? row : item));
  const clearDraft = (lineId: number) => setDrafts((previous) => { const next = { ...previous }; delete next[lineId]; return next; });
  const change = (row: PoolLine, patch: Draft) => setDrafts((previous) => ({
    ...previous, [row.line_id]: { ...previous[row.line_id], ...patch,
      baseVersion: previous[row.line_id]?.baseVersion ?? row.line_version },
  }));
  const advanceDraft = (lineId: number, from: number, to: number) => setDrafts((previous) => {
    const draft = previous[lineId];
    return draft?.baseVersion === from ? { ...previous, [lineId]: { ...draft, baseVersion: to } } : previous;
  });
  const priceFor = (row: PoolLine) => drafts[row.line_id]?.unit_price !== undefined ? drafts[row.line_id].unit_price ?? null : priceOf(row);
  const taxFor = (row: PoolLine) => drafts[row.line_id]?.is_tax_inclusive !== undefined ? drafts[row.line_id].is_tax_inclusive ?? null : row.price ? row.price.is_tax_inclusive : true;
  const basisFor = (row: PoolLine) => drafts[row.line_id]?.price_basis ?? row.price?.price_basis ?? "sale";
  /** 报价详情：有输入取输入，否则取当前价；空文本提交时存为空。 */
  const detailOf = (row: PoolLine) => {
    const draft = drafts[row.line_id] ?? {};
    return { lead_time: draft.lead_time ?? row.price?.lead_time ?? "", stock_location: draft.stock_location ?? row.price?.stock_location ?? "",
      notes: draft.notes ?? row.price?.notes ?? "", supplier_id: draft.supplier_id !== undefined ? draft.supplier_id : row.price?.supplier_id ?? null,
      qty_available: draft.qty_available !== undefined ? draft.qty_available : row.price?.qty_available == null ? null : Number(row.price.qty_available) };
  };
  const detailChanged = (row: PoolLine) => {
    const detail = detailOf(row);
    return detail.lead_time !== (row.price?.lead_time ?? "") || detail.stock_location !== (row.price?.stock_location ?? "")
      || detail.notes !== (row.price?.notes ?? "") || detail.supplier_id !== (row.price?.supplier_id ?? null)
      || detail.qty_available !== (row.price?.qty_available == null ? null : Number(row.price.qty_available));
  };
  const dirty = (row: PoolLine) => priceFor(row) != null && (row.price == null || priceFor(row) !== priceOf(row)
    || taxFor(row) !== row.price.is_tax_inclusive || basisFor(row) !== row.price.price_basis || detailChanged(row));

  /** 写入成功只换这一行，不重读整张表；冲突时显示最新内容并保留本人输入。 */
  const write = async (row: PoolLine, target: "price" | "skip" | "inquiry", action: () => Promise<PoolLine>, success?: string) => {
    if (busy != null) return;
    setBusy(row.line_id);
    try {
      const updated = await action();
      replace(updated);
      if (target === "inquiry") advanceDraft(row.line_id, row.line_version, updated.line_version);
      else clearDraft(row.line_id);
      notifyPoolChanged();
      // 这一行留在当前页不跳走，但筛选数量要跟上。
      void presalesPool.counts().then(setCounts).catch(() => undefined);
      if (success) void message.success(success);
    } catch (error) {
      resetQty();
      const conflict = poolConflict(error);
      if (conflict?.code === "version_conflict") {
        replace(conflict.latest);
        void message.warning(conflict.latest.answered_by
          ? `${row.pn} 刚被 ${conflict.latest.answered_by} 更新为 ${moneyExact(priceOf(conflict.latest))}，你的输入已保留，核对后再保存`
          : `${row.pn} 刚被其他人修改，你的输入已保留，核对后再保存`);
      } else await handleError(error);
    } finally { setBusy(undefined); }
  };

  const save = (row: PoolLine) => {
    const unitPrice = priceFor(row);
    if (unitPrice == null) return;
    const detail = detailChanged(row) ? detailOf(row) : undefined;
    void write(row, "price", () => presalesPool.setPrice(row.line_id, { expected_version: drafts[row.line_id]?.baseVersion ?? row.line_version, unit_price: unitPrice,
      is_tax_inclusive: taxFor(row), price_basis: basisFor(row), ...(detail ? { lead_time: detail.lead_time.trim() || null,
        stock_location: detail.stock_location.trim() || null, notes: detail.notes.trim() || null,
        qty_available: detail.qty_available, supplier_id: detail.supplier_id } : {}) }), `${row.pn} 报价已生效`);
  };

  const edit = (row: PoolLine, patch: Omit<PoolLineEdit, "expected_version" | "confirm_reset">) => {
    const send = (confirmReset: boolean) => void write(row, "inquiry", () => presalesPool.edit(row.line_id, { expected_version: row.line_version, ...patch, ...(confirmReset ? { confirm_reset: true } : {}) }));
    // 备注不影响价格；待报价行没有可作废的答复。
    if (row.answer_state === "pending" || !["qty", "return_defective", "require_exact_pn"].some((key) => key in patch)) { send(false); return; }
    modal.confirm({ title: "修改后原答复作废", content: `${row.pn} 改条件后，原来的${row.answer_state === "quoted" ? "报价" : "不报记录"}会作废并留在历史中，这一行回到待报价。确定修改吗？`,
      okText: "确定修改", cancelText: "取消", onOk: () => send(true), onCancel: resetQty });
  };

  const closeInquiry = async () => {
    if (!closing) return;
    setCloseBusy(true);
    try {
      const { row, outcome } = closing;
      await presales.closeInquiry(row.inquiry_id, { expected_version: row.inquiry_version, outcome,
        ...(outcome === "won" && row.price ? { won_offer_id: row.price.offer_id } : {}) });
      void message.success(`${row.inquiry_no} 已关单`);
      setClosing(undefined);
      notifyPoolChanged();
      void load();
    } catch (error) { await handleError(error, load); }
    finally { setCloseBusy(false); }
  };

  const showHistory = async (row: PoolLine) => {
    setHistory({ row });
    try { setHistory({ row, items: await presalesPool.history(row.line_id) }); }
    catch (error) { setHistory(undefined); await handleError(error); }
  };

  const copy = async (row: PoolLine) => {
    try { if (!navigator.clipboard) throw new Error("clipboard unavailable"); await navigator.clipboard.writeText(sentence(row)); void message.success("已复制，可直接发给客户"); }
    catch { void message.warning("复制失败，请选中报价文字手动复制"); }
  };

  const priceDisabled = (row: PoolLine) => busy != null || !row.part_id || row.inquiry_status === "closed";

  const columns: ColumnsType<PoolLine> = [
    { title: "询价人 / 单号", key: "who", width: 150, render: (_, row) => <div className="presales-cell">
      <b className="presales-sales">{row.requester ?? "—"}</b>
      <Link className="presales-muted" to={`/presales/inquiries/${row.inquiry_id}`}>{row.inquiry_no}{row.customer_name ? ` · ${row.customer_name}` : ""}</Link>
      <Space size={4} wrap>
        {row.priority === "urgent" && <Tag color="red">紧急</Tag>}
        {row.inquiry_status === "closed" && <Tag>{({ won: "已成交", lost: "未成交", cancelled: "已取消" })[row.closed_outcome ?? "cancelled"]}</Tag>}
      </Space>
    </div> },
    { title: "PN / 描述", key: "pn", render: (_, row) => <div className="presales-cell">
      <Space>{row.part_id && <PartThumb partId={row.part_id} pn={row.pn} size={32} />}<b>{row.pn}</b></Space>
      <span className="presales-muted presales-ellipsis" title={row.description ?? undefined}>{row.description || "—"}</span>
      {row.notes && <span className="presales-muted presales-ellipsis" title={row.notes}>备注：{row.notes}</span>}
      {!row.part_id && <Space size={4}><Tag color="warning">待绑定型号</Tag>
        {canBind && row.inquiry_status !== "closed" && <Button type="link" size="small" disabled={busy != null} onClick={() => setBinding(row)}>绑定 / 建档</Button>}
      </Space>}
    </div> },
    { title: "数量", key: "qty", width: 92, render: (_, row) => row.inquiry_status === "closed" ? Number(row.qty)
      : <InputNumber key={`${row.line_id}:${row.line_version}:${qtyNonce}`} aria-label={`${row.pn} 数量`} size="small" min={0.001} style={{ width: "100%" }} disabled={busy != null} defaultValue={Number(row.qty)}
        onBlur={(event) => { const value = Number(event.target.value); if (value > 0 && value !== Number(row.qty)) edit(row, { qty: value }); else resetQty(); }} /> },
    { title: "坏件返回", key: "return", width: 76, align: "center", render: (_, row) => <Checkbox aria-label={`${row.pn} 坏件返回`} checked={row.return_defective}
      disabled={busy != null || row.inquiry_status === "closed"} onChange={(event) => edit(row, { return_defective: event.target.checked })} /> },
    { title: "PN 一致", key: "exact", width: 70, align: "center", render: (_, row) => <Checkbox aria-label={`${row.pn} 要求 PN 一致`} checked={row.require_exact_pn}
      disabled={busy != null || row.inquiry_status === "closed"} onChange={(event) => edit(row, { require_exact_pn: event.target.checked })} /> },
    { title: "单价（元）", key: "price", width: 150, render: (_, row) => <div className="presales-cell">
      <InputNumber aria-label={`${row.pn} 单价`} disabled={priceDisabled(row)} min={0} max={999999999999.99} precision={2} style={{ width: "100%" }} placeholder="单价"
        value={priceFor(row)} onChange={(value) => change(row, { unit_price: value })} onPressEnter={() => { if (dirty(row)) save(row); }} />
      {drafts[row.line_id] && drafts[row.line_id].baseVersion !== row.line_version && <>
        <span className="presales-muted">行已更新，当前答复：{row.price ? sentence(row) : STATE_LABEL[row.answer_state]}。你的输入已保留。</span>
        <Button type="link" size="small" disabled={priceDisabled(row)}
          onClick={() => advanceDraft(row.line_id, drafts[row.line_id].baseVersion, row.line_version)}>已核对，按此版本编辑</Button>
      </>}
      {row.latest_same_pn && <Button type="link" size="small" className="presales-reuse" disabled={priceDisabled(row)}
        title={`${row.latest_same_pn.reporter ?? ""} ${formatBusinessDateTime(row.latest_same_pn.at)}`}
        onClick={() => change(row, { unit_price: Number(row.latest_same_pn!.unit_price), is_tax_inclusive: row.latest_same_pn!.is_tax_inclusive, price_basis: row.latest_same_pn!.price_basis })}>
        沿用 {moneyExact(Number(row.latest_same_pn.unit_price))}</Button>}
    </div> },
    { title: "税", key: "tax", width: 98, render: (_, row) => <Select aria-label={`${row.pn} 含税`} size="small" disabled={priceDisabled(row)} style={{ width: "100%" }} value={taxFor(row) == null ? "unknown" : taxFor(row) ? "inc" : "ex"}
      onSelect={(value) => change(row, { is_tax_inclusive: value === "unknown" ? null : value === "inc" })}
      options={[{ value: "inc", label: "含税" }, { value: "ex", label: "不含税" }, ...(taxFor(row) == null ? [{ value: "unknown", label: "未声明" }] : [])]} /> },
    { title: "性质", key: "basis", width: 92, render: (_, row) => <Select aria-label={`${row.pn} 价格性质`} size="small" disabled={priceDisabled(row)} style={{ width: "100%" }} value={basisFor(row)}
      onSelect={(value) => change(row, { price_basis: value })} options={[{ value: "sale", label: "售价" }, { value: "cost", label: "成本价" }]} /> },
    { title: "状态 / 更新人", key: "state", width: 130, render: (_, row) => <div className="presales-cell">
      <Tag color={STATE_COLOR[row.answer_state]}>{STATE_LABEL[row.answer_state]}</Tag>
      {row.answered_by && <span className="presales-muted">{row.answered_by} {formatBusinessDateTime(row.answered_at)}</span>}
      {row.answer_state === "skip" && row.answer_reason && <span className="presales-muted">{row.answer_reason}</span>}
    </div> },
    { title: "操作", key: "actions", width: 150, render: (_, row) => <Space size={4} wrap>
      <Button type="primary" size="small" loading={busy === row.line_id} disabled={priceDisabled(row) || !dirty(row)} onClick={() => save(row)}>保存</Button>
      {row.inquiry_status !== "closed" && row.answer_state !== "skip" && <SkipButton disabled={busy != null} onConfirm={(reason) => void write(row, "skip", () => presalesPool.skip(row.line_id, { expected_version: row.line_version, reason }), `${row.pn} 已标为不报`)} />}
      <Button type="link" size="small" onClick={() => void showHistory(row)}>历史</Button>
      {row.answer_state === "quoted" && <Button type="link" size="small" aria-label={`复制 ${row.pn} 报价给客户`} onClick={() => void copy(row)}>复制</Button>}
      {(row.is_mine || supervisor) && row.inquiry_status !== "closed" && <Button type="link" size="small" aria-label={`关闭询价 ${row.inquiry_no}`} disabled={busy != null} onClick={() => setClosing({ row, outcome: "lost" })}>关单</Button>}
    </Space> },
  ];

  const tabs: [PoolTab, string, number | undefined][] = [["pending", "待报价", counts?.pending], ["quoted", "已报价", counts?.quoted], ["skip", "不报", counts?.skip], ["mine", "我发起的", counts?.mine], ["all", "全部", counts?.all]];
  const exportIds = [...new Set(rows.filter((row) => selected.includes(row.line_id) && row.inquiry_status !== "closed").map((row) => row.inquiry_id))];
  return <section className="presales-page presales-stack">
    <PageHeader title="询价报价" subtitle="所有人共用一个池子：谁都能询价、报价和改价，保存立即生效，系统记录更新人" extra={<Space>
      <Link to="/presales/inquiries/new">Excel 询价</Link>
      <Link to="/presales/library">报价库</Link>
      <Link to="/presales/inquiries?status=draft">我的草稿{counts?.drafts ? ` ${counts.drafts}` : ""}</Link>
      <Button onClick={() => setDirect(!direct)}>主动报价</Button>
    </Space>} />
    {direct && <div className="presales-panel"><DirectQuote onClose={() => setDirect(false)} onDone={(text) => { setDirect(false); void message.success(text); void load(); }} /></div>}
    <Collapse size="small" items={[{ key: "new", label: "新建询价", children: <NewInquiryPanel onCreated={() => { setPage(1); void load(); }} /> }]} />
    <div className="presales-panel presales-stack">
      <div className="presales-toolbar">
        {tabs.map(([key, label, count]) => <Button key={key} aria-pressed={tab === key} type={tab === key ? "primary" : "default"} ghost={tab === key}
          onClick={() => { setTab(key); setPage(1); setSelected([]); }}>{label}{count != null ? ` ${count}` : ""}</Button>)}
        <Input.Search aria-label="搜索询价报价" allowClear placeholder="PN / 单号 / 询价人 / 客户" style={{ width: 260, marginLeft: "auto" }}
          onSearch={(value) => { setQuery(value); setPage(1); }} />
        <Button onClick={() => void load()} disabled={loading || busy != null}>刷新</Button>
      </div>
      <div className="presales-toolbar presales-tight">
        <WorkbookExportButton label="下载所选询价单的报价表" schemaName="presales_quote_sheet" disabled={!exportIds.length}
          fetchSchema={() => presalesWorkbooks.getQuoteSheetSchema()} filename="合并报价表.xlsx" onExport={(visible) => presalesWorkbooks.exportQuoteSheet(exportIds, visible)} />
        <Button onClick={() => setImportOpen(true)}>上传报价表</Button>
        <span className="presales-muted">勾选行后，报价表按未关单的询价单导出可填行。旧版直接引用报价的行请在页面改价。上传后须预览并确认。</span>
      </div>
      {failed && <Alert type="error" message="询价报价加载失败" action={<Button onClick={() => void load()}>重试</Button>} />}
      <Table aria-label="询价报价" rowKey="line_id" size="middle" dataSource={rows} columns={columns} tableLayout="fixed" scroll={{ x: 1260 }} loading={loading}
        rowSelection={{ selectedRowKeys: selected, onChange: (keys) => setSelected(keys as number[]),
          getCheckboxProps: (row) => ({ disabled: row.inquiry_status === "closed",
            "aria-label": `选择报价表来源 ${row.inquiry_no} ${row.pn}` }) }}
        rowClassName={(row) => dirty(row) ? "presales-row-editing" : ""}
        expandable={{ expandedRowRender: (row) => {
          const detail = detailOf(row);
          const off = priceDisabled(row);
          return <div className="presales-more">
            <label>交期<Input aria-label={`${row.pn} 交期`} disabled={off} value={detail.lead_time} onChange={(event) => change(row, { lead_time: event.target.value })} /></label>
            <label>所在地<Input aria-label={`${row.pn} 所在地`} disabled={off} value={detail.stock_location} onChange={(event) => change(row, { stock_location: event.target.value })} /></label>
            <label>可供数量<InputNumber aria-label={`${row.pn} 可供数量`} disabled={off} min={0} precision={3} style={{ width: "100%" }} value={detail.qty_available} onChange={(value) => change(row, { qty_available: value })} /></label>
            <label>供应商<CandidateSelect label="供应商" load={presales.suppliers} disabled={off} value={detail.supplier_id} onChange={(value) => change(row, { supplier_id: value ?? null })} />
              {row.price?.supplier_name && <span className="presales-muted">当前：{row.price.supplier_name}</span>}</label>
            <label className="presales-wide">报价备注<Input aria-label={`${row.pn} 报价备注`} disabled={off} value={detail.notes} onChange={(event) => change(row, { notes: event.target.value })} /></label>
            <label className="presales-wide">询价备注（失焦保存）<Input key={`${row.line_id}:${row.line_version}`} aria-label={`${row.pn} 询价备注`} disabled={busy != null || row.inquiry_status === "closed"} defaultValue={row.notes ?? ""}
              onBlur={(event) => { const value = event.target.value.trim() || null; if (value !== (row.notes ?? null)) edit(row, { notes: value }); }} /></label>
            {row.inquiry_notes && <span className="presales-wide presales-muted">客户要求：{row.inquiry_notes}</span>}
            {row.expected_delivery && <span className="presales-muted">期望交期：{row.expected_delivery}</span>}
            <span className="presales-wide presales-muted">交期、货源等改完后点这一行的「保存」生效。</span>
          </div>;
        } }}
        locale={{ emptyText: loading ? "加载中" : "没有符合条件的询价行" }}
        pagination={{ current: page, pageSize: PAGE_SIZE, total, showSizeChanger: false, hideOnSinglePage: true, onChange: setPage, showTotal: (value) => `共 ${value} 行` }} />
    </div>
    {closing && <Modal open title={`关闭询价 ${closing.row.inquiry_no}`} okText="确定关单" cancelText="取消" confirmLoading={closeBusy}
      onOk={() => void closeInquiry()} onCancel={() => { if (!closeBusy) setClosing(undefined); }}>
      <p>关单针对整张询价单，这张单的所有行都会离开待报价。价格和历史会保留。</p>
      <Radio.Group aria-label="关单结果" value={closing.outcome} onChange={(event) => setClosing({ ...closing, outcome: event.target.value })} options={[
        ...(closing.row.answer_state === "quoted" ? [{ value: "won", label: `已成交（${closing.row.pn} ${moneyExact(priceOf(closing.row))}）` }] : []),
        { value: "lost", label: "未成交" }, { value: "cancelled", label: "已取消" },
      ]} />
    </Modal>}
    {binding && <Modal open title={`绑定型号：${binding.pn}`} footer={null} destroyOnHidden onCancel={() => setBinding(undefined)}>
      <PartBinding compact line={{ id: binding.line_id, version: binding.line_version, pn_raw: binding.pn, description: binding.description } as InquiryLine}
        refresh={load} onBound={() => { setBinding(undefined); void load(); }} />
    </Modal>}
    <Drawer open={!!history} title={history ? `${history.row.pn} 的历史` : ""} onClose={() => setHistory(undefined)} loading={!!history && !history.items} destroyOnHidden>
      {history?.items && (history.items.length ? <Timeline items={history.items.map((item, index) => ({ key: index, children: <div className="presales-cell">
        <b>{historyText(item)}</b><span className="presales-muted">{item.actor ?? "—"} · {formatBusinessDateTime(item.at)}</span>
      </div> }))} /> : <span className="presales-muted">还没有记录</span>)}
    </Drawer>
    {importOpen && <WorkbookDialog mode="quote" onClose={() => setImportOpen(false)} onApplied={() => { setImportOpen(false); notifyPoolChanged(); void load(); }} />}
  </section>;
}
