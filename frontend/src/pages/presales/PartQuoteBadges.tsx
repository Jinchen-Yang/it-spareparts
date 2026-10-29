import { useEffect, useState } from "react";
import { Space, Tag } from "antd";
import { presales, type Offer } from "../../api/presales";
import PriceRefBadge from "../../components/pricing/PriceRefBadge";
import { isOfferActive, OfferBadge, useNow } from "./shared";

/** 供实时检索插槽使用：仅从受众过滤后的报价库读取角标。 */
export default function PartQuoteBadges({ partId }: { partId: number }) {
  const [offers, setOffers] = useState<Offer[]>([]);
  const now = useNow(offers.map((offer) => offer.valid_from));
  useEffect(() => {
    let disposed = false;
    setOffers([]);
    void presales.library([partId]).then((data) => { if (!disposed) setOffers(data[partId] ?? []); }).catch(() => {});
    return () => { disposed = true; };
  }, [partId]);
  const active = offers.filter((offer) => isOfferActive(offer, now));
  const lowest = active[0];
  return <Space wrap><PriceRefBadge partId={partId} />
    {lowest && <><OfferBadge offer={lowest} /><Tag>最低价 · {active.length} 位报价人</Tag></>}
  </Space>;
}
