import { useEffect, useRef, useState, type Key } from "react";
import { Button } from "antd";
import { useNavigate } from "react-router-dom";
import { presales, type Inquiry, type LibraryPart, type Offer } from "../../api/presales";
import LivePartSearch from "../../components/parts/LivePartSearch";
import PartThumb from "../../components/gallery/PartThumb";
import PriceRefBadge from "../../components/pricing/PriceRefBadge";
import { usePresalesError, isOfferActive, OfferBadge, useNow, flattenOffers, offerDeadline, activeOfferGroups } from "./shared";
import DataGrid, { type DataGridColumn } from "../../components/datagrid/DataGrid";
import "./presales.css";

interface LibraryRow { part: LibraryPart; offer: Offer }
import PartQuoteBadges from "./PartQuoteBadges";

export default function LibraryPage() {
  const handlePresalesError = usePresalesError();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<Key[]>([]);
  const [items, setItems] = useState<LibraryPart[]>([]);
  const [sources, setSources] = useState<Record<number, Inquiry["kind"]>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [reload, setReload] = useState(0);
  const sourceCache = useRef<Record<number, Inquiry["kind"]>>({});
  const now = useNow(items.flatMap((item) => flattenOffers(item.offers).flatMap((offer) => [offer.valid_from, offer.valid_until])));

  useEffect(() => {
    const controller = new AbortController();
    setItems([]);
    setExpanded([]);
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
        const ids = [...new Set(data.flatMap((part) => flattenOffers(part.offers).map((offer) => offer.inquiry_id)))].filter((id) => !sourceCache.current[id]);
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

  const columns: DataGridColumn<LibraryRow>[] = [
    { key: "pn", title: "PN", width: 190, type: "code", identity: true, value: ({ part }) => part.pn_std,
      render: ({ part }) => <span className="presales-inline"><PartThumb partId={part.part_id} pn={part.pn_std} size={24} /><span className="presales-mono">{part.pn_std}</span></span> },
    { key: "description", title: "描述", width: 220, type: "zh", value: ({ part }) => part.description },
    { key: "price", title: "有效报价 / 税口径", width: 310, type: "money", value: ({ offer }) => offer.unit_price,
      render: ({ offer }) => <div className="presales-library-row" aria-label={`报价 ${offer.id}`} aria-disabled={!isOfferActive(offer, now)}><OfferBadge offer={offer} /></div> },
    { key: "reporter", title: "报价人", width: 110, type: "zh", value: ({ offer }) => offer.reporter?.name },
    { key: "location", title: "货源", width: 140, type: "zh", value: ({ offer }) => offer.stock_location,
      render: ({ offer }) => "stock_location" in offer ? `所在地：${offer.stock_location || ""}` : null },
    { key: "days", title: "剩余天数", width: 90, type: "number", value: ({ offer }) => Math.max(0, Math.ceil((offerDeadline(offer.valid_until) - now) / 86400000)),
      render: ({ offer }) => <span className={`presales-pill ${isOfferActive(offer, now) ? "presales-pill-success" : "presales-pill-warning"}`}>
        {isOfferActive(offer, now) ? `${Math.max(1, Math.ceil((offerDeadline(offer.valid_until) - now) / 86400000))} 天` : "已失效"}</span> },
    { key: "time", title: "报价时间", width: 165, type: "time", value: ({ offer }) => offer.valid_from,
      render: ({ offer }) => offer.valid_from ? new Date(offer.valid_from).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "" },
    { key: "source", title: "来源", width: 130, type: "zh", value: ({ offer }) => sources[offer.inquiry_id],
      render: ({ offer }) => sources[offer.inquiry_id] ? `来源：${sources[offer.inquiry_id] === "proactive_offer" ? "主动报价" : "询价回复"}` : null },
    { key: "actions", title: "操作", width: 330, render: ({ part, offer }) => <span className="presales-inline">
      <Button size="small" disabled={!isOfferActive(offer, now)} onClick={() => reference(part, offer)}>用这条报价建询价单</Button>
      <Button size="small" type="link" onClick={() => navigate(`/presales/inquiries/${offer.inquiry_id}`)}>查看来源单</Button>
      {!!offer.more?.length && <button className="presales-cell-link" aria-expanded={expanded.includes(offer.id)} onClick={() => setExpanded((ids) => ids.includes(offer.id) ? ids.filter((id) => id !== offer.id) : [...ids, offer.id])}>
        {expanded.includes(offer.id) ? "收起更多" : "展开更多"}（{offer.more.length} 条）</button>}
    </span> },
  ];
  // 页面中已到期的整组继续置灰；仍有有效报价的组则显示最新有效项。
  const rows = items.flatMap((part) => part.offers.map((offer) => ({ part, offer: activeOfferGroups([offer], now)[0] ?? offer })));
  return <section className="presales-page">
    <div className="presales-heading"><h1>报价库</h1><span className="presales-filter-note">每个 PN 每位报价人显示最新一条，其余报价展开查看</span></div>
    <LivePartSearch inputValue={query} onInputChange={setQuery}
      onChange={(parts) => { if (parts[0]) setQuery(parts[0].pn_std); }}
      inputProps={{ placeholder: "搜索 PN、品牌、参数或描述", "aria-label": "报价库搜索", size: "small" }}
      renderThumb={(part) => <PartThumb partId={part.part_id} pn={part.pn_std} size={24} />}
      renderBadges={(part) => <PartQuoteBadges partId={part.part_id} />} />
    <DataGrid id="presales-library" ariaLabel="有效报价库" rows={rows} rowKey={({ offer }) => offer.id} columns={columns}
      defaultSort={{ key: "price", dir: "asc" }} loading={loading} error={error ? "报价库加载失败" : undefined} onRetry={() => setReload((v) => v + 1)}
      emptyText={query.trim() ? "没有可见的有效报价" : "输入 PN 搜索有效报价"} expandedRowKeys={expanded} onExpandedRowKeysChange={setExpanded}
      renderExpanded={({ part, offer }) => <>
        <div className="presales-details"><strong>{part.pn_std}</strong><span>{part.description}</span><PriceRefBadge partId={part.part_id} />
          {"qty_available" in offer && <span>可供数量：{offer.qty_available}</span>}{"lead_time" in offer && <span>交期：{offer.lead_time}</span>}
        </div>
        {!!offer.more?.length && <DataGrid id="presales-library-more" ariaLabel={`${part.pn_std} 更多报价`} rows={offer.more.map((other) => ({ part, offer: other }))}
          columns={columns} rowKey={({ offer: other }) => other.id} defaultSort={{ key: "price", dir: "asc" }}
          renderExpanded={({ offer: other }) => <div className="presales-details">
            {"qty_available" in other && <span>可供数量：{other.qty_available}</span>}
            {"lead_time" in other && <span>交期：{other.lead_time}</span>}
            {"notes" in other && <span>{other.notes}</span>}
          </div>} />}
      </>} />
    <span className="presales-filter-note">共 {rows.length} 条主报价 · 价格保留原税口径 · 有效期到点自动失效</span>
  </section>;
}
