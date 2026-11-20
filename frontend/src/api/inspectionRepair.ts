import { api } from "../api";

export type RepairRole = "sales" | "supervisor" | "technician" | "admin";
export type RepairMode = "internal" | "external";
export type RepairResult = "fixed" | "unfixed";
export type RepairDestination = "company" | "retry" | "shop";

export interface RepairAccount {
  username: string;
  displayName: string;
}

export interface RepairSession extends RepairAccount {
  roles: RepairRole[];
  technicians: RepairAccount[];
}

export interface RepairReturnCandidate {
  receiptId: string;
  version: number;
  headNo: string | null;
  occurredAt: string | null;
  pn: string;
  description: string | null;
  condition: string;
  availableQty: number;
}

export interface NewRepairLine {
  pn: string;
  description: string;
  qty: number;
  sourceReceiptId?: string | null;
  sourceVersion?: number | null;
}

export interface RepairDisposition {
  kind: RepairDestination;
  qty: number;
  result: RepairResult;
  retryOrderId?: string | null;
}

export interface RepairLine extends NewRepairLine {
  fixed: number;
  unfixed: number;
  sourceHeadNo?: string | null;
  previouslyHandled?: boolean;
  dispositions?: RepairDisposition[];
  lineIndex?: number;
}

export interface RepairOrder {
  id: string;
  version: number;
  creator: string;
  creatorName?: string | null;
  assignee?: string | null;
  assigneeName?: string | null;
  route: "pool" | "direct" | "assigned" | "claimed";
  createdAt: string;
  lines: RepairLine[];
  note?: string | null;
  mode: RepairMode;
  returned?: boolean;
  history: { name: string; event: string; at: string }[];
  repairSource?: { orderId: string; line: number } | null;
}

export type RepairWriteResponse = RepairOrder | { id: string; version: number; lines: [] };

export type RepairAction =
  | { type: "create"; mode: RepairMode; assignee?: string; note: string; lines: NewRepairLine[] }
  | { type: "assign"; id: string; assignee: string }
  | { type: "claim"; id: string }
  | { type: "return"; id: string }
  | { type: "result"; id: string; line: number; fixed: number; unfixed: number }
  | { type: "edit"; id: string; mode: RepairMode; assignee?: string; note: string; lines: RepairLine[]; sourceIndices: (number | null)[] }
  | { type: "dispose"; id: string; line: number; result: RepairResult; kind: RepairDestination; qty: number };

const base = "/inspection-repair";

export async function getRepairSession(signal?: AbortSignal): Promise<RepairSession> {
  return (await api.get<RepairSession>(`${base}/session`, { signal })).data;
}

export async function listRepairOrders(role: RepairRole, signal?: AbortSignal): Promise<RepairOrder[]> {
  return (await api.get<RepairOrder[]>(`${base}/orders`, { params: { role }, signal })).data;
}

export async function downloadFixedDispositionWorkbook(): Promise<Blob> {
  return (await api.get<Blob>(`${base}/orders/export`, {
    params: { role: "supervisor" }, responseType: "blob",
  })).data;
}

export async function listRepairReturnCandidates(
  q: string, page: number, pageSize: number, signal?: AbortSignal,
): Promise<{ items: RepairReturnCandidate[]; total: number }> {
  return (await api.get<{ items: RepairReturnCandidate[]; total: number }>(`${base}/return-candidates`, {
    params: { role: "supervisor", q: q || undefined, page, page_size: pageSize },
    signal,
  })).data;
}

export async function applyRepairAction(
  role: RepairRole, action: RepairAction, version: number | undefined, key: string,
): Promise<RepairWriteResponse> {
  const options = { params: { role }, headers: { "Idempotency-Key": key } };
  if (action.type === "create") {
    const { type, ...body } = action;
    return (await api.post<RepairWriteResponse>(`${base}/orders`, body, options)).data;
  }
  const { id, ...body } = action;
  return (await api.post<RepairWriteResponse>(`${base}/orders/${encodeURIComponent(id)}/actions`, { ...body, version }, options)).data;
}
