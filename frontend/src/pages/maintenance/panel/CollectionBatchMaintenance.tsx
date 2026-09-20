import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Input, InputNumber, Modal, Select, Space, Table, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import type {
  MaintenanceCollectionSnapshotRow,
  MaintenanceContractSummary,
} from "../../../api/maintenanceOperations";
import {
  createProjectCollection,
  getMaintenanceProjectWorkspace,
  patchProjectCollection,
} from "../../../api/maintenanceOperations";
import { readError } from "./panelUtils";

const { Text } = Typography;

/** 后端 DTO 硬约束镜像（金额 0 <= x < 1e12；凭据 128 / 备注 32767 / 原因 1000）。 */
export const COLLECTION_BATCH_LIMITS = {
  amountMax: 10 ** 12,
  receiptRefMax: 128,
  remarkMax: 32767,
  reasonMax: 1000,
  pageSize: 100,
} as const;

/** 月份 YYYY-MM → 当月首日 ISO；拒绝 year 0000、非真实月份和多余字符。 */
export function normalizeBatchMonth(value: string): string | null {
  const match = /^(\d{4})-(\d{2})$/.exec(value.trim());
  if (!match || match[1] === "0000") return null;
  const month = Number(match[2]);
  if (month < 1 || month > 12) return null;
  return `${match[1]}-${match[2]}-01`;
}

export function parseBatchAmount(value: string): number | null {
  const text = value.trim();
  if (!/^\d+(\.\d+)?$/.test(text)) return null;
  const num = Number(text);
  return Number.isFinite(num) ? num : null;
}

export function validateBatchAmount(value: number | null): string | null {
  if (value == null || !Number.isFinite(value)) return "金额必填且须为纯数字";
  if (value < 0) return "金额不能为负";
  if (value >= COLLECTION_BATCH_LIMITS.amountMax) return "金额须小于 1e12";
  return null;
}

export function isDefiniteRejection(error: unknown): boolean {
  const status = (error as { response?: { status?: unknown } })?.response?.status;
  return typeof status === "number" && status >= 400 && status < 500 && status !== 408;
}

export function isConflictRejection(error: unknown): boolean {
  return (error as { response?: { status?: unknown } })?.response?.status === 409;
}

export interface FrozenCreateLine {
  kind: "create";
  rowKey: string;
  project_contract_id: string;
  report_month: string;
  cumulative_amount: number;
  status: "confirmed" | "unconfirmed";
  receipt_reference: string | null;
  remark: string | null;
  reason: string;
}

export interface FrozenPatchLine {
  kind: "patch";
  rowKey: string;
  collection_id: string;
  version: number;
  updates: {
    report_month?: string;
    cumulative_amount?: number;
    status?: "confirmed" | "unconfirmed" | "void";
    receipt_reference?: string | null;
    remark?: string | null;
  };
  reason: string;
}

export type FrozenLine = FrozenCreateLine | FrozenPatchLine;
export type LineOutcome = "pending" | "ok" | "verified" | "failed" | "conflict" | "unknown";

export interface ResultLine {
  line: FrozenLine;
  outcome: LineOutcome;
  wasUnknown: boolean;
  detail: string;
}

function fmtAmount(value: number | null | undefined): string {
  return value == null ? "—" : `¥${Number(value).toFixed(2)}`;
}

/** 只比较调用方显式提供的字段；undefined 不得被解释成清空。 */
export function diffPatchUpdates(
  base: MaintenanceCollectionSnapshotRow,
  edited: {
    report_month?: string;
    cumulative_amount?: number;
    status?: "confirmed" | "unconfirmed";
    receipt_reference?: string | null;
    remark?: string | null;
  },
): FrozenPatchLine["updates"] {
  const updates: FrozenPatchLine["updates"] = {};
  const baseMonth = base.report_month.slice(0, 7);
  if (edited.report_month && edited.report_month.trim() && edited.report_month.trim() !== baseMonth) {
    const normalized = normalizeBatchMonth(edited.report_month);
    if (normalized && normalized !== base.report_month) updates.report_month = normalized;
  }
  if (edited.cumulative_amount != null
    && Number.isFinite(edited.cumulative_amount)
    && edited.cumulative_amount !== Number(base.cumulative_amount)) {
    updates.cumulative_amount = edited.cumulative_amount;
  }
  if (edited.status && edited.status !== base.status) updates.status = edited.status;
  if (edited.receipt_reference !== undefined) {
    const ref = edited.receipt_reference?.trim() || null;
    if (ref !== (base.receipt_reference?.trim() || null)) updates.receipt_reference = ref;
  }
  if (edited.remark !== undefined) {
    const remark = edited.remark?.trim() || null;
    if (remark !== (base.remark?.trim() || null)) updates.remark = remark;
  }
  return updates;
}

export function createRowMatches(row: MaintenanceCollectionSnapshotRow, line: FrozenCreateLine): boolean {
  return row.project_contract_id === line.project_contract_id
    && row.report_month === line.report_month
    && row.status !== "void"
    && Number(row.cumulative_amount) === line.cumulative_amount
    && row.status === line.status
    && (row.receipt_reference?.trim() || null) === (line.receipt_reference?.trim() || null)
    && (row.remark?.trim() || null) === (line.remark?.trim() || null);
}

export function patchTargetsReached(
  row: MaintenanceCollectionSnapshotRow,
  line: FrozenPatchLine,
): { reached: boolean; misses: string[] } {
  const misses: string[] = [];
  const u = line.updates;
  if (u.report_month != null && row.report_month !== u.report_month) misses.push("月份");
  if (u.cumulative_amount != null && Number(row.cumulative_amount) !== u.cumulative_amount) {
    misses.push(`金额（当前 ${fmtAmount(row.cumulative_amount)}）`);
  }
  if (u.status != null && row.status !== u.status) misses.push("状态");
  if (u.receipt_reference !== undefined
    && (row.receipt_reference?.trim() || null) !== (u.receipt_reference?.trim() || null)) misses.push("凭据");
  if (u.remark !== undefined
    && (row.remark?.trim() || null) !== (u.remark?.trim() || null)) misses.push("备注");
  return { reached: misses.length === 0, misses };
}

type BatchMode = "create" | "edit" | "void";
type Phase = "input" | "results";

interface CreateDraft {
  rowKey: string;
  project_contract_id?: string;
  report_month: string;
  cumulative_amount: number | null;
  status: "confirmed" | "unconfirmed";
  receipt_reference: string;
  remark: string;
}

interface EditDraft {
  rowKey: string;
  base: MaintenanceCollectionSnapshotRow;
  report_month: string;
  cumulative_amount: number | null;
  status: "confirmed" | "unconfirmed";
  receipt_reference: string;
  remark: string;
}

interface BatchHookArgs {
  projectId: string;
  selectedRows: MaintenanceCollectionSnapshotRow[];
  onRefresh: () => Promise<boolean>;
}

let draftSequence = 0;
const newCreateDraft = (): CreateDraft => ({
  rowKey: `collection-new-${++draftSequence}`,
  report_month: "",
  cumulative_amount: null,
  status: "unconfirmed",
  receipt_reference: "",
  remark: "",
});

const toEditDraft = (row: MaintenanceCollectionSnapshotRow): EditDraft => ({
  rowKey: row.collection_id,
  base: row,
  report_month: row.report_month.slice(0, 7),
  cumulative_amount: row.cumulative_amount == null ? null : Number(row.cumulative_amount),
  status: row.status === "confirmed" ? "confirmed" : "unconfirmed",
  receipt_reference: row.receipt_reference ?? "",
  remark: row.remark ?? "",
});

/**
 * 批量回款核心状态机。每行提交前冻结 payload；成功行锁定，unknown 行重试时复用同一快照。
 * PATCH 永不自动升级 version；项目切换或卸载会让旧循环停止续发且不能清掉新会话的锁。
 */
export function useCollectionBatchMaintenance({ projectId, selectedRows, onRefresh }: BatchHookArgs) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<BatchMode>("create");
  const [phase, setPhase] = useState<Phase>("input");
  const [createDrafts, setCreateDrafts] = useState<CreateDraft[]>([newCreateDraft()]);
  const [editDrafts, setEditDrafts] = useState<EditDraft[]>([]);
  const [reason, setReason] = useState("");
  const [results, setResults] = useState<ResultLine[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [contracts, setContracts] = useState<MaintenanceContractSummary[]>([]);
  const [contractsState, setContractsState] = useState<"idle" | "loading" | "done" | "error">("idle");
  const sessionRef = useRef<{ projectId: string; token: number } | null>(null);
  const busyRef = useRef(false);
  const epochRef = useRef(0);

  const activeSelected = useMemo(() => selectedRows.filter((row) => row.status !== "void"), [selectedRows]);
  const hasUnknown = results.some((row) => row.outcome === "unknown");

  useEffect(() => {
    epochRef.current += 1;
    sessionRef.current = null;
    busyRef.current = false;
    setOpen(false);
    setPhase("input");
    setCreateDrafts([newCreateDraft()]);
    setEditDrafts([]);
    setReason("");
    setResults([]);
    setError(null);
    setSubmitting(false);
    setContracts([]);
    setContractsState("idle");
  }, [projectId]);

  useEffect(() => () => {
    epochRef.current += 1;
    sessionRef.current = null;
  }, []);

  const loadContracts = async () => {
    const epoch = epochRef.current;
    setContractsState("loading");
    try {
      const response = await getMaintenanceProjectWorkspace(projectId, {
        collection_page_size: 1,
        requisition_page_size: 1,
        expense_page_size: 1,
      });
      if (epoch !== epochRef.current) return;
      setContracts(Array.isArray(response.data.project?.contracts) ? response.data.project.contracts : []);
      setContractsState("done");
    } catch {
      if (epoch === epochRef.current) setContractsState("error");
    }
  };

  const initialize = (nextMode: BatchMode) => {
    setMode(nextMode);
    setPhase("input");
    setReason("");
    setResults([]);
    setError(null);
    if (nextMode === "create") {
      setCreateDrafts([newCreateDraft()]);
      void loadContracts();
    } else {
      setEditDrafts(activeSelected.map(toEditDraft));
    }
  };

  const openMode = (nextMode: BatchMode) => {
    if (busyRef.current) return;
    if (hasUnknown) {
      setOpen(true);
      setError("存在结果未知的旧批次，必须先用冻结请求重试或刷新核对，不能无提示开启新批次。");
      return;
    }
    if (phase === "results" && results.length > 0 && mode === nextMode) {
      setOpen(true);
      return;
    }
    initialize(nextMode);
    setOpen(true);
  };

  const close = async () => {
    if (busyRef.current) return;
    setOpen(false);
    const shouldRefresh = results.some((row) => ["ok", "verified", "unknown", "conflict"].includes(row.outcome));
    if (!shouldRefresh) return;
    const hasKnownWrite = results.some((row) => row.outcome === "ok");
    const refreshFailure = hasKnownWrite
      ? "写入已完成但刷新失败，可重试刷新；请勿重复登记。"
      : "批次结果仍需核对且刷新失败，请重试刷新；不要重新登记或自动覆盖。";
    const epoch = epochRef.current;
    try {
      const refreshed = await onRefresh();
      if (epoch === epochRef.current && !refreshed) message.error(refreshFailure);
    } catch {
      if (epoch === epochRef.current) message.error(refreshFailure);
    }
  };

  const readAllCollections = async (isCurrent: () => boolean) => {
    const all: MaintenanceCollectionSnapshotRow[] = [];
    let page = 1;
    while (isCurrent()) {
      const response = await getMaintenanceProjectWorkspace(projectId, {
        collection_page: page,
        collection_page_size: COLLECTION_BATCH_LIMITS.pageSize,
        requisition_page_size: 1,
        expense_page_size: 1,
      });
      if (!isCurrent()) return null;
      const block = response.data.collection_snapshots;
      all.push(...(Array.isArray(block?.rows) ? block.rows : []));
      const total = Number(block?.total ?? all.length);
      if (all.length >= total || !block?.rows?.length) return all;
      page += 1;
    }
    return null;
  };

  const buildLines = (): FrozenLine[] | null => {
    const trimmedReason = reason.trim();
    if (!trimmedReason) {
      setError(mode === "void" ? "共同作废原因必填" : "共同操作原因必填");
      return null;
    }
    if (trimmedReason.length > COLLECTION_BATCH_LIMITS.reasonMax) {
      setError(`原因不能超过 ${COLLECTION_BATCH_LIMITS.reasonMax} 字`);
      return null;
    }
    if (mode === "create") {
      const effectiveIds = new Set(contracts.filter((item) => item.is_effective).map((item) => item.project_contract_id));
      const seen = new Set<string>();
      const lines: FrozenCreateLine[] = [];
      for (let index = 0; index < createDrafts.length; index += 1) {
        const draft = createDrafts[index];
        if (!draft.project_contract_id || !effectiveIds.has(draft.project_contract_id)) {
          setError(`第 ${index + 1} 行必须选择当前项目的有效合同`);
          return null;
        }
        const month = normalizeBatchMonth(draft.report_month);
        if (!month) {
          setError(`第 ${index + 1} 行月份须为真实的 YYYY-MM，且年份不能为 0000`);
          return null;
        }
        const amountError = validateBatchAmount(draft.cumulative_amount);
        if (amountError) {
          setError(`第 ${index + 1} 行${amountError}`);
          return null;
        }
        if (draft.receipt_reference.length > COLLECTION_BATCH_LIMITS.receiptRefMax
          || draft.remark.length > COLLECTION_BATCH_LIMITS.remarkMax) {
          setError(`第 ${index + 1} 行凭据或备注超过后端长度限制`);
          return null;
        }
        const uniqueKey = `${draft.project_contract_id}|${month}`;
        if (seen.has(uniqueKey)) {
          setError(`第 ${index + 1} 行与本批其他行的合同和月份重复`);
          return null;
        }
        seen.add(uniqueKey);
        lines.push({
          kind: "create",
          rowKey: draft.rowKey,
          project_contract_id: draft.project_contract_id,
          report_month: month,
          cumulative_amount: draft.cumulative_amount!,
          status: draft.status,
          receipt_reference: draft.receipt_reference.trim() || null,
          remark: draft.remark.trim() || null,
          reason: trimmedReason,
        });
      }
      return lines;
    }

    const lines: FrozenPatchLine[] = [];
    for (let index = 0; index < editDrafts.length; index += 1) {
      const draft = editDrafts[index];
      if (draft.base.status === "void") continue;
      if (mode === "void") {
        lines.push({
          kind: "patch",
          rowKey: draft.rowKey,
          collection_id: draft.base.collection_id,
          version: draft.base.version,
          updates: { status: "void" },
          reason: trimmedReason,
        });
        continue;
      }
      if (!normalizeBatchMonth(draft.report_month)) {
        setError(`第 ${index + 1} 行月份须为真实的 YYYY-MM，且年份不能为 0000`);
        return null;
      }
      if (draft.cumulative_amount != null) {
        const amountError = validateBatchAmount(draft.cumulative_amount);
        if (amountError) {
          setError(`第 ${index + 1} 行${amountError}`);
          return null;
        }
      }
      if (draft.receipt_reference.length > COLLECTION_BATCH_LIMITS.receiptRefMax
        || draft.remark.length > COLLECTION_BATCH_LIMITS.remarkMax) {
        setError(`第 ${index + 1} 行凭据或备注超过后端长度限制`);
        return null;
      }
      const updates = diffPatchUpdates(draft.base, {
        report_month: draft.report_month,
        cumulative_amount: draft.cumulative_amount ?? undefined,
        status: draft.status,
        receipt_reference: draft.receipt_reference,
        remark: draft.remark,
      });
      if (Object.keys(updates).length === 0) continue;
      lines.push({
        kind: "patch",
        rowKey: draft.rowKey,
        collection_id: draft.base.collection_id,
        version: draft.base.version,
        updates,
        reason: trimmedReason,
      });
    }
    if (!lines.length) {
      setError(mode === "edit" ? "所选行均未发生变化，不会发送 PATCH" : "没有可作废的有效行");
      return null;
    }
    return lines;
  };

  const submitLines = async (retry: boolean) => {
    if (busyRef.current) return;
    let pending: ResultLine[];
    let ordered: ResultLine[];
    if (retry) {
      pending = results
        .filter((row) => row.outcome === "failed" || row.outcome === "unknown")
        .map((row) => ({ ...row, outcome: "pending", detail: "" }));
      if (!pending.length) return;
      const keys = new Set(pending.map((row) => row.line.rowKey));
      ordered = results.map((row) => keys.has(row.line.rowKey)
        ? pending.find((item) => item.line.rowKey === row.line.rowKey)!
        : row);
    } else {
      const lines = buildLines();
      if (!lines?.length) return;
      pending = lines.map((line) => ({ line, outcome: "pending", wasUnknown: false, detail: "" }));
      ordered = pending;
    }

    const session = { projectId, token: epochRef.current };
    sessionRef.current = session;
    const isCurrent = () => sessionRef.current === session
      && session.projectId === projectId
      && session.token === epochRef.current;
    busyRef.current = true;
    setError(null);
    setResults([...ordered]);
    setPhase("results");
    setSubmitting(true);
    const currentRows = [...ordered];

    for (const pendingRow of pending) {
      if (!isCurrent()) return;
      let next: ResultLine;
      try {
        const line = pendingRow.line;
        if (line.kind === "create") {
          await createProjectCollection(session.projectId, {
            project_contract_id: line.project_contract_id,
            report_month: line.report_month,
            cumulative_amount: line.cumulative_amount,
            status: line.status,
            receipt_reference: line.receipt_reference,
            remark: line.remark,
            reason: line.reason,
          });
        } else {
          await patchProjectCollection(line.collection_id, {
            version: line.version,
            reason: line.reason,
            ...line.updates,
          });
        }
        if (!isCurrent()) return;
        next = { ...pendingRow, outcome: "ok", detail: "已写入" };
      } catch (requestError) {
        if (!isCurrent()) return;
        const line = pendingRow.line;
        if (isConflictRejection(requestError)) {
          if (line.kind === "create" && !pendingRow.wasUnknown) {
            next = {
              ...pendingRow,
              outcome: "conflict",
              detail: "合同+月份已存在；未自动覆盖，请刷新后人工核对。",
            };
          } else {
            try {
              const allRows = await readAllCollections(isCurrent);
              if (!allRows || !isCurrent()) return;
              if (line.kind === "create") {
                const existing = allRows.find((row) => row.project_contract_id === line.project_contract_id
                  && row.report_month === line.report_month);
                next = existing && createRowMatches(existing, line)
                  ? { ...pendingRow, outcome: "verified", detail: "唯一约束冲突后已全分页核对存在；不归因于本次请求。" }
                  : { ...pendingRow, outcome: "conflict", detail: "唯一键已有记录但业务字段不一致，请人工刷新核对；未自动 PATCH。" };
              } else {
                const existing = allRows.find((row) => row.collection_id === line.collection_id);
                const check = existing ? patchTargetsReached(existing, line) : { reached: false, misses: ["记录"] };
                next = check.reached
                  ? { ...pendingRow, outcome: "verified", detail: "版本冲突后已核对当前值达到目标；不归因于本次请求。" }
                  : { ...pendingRow, outcome: "conflict", detail: `版本冲突，当前值未达目标（${check.misses.join("、")}）；请刷新后重新选择，绝不升级 version 重放。` };
              }
            } catch (verifyError) {
              next = pendingRow.wasUnknown
                ? { ...pendingRow, outcome: "unknown", wasUnknown: true, detail: `${readError(verifyError, "读回核对失败")}；此前结果仍未知。` }
                : { ...pendingRow, outcome: "conflict", detail: "发生冲突且读回核对失败，请人工刷新后核对。" };
            }
          }
        } else if (isDefiniteRejection(requestError) && !pendingRow.wasUnknown) {
          next = { ...pendingRow, outcome: "failed", detail: readError(requestError, "请求被后端拒绝") };
        } else {
          next = {
            ...pendingRow,
            outcome: "unknown",
            wasUnknown: true,
            detail: isDefiniteRejection(requestError)
              ? `${readError(requestError, "重试被拒绝")}；此前请求结果仍未知，不能据此断言未写入。`
              : `${readError(requestError, "网络或服务异常")}；结果未知，重试将复用冻结 payload。`,
          };
        }
      }
      const index = currentRows.findIndex((row) => row.line.rowKey === pendingRow.line.rowKey);
      currentRows[index] = next;
      if (isCurrent()) setResults([...currentRows]);
    }

    if (!isCurrent()) return;
    busyRef.current = false;
    sessionRef.current = null;
    setSubmitting(false);
  };

  const startNewBatch = () => {
    if (busyRef.current || hasUnknown) return;
    initialize(mode);
  };

  return {
    open,
    mode,
    phase,
    createDrafts,
    setCreateDrafts,
    editDrafts,
    setEditDrafts,
    reason,
    setReason,
    results,
    error,
    submitting,
    contracts,
    contractsState,
    hasUnknown,
    openMode,
    close,
    loadContracts,
    submit: () => submitLines(false),
    retry: () => submitLines(true),
    startNewBatch,
  };
}

const outcomeLabel: Record<LineOutcome, { text: string; color?: string }> = {
  pending: { text: "提交中…" },
  ok: { text: "已写入", color: "green" },
  verified: { text: "已核对当前值", color: "blue" },
  failed: { text: "失败", color: "red" },
  conflict: { text: "冲突待人工核对", color: "volcano" },
  unknown: { text: "结果未知", color: "orange" },
};

export default function CollectionBatchMaintenance(props: BatchHookArgs) {
  const batch = useCollectionBatchMaintenance(props);
  const activeCount = props.selectedRows.filter((row) => row.status !== "void").length;
  const resultColumns: ColumnsType<ResultLine> = [
    {
      title: "记录",
      key: "record",
      render: (_value, row) => row.line.kind === "create"
        ? `${row.line.project_contract_id} / ${row.line.report_month.slice(0, 7)}`
        : `${row.line.collection_id} / v${row.line.version}`,
    },
    {
      title: "结果",
      dataIndex: "outcome",
      width: 150,
      render: (value: LineOutcome) => <Tag color={outcomeLabel[value].color}>{outcomeLabel[value].text}</Tag>,
    },
    { title: "说明", dataIndex: "detail" },
  ];

  const createColumns: ColumnsType<CreateDraft> = [
    {
      title: "合同编号",
      width: 190,
      render: (_value, row, index) => (
        <Select
          aria-label={`第${index + 1}行合同`}
          value={row.project_contract_id}
          loading={batch.contractsState === "loading"}
          style={{ width: 175 }}
          options={batch.contracts.map((contract) => ({
            value: contract.project_contract_id,
            label: contract.contract_no || "（无合同编号）",
            disabled: !contract.is_effective,
          }))}
          onChange={(value) => batch.setCreateDrafts((drafts) => drafts.map((item) => item.rowKey === row.rowKey
            ? { ...item, project_contract_id: value }
            : item))}
        />
      ),
    },
    {
      title: "月份",
      width: 125,
      render: (_value, row, index) => <Input aria-label={`第${index + 1}行月份`} value={row.report_month} placeholder="YYYY-MM" onChange={(event) => batch.setCreateDrafts((drafts) => drafts.map((item) => item.rowKey === row.rowKey ? { ...item, report_month: event.target.value } : item))} />,
    },
    {
      title: "累计金额",
      width: 140,
      render: (_value, row, index) => <InputNumber aria-label={`第${index + 1}行累计金额`} value={row.cumulative_amount} min={0} precision={2} style={{ width: 125 }} onChange={(value) => batch.setCreateDrafts((drafts) => drafts.map((item) => item.rowKey === row.rowKey ? { ...item, cumulative_amount: value } : item))} />,
    },
    {
      title: "状态",
      width: 120,
      render: (_value, row, index) => <Select aria-label={`第${index + 1}行状态`} value={row.status} style={{ width: 105 }} options={[{ value: "confirmed", label: "已确认" }, { value: "unconfirmed", label: "未确认" }]} onChange={(value) => batch.setCreateDrafts((drafts) => drafts.map((item) => item.rowKey === row.rowKey ? { ...item, status: value } : item))} />,
    },
    {
      title: "凭据",
      width: 145,
      render: (_value, row, index) => <Input aria-label={`第${index + 1}行凭据`} value={row.receipt_reference} maxLength={COLLECTION_BATCH_LIMITS.receiptRefMax} onChange={(event) => batch.setCreateDrafts((drafts) => drafts.map((item) => item.rowKey === row.rowKey ? { ...item, receipt_reference: event.target.value } : item))} />,
    },
    {
      title: "备注",
      width: 145,
      render: (_value, row, index) => <Input aria-label={`第${index + 1}行备注`} value={row.remark} maxLength={COLLECTION_BATCH_LIMITS.remarkMax} onChange={(event) => batch.setCreateDrafts((drafts) => drafts.map((item) => item.rowKey === row.rowKey ? { ...item, remark: event.target.value } : item))} />,
    },
    {
      title: "",
      width: 55,
      render: (_value, row) => <Button size="small" danger disabled={batch.createDrafts.length === 1} onClick={() => batch.setCreateDrafts((drafts) => drafts.filter((item) => item.rowKey !== row.rowKey))}>删</Button>,
    },
  ];

  const editColumns: ColumnsType<EditDraft> = [
    { title: "合同", render: (_value, row) => row.base.contract_no || "—", width: 150 },
    {
      title: "月份",
      width: 125,
      render: (_value, row, index) => batch.mode === "void" ? row.report_month : <Input aria-label={`第${index + 1}行修改月份`} value={row.report_month} onChange={(event) => batch.setEditDrafts((drafts) => drafts.map((item) => item.rowKey === row.rowKey ? { ...item, report_month: event.target.value } : item))} />,
    },
    {
      title: "累计金额",
      width: 140,
      render: (_value, row, index) => batch.mode === "void" ? fmtAmount(row.cumulative_amount) : <InputNumber aria-label={`第${index + 1}行修改金额`} value={row.cumulative_amount} min={0} precision={2} style={{ width: 125 }} onChange={(value) => batch.setEditDrafts((drafts) => drafts.map((item) => item.rowKey === row.rowKey ? { ...item, cumulative_amount: value } : item))} />,
    },
    {
      title: "状态",
      width: 115,
      render: (_value, row, index) => batch.mode === "void" ? <Tag color="red">将作废</Tag> : <Select aria-label={`第${index + 1}行修改状态`} value={row.status} style={{ width: 100 }} options={[{ value: "confirmed", label: "已确认" }, { value: "unconfirmed", label: "未确认" }]} onChange={(value) => batch.setEditDrafts((drafts) => drafts.map((item) => item.rowKey === row.rowKey ? { ...item, status: value } : item))} />,
    },
    {
      title: "凭据",
      width: 145,
      render: (_value, row, index) => batch.mode === "void" ? (row.receipt_reference || "—") : <Input aria-label={`第${index + 1}行修改凭据`} value={row.receipt_reference} maxLength={COLLECTION_BATCH_LIMITS.receiptRefMax} onChange={(event) => batch.setEditDrafts((drafts) => drafts.map((item) => item.rowKey === row.rowKey ? { ...item, receipt_reference: event.target.value } : item))} />,
    },
    {
      title: "备注",
      width: 145,
      render: (_value, row, index) => batch.mode === "void" ? (row.remark || "—") : <Input aria-label={`第${index + 1}行修改备注`} value={row.remark} maxLength={COLLECTION_BATCH_LIMITS.remarkMax} onChange={(event) => batch.setEditDrafts((drafts) => drafts.map((item) => item.rowKey === row.rowKey ? { ...item, remark: event.target.value } : item))} />,
    },
    { title: "版本", render: (_value, row) => `v${row.base.version}`, width: 65 },
  ];

  const retryable = batch.results.filter((row) => row.outcome === "failed" || row.outcome === "unknown").length;
  return (
    <>
      <Space size={4} wrap>
        <Button size="small" onClick={() => batch.openMode("create")}>批量登记</Button>
        <Button size="small" disabled={activeCount === 0} onClick={() => batch.openMode("edit")}>批量修改{activeCount ? `（${activeCount}）` : ""}</Button>
        <Button size="small" danger disabled={activeCount === 0} onClick={() => batch.openMode("void")}>批量作废{activeCount ? `（${activeCount}）` : ""}</Button>
      </Space>
      <Modal
        open={batch.open}
        title={batch.mode === "create" ? "批量登记回款" : batch.mode === "edit" ? "批量修改回款" : "批量作废回款"}
        width={1100}
        destroyOnHidden={false}
        maskClosable={!batch.submitting}
        closable={!batch.submitting}
        onCancel={() => { void batch.close(); }}
        footer={batch.phase === "results" ? (
          <Space>
            <Button disabled={batch.submitting || batch.hasUnknown} onClick={batch.startNewBatch}>新批次</Button>
            {retryable ? <Button disabled={batch.submitting} onClick={() => { void batch.retry(); }}>重试 {retryable} 条</Button> : null}
            <Button type="primary" disabled={batch.submitting} onClick={() => { void batch.close(); }}>关闭</Button>
          </Space>
        ) : (
          <Space>
            <Button disabled={batch.submitting} onClick={() => { void batch.close(); }}>取消</Button>
            <Button
              type="primary"
              danger={batch.mode === "void"}
              loading={batch.submitting}
              onClick={() => { void batch.submit(); }}
            >
              {batch.mode === "create" ? `登记 ${batch.createDrafts.length} 条` : batch.mode === "edit" ? "保存批量修改" : "确认批量作废"}
            </Button>
          </Space>
        )}
      >
        {batch.error ? <Alert type="error" showIcon message={batch.error} style={{ marginBottom: 12 }} /> : null}
        {batch.hasUnknown ? <Alert type="warning" showIcon message="结果未知行保留冻结请求身份；不可开启新批次。重试不会生成 idempotency_key，而是复用相同业务 payload。" style={{ marginBottom: 12 }} /> : null}
        {batch.phase === "results" ? (
          <Table<ResultLine> rowKey={(row) => row.line.rowKey} size="small" pagination={false} dataSource={batch.results} columns={resultColumns} />
        ) : (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            {batch.mode === "create" && batch.contractsState === "error" ? (
              <Alert type="error" message="当前项目合同加载失败" action={<Button size="small" onClick={() => { void batch.loadContracts(); }}>重试</Button>} />
            ) : null}
            {batch.mode === "create" ? (
              <>
                <Table<CreateDraft> rowKey="rowKey" size="small" pagination={false} scroll={{ x: 1000 }} dataSource={batch.createDrafts} columns={createColumns} />
                <Button size="small" onClick={() => batch.setCreateDrafts((drafts) => [...drafts, newCreateDraft()])}>新增一行</Button>
              </>
            ) : (
              <>
                {batch.mode === "void" ? <Alert type="warning" message="以下有效行将分别携带自己的 version 作废；这不是整批事务，部分成功会被保留。" /> : null}
                <Table<EditDraft> rowKey="rowKey" size="small" pagination={false} scroll={{ x: 900 }} dataSource={batch.editDrafts} columns={editColumns} />
              </>
            )}
            <label>
              <Text strong>{batch.mode === "void" ? "共同作废原因（必填）" : "共同操作原因（必填）"}</Text>
              <Input.TextArea
                aria-label={batch.mode === "void" ? "共同作废原因" : "共同操作原因"}
                value={batch.reason}
                rows={2}
                maxLength={COLLECTION_BATCH_LIMITS.reasonMax}
                onChange={(event) => batch.setReason(event.target.value)}
              />
            </label>
          </Space>
        )}
      </Modal>
    </>
  );
}
