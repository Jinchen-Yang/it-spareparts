import { useState, type ReactNode } from "react";
import { Button, Space } from "antd";
import type { Offer } from "../../api/presales";

/** 分组和受众由服务端决定；展开仅展示同一报价人的其余可见报价。 */
export default function OfferGroups({ offers, renderOffer }: { offers: Offer[]; renderOffer: (offer: Offer) => ReactNode }) {
  const [expanded, setExpanded] = useState<number[]>([]);
  return <Space direction="vertical" style={{ width: "100%" }}>{offers.map((offer) => <div key={offer.id} style={{ width: "100%" }}>
    {renderOffer(offer)}
    {!!offer.more?.length && <>
      <Button type="link" size="small" aria-expanded={expanded.includes(offer.id)}
        onClick={() => setExpanded((ids) => ids.includes(offer.id) ? ids.filter((id) => id !== offer.id) : [...ids, offer.id])}>
        {expanded.includes(offer.id) ? "收起更多" : "展开更多"}（{offer.more.length} 条）
      </Button>
      {expanded.includes(offer.id) && <Space direction="vertical" style={{ width: "100%", paddingLeft: 16 }}>
        {offer.more.map((more) => <div key={more.id} style={{ width: "100%" }}>{renderOffer(more)}</div>)}
      </Space>}
    </>}
  </div>)}</Space>;
}
