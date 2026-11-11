import { api } from "../api";

export interface CycleUnit {
  id: number;
  unit_uid: string;
  part_id: number;
  pn: string;
  station: string;
  outcome: string | null;
  condition_bucket: string | null;
  intake_condition: string | null;
  repair_round: number;
  assignee_username: string | null;
  version: number;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
  source_kind?: string;
  source_receipt_id?: string | null;
  serial_number?: string | null;
  vendor_name?: string | null;
  note?: string | null;
}

export interface CycleUnitPage {
  items: CycleUnit[];
  total: number;
  page: number;
  page_size: number;
}

export interface SellIntentStatus {
  unit_id: number;
  status: string;
  deliveryId?: string;
  attempts?: number;
  blockCode?: string | null;
  lastErrorCode?: string | null;
  receiptStatus?: string | null;
  nextAttemptAt?: string | null;
}

export async function listUnits(params: {
  station?: string;
  page?: number;
  page_size?: number;
}): Promise<CycleUnitPage> {
  const { data } = await api.get<CycleUnitPage>("/cycle/units", { params });
  return data;
}

export async function wipCounts(partIds: number[]): Promise<Record<string, unknown>> {
  const { data } = await api.get("/cycle/wip", { params: { part_ids: partIds.join(",") } });
  return data;
}

export async function sellIntent(unitId: number): Promise<SellIntentStatus> {
  const { data } = await api.get<SellIntentStatus>(`/cycle/units/${unitId}/sell-intent`);
  return data;
}

async function postUnit(unitId: number, action: string, body: object): Promise<CycleUnit> {
  const { data } = await api.post<CycleUnit>(`/cycle/units/${unitId}/${action}`, body);
  return data;
}

export const intakeExternal = (body: object) =>
  api.post<CycleUnit[]>("/cycle/units/intake-external", body).then((r) => r.data);

export const handoffFromReceipt = (body: object) =>
  api.post<CycleUnit[]>("/cycle/units/handoff-from-receipt", body).then((r) => r.data);

export const dispatchUnit = (id: number, body: object) => postUnit(id, "dispatch", body);
export const selfPass = (id: number, expectedVersion: number) =>
  postUnit(id, "self-pass", { expected_version: expectedVersion });
export const completeTest = (id: number, body: object) => postUnit(id, "complete-test", body);
export const completeRepair = (id: number, body: object) => postUnit(id, "complete-repair", body);
export const outsourceReturn = (id: number, body: object) => postUnit(id, "outsource-return", body);
export const closeUnit = (id: number, body: object) => postUnit(id, "close", body);
export const withdrawUnit = (id: number, body: object) => postUnit(id, "withdraw", body);
export const claimUnit = (id: number, body: object) => postUnit(id, "claim", body);
export const markSold = (id: number, expectedVersion: number) =>
  postUnit(id, "mark-sold", { expected_version: expectedVersion });
