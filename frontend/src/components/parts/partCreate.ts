import type { ManualSpecInput, MasterCreateInput, MasterSpecField } from "../../api";
import type { PartSuggestion } from "../../api/search";

export interface SpecDraft { id: number; name: string; value: string }

// 与服务端规范单位一致；只转换明确写出的单位，不猜测自由文本的数值。
function specNumber(key: string, value: string): string | null {
  const match = value.match(/^(\d+(?:\.\d+)?)\s*([A-Za-z/]*)$/);
  if (!match) return null;
  const factors: Record<string, Record<string, number>> = {
    capacity: { GB: 0, TB: 3, MB: -3 }, rpm: { K: 3, RPM: 0, "": 0 },
    speed: { GBPS: 0, "GB/S": 0 }, cache: { MB: 0 }, frequency: { MHZ: 0, "": 0 },
  };
  const shift = factors[key]?.[match[2].toUpperCase()];
  if (shift === undefined) return null;
  const [whole, fraction = ""] = match[1].split(".");
  const digits = whole + fraction;
  const point = whole.length + shift;
  const normalized = point <= 0 ? `0.${"0".repeat(-point)}${digits}`
    : point >= digits.length ? digits + "0".repeat(point - digits.length)
    : `${digits.slice(0, point)}.${digits.slice(point)}`;
  const [integer, decimal = ""] = normalized.split(".");
  const tail = decimal.replace(/0+$/, "");
  return (integer.replace(/^0+(?=\d)/, "") || "0") + (tail ? `.${tail}` : "");
}

export function buildSpecUpdates(rows: SpecDraft[], fields: MasterSpecField[]): Record<string, ManualSpecInput> {
  const result: Record<string, ManualSpecInput> = {};
  for (const row of rows) {
    const name = row.name.trim();
    const value = row.value.trim();
    if (!name && !value) continue;
    if (!name || !value) throw new Error("每个参数都需要填写名称和值；不用的参数请删除。");
    const standard = fields.find((field) => field.key === name || field.label === name);
    if (!standard && (Array.from(name).length > 25 || /[\u0000-\u001f\u007f]/.test(name))) {
      throw new Error("自定义参数名称须为 1–25 个字符，不能包含换行或控制字符。");
    }
    if (Array.from(value).length > 128) throw new Error(`参数「${name}」的值不能超过 128 个字符。`);
    const key = standard?.key ?? `custom:${name}`;
    if (Object.prototype.hasOwnProperty.call(result, key)) throw new Error(`参数「${name}」重复，请合并成一行。`);
    result[key] = { value, unit: standard?.unit ?? null, numeric_value: standard ? specNumber(standard.key, value) : null };
  }
  return result;
}

/** 使用建档回执的真实 ID 与规范 PN，加入草稿不再发一次可能失败的搜索请求。 */
export function createdPartSuggestion(id: number, pn: string, input: MasterCreateInput, fields: MasterSpecField[]): PartSuggestion {
  return {
    part_id: id, pn_std: pn, description: input.description, summary: input.summary,
    brand: input.brand, manufacturer: input.manufacturer,
    category_major: input.category_major, category_minor: input.category_minor,
    key_specs: Object.entries(input.spec_updates ?? {}).map(([key, spec]) => ({
      label: fields.find((field) => field.key === key)?.label ?? key.replace(/^custom:/, ""), value: spec.value,
    })),
    match_type: "exact", match_reason: "新建档案", matched_terms: [],
  };
}
