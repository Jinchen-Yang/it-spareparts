import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Empty, Space, Spin, Typography } from "antd";
import { useNavigate } from "react-router-dom";
import { presales, type Inquiry, type LibraryPart, type Offer } from "../../api/presales";
import LivePartSearch from "../../components/parts/LivePartSearch";
import PartThumb from "../../components/gallery/PartThumb";
import PriceRefBadge from "../../components/pricing/PriceRefBadge";
import { usePresalesError, isOfferActive, OfferBadge, useNow } from "./shared";
import PartQuoteBadges from "./PartQuoteBadges";

export default function LibraryPage() {
  const handlePresalesError = usePresalesError();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<LibraryPart[]>([]);
  const [sources, setSources] = useState<Record<number, Inquiry["kind"]>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [reload, setReload] = useState(0);
  const sourceCache = useRef<Record<number, Inquiry["kind"]>>({});
  const now = useNow(items.flatMap((item) => item.offers.flatMap((offer) => [offer.valid_from, offer.valid_until])));

  useEffect(() => {
    const controller = new AbortController();
    setItems([]);
    setSources({});
    setError(false);
    if (!query.trim()) { setLoading(false); return; }
    setLoading(true);
    const timer = setTimeout(() => {
      void presales.searchLibrary(query, controller.signal).then(async (data) => {
        if (controller.signal.aborted) return;
        setItems(data);
        setLoading(false);
        setSources({ ...sourceCache.current });
        const ids = [...new Set(data.flatMap((part) => part.offers.map((offer) => offer.inquiry_id)))].filter((id) => !sourceCache.current[id]);
        let index = 0;
        // 报价库没有来源类型：缓存真实单据类型，并限制补取并发，搜索变化即取消。
        await Promise.allSettled(Array.from({ length: Math.min(4, ids.length) }, async () => {
          while (index < ids.length && !controller.signal.aborted) {
            const id = ids[index++];
            try {
              const inquiry = await presales.getInquiry(id, controller.signal);
              if (!controller.signal.aborted) {
                sourceCache.current[inquiry.id] = inquiry.kind;
                setSources({ ...sourceCache.current });
              }
            } catch { /* 无权读取来源单时不推测来源类型，报价仍按库响应显示。 */ }
          }
        }));
      }).catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setLoading(false);
          setError(true);
          void handlePresalesError(error, () => setReload((value) => value + 1));
        }
      });
    }, 150);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [query, reload]);

  const reference = (part: LibraryPart, offer: Offer) => {
    if (!isOfferActive(offer)) return;
    navigate("/presales/inquiries/new", { state: { referenceOffer: offer, part } });
  };

  return <Space direction="vertical" size="large" style={{ width: "100%" }}>
    <Typography.Title level={3}>报价库</Typography.Title>
    <LivePartSearch inputValue={query} onInputChange={setQuery}
      onChange={(parts) => { if (parts[0]) setQuery(parts[0].pn_std); }}
      inputProps={{ placeholder: "搜索 PN、品牌、参数或描述", "aria-label": "报价库搜索" }}
      renderThumb={(part) => <PartThumb partId={part.part_id} pn={part.pn_std} />}
      renderBadges={(part) => <PartQuoteBadges partId={part.part_id} />} />
    {error && <Alert type="error" message="报价库加载失败" action={<Button onClick={() => setReload((value) => value + 1)}>重试</Button>} />}
    {loading && <Spin aria-label="报价库加载中" />}
    {!loading && !error && !items.some((part) => part.offers.length) && <Empty description={query.trim() ? "没有可见的有效报价" : "输入 PN 搜索有效报价"} />}
    {items.filter((part) => part.offers.length).map((part) => <Card key={part.part_id} title={<Space>
      <PartThumb partId={part.part_id} pn={part.pn_std} /><span>{part.pn_std}</span>
      {part.brand && <span>{part.brand}</span>}<PriceRefBadge partId={part.part_id} />
    </Space>}>
      {part.description && <Typography.Paragraph>{part.description}</Typography.Paragraph>}
      <Space direction="vertical" style={{ width: "100%" }}>
        {part.offers.map((offer) => {
          const active = isOfferActive(offer, now);
          return <div key={offer.id} aria-label={`报价 ${offer.id}`} aria-disabled={!active}
            style={{ width: "100%", padding: 12, border: "1px solid var(--mb-border-soft)", borderRadius: 6,
              background: active ? "var(--mb-surface)" : "var(--mb-inset)", color: active ? undefined : "var(--mb-text-3)" }}>
            <Space wrap><OfferBadge offer={offer} />
              {"stock_location" in offer && <span>所在地：{offer.stock_location}</span>}
              {"qty_available" in offer && <span>可供数量：{offer.qty_available}</span>}
              {"lead_time" in offer && <span>交期：{offer.lead_time}</span>}
              {sources[offer.inquiry_id] && <span>来源：{sources[offer.inquiry_id] === "proactive_offer" ? "主动报价" : "询价回复"}</span>}
              <Button disabled={!active} onClick={() => reference(part, offer)}>用这条报价建询价单</Button>
              <Button type="link" onClick={() => navigate(`/presales/inquiries/${offer.inquiry_id}`)}>查看来源单</Button>
            </Space>
          </div>;
        })}
      </Space>
    </Card>)}
  </Space>;
}
