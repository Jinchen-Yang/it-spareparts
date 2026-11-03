import { useEffect, useRef, useState } from "react";
import { App, AutoComplete, Button, Checkbox, Collapse, Input, InputNumber, Radio, Space, Table } from "antd";
import { DeleteOutlined } from "@ant-design/icons";
import { presales, type Offer } from "../../api/presales";
import { notifyPoolChanged, presalesPool } from "../../api/presalesPool";
import { suggestParts, type PartSuggestion } from "../../api/search";
import CreatePartModal from "../../components/parts/CreatePartModal";
import { readPermissionMap } from "../../nav";
import CandidateSelect from "./CandidateSelect";
import { LatestPriceTag, latestActiveOffer, usePresalesError } from "./shared";
import "./presales.css";

const EXACT_KEY = "presales:exact-pn";

interface Row { key: number; pn: string; part?: PartSuggestion; latest?: Offer; qty: number; exact: boolean; returnDefective: boolean; note: string }

function readExact() {
  try { return localStorage.getItem(EXACT_KEY) !== "0"; } catch { return true; }
}
function writeExact(value: boolean) {
  try { localStorage.setItem(EXACT_KEY, value ? "1" : "0"); } catch { /* 偏好不可写不影响填写 */ }
}
const normalize = (text: string) => text.trim().toUpperCase();

/** PN 输入：联想已建档型号；未选中时按 PN 精确匹配自动带出描述；粘贴多行拆成多行。 */
function PnInput({ row, onChange, onPasteMany }: { row: Row; onChange: (patch: Partial<Row>) => void; onPasteMany: (pns: string[]) => void }) {
  const [options, setOptions] = useState<PartSuggestion[]>([]);
  const [latest, setLatest] = useState<Record<number, Offer | undefined>>({});
  useEffect(() => {
    const query = row.pn.trim();
    if (!query || row.part?.pn_std === row.pn) { setOptions([]); return; }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      suggestParts(query, 8, controller.signal).then(async (items) => {
        if (controller.signal.aborted) return;
        setOptions(items);
        if (!items.length) return;
        // 候选一次批量读报价库，每个 PN 只标最新一条有效价。
        // 精确匹配要等价格回来再回写本行：先回写会触发本 effect 清理，取消价格请求。
        const library = await presales.library(items.map((item) => item.part_id)).catch(() => ({} as Record<number, Offer[]>));
        if (controller.signal.aborted) return;
        const prices = Object.fromEntries(items.map((item) => [item.part_id, latestActiveOffer(library[item.part_id] ?? [])]));
        setLatest(prices);
        const exact = items.find((item) => normalize(item.pn_std) === normalize(query));
        if (exact && !row.part) onChange({ part: exact, latest: prices[exact.part_id] });
      }).catch(() => undefined);
    }, 250);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [row.pn, row.part]);
  return <AutoComplete aria-label={`第 ${row.key} 行 PN`} value={row.pn} placeholder="输入或粘贴 PN" style={{ width: "100%" }} popupMatchSelectWidth={480}
    onChange={(value) => onChange({ pn: value, ...(row.part && normalize(value) !== normalize(row.part.pn_std) ? { part: undefined, latest: undefined } : {}) })}
    onSelect={(_, option) => { const part = options.find((item) => String(item.part_id) === option.key); if (part) onChange({ pn: part.pn_std, part, latest: latest[part.part_id] }); }}
    options={options.map((item) => ({ key: String(item.part_id), value: item.pn_std, label: <span className="presales-option">
      <b>{item.pn_std}</b><span className="presales-muted">{item.summary || item.description}</span><LatestPriceTag offer={latest[item.part_id]} />
    </span> }))}>
    <Input onPaste={(event) => {
      const parts = event.clipboardData.getData("text").split(/[\n\r\t,，;；]+/).map((text) => text.trim()).filter(Boolean);
      if (parts.length > 1) { event.preventDefault(); onPasteMany(parts); }
    }} />
  </AutoComplete>;
}

export default function NewInquiryPanel({ onCreated }: { onCreated: () => void }) {
  const { message } = App.useApp();
  const handleError = usePresalesError();
  const customerVisible = localStorage.getItem("role") === "admin" || readPermissionMap().data_customer !== false;
  const canCreatePart = localStorage.getItem("role") === "admin" || readPermissionMap().page_master_data === true;
  const [creating, setCreating] = useState<Row>();
  const nextKey = useRef(2);
  const [exactPref, setExactPref] = useState(readExact);
  const blank = (key: number): Row => ({ key, pn: "", qty: 1, exact: exactPref, returnDefective: true, note: "" });
  const [rows, setRows] = useState<Row[]>([blank(1)]);
  const [customerId, setCustomerId] = useState<number>();
  const [priority, setPriority] = useState<"normal" | "urgent">("normal");
  const [notes, setNotes] = useState("");
  const [delivery, setDelivery] = useState("");
  const [busy, setBusy] = useState(false);
  const update = (key: number, patch: Partial<Row>) => {
    if (patch.exact != null) { setExactPref(patch.exact); writeExact(patch.exact); }
    setRows((previous) => previous.map((row) => row.key === key ? { ...row, ...patch } : row));
  };
  const pasteMany = (key: number, pns: string[]) => setRows((previous) => {
    const index = previous.findIndex((row) => row.key === key);
    const added = pns.map((pn) => ({ ...blank(nextKey.current++), pn }));
    return [...previous.slice(0, index), { ...previous[index], pn: pns[0], part: undefined }, ...added.slice(1), ...previous.slice(index + 1)];
  });
  const filled = rows.filter((row) => row.pn.trim());
  const submit = async () => {
    if (busy) return;
    if (!filled.length) { void message.warning("请至少填写一行 PN"); return; }
    if (filled.some((row) => !(row.qty > 0))) { void message.warning("数量必须大于 0"); return; }
    setBusy(true);
    try {
      const created = await presalesPool.create({ ...(customerVisible && customerId != null ? { customer_id: customerId } : {}), priority, notes: notes.trim() || null,
        lines: filled.map((row) => ({ part_id: row.part?.part_id ?? null, pn_raw: row.part?.pn_std ?? row.pn.trim(),
          description: row.part ? row.part.summary || row.part.description || null : null, qty: row.qty, require_exact_pn: row.exact,
          return_defective: row.returnDefective, notes: row.note.trim() || null, expected_delivery: delivery || null })) });
      void message.success(`询价 ${created.inquiry_no} 已提交，共 ${created.line_count} 行`);
      setRows([blank(nextKey.current++)]); setCustomerId(undefined); setNotes(""); setDelivery(""); setPriority("normal");
      notifyPoolChanged();
      onCreated();
    } catch (error) { await handleError(error); }
    finally { setBusy(false); }
  };
  return <div className="presales-panel presales-stack">
    <div className="presales-toolbar">
      {customerVisible && <span className="presales-inline-field">客户<CandidateSelect label="客户" load={presales.customers} value={customerId} onChange={setCustomerId} disabled={busy} /></span>}
      <span className="presales-inline-field presales-inline-auto">优先级<Radio.Group aria-label="优先级" optionType="button" size="small" value={priority}
        onChange={(event) => setPriority(event.target.value)} options={[{ value: "normal", label: "普通" }, { value: "urgent", label: "紧急" }]} /></span>
    </div>
    <Table aria-label="新建询价行" rowKey="key" size="small" pagination={false} dataSource={rows} tableLayout="fixed" scroll={{ x: 900 }} columns={[
      { title: "#", key: "no", width: 44, render: (_, __, index) => <span className="presales-muted">{index + 1}</span> },
      { title: "PN / 型号", key: "pn", width: 240, render: (_, row) => <PnInput row={row} onChange={(patch) => update(row.key, patch)} onPasteMany={(pns) => pasteMany(row.key, pns)} /> },
      { title: "描述（自动带出）", key: "desc", render: (_, row) => <span className="presales-option">
        <span className="presales-muted" title={row.part?.description ?? undefined}>
          {row.part ? row.part.summary || row.part.description || row.part.pn_std : row.pn.trim() ? "未建档 · 可先提交，报价时再绑定" : ""}</span>
        {!row.part && row.pn.trim() && canCreatePart && <Button type="link" size="small" disabled={busy} onClick={() => setCreating(row)}>新建 PN 档案</Button>}
        {row.part && <LatestPriceTag offer={row.latest} />}</span> },
      { title: "数量", key: "qty", width: 96, render: (_, row) => <InputNumber aria-label={`第 ${row.key} 行数量`} min={0.001} max={99999999999.999} style={{ width: "100%" }}
        value={row.qty} onChange={(qty) => update(row.key, { qty: qty ?? 0 })} /> },
      { title: "PN 一致", key: "exact", width: 80, align: "center", render: (_, row) => <Checkbox aria-label={`第 ${row.key} 行要求 PN 一致`} checked={row.exact} onChange={(event) => update(row.key, { exact: event.target.checked })} /> },
      { title: "坏件返回", key: "return", width: 84, align: "center", render: (_, row) => <Checkbox aria-label={`第 ${row.key} 行坏件返回`} checked={row.returnDefective} onChange={(event) => update(row.key, { returnDefective: event.target.checked })} /> },
      { title: "备注", key: "note", width: 200, render: (_, row) => <Input aria-label={`第 ${row.key} 行备注`} placeholder="可选" value={row.note} onChange={(event) => update(row.key, { note: event.target.value })} /> },
      { title: "", key: "remove", width: 48, render: (_, row) => <Button type="text" danger aria-label={`删除第 ${row.key} 行`} icon={<DeleteOutlined />}
        disabled={rows.length === 1} onClick={() => setRows((previous) => previous.filter((other) => other.key !== row.key))} /> },
    ]} />
    <Collapse ghost size="small" items={[{ key: "more", label: "更多选项", children: <div className="presales-more">
      <label>期望交期（全部行）<Input type="date" aria-label="期望交期" value={delivery} onChange={(event) => setDelivery(event.target.value)} /></label>
      <label className="presales-wide">客户要求<Input.TextArea aria-label="客户要求" autoSize={{ minRows: 1, maxRows: 4 }} value={notes} onChange={(event) => setNotes(event.target.value)} /></label>
    </div> }]} />
    <div className="presales-toolbar">
      <Button onClick={() => setRows((previous) => [...previous, blank(nextKey.current++)])}>+ 添加一行</Button>
      <span className="presales-muted">{filled.length} 行 · 可从 Excel 直接粘贴多行 PN</span>
      <Space style={{ marginLeft: "auto" }}>
        <Button type="primary" loading={busy} onClick={() => void submit()}>提交询价</Button>
      </Space>
    </div>
    {creating && <CreatePartModal compact pn={creating.pn.trim()} onClose={() => setCreating(undefined)}
      onSelected={(part) => update(creating.key, { pn: part.pn_std, part, latest: undefined })} />}
  </div>;
}
