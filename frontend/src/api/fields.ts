import { api } from "../api";
import type { AudienceRule } from "./audience";

export type FieldLevel = "header" | "line";
export type FieldValueType = "text" | "number" | "date" | "bool" | "choice";

export interface FieldDefinition {
  id: number;
  owner_scope: "personal" | "team";
  owner_id: string | null;
  subject_type: string;
  level: FieldLevel;
  name: string;
  value_type: FieldValueType;
  choices: string[] | null;
  default_rule_id: number | null;
  default_rule: AudienceRule | null;
  auto_include: boolean;
  sort: number;
  archived: boolean;
  created_at?: string;
}

export interface FieldNameSuggestion {
  field_def_id: number;
  name: string;
  uses: number;
}

/** 由业务表单随自己的保存接口提交；空值由编辑器省略。 */
export type CustomFieldValue = ({ field_def_id: number; new_name?: never }
  | { new_name: string; field_def_id?: never }) & { value: string; rule?: AudienceRule };

export interface DefineFieldInput {
  subject_type: string;
  level: FieldLevel;
  name: string;
  owner_scope?: "personal" | "team";
  value_type?: FieldValueType;
  choices?: string[] | null;
  default_rule?: AudienceRule | null;
  auto_include?: boolean;
  sort?: number;
}

export async function listFieldDefinitions(subjectType: string, level: FieldLevel,
  signal?: AbortSignal): Promise<FieldDefinition[]> {
  const { data } = await api.get<FieldDefinition[]>("/fields/defs", {
    params: { subject_type: subjectType, level }, signal,
  });
  return data;
}

export async function getFieldTemplate(subjectType: string, level: FieldLevel,
  anchor?: string, signal?: AbortSignal): Promise<FieldDefinition[]> {
  const { data } = await api.get<FieldDefinition[]>("/fields/template", {
    params: { subject_type: subjectType, level, anchor }, signal,
  });
  return data;
}

export async function suggestFieldNames(subjectType: string, q: string,
  signal?: AbortSignal): Promise<FieldNameSuggestion[]> {
  const { data } = await api.get<FieldNameSuggestion[]>("/fields/name-suggestions", {
    params: { subject_type: subjectType, q }, signal,
  });
  return data;
}

export async function defineField(body: DefineFieldInput): Promise<FieldDefinition> {
  const { data } = await api.post<FieldDefinition>("/fields/defs", body);
  return data;
}

export async function updateFieldDefinition(id: number,
  body: Partial<Omit<DefineFieldInput, "subject_type" | "level" | "owner_scope">> & {
    archived?: boolean;
  }): Promise<FieldDefinition> {
  const { data } = await api.patch<FieldDefinition>(`/fields/defs/${id}`, body);
  return data;
}
