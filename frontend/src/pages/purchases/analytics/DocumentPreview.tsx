import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { Alert, Empty, Popover, Spin } from "antd";
import { api } from "../../../api";
import { toAnalyticsParams, type PurchaseAnalyticsFilters } from "../../../api/purchaseAnalytics";
import { makeReadCache, useDashboardRead } from "./useDashboardRead";
import { sessionStale } from "../../../utils/session";

type Scope = Record<string, string | null>;
interface Preview {
  orders: number;
  products: { pn: string | null; description: string | null; qty: number | null; unit: string | null }[];
  categories: { category: string | null; qty: number | null; unit: string | null }[];
  more_products: boolean;
  more_categories: boolean;
}
export const makePreviewCache = () => makeReadCache<Preview>(40);
export const PreviewCacheContext = createContext<ReturnType<typeof makePreviewCache> | null>(null);

export default function DocumentPreview({
  kind,
  number,
  orderId,
  filters,
  scope = {},
  ownerToken,
  onOwnerStale,
  children,
}: {
  kind: "purchase" | "sale";
  number: string;
  orderId?: number;
  filters: PurchaseAnalyticsFilters;
  scope?: Scope;
  ownerToken: string | null;
  onOwnerStale: () => void;
  children?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const shared = useContext(PreviewCacheContext);
  const local = useMemo(makePreviewCache, []);
  const params = toAnalyticsParams(filters, {
    scope: JSON.stringify(scope),
    ...(kind === "sale" ? { sales_no: number } : { order_id: orderId ?? 0 }),
  });
  const key = params.toString();
  const snapshot = useDashboardRead(
    open ? key : null,
    () =>
      (shared ?? local).get(
        key,
        async () => (await api.get<Preview>("/purchases/analytics/document-preview", { params })).data,
      ),
    ownerToken,
    onOwnerStale,
  );
  const data = snapshot?.data;
  const qty = (value: number | null, unit: string | null) =>
    unit ? `${value == null ? "数量未填" : value.toLocaleString("zh-CN", { maximumFractionDigits: 5 })} ${unit}` : "单位未填，数量无法合计";
  return (
    <Popover
      trigger={["hover", "focus", "click"]}
      open={open}
      onOpenChange={(next) => {
        if (sessionStale(ownerToken)) onOwnerStale();
        else setOpen(next);
      }}
      mouseEnterDelay={0.25}
      title={kind === "sale" ? "当前范围内的关联采购" : "当前范围内的采购货品"}
      content={
        <div style={{ maxWidth: 390, maxHeight: 335, overflow: "auto" }}>
          {snapshot?.error ? (
            <Alert type="error" message={snapshot.error} />
          ) : !data ? (
            <Spin size="small" />
          ) : !data.products.length ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的可见采购行" />
          ) : (
            <>
              <small>{data.orders} 张采购单 · 按 PN 和单位汇总</small>
              {data.products.map((p, i) => (
                <div key={i} style={{ marginBlock: 5 }}>
                  <b>{p.pn || "型号未填"}</b>　{qty(p.qty, p.unit)}
                  <div>{p.description || "暂无描述"}</div>
                </div>
              ))}
              {data.more_products && <small>按成交行数列前 8 项</small>}
              <div style={{ borderTop: "1px solid var(--mb-border)", paddingTop: 5 }}>
                {data.categories.map((c, i) => (
                  <div key={i}>
                    {c.category || "未分类"}　{qty(c.qty, c.unit)}
                  </div>
                ))}
                {data.more_categories && <small>仅列前 8 个品类与单位组合</small>}
              </div>
            </>
          )}
        </div>
      }
    >
      <span
        tabIndex={0}
        aria-label={`预览${number}货品`}
        style={{ cursor: "help", borderBottom: "1px dotted currentColor" }}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => e.stopPropagation()}
      >
        {children ?? number}
      </span>
    </Popover>
  );
}
