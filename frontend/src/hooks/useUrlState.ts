import { useSearchParams } from "react-router-dom";

export interface UrlStateField {
  defaultValue: string;
  isValid?: (value: string) => boolean;
}

/** 服务端页码：正安全整数；拒绝小数、指数、负数及溢出。 */
export const urlPageField: UrlStateField = {
  defaultValue: "1",
  isValid: (value) => /^[1-9]\d*$/.test(value) && Number.isSafeInteger(Number(value)),
};

type UrlState<Fields> = { [Key in keyof Fields]: string };
type UrlPatch<Fields> = Partial<{ [Key in keyof Fields]: string | null | undefined }>;

function readValue(field: UrlStateField, value: string | null | undefined): string {
  return value && (!field.isValid || field.isValid(value)) ? value : field.defaultValue;
}

/**
 * URL 是已提交状态的唯一来源；草稿由调用页维护。
 * 仅写 patch 指定键，空/默认/非法值删除，其他参数原样保留。
 * 筛选、分页和标签切换统一 replace，不堆浏览器历史；相关键须一次 patch。
 * 读取非法值时只回退，不在挂载时导航或清理外来链接。
 */
export function useUrlState<Fields extends Record<string, UrlStateField>>(fields: Fields) {
  const [searchParams, setSearchParams] = useSearchParams();
  const state = Object.fromEntries(Object.entries(fields).map(([key, field]) => (
    [key, readValue(field, searchParams.get(key))]
  ))) as UrlState<Fields>;

  const patch = (next: UrlPatch<Fields>) => {
    setSearchParams((previous) => {
      const merged = new URLSearchParams(previous);
      for (const key of Object.keys(next)) {
        if (!Object.prototype.hasOwnProperty.call(fields, key)) continue;
        const field = fields[key];
        const value = readValue(field, next[key]);
        if (value === "" || value === field.defaultValue) merged.delete(key);
        else merged.set(key, value);
      }
      return merged;
    }, { replace: true });
  };

  return [state, patch] as const;
}
