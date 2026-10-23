import type { ManualSpec, Part, ProfilePatch } from "../../api/pnCatalog";

export const PROFILE_LABELS: Record<string, string> = { description: "描述", brand: "品牌", manufacturer: "厂商", category_major: "品类", category_minor: "子类", machine_or_part: "整机 / 备件", unit: "单位" };
export const SPEC_LABELS: Record<string, string> = { cache: "缓存", capacity: "容量", form_factor: "尺寸规格", frequency: "频率", generation: "代次", interface: "接口", part_type: "部件类型", rank: "Rank", rpm: "转速", speed: "速率" };
export const SPEC_UNITS: Record<string, string | null> = { cache: "MB", capacity: "GB", frequency: "MHz", rpm: "RPM", speed: "Gbps" };
export const normalized = (value: string | null | undefined) => value?.trim() || null;
export interface ProfileDraft { fields: Record<string, string | null>; specs: Record<string, ManualSpec | null> }
export function profileDraft(part: Part): ProfileDraft {
  return { fields: { ...part.profile }, specs: Object.fromEntries(Object.entries(part.specs).map(([key, spec]) => [key, spec.deleted ? null : { value: spec.value, unit: spec.unit, numeric_value: spec.numeric_value }])) };
}
export function profilePatch(part: Part, draft: ProfileDraft): ProfilePatch {
  const changes = Object.fromEntries(Object.entries(draft.fields).filter(([key, value]) => normalized(value) !== normalized(part.profile[key])).map(([key, value]) => [key, normalized(value)]));
  if ("category_major" in changes || "category_minor" in changes) {
    changes.category_major = normalized(draft.fields.category_major);
    changes.category_minor = normalized(draft.fields.category_minor);
  }
  const initial = profileDraft(part).specs;
  const spec_updates = Object.fromEntries(Object.entries(draft.specs).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(initial[key] ?? null)));
  const paths = [...Object.keys(changes), ...Object.keys(spec_updates).map(key => `specs.${key}`)];
  return { expected_identity_revision: part.identity_revision, field_versions: Object.fromEntries(paths.map(key => [key, part.field_versions[key] || 0])), changes, spec_updates };
}
export function rebaseProfile(part: Part, draft: ProfileDraft, latest: Part): ProfileDraft {
  if (part.part_id !== latest.part_id || part.identity_revision !== latest.identity_revision || latest.identity.status !== "active") throw new Error("PN 身份已变化，请关闭编辑并重新查询；当前草稿仍保留。");
  const patch = profilePatch(part, draft);
  for (const key of Object.keys(patch.changes)) if (!(key in latest.profile)) throw new Error("字段权限已变化，无法沿用草稿");
  return { fields: { ...latest.profile, ...patch.changes }, specs: { ...profileDraft(latest).specs, ...patch.spec_updates } };
}
