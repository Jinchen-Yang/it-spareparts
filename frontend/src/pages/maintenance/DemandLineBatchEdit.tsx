import { useEffect, useRef, useState } from "react";
import { Alert, Button, Input, InputNumber, Modal, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { DemandLineRow } from "../../api/maintenanceDemands";
import { listDemandLines, patchDemandLine } from "../../api/maintenanceDemands";

const { Text } = Typography;
const TEXT_FIELD_MAX = 32767;

type EditStatus = "draft" | "queued" | "running" | "succeeded" | "failed" | "conflict" | "nochange" | "gone";

interface EditDraft {
  id: string;
  base: DemandLineRow;
  qty: number | null;
  returnQty: number | null;
  serialNumbers: string;
  description: string;
}

interface FrozenEdit {
  id: string;
  digest: string;
  updates: Record<string, unknown>;
  status: EditStatus;
  error?: string;
}

const statusTag: Record<EditStatus, { color: string; text: string }> = {
  draft: { color: "blue", text: "待重新修改" },
  queued: { color: "default", text: "等待提交" },
  running: { color: "processing", text: "提交中" },
  succeeded: { color: "success", text: "成功" },
  failed: { color: "error", text: "失败" },
  conflict: { color: "warning", text: "版本冲突" },
  nochange: { color: "default", text: "无变更" },
  gone: { color: "default", text: "已失效" },
};

function readError(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === "string" && detail ? detail : fallback;
}

function isConflict(error: unknown): boolean {
  return (error as { response?: { status?: number } })?.response?.status === 409;
}

function draftFrom(row: DemandLineRow): EditDraft {
  return {
    id: row.raw_line_id,
    base: row,
    qty: row.qty == null ? null : Number(row.qty),
    returnQty: row.return_qty == null ? null : Number(row.return_qty),
    serialNumbers: row.serial_numbers ?? "",
    description: row.description ?? "",
  };
}

function updatesFor(draft: EditDraft): Record<string, unknown> {
  const updates: Record<string, unknown> = {};
  if (draft.qty !== null
    && (draft.base.qty == null || Math.abs(Number(draft.base.qty) - draft.qty) > 1e-9)) {
    updates.qty = draft.qty;
  }
  if (draft.returnQty !== null
    && (draft.base.return_qty == null
      || Math.abs(Number(draft.base.return_qty) - draft.returnQty) > 1e-9)) {
    updates.return_qty = draft.returnQty;
  }
  const serialNumbers = draft.serialNumbers.trim();
  if (serialNumbers !== (draft.base.serial_numbers ?? "")) updates.serial_numbers = serialNumbers;
  const description = draft.description.trim();
  if (description !== (draft.base.description ?? "")) updates.description = description;
  return updates;
}

/**
 * 选中明细行的逐行批量 PATCH。成功行永不重发；普通失败保留原 digest/payload；
 * 409 必须显式 GET 最新行，把服务器值与新 digest 一起重建草稿后再由用户修改。
 */
export default function DemandLineBatchEdit({
  sourceOrderId,
  rows,
  onClose,
  onCommitted,
}: {
  sourceOrderId: string;
  rows: DemandLineRow[];
  onClose: () => void;
  onCommitted: () => void | Promise<unknown>;
}) {
  const [drafts, setDrafts] = useState<EditDraft[]>(() => rows.map(draftFrom));
  const [reason, setReason] = useState("");
  const [attempts, setAttempts] = useState<Record<string, FrozenEdit>>({});
  const attemptsRef = useRef<Record<string, FrozenEdit>>({});
  const [frozen, setFrozen] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const writeLock = useRef(false);
  const epochRef = useRef(0);

  useEffect(() => {
    epochRef.current += 1;
    writeLock.current = false;
    attemptsRef.current = {};
    setDrafts(rows.map(draftFrom));
    setAttempts({});
    setFrozen(false);
    setRunning(false);
    setError(null);
  }, [sourceOrderId, rows]);

  useEffect(() => () => { epochRef.current += 1; }, []);

  const syncAttempt = (id: string, next: FrozenEdit) => {
    const all = { ...attemptsRef.current, [id]: next };
    attemptsRef.current = all;
    setAttempts(all);
  };

  const updateDraft = (id: string, patch: Partial<EditDraft>) => {
    setDrafts((current) => current.map((row) => row.id === id ? { ...row, ...patch } : row));
  };

  const run = async (epoch: number, targets: FrozenEdit[]) => {
    let wroteAny = false;
    for (const target of targets) {
      if (epochRef.current !== epoch) return;
      syncAttempt(target.id, { ...target, status: "running", error: undefined });
      try {
        await patchDemandLine(target.id, target.updates, reason.trim(), target.digest);
        if (epochRef.current !== epoch) return;
        wroteAny = true;
        syncAttempt(target.id, { ...target, status: "succeeded", error: undefined });
      } catch (cause) {
        if (epochRef.current !== epoch) return;
        if (isConflict(cause)) {
          syncAttempt(target.id, {
            ...target,
            status: "conflict",
            error: readError(cause, "数据已被修改，请重新加载后重新编辑"),
          });
        } else {
          syncAttempt(target.id, {
            ...target,
            status: "failed",
            error: readError(cause, "修改失败；继续重试只会处理失败行"),
          });
        }
      }
    }
    if (epochRef.current !== epoch || !wroteAny) return;
    try {
      const refreshed = await onCommitted();
      if (epochRef.current !== epoch) return;
      if (refreshed === false || refreshed === null) {
        setError("修改已保存，但列表刷新失败；请稍后手动刷新查看最新数据");
      }
    } catch {
      if (epochRef.current === epoch) {
        setError("修改已保存，但列表刷新失败；请稍后手动刷新查看最新数据");
      }
    }
  };

  const submit = async () => {
    if (writeLock.current) return;
    writeLock.current = true;
    const epoch = epochRef.current;
    setRunning(true);
    setError(null);
    try {
      if (!reason.trim()) {
        setError("共同修改原因必填，且不能只填空白");
        return;
      }
      const invalid = drafts.findIndex((draft) =>
        (draft.qty === null && draft.base.qty !== null)
        || (draft.qty !== null && (!Number.isFinite(draft.qty) || draft.qty < 0))
        || (draft.returnQty === null && draft.base.return_qty !== null)
        || (draft.returnQty !== null && (!Number.isFinite(draft.returnQty) || draft.returnQty < 0))
        || draft.serialNumbers.trim().length > TEXT_FIELD_MAX
        || draft.description.trim().length > TEXT_FIELD_MAX);
      if (invalid >= 0) {
        setError(`第 ${invalid + 1} 行不合法：主动修改的数量不能留空且必须 ≥ 0，SN 和描述不能超过 ${TEXT_FIELD_MAX} 字符`);
        return;
      }

      const targets: FrozenEdit[] = [];
      const nextAttempts = { ...attemptsRef.current };
      for (const draft of drafts) {
        const existing = nextAttempts[draft.id];
        if (existing?.status === "failed") {
          targets.push({ ...existing, status: "queued", error: undefined });
          continue;
        }
        if (existing && existing.status !== "draft") continue;
        const updates = updatesFor(draft);
        const attempt: FrozenEdit = {
          id: draft.id,
          digest: draft.base.digest,
          updates,
          status: Object.keys(updates).length ? "queued" : "nochange",
        };
        nextAttempts[draft.id] = attempt;
        if (attempt.status === "queued") targets.push(attempt);
      }
      attemptsRef.current = nextAttempts;
      setAttempts(nextAttempts);
      setFrozen(true);
      if (!targets.length) return;
      await run(epoch, targets);
    } finally {
      if (epochRef.current === epoch) {
        setRunning(false);
        writeLock.current = false;
      }
    }
  };

  const reloadConflict = async (id: string) => {
    if (writeLock.current || attemptsRef.current[id]?.status !== "conflict") return;
    writeLock.current = true;
    const epoch = epochRef.current;
    setRunning(true);
    try {
      const response = await listDemandLines(sourceOrderId);
      if (epochRef.current !== epoch) return;
      const fresh = response.data.items.find((row) => row.raw_line_id === id && row.is_active);
      const previous = attemptsRef.current[id];
      if (!fresh) {
        syncAttempt(id, {
          ...previous,
          status: "gone",
          error: "该行已不存在或已作废，已禁止继续提交",
        });
        return;
      }
      setDrafts((current) => current.map((draft) => draft.id === id ? draftFrom(fresh) : draft));
      syncAttempt(id, {
        id,
        digest: fresh.digest,
        updates: {},
        status: "draft",
        error: "已加载服务器最新数据；请重新修改后再提交",
      });
    } catch (cause) {
      if (epochRef.current === epoch) {
        const previous = attemptsRef.current[id];
        syncAttempt(id, {
          ...previous,
          status: "conflict",
          error: readError(cause, "重新加载失败，请重试"),
        });
      }
    } finally {
      if (epochRef.current === epoch) {
        setRunning(false);
        writeLock.current = false;
      }
    }
  };

  const editable = (id: string) => {
    const status = attempts[id]?.status;
    return !running && (!frozen || status === "draft");
  };

  const columns: ColumnsType<EditDraft> = [
    { title: "#", width: 44, render: (_v, row) => row.base.line_no ?? "—" },
    { title: "PN", width: 160, render: (_v, row) => <Text code>{row.base.pn_std ?? "—"}</Text> },
    {
      title: "需求数量", width: 125,
      render: (_v, row) => <InputNumber
        aria-label={`第${row.base.line_no ?? row.id}行需求数量`}
        value={row.qty} min={0} precision={3} disabled={!editable(row.id)}
        onChange={(value) => updateDraft(row.id, { qty: value })}
      />,
    },
    {
      title: "退货数量", width: 125,
      render: (_v, row) => <InputNumber
        aria-label={`第${row.base.line_no ?? row.id}行退货数量`}
        value={row.returnQty} min={0} precision={3} disabled={!editable(row.id)}
        onChange={(value) => updateDraft(row.id, { returnQty: value })}
      />,
    },
    {
      title: "SN", width: 210,
      render: (_v, row) => <Input.TextArea
        aria-label={`第${row.base.line_no ?? row.id}行SN`}
        value={row.serialNumbers} rows={2} disabled={!editable(row.id)}
        onChange={(event) => updateDraft(row.id, { serialNumbers: event.target.value })}
      />,
    },
    {
      title: "描述", width: 190,
      render: (_v, row) => <Input.TextArea
        aria-label={`第${row.base.line_no ?? row.id}行描述`}
        value={row.description} rows={2} disabled={!editable(row.id)}
        onChange={(event) => updateDraft(row.id, { description: event.target.value })}
      />,
    },
    {
      title: "结果", width: 280,
      render: (_v, row) => {
        const attempt = attempts[row.id];
        if (!attempt) return "—";
        return (
          <Space direction="vertical" size={2}>
            <Tag color={statusTag[attempt.status].color}>{statusTag[attempt.status].text}</Tag>
            {attempt.error ? (
              <Text type={attempt.status === "draft" ? "secondary" : "danger"} style={{ fontSize: 12 }}>
                {attempt.error}
              </Text>
            ) : null}
            {attempt.status === "conflict" ? (
              <Button size="small" disabled={running} onClick={() => { void reloadConflict(row.id); }}>
                重新加载冲突行
              </Button>
            ) : null}
          </Space>
        );
      },
    },
  ];

  const retryable = Object.values(attempts).filter((attempt) =>
    attempt.status === "failed" || attempt.status === "draft").length;
  const succeeded = Object.values(attempts).filter((attempt) => attempt.status === "succeeded").length;
  const conflicts = Object.values(attempts).filter((attempt) => attempt.status === "conflict").length;

  return (
    <Modal
      open width={1120} title={`批量修改选中行（${rows.length} 行）`}
      footer={[
        <Button key="close" disabled={running} onClick={() => {
          epochRef.current += 1;
          writeLock.current = false;
          onClose();
        }}>关闭</Button>,
        <Button
          key="submit" type="primary" loading={running}
          disabled={frozen && retryable === 0}
          onClick={() => { void submit(); }}
        >{frozen ? `提交可重试行${retryable ? `（${retryable}）` : ""}` : "提交整批修改"}</Button>,
      ]}
      onCancel={() => {
        if (running) return;
        epochRef.current += 1;
        writeLock.current = false;
        onClose();
      }}
      maskClosable={false}
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Alert
          type="info" showIcon
          message="系统逐行保存，部分行失败不影响成功行；开始后本批修改内容固定，继续重试只处理失败行。型号请继续使用单条编辑入口。"
        />
        {error ? <Alert type="error" showIcon message={error} /> : null}
        {frozen ? (
          <Alert
            type={conflicts ? "warning" : "info"} showIcon
            message={`当前结果：成功 ${succeeded} 行，数据冲突 ${conflicts} 行。冲突行不会自动沿用旧修改，必须逐行重新加载并重新编辑。`}
          />
        ) : null}
        <Table<EditDraft>
          rowKey="id" size="small" dataSource={drafts} columns={columns}
          pagination={false} scroll={{ x: 1080 }}
        />
        <div>
          <Text type="secondary">共同修改原因（必填，审计留痕）</Text>
          <Input.TextArea
            aria-label="共同修改原因" rows={2} value={reason} disabled={frozen || running}
            placeholder="如：按现场盘点结果批量更正"
            onChange={(event) => setReason(event.target.value)}
          />
        </div>
      </Space>
    </Modal>
  );
}
