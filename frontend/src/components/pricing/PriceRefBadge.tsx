/**
 * M3 价格参考角标：默认展示「售 ¥x」，有采购价权限时再展示「采 ¥y」。
 * 悬停 Popover 看双税口径、样本/单数、窗口、历史询价参考与固定免责文案。
 * 无权与无样本是两种不同状态，必须分开显示。
 */
import { useEffect, useRef, useState } from "react";
import { Divider, Popover, Tag } from "antd";
import { fetchPriceRefs } from "../../api/priceRefs";
import type { PriceRef, PriceRefSide } from "../../api/priceRefs";
import { moneyExact } from "../../utils/format";

export interface PriceRefBadgeProps {
  /** 必填之一：只有 partId 时由组件自行批量拉取。 */
  partId?: number;
  /** 必填之一：父级已取回的数据；传入后组件不再发请求。 */
  data?: PriceRef | null;
}

type BasisLabel = "含税" | "不含税";

function primaryAmount(side: PriceRefSide | null): { value: number; basis: BasisLabel } | null {
  if (!side) return null;
  if (side.avg_inc_tax != null) return { value: side.avg_inc_tax, basis: "含税" };
  if (side.avg_ex_tax != null) return { value: side.avg_ex_tax, basis: "不含税" };
  return null;
}

function SideDetail({ title, side }: { title: string; side: PriceRefSide }) {
  return (
    <div style={detailBlockStyle}>
      <div style={detailTitleStyle}>{title}</div>
      <div>含税 {moneyExact(side.avg_inc_tax)}　不含税 {moneyExact(side.avg_ex_tax)}</div>
      <div style={mutedStyle}>
        样本 {side.sample_count} 行 / {side.order_count} 单
      </div>
      <div style={mutedStyle}>
        窗口 {side.oldest_date || "—"} ~ {side.latest_date || "—"}
      </div>
    </div>
  );
}

function Detail({ data }: { data: PriceRef }) {
  const inquiry = data.inquiry_ref;
  return (
    <div style={popoverStyle} aria-label="价格参考明细">
      {data.sale ? <SideDetail title="销售均价" side={data.sale} /> : (
        <div style={detailBlockStyle}>
          <div style={detailTitleStyle}>销售均价</div>
          <div style={mutedStyle}>暂无数据</div>
        </div>
      )}
      <Divider style={{ margin: "8px 0" }} />
      <div style={detailBlockStyle}>
        <div style={detailTitleStyle}>采购均价</div>
        {data.purchase_restricted ? (
          <div style={mutedStyle}>采购价：无权查看</div>
        ) : data.purchase ? (
          <>
            <div>含税 {moneyExact(data.purchase.avg_inc_tax)}　不含税 {moneyExact(data.purchase.avg_ex_tax)}</div>
            <div style={mutedStyle}>样本 {data.purchase.sample_count} 行 / {data.purchase.order_count} 单</div>
            <div style={mutedStyle}>窗口 {data.purchase.oldest_date || "—"} ~ {data.purchase.latest_date || "—"}</div>
          </>
        ) : <div style={mutedStyle}>暂无数据</div>}
      </div>
      <Divider style={{ margin: "8px 0" }} />
      <div style={detailBlockStyle}>
        <div style={detailTitleStyle}>历史询价参考</div>
        {inquiry.count > 0 ? (
          <>
            <div>
              {moneyExact(inquiry.min_money)} ~ {moneyExact(inquiry.max_money)}
              <span style={mutedStyle}>（共 {inquiry.count} 条）</span>
            </div>
            <div style={mutedStyle}>最近 {moneyExact(inquiry.last_money)}</div>
          </>
        ) : <div style={mutedStyle}>暂无数据</div>}
      </div>
      <div style={disclaimerStyle}>参考价，不等于定价</div>
    </div>
  );
}

export default function PriceRefBadge({ partId, data: dataProp }: PriceRefBadgeProps) {
  const provided = dataProp !== undefined;
  const [data, setData] = useState<PriceRef | null | undefined>(dataProp);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const requestSeq = useRef(0);

  useEffect(() => {
    if (provided) {
      requestSeq.current += 1;
      setData(dataProp);
      setLoading(false);
      setFailed(false);
      return;
    }
    if (!partId) {
      requestSeq.current += 1;
      setData(undefined);
      setLoading(false);
      setFailed(false);
      return;
    }
    const current = ++requestSeq.current;
    setLoading(true);
    setFailed(false);
    fetchPriceRefs([partId])
      .then((values) => {
        if (current !== requestSeq.current) return;
        setData(values[partId] ?? null);
      })
      .catch(() => {
        if (current !== requestSeq.current) return;
        setFailed(true);
      })
      .finally(() => {
        if (current === requestSeq.current) setLoading(false);
      });
    return () => { requestSeq.current += 1; };
  }, [partId, dataProp, provided]);

  if (!provided && !partId) return null;
  if (loading) return <Tag>参考价加载中…</Tag>;
  if (failed) return <Tag color="orange">参考价加载失败</Tag>;
  if (data === undefined) return null;

  const sale = primaryAmount(data?.sale ?? null);
  const purchase = primaryAmount(data?.purchase ?? null);
  const restricted = data?.purchase_restricted === true;

  return (
    <Popover
      title="价格参考"
      content={data ? <Detail data={data} /> : <div style={mutedStyle}>暂无数据</div>}
      trigger="hover"
    >
      <span role="status" aria-label="价格参考角标" style={badgeRowStyle}>
        {sale ? (
          <Tag color="blue" style={tagStyle}>
            售 {moneyExact(sale.value)} <span style={basisStyle}>{sale.basis}</span>
          </Tag>
        ) : <Tag style={tagStyle}>售 暂无数据</Tag>}
        {restricted ? (
          <Tag style={tagStyle}>采购价：无权查看</Tag>
        ) : purchase ? (
          <Tag color="geekblue" style={tagStyle}>
            采 {moneyExact(purchase.value)} <span style={basisStyle}>{purchase.basis}</span>
          </Tag>
        ) : <Tag style={tagStyle}>采购价：暂无数据</Tag>}
      </span>
    </Popover>
  );
}

const badgeRowStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 4,
  flexWrap: "wrap",
};
const tagStyle: React.CSSProperties = { marginInlineEnd: 0, cursor: "default" };
const basisStyle: React.CSSProperties = { fontSize: 11, opacity: 0.72, marginInlineStart: 2 };
const popoverStyle: React.CSSProperties = { width: 300, fontSize: 13 };
const detailBlockStyle: React.CSSProperties = { lineHeight: 1.8 };
const detailTitleStyle: React.CSSProperties = { fontWeight: 600 };
const mutedStyle: React.CSSProperties = { color: "var(--mb-text-3)", fontSize: 12.5 };
const disclaimerStyle: React.CSSProperties = {
  marginTop: 10,
  paddingTop: 8,
  borderTop: "1px dashed var(--mb-border)",
  color: "var(--mb-text-3)",
  fontSize: 12,
};
