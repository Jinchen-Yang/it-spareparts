import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, App, Button, Checkbox, Empty, Input, Modal, Pagination, Select, Space, Spin, Tabs, Tag } from "antd";
import { PlusOutlined, ReloadOutlined, SearchOutlined } from "@ant-design/icons";
import PageHeader from "../../components/PageHeader";
import {
  applyRepairAction, downloadFixedDispositionWorkbook, getRepairSession, listRepairOrders,
  type RepairAction, type RepairOrder, type RepairRole, type RepairSession,
} from "../../api/inspectionRepair";
import { saveBlob } from "../../api/maintenanceWorkbooks";
import { parseApiError } from "../../utils/apiError";
import { formatBusinessDateTime } from "../../utils/date";
import {
  commandOutcome, newIdempotencyKey,
} from "./repairPoolRules";
import { CreateOrderModal, OrderDrawer } from "./repairOrders/WorkOrderForms";
import { SupervisorOrderEditor } from "./repairOrders/SupervisorOrderEditor";
import WorkOrderAnalytics from "./repairOrders/WorkOrderAnalytics";
import {
  fixedDispositionLabel, fixedDispositionOrderQty, orderOwner, orderStage, pendingQty, roleNames, stages, type Stage,
} from "./repairOrders/workOrders";
import "./repairOrders/repairOrdersPage.css";

type Tab = "pool" | "mine" | "analysis";
const tabs = [
  { key: "pool", label: "维修任务池" },
  { key: "mine", label: "我的任务" },
  { key: "analysis", label: "分析" },
];

const stageColor: Record<Stage, string> = {
  待领取: "gold", 维修中: "blue", 待定去向: "purple", 已完成: "green", 已退回主管: "volcano",
};
const actionSuccess: Record<RepairAction["type"], string> = {
  create: "已创建维修单",
  assign: "已分配任务",
  claim: "已领取，可在我的任务办理",
  return: "已退回主管，已登记结果和负责人记录保留在原单",
  result: "已登记维修结果",
  edit: "已保存修改",
  dispose: "已登记去向",
};
const pageSize = 20;

interface RepairCommand {
  role: RepairRole;
  action: RepairAction;
  version?: number;
  key: string;
}

// 离开维修路由再返回时，仍可用原请求确认结果。只在当前页面进程内保存。
const pendingRepairs = new Map<string, RepairCommand>();

function OrderTable({
  orders, role, busy, onOpen, onClaim, onEdit,
}: {
  orders: RepairOrder[];
  role: RepairRole;
  busy: boolean;
  onOpen: (order: RepairOrder) => void;
  onClaim: (order: RepairOrder) => void;
  onEdit: (order: RepairOrder) => void;
}) {
  return (
    <table className="wo-table">
      <thead><tr>{["维修单", "备件 / PN", "维修进度", "当前负责人 / 方式", "操作"].map(label => <th key={label} scope="col">{label}</th>)}</tr></thead>
      <tbody>
        {orders.map(order => {
          const first = order.lines[0];
          const admin = role === "admin";
          const stage = admin ? "待定去向" : orderStage(order);
          const count = order.lines.reduce((sum, line) => sum + line.qty, 0);
          const reported = order.lines.reduce((sum, line) => sum + line.fixed + line.unfixed, 0);
          const pending = order.lines.reduce((sum, line) => sum + pendingQty(line, "fixed") + pendingQty(line, "unfixed"), 0);
          const progress = admin ? [] : [`已登记 ${reported}/${count} 件`];
          if (pending > 0) progress.push(`待定去向 ${pending} 件`);
          const taggedQty = fixedDispositionOrderQty(order);
          const assignable = role === "supervisor" && (stage === "待领取" || stage === "已退回主管");
          return <tr key={order.id}>
            <td className="wo-id">
              <Button type="link" size="small" disabled={busy} onClick={() => onOpen(order)}>{order.id}</Button>
              <small>{order.creatorName || order.creator} · {formatBusinessDateTime(order.createdAt)}</small>
            </td>
            <td className="wo-part">
              <strong>{first?.description || first?.pn || "—"}</strong>
              <small>{first?.pn}{order.lines.length > 1 && ` 等 ${order.lines.length} 条`} · {admin ? pending : count} 件</small>
            </td>
            <td className="wo-stage-cell">
              <Tag color={stageColor[stage]}>{stage}</Tag>
              {taggedQty > 0 && <Tag className="wo-result-tag" color="green">{fixedDispositionLabel} {taggedQty} 件</Tag>}
              <small>{progress.join(" · ")}</small>
            </td>
            <td className="wo-owner">{orderOwner(order)}<small>{order.mode === "external" ? "委外" : "委内"}</small></td>
            <td className="wo-row-action">
              {role === "supervisor" && <Button type="link" size="small" disabled={busy} onClick={() => onEdit(order)}>编辑</Button>}
              {assignable && <Button type="link" size="small" disabled={busy} onClick={() => onOpen(order)}>
                {stage === "已退回主管" ? "重新分配" : "分配"}
              </Button>}
              {role === "technician" && stage === "待领取" && <Button size="small" disabled={busy} onClick={() => onClaim(order)}>领取任务</Button>}
              {role === "technician" && stage === "维修中" && <Button type="link" size="small" disabled={busy} onClick={() => onOpen(order)}>办理</Button>}
              {admin && <Button type="link" size="small" disabled={busy} onClick={() => onOpen(order)}>登记去向</Button>}
            </td>
          </tr>;
        })}
      </tbody>
    </table>
  );
}

function RepairWorkbench({ session, role, onBusyChange }: {
  session: RepairSession;
  role: RepairRole;
  onBusyChange: (busy: boolean) => void;
}) {
  const { message } = App.useApp();
  const [orders, setOrders] = useState<RepairOrder[]>();
  const [readError, setReadError] = useState<string>();
  const [reading, setReading] = useState(true);
  const readController = useRef<AbortController>();
  const [tab, setTab] = useState<Tab>("pool");
  const [publicOnly, setPublicOnly] = useState(role === "technician");
  const [supervisorInbox, setSupervisorInbox] = useState(true);
  const [search, setSearch] = useState("");
  const [stageFilter, setStageFilter] = useState<Stage>();
  const [taggedOnly, setTaggedOnly] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [page, setPage] = useState(1);
  const [createOpen, setCreateOpen] = useState(false);
  const [detailId, setDetailId] = useState("");
  const [editOrder, setEditOrder] = useState<RepairOrder>();
  const [busy, setBusy] = useState(false);
  const actionLock = useRef(false);
  const [unconfirmed, setUnconfirmed] = useState(() => pendingRepairs.get(session.username));
  const [editConflict, setEditConflict] = useState<{ id: string; version?: number }>();

  const reload = useCallback(async () => {
    readController.current?.abort();
    const controller = new AbortController();
    readController.current = controller;
    setReading(true);
    setReadError(undefined);
    try {
      const data = await listRepairOrders(role, controller.signal);
      if (!controller.signal.aborted) setOrders(data);
    } catch (error) {
      if (!controller.signal.aborted) setReadError(parseApiError(error).message);
    } finally {
      if (!controller.signal.aborted) setReading(false);
    }
  }, [role]);

  useEffect(() => {
    void reload();
    return () => readController.current?.abort();
  }, [reload]);

  useEffect(() => {
    onBusyChange(busy || !!unconfirmed);
    if (!busy && !unconfirmed) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [busy, onBusyChange, unconfirmed]);

  function resetListFilters() {
    setSearch("");
    setStageFilter(undefined);
    setTaggedOnly(false);
    setPage(1);
  }

  function changeTab(next: Tab) {
    setTab(next);
    resetListFilters();
  }

  async function exportFixedDispositions() {
    setExporting(true);
    try {
      const workbook = await downloadFixedDispositionWorkbook();
      saveBlob(workbook, "已登记去向的修好件.xlsx");
    } catch (error) {
      message.error(parseApiError(error, "导出维修去向失败").message);
    } finally {
      setExporting(false);
    }
  }

  async function sendCommand(command: RepairCommand, retrying = false): Promise<boolean> {
    if (actionLock.current) return false;
    const { action, version, key } = command;
    pendingRepairs.set(session.username, command);
    actionLock.current = true;
    setBusy(true);
    onBusyChange(true);
    const clearPending = () => {
      if (pendingRepairs.get(session.username) === command) pendingRepairs.delete(session.username);
      setUnconfirmed(undefined);
    };
    try {
      let saved;
      try {
        saved = await applyRepairAction(command.role, action, version, key);
      } catch (error) {
        if (commandOutcome(error) === "unknown") {
          setUnconfirmed(command);
          return false;
        }
        clearPending();
        const info = parseApiError(error);
        if (info.status === 409) {
          if (action.type === "edit" && (info.kind === "version_conflict" || /维修单已变化/.test(info.message))) {
            setEditConflict({ id: action.id, version });
          }
          await reload();
        }
        message.error(info.message);
        return false;
      }
      clearPending();
      if (!saved.lines.length || (role === "technician" && action.type === "return")) {
        setOrders(previous => previous?.filter(order => order.id !== saved.id));
        setDetailId("");
      }
      if (action.type === "claim") {
        setDetailId("");
        changeTab("mine");
      }
      if (action.type === "create") {
        changeTab("pool");
        setPublicOnly(!action.assignee);
      }
      if (retrying) {
        setCreateOpen(false);
        setEditOrder(undefined);
        setDetailId("");
      }
      await reload();
      message.success(actionSuccess[action.type]);
      return true;
    } finally {
      actionLock.current = false;
      setBusy(false);
    }
  }

  function onAction(action: RepairAction, version?: number): Promise<boolean> {
    if (reading || readError || pendingRepairs.has(session.username)) return Promise.resolve(false);
    return sendCommand({ role, action, version, key: newIdempotencyKey("repair-order") });
  }

  const visible = orders ?? [];
  const available = visible.filter(order => orderStage(order) === "待领取");
  const created = visible.filter(order => order.creator === session.username);
  const returned = visible.filter(order => orderStage(order) === "已退回主管");
  const tabsFilter = tab === "pool" ? (publicOnly ? available : visible)
    : role === "supervisor" ? (supervisorInbox ? returned : created)
      : role === "technician" ? visible.filter(order => order.assignee === session.username)
        : role === "sales" ? created : visible;
  const query = search.trim().toLocaleLowerCase();
  const supervisorTotalPool = role === "supervisor" && tab === "pool" && !publicOnly;
  const matches = tabsFilter.filter(order =>
    (!taggedOnly || fixedDispositionOrderQty(order) > 0) &&
    (!stageFilter || orderStage(order) === stageFilter) &&
    (!query || [order.id, order.creatorName, order.assigneeName, order.creator, order.assignee,
      ...order.lines.flatMap(line => [line.pn, line.description])]
      .some(value => value?.toLocaleLowerCase().includes(query))),
  );
  const currentPage = Math.min(page, Math.max(1, Math.ceil(matches.length / pageSize)));
  const detail = visible.find(order => order.id === detailId);
  const canCreate = role === "sales" || role === "supervisor";
  const dialogBusy = busy || reading || !!unconfirmed;
  const blocked = dialogBusy || !!readError;
  const drawerProps = { role, user: session, technicians: session.technicians, busy: dialogBusy, saveBlocked: !!readError, onAction };
  const closeEditor = () => { setEditOrder(undefined); setEditConflict(undefined); };
  const latestEditOrder = editOrder && visible.find(order => order.id === editOrder.id);

  return <>
    <Tabs activeKey={tab} onChange={key => changeTab(key as Tab)} items={tabs.map(item => ({ ...item, disabled: busy || !!unconfirmed }))} />
    {readError && <Alert type="error" showIcon message={readError} className="wo-page-error"
      action={<Button size="small" disabled={reading || busy || !!unconfirmed} onClick={() => void reload()}>重试</Button>} />}
    {(!orders && !readError) && <div className="wo-page-loading" role="status"><Spin /> 正在加载维修单</div>}
    {orders && (tab === "analysis" ? (
      <WorkOrderAnalytics orders={visible} pendingOnly={role === "admin"} onOpen={setDetailId} />
    ) : <section className="wo-panel" aria-label="维修单列表">
      <div className="wo-list-head">
        {tab === "pool" && role !== "admin" ? (
          <div className="wo-switch" role="group" aria-label="维修任务池范围">
            <button type="button" aria-pressed={!publicOnly} onClick={() => { setPublicOnly(false); resetListFilters(); }}>{role === "sales" ? "我发起的" : "任务总池"} {visible.length}</button>
            <button type="button" aria-pressed={publicOnly} onClick={() => { setPublicOnly(true); resetListFilters(); }}>{role === "sales" ? "我的待领取" : "公共池"} {available.length}</button>
          </div>
        ) : tab === "mine" && role === "supervisor" ? (
          <div className="wo-switch" role="group" aria-label="主管任务范围">
            <button type="button" aria-pressed={supervisorInbox} onClick={() => { setSupervisorInbox(true); resetListFilters(); }}>退回待处理 {returned.length}</button>
            <button type="button" aria-pressed={!supervisorInbox} onClick={() => { setSupervisorInbox(false); resetListFilters(); }}>我创建的 {created.length}</button>
          </div>
        ) : <strong>{role === "admin" ? "待登记去向" : "我的维修单"}</strong>}
        <div className="wo-filters">
          <Input allowClear prefix={<SearchOutlined />} aria-label="搜索维修单" placeholder="搜索单号、PN 或人员"
            value={search} onChange={event => { setSearch(event.target.value); setPage(1); }} />
          {role !== "admin" && !(tab === "pool" && publicOnly) && !(role === "supervisor" && tab === "mine" && supervisorInbox) &&
            <Select allowClear aria-label="筛选维修进度" placeholder="全部进度" value={stageFilter}
              onChange={(value?: Stage) => { setStageFilter(value); setPage(1); }}
              options={stages.map(stage => ({ value: stage, label: stage }))} />}
          {supervisorTotalPool && <>
            <Checkbox checked={taggedOnly} onChange={event => { setTaggedOnly(event.target.checked); setPage(1); }}>
              仅看{fixedDispositionLabel}
            </Checkbox>
            <Button loading={exporting} disabled={busy} onClick={() => void exportFixedDispositions()}
              title="导出主管可见范围内的全部已登记去向修好件">
              导出全部已登记去向的修好件
            </Button>
          </>}
          <Button icon={<ReloadOutlined />} onClick={() => void reload()} disabled={busy || reading || !!unconfirmed} aria-label="刷新维修单" />
          {tab === "pool" && canCreate && <Button type="primary" icon={<PlusOutlined aria-hidden="true" />} disabled={blocked}
            onClick={() => setCreateOpen(true)}>创建维修单</Button>}
        </div>
      </div>
      {matches.length > 0 ? (
        <OrderTable orders={matches.slice((currentPage - 1) * pageSize, currentPage * pageSize)}
          role={role} busy={blocked} onOpen={order => setDetailId(order.id)} onEdit={setEditOrder}
          onClaim={order => void onAction({ type: "claim", id: order.id }, order.version)} />
      ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={query || stageFilter || taggedOnly ? "没有匹配的维修单" : "暂无维修单"}>
        {(query || stageFilter || taggedOnly) && <Button onClick={resetListFilters}>清除筛选</Button>}
      </Empty>}
      <div className="wo-list-footer">
        <span>共 {matches.length} 张维修单</span>
        <Pagination size="small" hideOnSinglePage current={currentPage} pageSize={pageSize} total={matches.length} onChange={setPage} showSizeChanger={false} />
      </div>
    </section>)}
    {createOpen && canCreate && <CreateOrderModal {...drawerProps} onClose={() => setCreateOpen(false)} />}
    {editOrder && role === "supervisor" && <SupervisorOrderEditor order={editOrder} technicians={session.technicians}
      busy={busy || reading || !!unconfirmed} saveBlocked={!!readError}
      latestOrder={latestEditOrder} versionConflict={editConflict?.id === editOrder.id && editConflict.version === editOrder.version}
      onAction={onAction} onClose={closeEditor} />}
    {detail && <OrderDrawer key={detail.id} order={detail} {...drawerProps}
      conflictedVersion={editConflict?.id === detail.id ? editConflict.version : undefined}
      onClose={() => { setDetailId(""); setEditConflict(undefined); }} />}
    {unconfirmed && <Modal open title="上次操作结果待确认" zIndex={1300} closable={false} keyboard={false} maskClosable={false}
      footer={<Button type="primary" loading={busy} onClick={() => void sendCommand(unconfirmed, true)}>重试确认</Button>}>
      请点击「重试确认」查询这次操作的结果。重试会使用同一次提交的内容。
    </Modal>}
  </>;
}

export default function RepairOrdersPage() {
  const [session, setSession] = useState<RepairSession>();
  const [role, setRole] = useState<RepairRole>();
  const [error, setError] = useState<string>();
  const [retry, setRetry] = useState(0);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    getRepairSession(controller.signal).then(data => {
      if (!controller.signal.aborted) {
        const pendingRole = pendingRepairs.get(data.username)?.role;
        setSession(data);
        setRole(pendingRole && data.roles.includes(pendingRole)
          ? pendingRole : data.roles.includes("supervisor") ? "supervisor" : data.roles[0]);
      }
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) setError(parseApiError(failure).message);
    });
    return () => controller.abort();
  }, [retry]);

  return (
    <div className="wo-page">
      <PageHeader title="检测与维修" extra={session && role && (
        <Space wrap>
          {session.displayName || session.username}
          {session.roles.length > 1
            ? <Select value={role} aria-label="办理角色" disabled={busy} onChange={setRole}
              options={session.roles.map(item => ({ value: item, label: roleNames[item] }))} />
            : <Tag>{roleNames[role]}</Tag>}
        </Space>
      )} />
      {error && <Alert type="error" showIcon message={error}
        action={<Button onClick={() => { setError(undefined); setRetry(value => value + 1); }}>重试</Button>} />}
      {!error && !session && <div className="wo-page-loading" role="status"><Spin /> 正在核对账号权限</div>}
      {session && role && <RepairWorkbench key={role} session={session} role={role} onBusyChange={setBusy} />}
      {session && !role && <Alert type="warning" showIcon message="当前账号没有维修任务权限" />}
    </div>
  );
}
