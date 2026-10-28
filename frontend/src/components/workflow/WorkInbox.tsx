import { useEffect, useRef, useState, type Key, type ReactNode } from "react";
import { Alert, App, Badge, Button, Input, Modal, Table, Select, Space, Tabs, Tag } from "antd";
import {
  claimWorkItem, completeWorkItem, declineWorkItem, listWorkCandidates, listWorkItems,
  reassignWorkItem, releaseWorkItem, startWorkItem, transferWorkItem,
  type WorkAction, type WorkActionBody, type WorkCandidate, type WorkItem, type WorkTab,
} from "../../api/workItems";
import { formatBusinessDateTime } from "../../utils/date";
import { refreshWorkBadges, useWorkBadge } from "./useWorkBadge";
import { parseApiError, type ApiErrorInfo } from "../../utils/apiError";
import { ApiErrorFeedback, useApiError } from "../ApiErrorFeedback";

interface Props {
  subjectType?: string;
  renderRow: (item: WorkItem) => ReactNode;
  onOpen: (item: WorkItem) => void | Promise<void>;
  beforeOpen?: (item: WorkItem) => boolean | Promise<boolean>;
  refreshKey?: number;
  onSelectionChange?: (items: WorkItem[]) => void;
  isRowSelectable?: (item: WorkItem) => boolean;
}

type ReasonAction = "release" | "transfer" | "reassign" | "decline";
const actionLabels: Record<WorkAction, string> = {
  claim: "领取", start: "打开", release: "退回", transfer: "转交", reassign: "改派",
  complete: "完成", decline: "拒绝",
};
const statusLabels = { pending: "待处理", in_progress: "处理中", done: "已完成", declined: "已拒绝", cancelled: "已取消" };

export default function WorkInbox({ subjectType, renderRow, onOpen, beforeOpen, refreshKey = 0,
  onSelectionChange, isRowSelectable }: Props) {
  const { message } = App.useApp();
  const handleError = useApiError();
  const badge = useWorkBadge();
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<WorkTab>("assigned");
  const [page, setPage] = useState(1);
  const [items, setItems] = useState<WorkItem[]>([]);
  const [selected, setSelected] = useState<Key[]>([]);
  const [total, setTotal] = useState(0);
  const [canSupervise, setCanSupervise] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<ApiErrorInfo>();
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState<number | null>(null);
  const [dialog, setDialog] = useState<{ action: ReasonAction; item: WorkItem }>();
  const [reason, setReason] = useState("");
  const [target, setTarget] = useState<number>();
  const [candidates, setCandidates] = useState<WorkCandidate[]>([]);
  const [candidatesLoading, setCandidatesLoading] = useState(false);
  const [candidateError, setCandidateError] = useState<ApiErrorInfo>();
  const mounted = useRef(true);
  const contextVersion = useRef(0);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setSelected([]); onSelectionChange?.([]);
    setItems([]);
    setTotal(0);
    setLoading(true);
    setLoadError(undefined);
    void listWorkItems({ tab, subject_type: subjectType, page, page_size: 20 }, controller.signal)
      .then(async (data) => {
        if (controller.signal.aborted) return;
        setItems(data.items);
        setTotal(data.total);
        setCanSupervise(data.can_supervise);
        setDialog((current) => {
          if (!current) return current;
          const latest = data.items.find((item) => item.id === current.item.id);
          // 保留原因与接收人，但下次由用户确认时必须携带刷新后的版本。
          return { ...current, item: latest ?? { ...current.item, allowed_actions: [] } };
        });
        if (tab === "all" && !data.can_supervise) { setTab("assigned"); setPage(1); }
      })
      .catch((error) => {
        if (!controller.signal.aborted) setLoadError(parseApiError(error, "待办加载失败，请重试"));
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [tab, subjectType, page, reload, refreshKey, onSelectionChange]);

  useEffect(() => {
    ++contextVersion.current;
    setTab("assigned");
    setPage(1);
    setCanSupervise(false);
    setDialog(undefined);
    setBusy(null);
  }, [subjectType]);

  useEffect(() => {
    if (!dialog || !["transfer", "reassign"].includes(dialog.action)) return;
    const controller = new AbortController();
    setCandidates([]);
    setCandidateError(undefined);
    setCandidatesLoading(true);
    void listWorkCandidates(dialog.item.pool_key, controller.signal)
      .then((data) => { if (!controller.signal.aborted) setCandidates(data.items); })
      .catch((error) => {
        if (!controller.signal.aborted) setCandidateError(parseApiError(error, "候选人加载失败，请重试"));
      })
      .finally(() => { if (!controller.signal.aborted) setCandidatesLoading(false); });
    return () => controller.abort();
  }, [dialog]);

  const refresh = () => {
    setReload((value) => value + 1);
    refreshWorkBadges();
  };

  const perform = async (item: WorkItem, action: WorkAction, body: WorkActionBody) => {
    if (busy !== null) return;
    const handlers = {
      claim: claimWorkItem, start: startWorkItem, release: releaseWorkItem,
      transfer: transferWorkItem, reassign: reassignWorkItem,
      complete: completeWorkItem, decline: declineWorkItem,
    };
    const actionContext = contextVersion.current;
    const isCurrent = () => mounted.current && actionContext === contextVersion.current;
    setBusy(item.id);
    let completed = false;
    try {
      const changed = await handlers[action](item.id, body);
      completed = true;
      if (!isCurrent()) { refreshWorkBadges(); return; }
      setDialog(undefined);
      refresh();
      if (action === "start") await onOpen(changed);
      else message.success(`待办已${actionLabels[action]}`);
    } catch (error: unknown) {
      if (!isCurrent()) return;
      if (completed) handleError.afterSuccess(error, action === "start" ? "开始处理待办" : `待办${actionLabels[action]}`);
      else await handleError(error, refresh);
    } finally {
      if (isCurrent()) setBusy(null);
    }
  };

  const openDialog = (item: WorkItem, action: ReasonAction) => {
    setReason("");
    setTarget(undefined);
    setCandidatesLoading(false);
    setCandidateError(undefined);
    setDialog({ item, action });
  };

  const submitDialog = () => {
    if (!dialog || !reason.trim() || !dialog.item.allowed_actions.includes(dialog.action)) return;
    const needsTarget = dialog.action === "transfer" || dialog.action === "reassign";
    if (needsTarget && target === undefined) return;
    void perform(dialog.item, dialog.action, {
      expected_version: dialog.item.version,
      reason: reason.trim(),
      ...(needsTarget ? { to_user_id: target === 0 ? null : target } : {}),
    });
  };

  const tabs: { key: WorkTab; label: ReactNode }[] = [
    { key: "assigned", label: <Space>指派给我<Badge count={badge.assigned} /></Space> },
    { key: "pool", label: <Space>公共池<Badge count={badge.pool} /></Space> },
    { key: "claimed", label: <Space>我已领取<Badge count={badge.claimed} /></Space> },
    { key: "done", label: "已处理" },
  ];
  if (canSupervise) tabs.push({ key: "all", label: "全部" });

  const openItem = async (item: WorkItem) => {
    try {
      if (beforeOpen && !await beforeOpen(item)) return;
      if (item.allowed_actions.includes("start")) await perform(item, "start", { expected_version: item.version });
      else await onOpen(item);
    } catch (error) { await handleError(error); }
  };
  const actions = (item: WorkItem) => <div className="presales-actions">
    <Button size="small" disabled={busy !== null} onClick={() => void openItem(item)}>打开</Button>
    {item.allowed_actions.filter((action) => action !== "start").map((action) => <Button key={action}
      size="small" loading={busy === item.id} disabled={busy !== null} onClick={() => {
        if (["release", "transfer", "reassign", "decline"].includes(action)) openDialog(item, action as ReasonAction);
        else void perform(item, action, { expected_version: item.version });
      }}>{actionLabels[action]}</Button>)}
  </div>;
  const queue = items.filter((item) => String(item.summary.created_by_name ?? "").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  return <>
      <Tabs activeKey={tab} items={tabs} onChange={(key) => { setTab(key as WorkTab); setPage(1); }} />
      <Input aria-label="筛选待办" placeholder="筛选询价人（当前页）" value={query} onChange={(event) => setQuery(event.target.value)} style={{ width: 260, marginBottom: 16 }} />
      {loadError && <Alert type="error" showIcon message="待办加载失败" description={<ApiErrorFeedback info={loadError} />} action={<Button onClick={() => setReload((value) => value + 1)}>重试</Button>} />}
      <Table className="presales-inquiry-list" aria-label="报价工作台队列" rowKey="id" dataSource={queue} loading={loading} tableLayout="fixed"
        pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: setPage }}
        rowSelection={onSelectionChange ? { selectedRowKeys: selected, getCheckboxProps: (item) => ({ disabled: isRowSelectable ? !isRowSelectable(item) : false }),
          onChange: (keys) => { setSelected(keys); onSelectionChange(items.filter((item) => keys.includes(item.id))); } } : undefined}
        locale={{ emptyText: loadError ? "未加载待办" : "暂无待办" }} columns={[
          { title: "询价人", key: "inquirer", render: (_, item) => renderRow(item) },
          { title: "PN", key: "pn", render: (_, item) => {
            const pns = Array.isArray(item.summary.pns) ? item.summary.pns.map(String).filter(Boolean) : [];
            return pns.length ? pns.join(" · ") : "—";
          } },
          { title: "行数", key: "lines", sorter: (a, b) => Number(a.summary.line_count ?? 0) - Number(b.summary.line_count ?? 0), render: (_, item) => String(item.summary.line_count ?? "—") },
          { title: "状态", key: "status", render: (_, item) => <><Tag>{statusLabels[item.status]}</Tag>{item.paused && <Tag>已暂停</Tag>}</> },
          { title: "最近动态", key: "updated", sorter: (a, b) => Date.parse(a.updated_at) - Date.parse(b.updated_at), render: (_, item) => {
            const [date, time = ""] = formatBusinessDateTime(item.updated_at).split(/\s+/);
            return <span className="presales-stamp"><span>{date}</span>{time && <span>{time}</span>}</span>;
          } },
          { title: "操作", key: "actions", width: 148, render: (_, item) => actions(item) },
        ]} />
      <Modal
        open={!!dialog}
        title={dialog ? `${actionLabels[dialog.action]}待办` : "待办操作"}
        okText={dialog ? `确认${actionLabels[dialog.action]}` : "确认"}
        cancelText="取消"
        onCancel={() => { if (busy === null) setDialog(undefined); }}
        onOk={submitDialog}
        confirmLoading={busy !== null}
        okButtonProps={{ disabled: !reason.trim() || (!!dialog && (!dialog.item.allowed_actions.includes(dialog.action) || (["transfer", "reassign"].includes(dialog.action) && (target === undefined || candidatesLoading)))) }}
        destroyOnHidden
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          {dialog && ["transfer", "reassign"].includes(dialog.action) && (
            <>
              {candidateError && <Alert type="error" message="候选人加载失败" description={<ApiErrorFeedback info={candidateError} />} />}
              <Select
                aria-label="接收人"
                placeholder="请选择接收人"
                style={{ width: "100%" }}
                value={target}
                loading={candidatesLoading}
                disabled={candidatesLoading || busy !== null}
                showSearch
                optionFilterProp="label"
                options={[
                  ...(dialog.action === "reassign" ? [{ value: 0, label: "收回公共池" }] : []),
                  ...candidates.filter((candidate) => candidate.id !== dialog.item.assignee_id)
                    .map((candidate) => ({ value: candidate.id, label: candidate.display_name })),
                ]}
                onChange={setTarget}
              />
            </>
          )}
          <Input.TextArea aria-label="原因" placeholder="请填写原因（必填）" value={reason} disabled={busy !== null} rows={3} onChange={(event) => setReason(event.target.value)} />
        </Space>
      </Modal>
    </>;
}
