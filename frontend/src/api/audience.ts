import { api } from "../api";

export type AudienceMode = "public" | "private" | "include" | "exclude";

export interface AudienceRule {
  mode: AudienceMode;
  user_ids: string[];
  group_ids: number[];
}

export interface AudiencePerson {
  id: string;
  display_name: string;
}

export interface AudienceGroup {
  id: number;
  name: string;
  scope: "personal" | "public";
  owner_id: string;
  user_ids: string[];
  archived: boolean;
  created_at?: string;
  updated_at?: string;
}

export async function listAudiencePeople(q = "", signal?: AbortSignal): Promise<AudiencePerson[]> {
  const { data } = await api.get<AudiencePerson[]>("/audience/people", { params: { q }, signal });
  return data;
}

export async function listAudienceGroups(signal?: AbortSignal): Promise<AudienceGroup[]> {
  const { data } = await api.get<AudienceGroup[]>("/audience/groups", { signal });
  return data;
}

export async function createAudienceGroup(body: {
  name: string; scope: AudienceGroup["scope"]; user_ids: string[];
}): Promise<AudienceGroup> {
  const { data } = await api.post<AudienceGroup>("/audience/groups", body);
  return data;
}

export async function updateAudienceGroup(id: number, body: {
  name?: string; user_ids?: string[];
}): Promise<AudienceGroup> {
  const { data } = await api.patch<AudienceGroup>(`/audience/groups/${id}`, body);
  return data;
}

export async function archiveAudienceGroup(id: number): Promise<void> {
  await api.delete(`/audience/groups/${id}`);
}
