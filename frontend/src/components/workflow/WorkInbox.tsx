import { useEffect, useRef, useState, type ReactNode } from "react";
import { Alert, App, Badge, Button, Input, List, Modal, Select, Space, Tabs, Tag } from "antd";
import {
  claimWorkItem, completeWorkItem, declineWorkItem, listWorkCandidates, listWorkItems,
  reassignWorkItem, releaseWorkItem, startWorkItem, transferWorkItem,
  type WorkAction, type WorkActionBody, type WorkCandidate, type WorkItem, type WorkTab,
} from "../../api/workItems";
import { refreshWorkBadges, useWorkBadge } from "./useWorkBadge";

interface Props {
  subjectType?: string;
  renderRow: (item: WorkItem) => ReactNode;
  onOpen: (item: WorkItem) => void | Promise<void>;
}

type ReasonAction = "release" | "transfer" | "reassign" | "decline";
const actionLabels: Record<WorkAction, string> = {
  claim: "领取", start: "打开", release: "退回", transfer: "转交", reassign: "改派",
  complete: "完成", decline: "拒绝",
};
const statusLabels = { pending: "待处理", in_progress: "处理中", done: "已完成", declined: "已拒绝", cancelled: "已取消" };

function errorMessage(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail && typeof detail.message === "string") return detail.message;
  return fallback;
}

export default function WorkInbox({ subjectType, renderRow, onOpen }: Props) {
  const { message } = App.useApp();
  const badge = useWorkBadge();
  const [tab, setTab] = useState<WorkTab>("assigned");
  const [page, setPage] = useState(1);
  const [items, setItems] = useState<WorkItem[]>([]);
  const [total, setTotal] = useState(0);
  const [canSupervise, setCanSupervise] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string>();
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState<number | null>(null);
  const [dialog, setDialog] = useState<{ action: ReasonAction; item: WorkItem }>();
  const [reason, setReason] = useState("");
  const [target, setTarget] = useState<number>();
  const [candidates, setCandidates] = useState<WorkCandidate[]>([]);
  const [candidatesLoading, setCandidatesLoading] = useState(false);
  const [candidateError, setCandidateError] = useState<string>();
  const mounted = useRef(true);
  const contextVersion = useRef(0);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setItems([]);
    setTotal(0);
    setLoading(true);
    setLoadError(undefined);
    void listWorkItems({ tab, subject_type: subjectType, page, page_size: 20 }, controller.signal)
      .then((data) => {
        if (controller.signal.aborted) return;
        setItems(data.items);
        setTotal(data.total);
        setCanSupervise(data.can_supervise);
        if (tab === "all" && !data.can_supervise) { setTab("assigned"); setPage(1); }
      })
      .catch((error) => {
        if (!controller.signal.aborted) setLoadError(errorMessage(error, "待办加载失败，请重试"));
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [tab, subjectType, page, reload]);

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
        if (!controller.signal.aborted) setCandidateError(errorMessage(error, "候选人加载失败，请重试"));
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
    try {
      const changed = await handlers[action](item.id, body);
      if (!isCurrent()) { refreshWorkBadges(); return; }
      setDialog(undefined);
      refresh();
      if (action === "start") await onOpen(changed);
      else message.success(`待办已${actionLabels[action]}`);
    } catch (error: unknown) {
      if (!isCurrent()) return;
      const response = (error as { response?: { status?: number; data?: { detail?: { claimed_by_name?: string } } } })?.response;
      if (response?.status === 409) {
        const name = response.data?.detail?.claimed_by_name;
        message.warning(action === "claim" && name ? `已被 ${name} 领取` : errorMessage(error, "待办已变化，请刷新后重试"));
        setDialog(undefined);
        refresh();
      } else message.error(errorMessage(error, `${actionLabels[action]}失败，请重试`));
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
    if (!dialog || !reason.trim()) return;
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

  return (
    <>
      <Tabs activeKey={tab} items={tabs} onChange={(key) => { setTab(key as WorkTab); setPage(1); }} />
      {loadError && <Alert type="error" showIcon message={loadError} action={<Button onClick={() => setReload((value) => value + 1)}>重试</Button>} />}
      <List
        rowKey="id"
        loading={loading}
        dataSource={items}
        locale={{ emptyText: loadError ? "未加载待办" : "暂无待办" }}
        pagination={{ current: page, pageSize: 20, total, showSizeChanger: false, onChange: setPage }}
        renderItem={(item) => (
          <List.Item actions={[
            <Button key="open" disabled={busy !== null} onClick={() => {
              if (item.allowed_actions.includes("start")) void perform(item, "start", { expected_version: item.version });
              else void onOpen(item);
            }}>打开</Button>,
            ...item.allowed_actions.filter((action) => action !== "start").map((action) => (
              <Button key={action} loading={busy === item.id} disabled={busy !== null} onClick={() => {
                if (["release", "transfer", "reassign", "decline"].includes(action)) openDialog(item, action as ReasonAction);
                else void perform(item, action, { expected_version: item.version });
              }}>{actionLabels[action]}</Button>
            )),
          ]}>
            <Space direction="vertical">
              {renderRow(item)}
              <Space><Tag>{statusLabels[item.status]}</Tag>{item.paused && <Tag color="orange">已暂停</Tag>}</Space>
            </Space>
          </List.Item>
        )}
      />
      <Modal
        open={!!dialog}
        title={dialog ? `${actionLabels[dialog.action]}待办` : "待办操作"}
        okText={dialog ? `确认${actionLabels[dialog.action]}` : "确认"}
        cancelText="取消"
        onCancel={() => { if (busy === null) setDialog(undefined); }}
        onOk={submitDialog}
        confirmLoading={busy !== null}
        okButtonProps={{ disabled: !reason.trim() || (!!dialog && ["transfer", "reassign"].includes(dialog.action) && (target === undefined || candidatesLoading)) }}
        destroyOnHidden
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          {dialog && ["transfer", "reassign"].includes(dialog.action) && (
            <>
              {candidateError && <Alert type="error" message={candidateError} />}
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
    </>
  );
}
