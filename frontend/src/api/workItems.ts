import { api } from "../api";

export type WorkTab = "assigned" | "pool" | "claimed" | "done" | "all";
export type WorkStatus = "pending" | "in_progress" | "done" | "declined" | "cancelled";
export type WorkAction = "claim" | "start" | "release" | "transfer" | "reassign" | "complete" | "decline";

export interface WorkItem {
  id: number;
  subject_type: string;
  subject_id: number;
  pool_key: string;
  assignee_id: number | null;
  assignee_name?: string | null;
  claimed_by: number | null;
  claimed_by_name?: string | null;
  claimed_at?: string | null;
  status: WorkStatus;
  version: number;
  paused: boolean;
  created_by?: number | null;
  created_at: string;
  updated_at: string;
  closed_at?: string | null;
  last_reason?: string | null;
  summary: Record<string, unknown>;
  allowed_actions: WorkAction[];
}

export interface WorkBadge {
  assigned: number;
  pool: number;
  claimed: number;
}

export interface WorkItemPage {
  items: WorkItem[];
  total: number;
  page: number;
  page_size: number;
  can_supervise: boolean;
}

export interface WorkCandidate {
  id: number;
  display_name: string;
}

export interface SubjectEvent {
  id: number;
  subject_type: string;
  subject_id: number;
  work_item_id: number | null;
  kind: string;
  actor: number | null;
  actor_name?: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface SubjectEventPage {
  items: SubjectEvent[];
  total: number;
  page: number;
  page_size: number;
}

export interface WorkActionBody {
  expected_version: number;
  reason?: string;
  to_user_id?: number | null;
}

export async function listWorkItems(
  params: { tab: WorkTab; subject_type?: string; page?: number; page_size?: number },
  signal?: AbortSignal,
): Promise<WorkItemPage> {
  const { data } = await api.get<WorkItemPage>("/work-items", { params, signal });
  return data;
}

export async function getWorkBadge(signal?: AbortSignal): Promise<WorkBadge> {
  const { data } = await api.get<WorkBadge>("/work-items/badge", { signal });
  return data;
}

async function actOnWorkItem(id: number, action: WorkAction, body: WorkActionBody): Promise<WorkItem> {
  const { data } = await api.post<WorkItem>(`/work-items/${id}/${action}`, body);
  return data;
}

export const claimWorkItem = (id: number, body: WorkActionBody) => actOnWorkItem(id, "claim", body);
export const startWorkItem = (id: number, body: WorkActionBody) => actOnWorkItem(id, "start", body);
export const releaseWorkItem = (id: number, body: WorkActionBody) => actOnWorkItem(id, "release", body);
export const transferWorkItem = (id: number, body: WorkActionBody) => actOnWorkItem(id, "transfer", body);
export const reassignWorkItem = (id: number, body: WorkActionBody) => actOnWorkItem(id, "reassign", body);
export const completeWorkItem = (id: number, body: WorkActionBody) => actOnWorkItem(id, "complete", body);
export const declineWorkItem = (id: number, body: WorkActionBody) => actOnWorkItem(id, "decline", body);

export async function listWorkCandidates(poolKey: string, signal?: AbortSignal): Promise<{ items: WorkCandidate[] }> {
  const { data } = await api.get<{ items: WorkCandidate[] }>("/work-items/candidates", {
    params: { pool_key: poolKey }, signal,
  });
  return data;
}

export async function listSubjectEvents(
  subjectType: string, subjectId: number,
  params: { page?: number; page_size?: number } = {}, signal?: AbortSignal,
): Promise<SubjectEventPage> {
  const { data } = await api.get<SubjectEventPage>(
    `/subjects/${encodeURIComponent(subjectType)}/${subjectId}/events`, { params, signal },
  );
  return data;
}
