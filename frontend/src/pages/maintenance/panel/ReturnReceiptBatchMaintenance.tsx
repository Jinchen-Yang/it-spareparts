import { useEffect, useRef, useState } from "react";
import {
  Alert, Button, Input, InputNumber, Modal, Select, Space, Table, Tag, Typography, message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  type ReturnReceipt, type ReturnReceiptUpdateInput,
  updateReturnReceipt, voidReturnReceipt,
} from "../../../api/maintenanceOperations";
import { readError } from "./panelUtils";

const { Text } = Typography;

const CONDITIONS = ["成品", "坏品", "废品"] as const;
/** 凭据字段上限（v1.36）：与后端 ReceiptUpdate.evidence_ref 对齐；不设原生 maxLength 静默截断。 */
const EVIDENCE_MAX = 16384;
const SN_COUNT_MAX = 1000;
const QTY_MAX = 99999999999;

/** 行内编辑快照：touched 区分「未改（不发该字段）」与「清空（显式发 null）」。 */
interface EditRow {
  receiptId: string;
  pn: string;
  version: number;
  kind: string | undefined;
  hasSerials: boolean;
  qtyText: string;
  qtyOriginal: number;
  qty: number | null;
  qtyTouched: boolean;
  condition: string | null;
  conditionOriginal: string | null;
  conditionTouched: boolean;
  note: string;
  noteOriginal: string;
  noteTouched: boolean;
  evidence: string;
  evidenceOriginal: string;
  evidenceTouched: boolean;
  serialsText: string;
  serialsOriginal: string[];
  serialsTouched: boolean;
}

type RowStatus = "pending" | "ok" | "conflict" | "unknown" | "failed";

interface ResultRow {
  receiptId: string;
  pn: string;
  qtyText: string;
  status: RowStatus;
  detail: string;
}

/** 提交时冻结的行计划：{id, version, payload}。结果阶段的重试只允许原样重放。 */
interface FrozenRow {
  receiptId: string;
  pn: string;
  qtyText: string;
  version: number;
  reason: string;
  diff: Partial<ReturnReceiptUpdateInput>;
}

const parseSerials = (text: string): string[] =>
  text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);

const toEditRow = (receipt: ReturnReceipt): EditRow => ({
  receiptId: receipt.receipt_id,
  pn: receipt.pn,
  version: receipt.version,
  kind: receipt.receipt_kind,
  hasSerials: !!receipt.serial_numbers?.length,
  qtyText: receipt.qty,
  qtyOriginal: Number(receipt.qty),
  qty: Number(receipt.qty),
  qtyTouched: false,
  condition: receipt.condition ?? null,
  conditionOriginal: receipt.condition ?? null,
  conditionTouched: false,
  note: receipt.note ?? "",
  noteOriginal: (receipt.note ?? "").trim(),
  noteTouched: false,
  evidence: receipt.evidence_ref ?? "",
  evidenceOriginal: (receipt.evidence_ref ?? "").trim(),
  evidenceTouched: false,
  serialsText: receipt.serial_numbers?.length ? receipt.serial_numbers.join("\n") : "",
  serialsOriginal: receipt.serial_numbers ?? [],
  serialsTouched: false,
});

/** 行级校验：不合法行阻止该行提交，不影响其他行。 */
export function editRowError(row: EditRow): string | null {
  if (row.qtyTouched) {
    const value = row.qty;
    if (value == null || !Number.isInteger(value) || value <= 0 || value > QTY_MAX) {
      return "数量须为正整数，且不超过 99999999999";
    }
  }
  if (row.evidenceTouched && row.evidence.length > EVIDENCE_MAX) {
    return `凭据超过 ${EVIDENCE_MAX} 字符上限（当前 ${row.evidence.length}），请删减后再保存`;
  }
  // SN 与「最终数量」一致性：改 qty 或改 SN 任一触碰都要按合并后的终态校验
  // （与后端 update_receipt 相同口径：qty 单独改也不能留下与 SN 不一致的行）
  if (row.serialsTouched || (row.qtyTouched && (row.hasSerials || row.serialsTouched))) {
    const serials = row.serialsTouched ? parseSerials(row.serialsText) : row.serialsOriginal;
    if (row.serialsTouched) {
      const seen = new Set<string>();
      for (const sn of serials) {
        if (!sn) return "SN 不能为空白";
        if (sn.length > 128) return `SN 长度不能超过 128（当前 ${sn.length}）：${sn.slice(0, 16)}…`;
        if (seen.has(sn)) return `SN 重复：${sn}`;
        seen.add(sn);
      }
      if (serials.length > SN_COUNT_MAX) {
        return `SN 超过单条 ${SN_COUNT_MAX} 个上限（当前 ${serials.length} 个）`;
      }
    }
    if (serials.length) {
      const finalQty = row.qtyTouched ? row.qty : row.qtyOriginal;
      if (finalQty == null || serials.length !== finalQty) {
        return `带 SN 时数量必须等于 SN 个数（当前数量 ${finalQty ?? "—"}，SN ${serials.length} 个）`;
      }
    }
  }
  return null;
}

/** 只发有实际变更的字段；后端 PATCH exclude_unset，未列字段不会被覆盖。 */
export function buildEditDiff(row: EditRow): Partial<ReturnReceiptUpdateInput> {
  const diff: Partial<ReturnReceiptUpdateInput> = {};
  if (row.qtyTouched && row.qty != null && row.qty !== row.qtyOriginal) diff.qty = row.qty;
  if (row.conditionTouched && row.condition !== row.conditionOriginal) {
    diff.condition = row.condition as ReturnReceiptUpdateInput["condition"];
  }
  if (row.noteTouched && row.note.trim() !== row.noteOriginal) diff.note = row.note.trim() || null;
  if (row.evidenceTouched && row.evidence.trim() !== row.evidenceOriginal) {
    diff.evidence_ref = row.evidence.trim() || null;
  }
  if (row.serialsTouched
    && parseSerials(row.serialsText).join("\n") !== row.serialsOriginal.join("\n")) {
    diff.serial_numbers = parseSerials(row.serialsText);
  }
  return diff;
}

/** 网络层无响应或 5xx 归为「结果未知」：首次可能已落库，绝不能伪称失败或成功。 */
const errorStatus = (error: unknown): RowStatus => {
  const status = (error as { response?: { status?: number } })?.response?.status;
  if (status === 409) return "conflict";
  // 401/403/422 等 4xx 是确定拒绝，说明那次请求未落库；无响应/5xx 仍未知
  if (status != null && status >= 400 && status < 500) return "failed";
  return "unknown";
};

const UNKNOWN_RETRY_HINT = "网络异常或服务错误，结果未知。可原样重试（沿用原版本号，若首次已成功将返回版本冲突），或刷新核对";
const CONFLICT_HINT = "版本冲突：可能此前一次提交已成功，或他人已修改。请刷新核对，系统不会自动用新版本覆盖";
/** 重试请求收到 4xx（非 409）：只能证明这次重试被拒，不能证明首次未落库 → 保持「结果未知」。 */
const RETRY_4XX_HINT = "本次重试被拒绝：不改变原提交「结果未知」判定，请刷新核对实际状态";

const STATUS_TAG: Record<RowStatus, { color: string; label: string }> = {
  pending: { color: "default", label: "等待提交" },
  ok: { color: "green", label: "已提交" },
  conflict: { color: "red", label: "版本冲突" },
  unknown: { color: "orange", label: "结果未知" },
  failed: { color: "default", label: "未提交" },
};

/**
 * 返还台账批量修改 / 批量作废（v1.36 剩余需求）：
 * 逐行独立编辑、独立版本 CAS、共同原因留痕；只提交有实际变更的行。
 * 部分成功语义：行间不构成事务；成功行锁定不重发；409 不自动升级 version；
 * 网络失败/5xx 视为结果未知，只允许沿用原 version 原样重试（首次已成功时会得到 409）。
 */
export default function ReturnReceiptBatchMaintenance({ mode, receipts, onDone }: {
  mode: "update" | "void";
  /** 当前勾选且有效的返还记录（父级已过滤已作废行；打开弹窗时冻结快照）。 */
  receipts: ReturnReceipt[];
  onDone: () => Promise<void>;
}) {
  const isVoid = mode === "void";
  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<"edit" | "results">("edit");
  const [rows, setRows] = useState<EditRow[]>([]);
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [retryingId, setRetryingId] = useState<string | null>(null);
  const [results, setResults] = useState<ResultRow[]>([]);
  const lockRef = useRef(false);
  const frozenRef = useRef<FrozenRow[]>([]);
  // 只认最新一发：卸载/项目切换（组件随选择清空被卸载）后，在途循环不得续发后续行
  const seqRef = useRef(0);
  useEffect(() => () => { seqRef.current += 1; }, []);

  const busy = submitting || retryingId !== null;

  const patchRow = (receiptId: string, patch: Partial<EditRow>) => {
    setRows((prev) => prev.map((row) => (row.receiptId === receiptId ? { ...row, ...patch } : row)));
  };

  const planOf = (reasonTrim: string) => {
    if (isVoid) {
      return rows.map((row) => ({ row, diff: {} as Partial<ReturnReceiptUpdateInput>, skip: null as string | null }));
    }
    return rows.map((row) => {
      const error = editRowError(row);
      if (error) return { row, diff: null, skip: error };
      const diff = buildEditDiff(row);
      if (!Object.keys(diff).length) return { row, diff: null, skip: "未作任何修改，未提交" };
      return { row, diff, skip: null };
    }).map((item) => ({ ...item, diff: item.diff ?? {} as Partial<ReturnReceiptUpdateInput> }));
  };

  const sendableCount = (() => {
    if (!open || phase !== "edit") return 0;
    const reasonTrim = reason.trim();
    if (!reasonTrim) return 0;
    return planOf(reasonTrim).filter((item) => !item.skip).length;
  })();

  const markResult = (receiptId: string, patch: Partial<ResultRow>) => {
    setResults((prev) => prev.map((row) => (row.receiptId === receiptId ? { ...row, ...patch } : row)));
  };

  const sendRow = async (item: FrozenRow) => {
    if (isVoid) {
      await voidReturnReceipt(item.receiptId, { version: item.version, reason: item.reason });
    } else {
      await updateReturnReceipt(item.receiptId, { version: item.version, reason: item.reason, ...item.diff });
    }
  };

  const describeError = (error: unknown, status: RowStatus): string => {
    if (status === "conflict") return `${CONFLICT_HINT}（${readError(error, "版本冲突")}）`;
    if (status === "unknown") return `${UNKNOWN_RETRY_HINT}（${readError(error, "网络异常")}）`;
    return readError(error, "提交被拒绝");
  };

  /** 用本次提交的最终局部结果算汇总提示——不读 state（闭包里还是初始空数组）。 */
  const summarize = (finalRows: ResultRow[]) => {
    const counts = finalRows.reduce<Record<RowStatus, number>>(
      (acc, row) => ({ ...acc, [row.status]: acc[row.status] + 1 }),
      { pending: 0, ok: 0, conflict: 0, unknown: 0, failed: 0 },
    );
    const label = isVoid ? "批量作废" : "批量修改";
    if (counts.ok === finalRows.length && finalRows.length > 0) {
      message.success(`${label}完成：${counts.ok} 条`);
    } else {
      const parts = [
        counts.ok ? `成功 ${counts.ok}` : null,
        counts.conflict ? `冲突 ${counts.conflict}` : null,
        counts.unknown ? `结果未知 ${counts.unknown}` : null,
        counts.failed ? `未提交 ${counts.failed}` : null,
      ].filter(Boolean);
      message.warning(`${label}结束：${parts.join("，")}；请按行核对结果`);
    }
  };

  const submitAll = async () => {
    // 同步锁：双击的第二次点击在 state 刷新前就应被挡下
    if (lockRef.current) return;
    const reasonTrim = reason.trim();
    if (!reasonTrim) {
      message.error(isVoid ? "批量作废必须填写共同原因" : "批量修改必须填写共同原因");
      return;
    }
    const plan = planOf(reasonTrim);
    const sendable = plan.filter((item) => !item.skip);
    if (!sendable.length) {
      message.warning("没有可提交的行：请先修改字段（或核对行内校验）");
      return;
    }
    lockRef.current = true;
    const mySeq = ++seqRef.current;
    setSubmitting(true);
    frozenRef.current = sendable.map(({ row, diff }) => ({
      receiptId: row.receiptId,
      pn: row.pn,
      qtyText: row.qtyText,
      version: row.version,
      reason: reasonTrim,
      diff,
    }));
    const finalRows: ResultRow[] = plan.map(({ row, skip }) => ({
      receiptId: row.receiptId,
      pn: row.pn,
      qtyText: row.qtyText,
      status: skip ? "failed" : "pending",
      detail: skip ?? "等待提交",
    }));
    setResults(finalRows);
    setPhase("results");
    for (const item of frozenRef.current) {
      if (seqRef.current !== mySeq) return;
      try {
        await sendRow(item);
        if (seqRef.current !== mySeq) return;
        const target = finalRows.find((row) => row.receiptId === item.receiptId);
        if (target) { target.status = "ok"; target.detail = "已提交"; }
        setResults([...finalRows]);
      } catch (error) {
        if (seqRef.current !== mySeq) return;
        const status = errorStatus(error);
        const target = finalRows.find((row) => row.receiptId === item.receiptId);
        if (target) { target.status = status; target.detail = describeError(error, status); }
        setResults([...finalRows]);
      }
    }
    if (seqRef.current !== mySeq) return;
    setSubmitting(false);
    lockRef.current = false;
    summarize(finalRows);
  };

  const retryRow = async (receiptId: string) => {
    // 同步锁（与 submitAll 同一 lockRef）：双击重试在 state 刷新前就被挡下
    if (lockRef.current) return;
    const item = frozenRef.current.find((row) => row.receiptId === receiptId);
    if (!item) return;
    lockRef.current = true;
    const mySeq = seqRef.current;
    setRetryingId(receiptId);
    markResult(receiptId, { detail: "正在原样重试（沿用原版本号，若首次已成功将返回版本冲突）…" });
    try {
      await sendRow(item);
      if (seqRef.current !== mySeq) return;
      markResult(receiptId, { status: "ok", detail: "已提交" });
    } catch (error) {
      if (seqRef.current !== mySeq) return;
      const status = errorStatus(error);
      if (status === "conflict" || status === "unknown") {
        markResult(receiptId, { status, detail: describeError(error, status) });
      } else {
        // 401/403/422 等 4xx：只证明这次重试被拒，不证明首次未落库 → 维持「结果未知」
        markResult(receiptId, {
          status: "unknown",
          detail: `${RETRY_4XX_HINT}（${readError(error, "重试被拒绝")}）`,
        });
      }
    } finally {
      lockRef.current = false;
      if (seqRef.current === mySeq) setRetryingId(null);
    }
  };

  const openModal = () => {
    setRows(receipts.map(toEditRow));
    setReason("");
    setResults([]);
    setPhase("edit");
    frozenRef.current = [];
    setOpen(true);
  };

  const close = () => {
    if (busy) return;
    setOpen(false);
    // ok/conflict/unknown 都意味着服务端状态可能已变（conflict 可能是首次重试已成功）→ 刷新父列表
    if (results.some((row) => row.status !== "failed")) void onDone();
  };

  const counts = results.reduce<{ ok: number; conflict: number; unknown: number }>(
    (acc, row) => ({
      ok: acc.ok + (row.status === "ok" ? 1 : 0),
      conflict: acc.conflict + (row.status === "conflict" ? 1 : 0),
      unknown: acc.unknown + (row.status === "unknown" ? 1 : 0),
    }),
    { ok: 0, conflict: 0, unknown: 0 },
  );

  const editColumns: ColumnsType<EditRow> = [
    {
      title: "返件 PN", dataIndex: "pn", width: 150,
      render: (_v, row) => (
        <Space direction="vertical" size={0}>
          <Text copyable style={{ fontFamily: "monospace", fontSize: 12 }}>{row.pn}</Text>
          <Space size={4}>
            <Tag style={{ marginRight: 0 }}>v{row.version}</Tag>
            {row.kind === "machine" ? <Tag style={{ marginRight: 0 }}>整机</Tag> : null}
          </Space>
        </Space>
      ),
    },
    { title: "原数量", dataIndex: "qtyText", width: 70 },
    {
      title: "数量", width: 100,
      render: (_v, row) => (
        <InputNumber
          min={1}
          max={QTY_MAX}
          style={{ width: "100%" }}
          value={row.kind === "machine" ? 1 : row.qty}
          disabled={row.kind === "machine"}
          onChange={(value) => patchRow(row.receiptId, { qty: value, qtyTouched: true })}
        />
      ),
    },
    {
      title: "件况", width: 96,
      render: (_v, row) => (
        <Select
          allowClear
          style={{ width: "100%" }}
          size="small"
          placeholder="未填写"
          value={row.condition ?? undefined}
          options={CONDITIONS.map((value) => ({ value, label: value }))}
          onChange={(value) => patchRow(row.receiptId, { condition: value ?? null, conditionTouched: true })}
        />
      ),
    },
    {
      title: "逐件 SN（每行一个）", width: 240,
      render: (_v, row) => row.kind === "machine"
        ? <Text type="secondary" style={{ fontSize: 12 }}>整机不单独记录 SN</Text>
        : row.hasSerials
          ? (
            <Input.TextArea
              rows={3}
              style={{ fontFamily: "monospace", fontSize: 12 }}
              placeholder="每行一个 SN"
              value={row.serialsText}
              onChange={(event) => patchRow(row.receiptId, { serialsText: event.target.value, serialsTouched: true })}
            />
          )
          : <Text type="secondary" style={{ fontSize: 12 }}>—（无逐件凭证，保持不变）</Text>,
    },
    {
      title: "备注", width: 160,
      render: (_v, row) => (
        <Input.TextArea
          rows={2}
          placeholder="备注"
          maxLength={512}
          value={row.note}
          onChange={(event) => patchRow(row.receiptId, { note: event.target.value, noteTouched: true })}
        />
      ),
    },
    {
      title: "凭据/单号", width: 190,
      render: (_v, row) => (
        <Input.TextArea
          rows={2}
          placeholder="凭据/单号"
          style={{ fontFamily: "monospace", fontSize: 12 }}
          value={row.evidence}
          onChange={(event) => patchRow(row.receiptId, { evidence: event.target.value, evidenceTouched: true })}
        />
      ),
    },
    {
      title: "行校验", width: 200,
      render: (_v, row) => {
        const error = editRowError(row);
        if (error) return <Text type="danger" style={{ fontSize: 12 }}>{error}</Text>;
        const changed = Object.keys(buildEditDiff(row)).length > 0;
        return changed ? <Tag color="blue">将提交变更</Tag> : <Tag>未修改，不发</Tag>;
      },
    },
  ];

  const resultColumns: ColumnsType<ResultRow> = [
    { title: "返件 PN", dataIndex: "pn", render: (v: string) => <Text style={{ fontFamily: "monospace", fontSize: 12 }}>{v}</Text> },
    { title: "数量", dataIndex: "qtyText", width: 70 },
    {
      title: "结果", dataIndex: "status", width: 96,
      render: (status: RowStatus) => <Tag color={STATUS_TAG[status].color}>{STATUS_TAG[status].label}</Tag>,
    },
    {
      title: "说明", dataIndex: "detail",
      render: (detail: string, row) => (
        <Space direction="vertical" size={4} style={{ width: "100%" }}>
          <Text type={row.status === "ok" ? undefined : row.status === "failed" ? "secondary" : "danger"} style={{ fontSize: 12 }}>{detail}</Text>
          {row.status === "unknown" ? (
            <Button
              size="small"
              loading={retryingId === row.receiptId}
              disabled={busy}
              onClick={() => { void retryRow(row.receiptId); }}
            >
              原样重试（原版本号）
            </Button>
          ) : null}
        </Space>
      ),
    },
  ];

  if (!receipts.length) return null;
  const actionLabel = isVoid ? "批量作废" : "批量修改";

  return (
    <>
      <Button size="small" danger={isVoid} onClick={openModal}>{actionLabel}</Button>
      <Modal
        open={open}
        title={`${actionLabel}返还（已选 ${rows.length} 条）`}
        width={isVoid ? 720 : 1080}
        confirmLoading={submitting}
        okText={phase === "results" ? "关闭" : isVoid
          ? (sendableCount ? `作废 ${sendableCount} 条` : "确认作废")
          : (sendableCount ? `修改 ${sendableCount} 条` : "确认修改")}
        okButtonProps={phase === "edit" ? { danger: isVoid, disabled: !sendableCount } : undefined}
        cancelText={phase === "results" ? undefined : "取消"}
        cancelButtonProps={{ disabled: busy }}
        maskClosable={!busy}
        onCancel={() => { if (phase === "edit" && !busy) { setOpen(false); return; } close(); }}
        onOk={() => { if (phase === "edit") { void submitAll(); return; } close(); }}
      >
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          {phase === "edit" ? (
            <>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {isVoid
                  ? "逐条按各自版本号作废（无事务：行间独立成败）；作废后退出项目/需求单全部有效统计，历史与审计保留。"
                  : "逐行独立编辑、独立版本 CAS；未修改的行不会提交；清空备注/凭据会显式置空；未显示字段（项目/PN/需求单）不会被覆盖。"}
              </Text>
              {isVoid ? (
                <Table<EditRow>
                  size="small"
                  rowKey="receiptId"
                  pagination={false}
                  dataSource={rows}
                  scroll={{ y: 320 }}
                  columns={[
                    { title: "返件 PN", dataIndex: "pn", render: (v: string, row) => (
                      <Space direction="vertical" size={0}>
                        <Text style={{ fontFamily: "monospace", fontSize: 12 }}>{v}</Text>
                        <Tag style={{ marginRight: 0 }}>v{row.version}</Tag>
                      </Space>
                    ) },
                    { title: "数量", dataIndex: "qtyText", width: 80 },
                    { title: "件况", dataIndex: "condition", width: 90, render: (v: string | null) => v ?? "未填写" },
                    { title: "备注", dataIndex: "note", render: (v: string) => v || "—" },
                  ]}
                />
              ) : (
                <Table<EditRow>
                  size="small"
                  rowKey="receiptId"
                  pagination={false}
                  dataSource={rows}
                  columns={editColumns}
                  scroll={{ x: 1040, y: 380 }}
                />
              )}
              <Alert
                type="info"
                showIcon
                message={isVoid ? "作废原因（必填，应用于全部选中行）" : "修改原因（必填，应用于全部实际提交的行）"}
                description={(
                  <Input.TextArea
                    rows={2}
                    maxLength={256}
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    placeholder={isVoid ? "如：批量重复登记 / 录入错误" : "如：实物清点批量修正"}
                  />
                )}
              />
            </>
          ) : (
            <>
              {counts.conflict || counts.unknown ? (
                <Alert
                  type="warning"
                  showIcon
                  message={counts.conflict ? "存在版本冲突：请刷新核对后再操作，系统不会自动用新版本覆盖" : "存在结果未知的行：请刷新核对，或原样重试"}
                  action={<Button size="small" onClick={() => { void onDone(); }}>刷新列表</Button>}
                />
              ) : counts.ok === results.length ? (
                <Alert type="success" showIcon message={`全部 ${counts.ok} 条已提交`} />
              ) : (
                <Alert type="warning" showIcon message="部分行未提交，请按行核对结果" />
              )}
              <Table<ResultRow>
                size="small"
                rowKey="receiptId"
                pagination={false}
                dataSource={results}
                columns={resultColumns}
                scroll={{ y: 360 }}
              />
            </>
          )}
        </Space>
      </Modal>
    </>
  );
}
