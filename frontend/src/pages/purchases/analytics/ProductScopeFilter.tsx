import { useEffect, useMemo, useState } from "react";
import { Select } from "antd";
import { api } from "../../../api";
import {
  type PurchaseAnalyticsFilters,
  type PurchaseScope,
  toAnalyticsParams,
} from "../../../api/purchaseAnalytics";
import { makeReadCache, useDashboardRead } from "./useDashboardRead";
type Axis = "pn" | "pool" | "unit";
type Result = { items: { key: string | null; label: string | null }[]; has_more: boolean };
const names = { pn: "PN / 描述", pool: "互通池", unit: "数量单位" };
const missing = { pn: "型号未填", pool: "未入池", unit: "单位未填" };

export default function ProductScopeFilter({
  filters,
  scope,
  labels,
  owner,
  stale,
  revision,
  onChange,
}: {
  filters: PurchaseAnalyticsFilters;
  scope: PurchaseScope;
  labels: Partial<Record<Axis, string | null>>;
  owner: string | null;
  stale: () => void;
  revision: number;
  onChange: (axis: Axis, item: Result["items"][number] | undefined) => void;
}) {
  const [open, setOpen] = useState<Axis | null>(null);
  const [draft, search] = useState("");
  const [term, setTerm] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setTerm(draft), 180);
    return () => clearTimeout(timer);
  }, [draft]);
  const cache = useMemo(() => makeReadCache<Result>(), [owner, revision]);
  const params = toAnalyticsParams(filters, { scope: JSON.stringify(scope), axis: open || "", q: term });
  const key = JSON.stringify([params.toString(), revision]);
  const response = useDashboardRead(
    open ? key : null,
    () =>
      cache.get(
        key,
        async () => (await api.get<Result>("/purchases/analytics/product-options", { params })).data,
      ),
    owner,
    stale,
  );
  return (
    <div className="purchase-product-filters">
      {(Object.keys(names) as Axis[]).map((axis) => {
        const items = axis === open ? response?.data?.items || [] : [];
        const label = (v: Result["items"][number]) =>
          v.key === null
            ? missing[axis]
            : axis === "pn"
              ? `${v.key} · ${v.label || "暂无目录描述"}`
              : v.label || v.key;
        return (
          <Select
            key={axis}
            showSearch
            allowClear
            size="small"
            aria-label={names[axis]}
            placeholder={names[axis]}
            value={axis in scope ? JSON.stringify(scope[axis]) : undefined}
            optionLabelProp="label"
            filterOption={false}
            style={{ minWidth: axis === "pn" ? 180 : 120, maxWidth: 360 }}
            options={items.map((item) => ({ value: JSON.stringify(item.key), label: label(item) }))}
            loading={open === axis && !response}
            notFoundContent={response?.error || (response ? "无匹配项" : "加载中…")}
            onSearch={search}
            onOpenChange={(visible) => {
              search("");
              setTerm("");
              setOpen(visible ? axis : null);
            }}
            labelRender={(picked) =>
              picked.label && picked.label !== picked.value
                ? picked.label
                : labels[axis] || (scope[axis] === null ? missing[axis] : scope[axis])
            }
            onChange={(value) =>
              onChange(
                axis,
                items.find((item) => JSON.stringify(item.key) === value),
              )
            }
            popupRender={(menu) => (
              <>
                {menu}
                {response?.data?.has_more && <small>显示前 30 项，输入名称继续查找</small>}
              </>
            )}
          />
        );
      })}
    </div>
  );
}
