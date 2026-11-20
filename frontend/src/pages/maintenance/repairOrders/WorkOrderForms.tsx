import { useState } from "react";
import { Alert, App, Button, Drawer, Form, Input, InputNumber, Modal, Radio, Select, Tag } from "antd";
import { DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import type {
  NewRepairLine, RepairAccount, RepairAction, RepairDestination, RepairLine, RepairMode,
  RepairOrder, RepairResult, RepairRole, RepairReturnCandidate,
} from "../../../api/inspectionRepair";
import { formatBusinessDateTime } from "../../../utils/date";
import { destinationLabels, fixedDispositionLabel, fixedDispositionQty, orderOwner, orderStage, pendingQty } from "./workOrders";
import { SupervisorOrderEditor } from "./SupervisorOrderEditor";
import ReturnSourcePicker from "./ReturnSourcePicker";
import "./workOrderForms.css";

type DialogProps = {
  role: RepairRole;
  user: RepairAccount;
  technicians: RepairAccount[];
  busy: boolean;
  saveBlocked?: boolean;
  onClose: () => void;
  onAction: (action: RepairAction, version?: number) => Promise<boolean>;
};

type CreateLine = NewRepairLine & { sourceHeadNo?: string; sourceAvailableQty?: number };
type CreateValues = {
  lines: CreateLine[];
  note?: string;
  mode: RepairMode;
  route: "pool" | "direct";
  assignee?: string;
};

const results = { fixed: "修好", unfixed: "没修好" } as const;
const methods = { internal: "委内维修", external: "委外维修" } as const;
const technicianOptions = (accounts: RepairAccount[]) => accounts.map(({ username, displayName }) => ({
  value: username,
  label: displayName && displayName !== username ? `${displayName}（${username}）` : username,
}));

export function CreateOrderModal({ role, user, technicians, busy, saveBlocked, onClose, onAction }: DialogProps) {
  const [form] = Form.useForm<CreateValues>();
  const { modal } = App.useApp();
  const route = Form.useWatch("route", form);
  const lines = Form.useWatch("lines", form);
  const [refresh, setRefresh] = useState(0);
  const supervisor = role === "supervisor";
  const selected = new Map<string, CreateLine>();
  for (const line of lines ?? []) if (line?.sourceReceiptId) selected.set(line.sourceReceiptId, line);
  if (role !== "sales" && !supervisor) return null;

  async function save(values: CreateValues) {
    const created = await onAction({
      type: "create", mode: values.mode,
      lines: values.lines.map(({ pn, description, qty, sourceReceiptId, sourceVersion }) => ({
        pn: pn.trim(), description: description?.trim() ?? "", qty,
        ...(sourceReceiptId ? { sourceReceiptId, sourceVersion } : {}),
      })),
      note: values.note?.trim() ?? "",
      assignee: supervisor || values.route === "direct" ? values.assignee : undefined,
    });
    if (created) onClose();
    else setRefresh(value => value + 1);
  }

  function pickSource(candidate: RepairReturnCandidate) {
    const draft = form.getFieldValue("lines") as CreateLine[] | undefined;
    const source = {
      pn: candidate.pn,
      description: candidate.description ?? "",
      qty: 1,
      sourceReceiptId: candidate.receiptId,
      sourceVersion: candidate.version,
      sourceHeadNo: candidate.headNo ?? "未登记来源单号",
      sourceAvailableQty: candidate.availableQty,
    };
    const existing = draft?.findIndex(line => line?.sourceReceiptId === candidate.receiptId) ?? -1;
    if (existing >= 0) {
      form.setFieldValue(["lines", existing], { ...source, qty: draft?.[existing]?.qty ?? source.qty });
      return;
    }
    const first = draft?.[0];
    if (draft?.length === 1 && !first?.pn && !first?.description && first?.qty === 1) {
      form.setFieldValue(["lines", 0], source);
      return;
    }
    form.setFieldValue("lines", [...(draft ?? []), source]);
  }

  function cancel() {
    if (busy) return;
    if (!form.isFieldsTouched() && !selected.size) return onClose();
    modal.confirm({
      title: "放弃这张未提交的维修单？", okText: "放弃草稿", cancelText: "继续填写", onOk: onClose,
    });
  }

  return (
    <Modal title="创建维修单" open width={supervisor ? 1100 : 700} maskClosable={false} onCancel={cancel}
      footer={<><Button disabled={busy} onClick={cancel}>取消</Button><Button type="primary" loading={busy} disabled={saveBlocked} onClick={() => form.submit()}>创建维修单</Button></>}>
      {saveBlocked && <Alert type="error" showIcon message="未能刷新维修单。请关闭当前表单后刷新列表。" />}
      <Form form={form} name="repair-order-create" layout="vertical" disabled={busy} onFinish={save}
        className="wo-form" initialValues={{ lines: [{ qty: 1 }], route: supervisor ? "direct" : "pool" }}>
        <div className={supervisor ? "wo-create-layout" : undefined}>
          {supervisor && <ReturnSourcePicker onPick={pickSource}
            selected={selected} refresh={refresh} busy={busy} />}
          <div className="wo-create-details">
            <div className="wo-form-section-heading"><h3>维修明细</h3><span>建单人 {user.displayName || user.username}</span></div>
            <Form.List name="lines">
              {(fields, { add, remove }) => <>
                {fields.map(({ key, name }) => {
                const line = lines?.[name];
                const source = line?.sourceReceiptId
                  ? <p className="wo-source-label">维保返还 {line.sourceHeadNo} · 可选 {line.sourceAvailableQty} 件</p> : null;
                return <div className="wo-entry" key={key}>
                  <div className="wo-entry-head"><strong>任务条 {name + 1}</strong>
                    {fields.length > 1 && <Button type="text" size="small" icon={<DeleteOutlined />}
                      aria-label={`移除第 ${name + 1} 条明细`} onClick={() => remove(name)}>移除</Button>}
                  </div>
                  {source}
                  <div className="wo-entry-grid">
                    <Form.Item name={[name, "pn"]} label="备件 PN"
                      rules={[{ required: true, whitespace: true, message: "请填写备件 PN" }]}>
                      <Input placeholder="输入备件 PN" maxLength={128} autoComplete="off" readOnly={!!source} />
                    </Form.Item>
                    <Form.Item name={[name, "description"]} label="备件描述">
                      <Input placeholder="填写型号或描述" maxLength={256} readOnly={!!source} />
                    </Form.Item>
                    <Form.Item name={[name, "qty"]} label="数量" rules={[
                      { required: true, message: "请填写数量" },
                      { type: "integer", min: 1, max: line?.sourceAvailableQty, message: line?.sourceAvailableQty
                        ? `请输入不超过 ${line.sourceAvailableQty} 的正整数` : "请输入正整数" },
                    ]}>
                      <InputNumber min={1} max={line?.sourceAvailableQty} precision={0} />
                    </Form.Item>
                  </div>
                </div>;
              })}
                <Button icon={<PlusOutlined />} onClick={() => add({ qty: 1 })}>添加手工任务条</Button>
              </>}
            </Form.List>
            <div className="wo-form-section-heading"><h3>任务派发</h3></div>
              <Form.Item name="mode" label="整单维修方式" rules={[{ required: true, message: "请选择委内或委外维修" }]}>
                <Radio.Group><Radio value="internal">委内维修</Radio><Radio value="external">委外维修</Radio></Radio.Group>
              </Form.Item>
              {!supervisor && <Form.Item name="route" label="派发方式"><Radio.Group>
                <Radio value="pool">放入公共池</Radio><Radio value="direct">直接派给维修人员</Radio>
              </Radio.Group></Form.Item>}
              {(supervisor || route === "direct") && <Form.Item name="assignee" label="维修人员"
                rules={[{ required: true, message: "请选择维修人员" }]} preserve={false}>
                <Select placeholder="选择维修人员" optionFilterProp="label" showSearch options={technicianOptions(technicians)} />
              </Form.Item>}
              <Form.Item name="note" label="备注"><Input.TextArea autoSize={{ minRows: 2, maxRows: 5 }} maxLength={1000} placeholder="可填写故障现象或维修要求" /></Form.Item>
          </div>
        </div>
      </Form>
    </Modal>
  );
}

function ResultEditor({ order, index, line, busy, onAction }: {
  order: RepairOrder; index: number; line: RepairLine; busy: boolean; onAction: DialogProps["onAction"];
}) {
  const [fixed, setFixed] = useState<number | null>(null);
  const [unfixed, setUnfixed] = useState<number | null>(null);
  const remaining = line.qty - line.fixed - line.unfixed;
  const submitted = (fixed ?? 0) + (unfixed ?? 0);

  async function save() {
    if (await onAction({ type: "result", id: order.id, line: index, fixed: fixed ?? 0, unfixed: unfixed ?? 0 }, order.version)) {
      setFixed(null);
      setUnfixed(null);
    }
  }

  return (
    <div className="wo-line-form">
      <div className="wo-quantities">
        <label>本次修好数量<InputNumber disabled={busy} min={0} max={remaining} precision={0} aria-label="本次修好数量" placeholder="0" value={fixed} onChange={setFixed} /></label>
        <label>本次没修好数量<InputNumber disabled={busy} min={0} max={remaining} precision={0} aria-label="本次没修好数量" placeholder="0" value={unfixed} onChange={setUnfixed} /></label>
      </div>
      {submitted > remaining && <p role="alert" className="wo-invalid">本次合计 {submitted} 件，剩余可登记 {remaining} 件。</p>}
      <Button type="primary" loading={busy} disabled={submitted < 1 || submitted > remaining} onClick={save}>
        登记维修结果
      </Button>
    </div>
  );
}

function DispositionEditor({ order, index, result, available, busy, onAction }: {
  order: RepairOrder; index: number; result: RepairResult; available: number; busy: boolean; onAction: DialogProps["onAction"];
}) {
  const [kind, setKind] = useState<RepairDestination>();
  const [qty, setQty] = useState<number | null>(null);
  async function save() {
    if (kind && qty && await onAction({ type: "dispose", id: order.id, line: index, result, kind, qty }, order.version)) {
      setQty(null);
      setKind(undefined);
    }
  }
  const options = (result === "fixed" ? ["company", "shop"] as const : ["company", "shop", "retry"] as const)
    .map((value) => ({ value, label: destinationLabels[value] }));

  return (
    <div className="wo-dispose-row">
      <strong>{results[result]} · 待分 {available} 件</strong>
      <div className="wo-quantities">
        <label>去向<Select disabled={busy} aria-label={`${results[result]}件去向`} placeholder="选择去向" value={kind} onChange={setKind} options={options} /></label>
        <label>本次数量<InputNumber disabled={busy} aria-label={`${results[result]}件去向数量`} min={1} max={available} precision={0} value={qty} onChange={setQty} /></label>
      </div>
      <Button loading={busy} disabled={!kind || !qty || qty < 1 || qty > available} onClick={save}>登记去向</Button>
    </div>
  );
}

const routeLabels: Record<RepairOrder["route"], string> = {
  pool: "公共池待领", direct: "销售直接派发", assigned: "主管分配", claimed: "公共池自领",
};

export function OrderDrawer({ order, role, user, technicians, busy, saveBlocked, conflictedVersion, onClose, onAction }: DialogProps & {
  order: RepairOrder;
  conflictedVersion?: number;
}) {
  const [assignee, setAssignee] = useState<string>();
  const { modal } = App.useApp();
  const [editing, setEditing] = useState<RepairOrder | null>(null);
  const stage = orderStage(order);
  const unassigned = stage === "待领取";
  const returned = stage === "已退回主管";
  const mine = stage === "维修中" && role === "technician" && order.assignee === user.username;
  const admin = role === "admin";
  const owner = orderOwner(order);
  const visibleLines = order.lines.map((line, index) => ({ line, index: line.lineIndex ?? index }));

  function confirmReturn() {
    modal.confirm({
      title: "将整张维修单退回主管？",
      content: "已登记的修好和没修好数量会保留，剩余数量交由维修主管处理。",
      okText: "确认退回主管",
      cancelText: "继续办理",
      onOk: async () => { if (await onAction({ type: "return", id: order.id }, order.version)) onClose(); },
    });
  }

  return (
    <Drawer open onClose={() => { if (!busy) onClose(); }} width="min(100%, 660px)" title={`维修单 · ${order.id}`}>
      <div className="wo-detail">
        {saveBlocked && <Alert type="error" showIcon message="未能刷新维修单。请关闭当前表单后刷新列表。" />}
        <dl className="wo-detail-meta">
          <div><dt>建单人</dt><dd>{order.creatorName || order.creator}</dd></div>
          <div><dt>当前负责人</dt><dd>{owner}</dd></div>
          <div><dt>整单维修方式</dt><dd>{order.mode ? methods[order.mode] : "待主管补录"}</dd></div>
          <div><dt>原派发方式</dt><dd>{routeLabels[order.route]}</dd></div>
          <div><dt>创建时间</dt><dd>{formatBusinessDateTime(order.createdAt)}</dd></div>
        </dl>
        {!admin && order.repairSource && <p className="wo-note"><strong>二次维修来源</strong> {order.repairSource.orderId} · 第 {order.repairSource.line + 1} 条</p>}
        {!admin && order.note && <p className="wo-note"><strong>建单备注</strong> {order.note}</p>}
        {returned && <p className="wo-note">已退回主管，由维修主管处理剩余任务。</p>}
        <div className="wo-order-actions">
          {mine && <Button loading={busy} disabled={saveBlocked} onClick={confirmReturn}>我修不好，退回主管</Button>}
          {role === "supervisor" && <Button disabled={busy || saveBlocked} onClick={() => setEditing(order)}>编辑维修单</Button>}
        </div>
        {unassigned && role === "technician" &&
          <Button type="primary" loading={busy} disabled={saveBlocked} onClick={() => void onAction({ type: "claim", id: order.id }, order.version)}>领取任务</Button>}
        {(unassigned || returned) && role === "supervisor" && <div className="wo-assignment">
          <Select aria-label="选择维修人员" placeholder="选择维修人员" value={assignee} options={technicianOptions(technicians)} disabled={busy} onChange={setAssignee} />
          <Button type="primary" loading={busy} disabled={!assignee || saveBlocked} onClick={() => { if (assignee) void onAction({ type: "assign", id: order.id, assignee }, order.version); }}>{returned ? "重新分配任务" : "分配任务"}</Button>
        </div>}
        <div className="wo-form-section-heading"><h3>{admin ? "待分配去向" : "维修明细"}</h3><span>共 {visibleLines.length} 条</span></div>
        {visibleLines.map(({ line, index }) => {
          const remaining = line.qty - line.fixed - line.unfixed;
          const dispositions = line.dispositions ?? [];
          const taggedQty = fixedDispositionQty(line);
          return (
            <section className="wo-detail-line" key={`${order.id}-${index}`} aria-label={`任务条 ${index + 1} ${line.pn}`}>
              <div className="wo-line-heading">
                <h4>{line.pn}</h4>
                {!admin && <Tag>{line.qty} 件</Tag>}
                {taggedQty > 0 && <Tag color="green">{fixedDispositionLabel} {taggedQty} 件</Tag>}
              </div>
              {line.description && <p className="wo-line-description">{line.description}</p>}
              {line.sourceHeadNo && <p className="wo-source-label">维保返还 {line.sourceHeadNo}</p>}
              {!admin && <div className="wo-counts"><span>修好 <b>{line.fixed}</b></span><span>没修好 <b>{line.unfixed}</b></span><span>待出结果 <b>{remaining}</b></span></div>}
              {mine && remaining > 0 && <ResultEditor order={order} index={index} line={line} busy={busy || !!saveBlocked} onAction={onAction} />}
              {admin && (["fixed", "unfixed"] as const).map((result) => {
                const available = pendingQty(line, result);
                return available > 0 && <DispositionEditor key={result} order={order} index={index} result={result} available={available} busy={busy || !!saveBlocked} onAction={onAction} />;
              })}
              {!admin && dispositions.length > 0 && <div className="wo-dispositions"><strong>已登记去向</strong>
                <ul>{dispositions.map((entry, n) => <li key={n}>{results[entry.result]} · {entry.qty} 件 → {destinationLabels[entry.kind]}</li>)}</ul>
              </div>}
            </section>
          );
        })}
        <div className="wo-form-section-heading"><h3>历史负责人</h3></div>
        {order.history?.length ? <ol className="wo-owner-history">
          {order.history.map(({ name, event, at }, index) => {
            const time = new Date(at);
            return <li key={index}>
              <div><strong>{name}</strong><span>{event}</span></div>
              <time dateTime={Number.isNaN(time.getTime()) ? undefined : at}>
                {Number.isNaN(time.getTime()) ? "时间未记录" : formatBusinessDateTime(at)}
              </time>
            </li>;
          })}
        </ol> : <p className="wo-line-description">暂无负责人记录</p>}
        {editing && role === "supervisor" &&
          <SupervisorOrderEditor order={editing} latestOrder={order} technicians={technicians} busy={busy}
            saveBlocked={saveBlocked} versionConflict={conflictedVersion === editing.version} onAction={onAction} onClose={() => setEditing(null)} />}
      </div>
    </Drawer>
  );
}
