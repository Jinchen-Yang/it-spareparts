import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Input, Select, Space, Switch, Typography } from "antd";
import {
  createAudienceGroup, listAudienceGroups, listAudiencePeople,
  type AudienceGroup, type AudiencePerson, type AudienceRule, type AudienceRuleRead,
} from "../../api/audience";
import { editableAudience } from "./rules";
import { COLORS } from "../../theme";

export const PUBLIC_AUDIENCE: AudienceRule = { mode: "public", user_ids: [], group_ids: [] };

export interface AudiencePickerProps {
  value?: AudienceRule;
  savedRule?: AudienceRuleRead;
  onChange?: (value: AudienceRule) => void;
  people?: AudiencePerson[];
  groups?: AudienceGroup[];
  disabled?: boolean;
  /** 只保存分组使用次数；调用方可传账号标识来隔离本机偏好。 */
  preferenceKey?: string;
}

function readFrequency(key: string): Record<string, number> {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(key) || "{}");
    if (!stored || typeof stored !== "object" || Array.isArray(stored)) return {};
    return Object.fromEntries(Object.entries(stored).filter(([, uses]) =>
      typeof uses === "number" && Number.isFinite(uses) && uses > 0));
  } catch { return {}; }
}

export function AudiencePicker({ value, savedRule, onChange, people: providedPeople, groups: providedGroups,
  disabled = false, preferenceKey = "audience.group-frequency" }: AudiencePickerProps) {
  const [innerValue, setInnerValue] = useState<AudienceRule>();
  const current = value ?? innerValue ?? (savedRule ? editableAudience(savedRule) : PUBLIC_AUDIENCE);
  const [people, setPeople] = useState<AudiencePerson[]>(providedPeople ?? []);
  const [groups, setGroups] = useState<AudienceGroup[]>(providedGroups ?? []);
  const [frequency, setFrequency] = useState(() => readFrequency(preferenceKey));
  const [query, setQuery] = useState("");
  const [groupName, setGroupName] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => { setInnerValue(undefined); }, [savedRule]);
  useEffect(() => { setFrequency(readFrequency(preferenceKey)); }, [preferenceKey]);
  useEffect(() => {
    if (providedGroups) { setGroups(providedGroups); return; }
    const controller = new AbortController();
    listAudienceGroups(controller.signal).then((rows) => {
      if (!controller.signal.aborted) setGroups(rows);
    }).catch(() => {
      if (!controller.signal.aborted) setError("分组加载失败，请稍后重试");
    });
    return () => controller.abort();
  }, [providedGroups]);
  useEffect(() => {
    if (providedPeople) { setPeople(providedPeople); return; }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      listAudiencePeople(query, controller.signal).then((rows) => {
        if (!controller.signal.aborted) {
          setPeople((previous) => Array.from(new Map([...previous, ...rows].map((p) => [p.id, p])).values()));
        }
      }).catch(() => {
        if (!controller.signal.aborted) setError("人员加载失败，请稍后重试");
      });
    }, query ? 150 : 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [providedPeople, query]);

  const sortedGroups = useMemo(() => groups.filter((g) => !g.archived).sort((a, b) =>
    (frequency[b.id] || 0) - (frequency[a.id] || 0) || a.name.localeCompare(b.name, "zh-CN")), [groups, frequency]);
  const groupOptions = sortedGroups.map((g) => ({ value: `group:${g.id}`, searchText: g.name,
    label: `${frequency[g.id] ? "常用 · " : ""}${g.name}（${g.scope === "public" ? "公共" : "个人"}分组）` }));
  const peopleOptions = people.map((p) => ({ value: `user:${p.id}`, label: p.display_name || p.id,
    searchText: `${p.display_name || ""} ${p.id}` }));
  // 当前规则可能引用不在本轮搜索结果或本人分组列表里的成员，仍须用回读名称显示。
  savedRule?.groups.forEach((group) => {
    if (!groupOptions.some((option) => option.value === `group:${group.id}`)) {
      groupOptions.push({ value: `group:${group.id}`, label: group.name, searchText: group.name });
    }
  });
  savedRule?.users.forEach((user) => {
    if (!peopleOptions.some((option) => option.value === `user:${user.id}`)) {
      peopleOptions.push({ value: `user:${user.id}`, label: user.name, searchText: `${user.name} ${user.id}` });
    }
  });
  const options = [{ label: "分组", options: groupOptions }, { label: "人员", options: peopleOptions }];
  const emit = (next: AudienceRule) => { setInnerValue(next); onChange?.(next); };
  const remember = (ids: number[]) => {
    const next = { ...frequency };
    ids.forEach((id) => { next[id] = (next[id] || 0) + 1; });
    setFrequency(next);
    try { localStorage.setItem(preferenceKey, JSON.stringify(next)); } catch { /* 偏好不可写不影响规则 */ }
  };
  const select = (mode: "include" | "exclude", selected: string[]) => {
    const user_ids = selected.filter((s) => s.startsWith("user:")).map((s) => s.slice(5));
    const group_ids = selected.filter((s) => s.startsWith("group:")).map((s) => Number(s.slice(6)));
    remember(group_ids.filter((id) => current.mode !== mode || !current.group_ids.includes(id)));
    // 一个规则只能使用一个面板，切换面板会清除另一侧，避免互相矛盾。
    emit(selected.length ? { mode, user_ids, group_ids } : PUBLIC_AUDIENCE);
  };
  const saveGroup = async () => {
    if (!groupName.trim() || !current.user_ids.length || saving) return;
    setSaving(true); setError("");
    try {
      const group = await createAudienceGroup({ name: groupName.trim(), scope: "personal", user_ids: current.user_ids });
      setGroups((previous) => [...previous.filter((g) => g.id !== group.id), group]);
      remember([group.id]);
      emit({ ...current, user_ids: [], group_ids: [...new Set([...current.group_ids, group.id])] });
      setGroupName("");
    } catch { setError("分组保存失败，请检查名称后重试"); }
    finally { setSaving(false); }
  };

  return <Space direction="vertical" style={{ width: "100%", minWidth: 260 }} size="middle">
    <Space wrap>
      <Button aria-label="公开" type={current.mode === "public" ? "primary" : "default"}
        disabled={disabled || saving} onClick={() => emit(PUBLIC_AUDIENCE)}>公开</Button>
      <span>私密 <Switch aria-label="私密" checked={current.mode === "private"} disabled={disabled || saving}
        onChange={(checked) => emit(checked ? { mode: "private", user_ids: [], group_ids: [] } : PUBLIC_AUDIENCE)} /></span>
    </Space>
    <Typography.Text type="secondary">填写人本人及主管始终可见。</Typography.Text>
    {(["include", "exclude"] as const).map((mode) => {
      const label = mode === "include" ? "谁可以看" : "不给谁看";
      const selected = current.mode === mode
        ? [...current.user_ids.map((id) => `user:${id}`), ...current.group_ids.map((id) => `group:${id}`)] : [];
      return <fieldset key={mode} style={{ border: `1px solid ${COLORS.border}`, borderRadius: 6, padding: 10 }}>
        <legend style={{ fontSize: 14, width: "auto", marginBottom: 4 }}>{label}</legend>
        <Select aria-label={label} mode="multiple" showSearch style={{ width: "100%" }}
          placeholder="选择人员或分组" value={selected} options={options} optionFilterProp="searchText"
          disabled={disabled || saving} onSearch={setQuery} onChange={(next) => select(mode, next)} />
      </fieldset>;
    })}
    <Space.Compact style={{ width: "100%" }}>
      <Input aria-label="新分组名称" placeholder="把选中的人员存成分组" maxLength={64} value={groupName}
        disabled={disabled || saving} onChange={(event) => setGroupName(event.target.value)} />
      <Button onClick={saveGroup} loading={saving}
        disabled={disabled || !current.user_ids.length || !groupName.trim()}>存为分组</Button>
    </Space.Compact>
    {error && <Alert type="error" role="alert" message={error} />}
  </Space>;
}

export default AudiencePicker;
