import type { AudienceRule, AudienceRuleRead } from "../../api/audience";

export function editableAudience(rule: AudienceRuleRead): AudienceRule {
  return { mode: rule.mode, user_ids: rule.users.map((user) => user.id), group_ids: rule.groups.map((group) => group.id) };
}

/** 回到原规则也算未修改，保存业务内容时不重写受众。 */
export function changedAudience(value?: AudienceRule, saved?: AudienceRuleRead): AudienceRule | undefined {
  if (!value || !saved) return value;
  const original = editableAudience(saved);
  const sameIds = (left: (string | number)[], right: (string | number)[]) =>
    JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());
  return value.mode === original.mode && sameIds(value.user_ids, original.user_ids)
    && sameIds(value.group_ids, original.group_ids) ? undefined : value;
}
