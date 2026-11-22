import { useId, useRef, useState } from "react";
import { Alert, Button, ConfigProvider, Input, InputNumber, Modal, Radio, Select } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import type { RepairAccount, RepairAction, RepairLine, RepairOrder } from "../../../api/inspectionRepair";
import { RepairPnInput } from "./RepairPnInput";
import { destinationLabels } from "./workOrders";
import "./supervisorOrderEditor.css";

type Quantity = "qty" | "fixed" | "unfixed";
type Disposition = NonNullable<RepairLine["dispositions"]>[number];
type DraftLine = Pick<RepairLine, "pn" | "description"> & Record<Quantity, number | null> & {
  key: number;
  original?: RepairLine;
  destinations: (Omit<Disposition, "qty"> & { qty: number | null })[];
};
const quantityLabels: Record<Quantity, string> = { qty: "任务数量", fixed: "修好", unfixed: "没修好" };
const quantityFields: Quantity[] = ["qty", "fixed", "unfixed"];

export function SupervisorOrderEditor({
  order, latestOrder, technicians, busy, saveBlocked, versionConflict, onAction, onClose,
}: {
  order: RepairOrder;
  latestOrder?: RepairOrder;
  technicians: RepairAccount[];
  busy: boolean;
  saveBlocked?: boolean;
  versionConflict?: boolean;
  onAction: (action: RepairAction, version?: number) => Promise<boolean>;
  onClose: () => void;
}) {
  const [mode, setMode] = useState(order.mode);
  const [assignee, setAssignee] = useState(order.assignee ?? undefined);
  const [note, setNote] = useState(order.note ?? "");
  const [error, setError] = useState("");
  const [lines, setLines] = useState<DraftLine[]>(() => order.lines.map((line, index) => ({
    ...line, key: index, original: line, destinations: (line.dispositions ?? []).map(item => ({ ...item })),
  })));
  const pnInputPrefix = useId();
  const nextKey = useRef(lines.length);
  const stillInPool = order.route === "pool" && !order.returned && !order.assignee
    && lines.every(line => !line.fixed && !line.unfixed);

  function updateLine(key: number, patch: Partial<Pick<DraftLine, "pn" | "description" | "destinations" | Quantity>>) {
    setLines(current => current.map(line => line.key === key ? { ...line, ...patch } : line));
    setError("");
  }
  const stale = !!versionConflict || (latestOrder !== undefined && latestOrder.version !== order.version);

  async function save() {
    if (stale || saveBlocked) return;
    if (!mode) return setError("请选择维修方式。");
    if (!lines.length) return setError("维修单至少需要一条任务条。");
    const edited: RepairLine[] = [];
    for (const [index, line] of lines.entries()) {
      const task = `第 ${index + 1} 条`;
      if (!line.pn.trim()) return setError(`${task}请填写备件 PN。`);
      const { qty, fixed, unfixed } = line;
      if (qty === null || !Number.isSafeInteger(qty) || qty < 1) return setError(`${task}的任务数量须为正整数。`);
      if (fixed === null || unfixed === null || !Number.isSafeInteger(fixed) || !Number.isSafeInteger(unfixed) || fixed < 0 || unfixed < 0) {
        return setError(`${task}的修好和没修好数量须为非负整数。`);
      }
      if (fixed + unfixed > qty) return setError(`${task}已登记的结果合计 ${fixed + unfixed} 件，超过任务数量 ${qty} 件。`);
      const allocated = { fixed: 0, unfixed: 0 };
      const dispositions: Disposition[] = [];
      for (const entry of line.destinations) {
        if (entry.qty === null || !Number.isSafeInteger(entry.qty) || entry.qty < 0) {
          return setError(`${task}的去向数量须为非负整数。`);
        }
        allocated[entry.result] += entry.qty;
        dispositions.push({ ...entry, qty: entry.qty });
      }
      if (fixed < allocated.fixed || unfixed < allocated.unfixed) {
        return setError(`${task}去向合计为修好 ${allocated.fixed} 件、没修好 ${allocated.unfixed} 件。请调整本条的去向数量或维修结果，使各类去向不超过对应结果。`);
      }
      const original = line.original;
      const sourceReceiptId = original?.sourceReceiptId;
      const current = latestOrder ? latestOrder.lines[line.key] : original;
      const sourceVersion = current?.sourceReceiptId === sourceReceiptId ? current?.sourceVersion : undefined;
      const increasingSourceQty = !!sourceReceiptId && qty > (original?.qty ?? qty);
      if (increasingSourceQty && !sourceVersion) {
        return setError(`${task}追加送修数量需要当前返还明细版本。请关闭表单并重新打开维修单。`);
      }
      edited.push({
        pn: line.pn.trim(), description: line.description.trim(), qty, fixed, unfixed, dispositions,
        ...(sourceReceiptId ? { sourceReceiptId } : {}),
        ...(increasingSourceQty && sourceVersion ? { sourceVersion } : {}),
      });
    }
    if (await onAction({
      type: "edit", id: order.id, mode, assignee, note: note.trim(),
      lines: edited, sourceIndices: lines.map(line => line.original ? line.key : null),
    }, order.version)) onClose();
  }

  return (
    <Modal open title={`编辑维修单 · ${order.id}`} width={720} maskClosable={false} onCancel={() => { if (!busy) onClose(); }}
      onOk={save} confirmLoading={busy} okButtonProps={{ disabled: stale || saveBlocked }} okText="保存修改" cancelText="取消">
      <ConfigProvider componentDisabled={busy}>
      <div className="wo-edit">
        {stale && <Alert type="warning" showIcon
          message="维修单版本已更新。请关闭当前表单，再打开维修单按最新版本修改。"
          action={<Button size="small" onClick={onClose}>关闭当前表单</Button>} />}
        {saveBlocked && !stale && <Alert type="error" showIcon message="未能刷新维修单。请关闭当前表单后刷新列表。" />}
        <div className="wo-edit-head">
          <label>建单人<Input value={order.creatorName || order.creator} readOnly /></label>
          <label>承接人<Select aria-label="编辑承接人" allowClear value={assignee} onChange={setAssignee}
            placeholder={stillInPool ? "在公共池待领取" : "由维修主管接手"}
            options={technicians.map(({ username, displayName }) => ({ value: username, label: displayName || username }))} /></label>
        </div>
        <fieldset><legend>维修方式</legend><Radio.Group value={mode} onChange={event => setMode(event.target.value)}>
          <Radio value="internal">委内</Radio><Radio value="external">委外</Radio>
        </Radio.Group></fieldset>
        <label>备注<Input.TextArea value={note} maxLength={1000} autoSize={{ minRows: 2, maxRows: 4 }} onChange={event => setNote(event.target.value)} /></label>
        {lines.map((line, index) => {
          const dispositions = line.original?.dispositions ?? [];
          const pnReadOnly = !!(line.original && (line.original.sourceReceiptId || order.repairSource || dispositions.some(entry => entry.kind === "retry")));
          const pnInputId = `${pnInputPrefix}-${line.key}`;
          const deletable = !line.original || (!line.original.previouslyHandled && !line.original.fixed && !line.original.unfixed && !dispositions.length);
          return <section key={line.key} className="wo-edit-line" aria-label={`编辑第 ${index + 1} 条任务条`}>
            <div className="wo-edit-line-head"><strong>任务条 {index + 1}</strong>
              {deletable && lines.length > 1 && <Button size="small" type="text" danger aria-label={`移除第 ${index + 1} 条任务条`}
                onClick={() => setLines(current => current.filter(item => item.key !== line.key))}>移除</Button>}</div>
            {line.original?.sourceReceiptId && <p className="wo-source-label">维保返还 {line.original.sourceHeadNo || "未登记来源单号"}</p>}
            <div className="wo-edit-head">
              <div className="wo-edit-pn">
                <label htmlFor={pnInputId}>备件 PN</label>
                <RepairPnInput id={pnInputId} value={line.pn} readOnly={pnReadOnly} disabled={busy}
                  onChange={pn => updateLine(line.key, { pn })} />
              </div>
              <label>备件描述<Input value={line.description} maxLength={256} onChange={event => updateLine(line.key, { description: event.target.value })} /></label>
            </div>
            <div className="wo-edit-qty">{quantityFields.map(field =>
              <label key={field}>{quantityLabels[field]}
                <InputNumber aria-label={`第 ${index + 1} 条${quantityLabels[field]}`} min={field === "qty" ? 1 : 0} step={1}
                  value={line[field]} onChange={value => updateLine(line.key, { [field]: value })} />
              </label>)}</div>
            {line.destinations.length > 0 && <fieldset className="wo-edit-destinations"><legend>已登记去向</legend>
              {line.destinations.map((entry, n) => {
                const label = `${entry.result === "fixed" ? "修好" : "没修好"} → ${destinationLabels[entry.kind]}`;
                return entry.kind === "retry"
                  ? <p key={n}>{label} · {entry.qty} 件。已生成关联的二次维修单{entry.retryOrderId ? ` ${entry.retryOrderId}` : ""}，此数量随原关联保留。</p>
                  : <label key={n}>{label}（件）
                    <InputNumber min={0} step={1} value={entry.qty} aria-label={`第 ${index + 1} 条${label}去向数量`}
                      onChange={value => updateLine(line.key, { destinations: line.destinations.map((item, i) => i === n ? { ...item, qty: value } : item) })} />
                  </label>;
              })}</fieldset>}
          </section>;
        })}
        <Button className="wo-edit-add" icon={<PlusOutlined />} onClick={() => {
          const key = nextKey.current++;
          setLines(current => [...current, { key, pn: "", description: "", qty: 1, fixed: 0, unfixed: 0, destinations: [] }]);
        }}>添加任务条</Button>
        {error && <Alert type="error" role="alert" showIcon message={error} />}
      </div>
      </ConfigProvider>
    </Modal>
  );
}
