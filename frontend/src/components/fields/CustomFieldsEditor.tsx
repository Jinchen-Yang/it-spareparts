import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Input, Select, Space, Typography } from "antd";
import { EyeInvisibleOutlined, PlusOutlined } from "@ant-design/icons";
import {
  defineField, listFieldDefinitions, suggestFieldNames, type CustomFieldValue, type FieldDefinition,
  type FieldLevel, type FieldNameSuggestion, type FieldValueType,
} from "../../api/fields";
import type { AudienceRule } from "../../api/audience";
import AudienceLock from "../audience/AudienceLock";
import { PUBLIC_AUDIENCE, type AudiencePickerProps } from "../audience/AudiencePicker";

interface EditorField {
  key: string;
  id?: number;
  name: string;
  value_type: FieldValueType;
  choices?: string[] | null;
  default_rule?: AudienceRule | null;
}
const fromDefinition = (definition: FieldDefinition): EditorField => ({ ...definition, key: `id:${definition.id}` });
const valueKey = (value: CustomFieldValue) => value.field_def_id !== undefined
  ? `id:${value.field_def_id}` : `new:${value.new_name}`;

export interface CustomFieldsEditorProps {
  subjectType: string;
  level: FieldLevel;
  /** 业务层调用 getFieldTemplate（可带 anchor）取得；只带定义，不带历史值。 */
  template: FieldDefinition[];
  definitions?: FieldDefinition[];
  value?: CustomFieldValue[];
  onChange?: (values: CustomFieldValue[]) => void;
  disabled?: boolean;
  audienceProps?: Omit<AudiencePickerProps, "value" | "onChange" | "disabled">;
}

/** 一张单据/一行使用一个实例；切换单据或 anchor 时由调用方用 key 重建实例。 */
export function CustomFieldsEditor({ subjectType, level, template, definitions, value, onChange,
  disabled = false, audienceProps }: CustomFieldsEditorProps) {
  const [innerValue, setInnerValue] = useState<CustomFieldValue[]>([]);
  const current = value ?? innerValue;
  const [pool, setPool] = useState<FieldDefinition[]>(definitions ?? []);
  const [added, setAdded] = useState<EditorField[]>([]);
  const [hidden, setHidden] = useState<string[]>([]);
  const [rules, setRules] = useState<Record<string, AudienceRule>>({});
  const [adding, setAdding] = useState(false);
  const [query, setQuery] = useState("");
  const [suggestions, setSuggestions] = useState<FieldNameSuggestion[]>([]);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [definitionsReady, setDefinitionsReady] = useState(!!definitions);

  useEffect(() => {
    // 接受过的有值项同步回空框草稿，清空后重填不会复活更早的规则。
    setRules((previous) => {
      const next = { ...previous };
      let changed = false;
      current.filter((item) => item.value).forEach((item) => {
        const key = valueKey(item);
        if (item.rule && next[key] !== item.rule) { next[key] = item.rule; changed = true; }
        else if (!item.rule && key in next) { delete next[key]; changed = true; }
      });
      return changed ? next : previous;
    });
  }, [current]);
  useEffect(() => {
    if (definitions) { setPool(definitions); setDefinitionsReady(true); }
  }, [definitions]);
  useEffect(() => {
    if (definitions || !adding) return;
    const controller = new AbortController();
    setDefinitionsReady(false);
    listFieldDefinitions(subjectType, level, controller.signal).then((rows) => {
      if (!controller.signal.aborted) { setPool(rows); setDefinitionsReady(true); }
    }).catch(() => {
      if (!controller.signal.aborted) setError("字段池加载失败，请稍后重试");
    });
    return () => controller.abort();
  }, [definitions, adding, subjectType, level]);
  useEffect(() => {
    if (!adding) return;
    const controller = new AbortController();
    setLoading(true); setSuggestions([]);
    const timer = window.setTimeout(() => {
      suggestFieldNames(subjectType, query.trim(), controller.signal).then((rows) => {
        if (!controller.signal.aborted) setSuggestions(rows);
      }).catch(() => {
        if (!controller.signal.aborted) setError("字段名称联想加载失败，请稍后重试");
      }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, 150);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [adding, query, subjectType]);

  const fields = useMemo(() => {
    const entries = new Map<string, EditorField>();
    template.filter((definition) => !definition.archived).forEach((definition) => {
      const field = fromDefinition(definition); entries.set(field.key, field);
    });
    added.forEach((field) => entries.set(field.key, field));
    current.forEach((item) => {
      const key = valueKey(item);
      if (entries.has(key)) return;
      const definition = pool.find((field) => field.id === item.field_def_id);
      if (definition) entries.set(key, fromDefinition(definition));
      else if (item.new_name) entries.set(key, { key, name: item.new_name, value_type: "text" });
    });
    return Array.from(entries.values()).filter((field) => !hidden.includes(field.key));
  }, [template, added, current, pool, hidden]);
  const emit = (next: CustomFieldValue[]) => {
    const filled = next.filter((item) => item.value !== "" && item.value != null && !hidden.includes(valueKey(item)));
    setInnerValue(filled); onChange?.(filled);
  };
  const changeValue = (field: EditorField, next: string) => {
    const previous = current.find((item) => valueKey(item) === field.key);
    // 有值项以受控回填为准；本地规则仅是尚未填写的空框草稿。
    const rule = previous?.value ? previous.rule : rules[field.key] ?? previous?.rule;
    const identity = field.id !== undefined ? { field_def_id: field.id } : { new_name: field.name };
    const entry: CustomFieldValue = { ...identity, value: next, ...(rule ? { rule } : {}) };
    emit([...current.filter((item) => valueKey(item) !== field.key), entry]);
  };
  const changeRule = (field: EditorField, rule: AudienceRule) => {
    if (!current.some((item) => valueKey(item) === field.key && item.value)) {
      setRules((previous) => ({ ...previous, [field.key]: rule }));
    }
    emit(current.map((item) => valueKey(item) === field.key ? { ...item, rule } : item));
  };
  const hide = (field: EditorField) => {
    setHidden((previous) => [...previous, field.key]);
    emit(current.filter((item) => valueKey(item) !== field.key));
  };
  const add = (field: EditorField) => {
    if (fields.length >= 20) { setError("每层最多 20 个字段"); return; }
    setHidden((previous) => previous.filter((key) => key !== field.key));
    setAdded((previous) => [...previous.filter((item) => item.key !== field.key), field]);
    setAdding(false); setQuery(""); setError("");
  };
  const matching = suggestions.flatMap((suggestion) => {
    const definition = [...template, ...pool].find((item) => item.id === suggestion.field_def_id && item.level === level);
    if (!definition || fields.some((field) => field.key === `id:${definition.id}`)) return [];
    return [definition];
  });
  const newName = query.trim();
  const existing = [...template, ...pool].find((definition) => definition.name === newName && !definition.archived);
  const nameExists = existing || fields.some((field) => field.name === newName);
  const create = async () => {
    if (!newName || newName.length > 32 || nameExists || !definitionsReady || creating || fields.length >= 20) return;
    setCreating(true); setError("");
    try {
      // 新增的是可复用定义，即使本单不填写也保留空框；业务值仍由调用方保存。
      const definition = await defineField({ subject_type: subjectType, level, name: newName,
        owner_scope: "personal", value_type: "text", auto_include: true });
      setPool((previous) => [...previous.filter((field) => field.id !== definition.id), definition]);
      add(fromDefinition(definition));
    } catch { setError("字段创建失败，请检查名称后重试"); }
    finally { setCreating(false); }
  };

  return <Space direction="vertical" style={{ width: "100%" }} size="middle">
    {fields.map((field) => {
      const item = current.find((entry) => valueKey(entry) === field.key);
      const fieldValue = item?.value ?? "";
      const label = `${field.name}字段值`;
      let input;
      if (field.value_type === "bool" || field.value_type === "choice") {
        const options = field.value_type === "bool" ? [{ value: "true", label: "是" }, { value: "false", label: "否" }]
          : (field.choices ?? []).map((choice) => ({ value: choice, label: choice }));
        input = <Select aria-label={label} style={{ width: "100%" }} options={options} allowClear
          value={fieldValue || undefined} disabled={disabled} onChange={(next) => changeValue(field, next ?? "")} />;
      } else {
        input = <Input aria-label={label} value={fieldValue} maxLength={2000} disabled={disabled}
          type={field.value_type === "date" ? "date" : field.value_type === "number" ? "number" : "text"}
          step={field.value_type === "number" ? "any" : undefined}
          onChange={(event) => changeValue(field, event.target.value)} />;
      }
      return <div key={field.key}>
        <Space style={{ marginBottom: 4 }}>
          <Typography.Text>{field.name}</Typography.Text>
          <AudienceLock {...audienceProps} label={field.name} disabled={disabled}
            value={(item?.value ? item.rule : rules[field.key] ?? item?.rule) ?? field.default_rule ?? PUBLIC_AUDIENCE}
            onChange={(rule) => changeRule(field, rule)} />
          <Button type="text" size="small" icon={<EyeInvisibleOutlined />} disabled={disabled}
            aria-label={`临时隐藏${field.name}`} onClick={() => hide(field)} />
        </Space>
        {input}
      </div>;
    })}
    {adding ? <Space direction="vertical" style={{ width: "100%" }}>
      <Input autoFocus aria-label="字段名称" placeholder="输入字段名称，选择已有字段或新建" maxLength={32}
        value={query} disabled={disabled || creating} onChange={(event) => setQuery(event.target.value)} />
      {loading && <Typography.Text role="status">名称联想中…</Typography.Text>}
      {!!matching.length && <div role="listbox" aria-label="字段名称建议">
        {matching.map((definition) => <Button key={definition.id} role="option" type="text" disabled={disabled || creating}
          onClick={() => add(fromDefinition(definition))}>使用字段：{definition.name}</Button>)}
      </div>}
      {existing && !fields.some((field) => field.key === `id:${existing.id}`)
        && !matching.some((field) => field.id === existing.id)
        && <Button disabled={disabled || creating} onClick={() => add(fromDefinition(existing))}>使用字段：{existing.name}</Button>}
      <Space>
        <Button loading={creating} disabled={disabled || !newName || newName.length > 32 || !!nameExists || !definitionsReady}
          onClick={create}>
          新建字段{newName ? `「${newName}」` : ""}
        </Button>
        <Button onClick={() => { setAdding(false); setQuery(""); }} disabled={disabled || creating}>取消</Button>
      </Space>
    </Space> : <Button aria-label="新增字段" type="dashed" icon={<PlusOutlined />} disabled={disabled || fields.length >= 20}
      onClick={() => { setAdding(true); setError(""); }}>新增字段</Button>}
    {error && <Alert role="alert" type="error" message={error} />}
  </Space>;
}

export default CustomFieldsEditor;
