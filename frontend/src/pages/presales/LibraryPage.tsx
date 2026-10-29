import { useEffect, useRef, useState } from "react";
import { Alert, Button, Descriptions, Drawer, Space, Table, Tag } from "antd";
import { useNavigate } from "react-router-dom";
import { presales, type Inquiry, type LibraryPart, type Offer } from "../../api/presales";
import LivePartSearch from "../../components/parts/LivePartSearch";
import PartThumb from "../../components/gallery/PartThumb";
import PriceRefBadge from "../../components/pricing/PriceRefBadge";
import { usePresalesError, isOfferActive, OfferBadge, useNow, flattenOffers, activeOfferGroups } from "./shared";
import PageHeader from "../../components/PageHeader";
import { formatBusinessDateTime } from "../../utils/date";
import "./presales.css";

interface LibraryRow { part: LibraryPart; offer: Offer }
import PartQuoteBadges from "./PartQuoteBadges";

export default function LibraryPage() {
  const handlePresalesError = usePresalesError();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<LibraryRow>();
  const [items, setItems] = useState<LibraryPart[]>([]);
  const [sources, setSources] = useState<Record<number, Inquiry["kind"]>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [reload, setReload] = useState(0);
  const sourceCache = useRef<Record<number, Inquiry["kind"]>>({});
  const now = useNow(items.flatMap((item) => flattenOffers(item.offers).map((offer) => offer.valid_from)));

  useEffect(() => {
    const controller = new AbortController();
    setItems([]);
    setSelected(undefined);
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
        const ids = [...new Set(data.flatMap((part) => flattenOffers(part.offers).map((offer) => offer.inquiry_id)))].filter((id): id is number => id != null && !sourceCache.current[id]);
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

  const rows = items.flatMap((part) => part.offers.map((offer) => ({ part, offer: activeOfferGroups([offer], now)[0] ?? offer })));
  const detailOffers = selected ? flattenOffers([selected.offer]) : [];
  return <section className="presales-page">
    <PageHeader title="报价库" subtitle="按型号查询有效报价，引用报价创建询价单" />
    <div className="presales-panel">
      <div className="presales-toolbar"><LivePartSearch inputValue={query} onInputChange={setQuery} onChange={(parts) => { if (parts[0]) setQuery(parts[0].pn_std); }}
        inputProps={{ placeholder: "搜索 PN、品牌、参数或描述", "aria-label": "报价库搜索" }} renderThumb={(part) => <PartThumb partId={part.part_id} pn={part.pn_std} size={24} />} renderBadges={(part) => <PartQuoteBadges partId={part.part_id} />} /></div>
      {error && <Alert type="error" message="报价库加载失败" action={<Button onClick={() => setReload((value) => value + 1)}>重试</Button>} />}
      <Table aria-label="有效报价库" rowKey={({ offer }) => offer.id} dataSource={rows} loading={loading} tableLayout="fixed" pagination={{ defaultPageSize: 20 }} columns={[
        { title: "PN / 描述", key: "pn", sorter: (a, b) => a.part.pn_std.localeCompare(b.part.pn_std), render: (_, { part }) => <><Space><PartThumb partId={part.part_id} pn={part.pn_std} size={24} />{part.pn_std}</Space><div className="presales-muted">{part.description}</div></> },
        { title: "有效报价 / 税口径", key: "price", defaultSortOrder: "ascend", sorter: (a, b) => Number(a.offer.unit_price) - Number(b.offer.unit_price), render: (_, { offer }) => <div aria-label={`报价 ${offer.id}`} aria-disabled={!isOfferActive(offer, now)}><OfferBadge offer={offer} />{offer.reporter && <div className="presales-muted">{offer.reporter.name}</div>}</div> },
        { title: "供货 / 来源", key: "source", width: 180, render: (_, { offer }) => <>{"stock_location" in offer && <div>所在地：{offer.stock_location || ""}</div>}{offer.inquiry_id == null ? <div>来源：主动报价</div> : sources[offer.inquiry_id] && <div>来源：{sources[offer.inquiry_id] === "proactive_offer" ? "主动报价" : "询价回复"}</div>}</> },
        { title: "报价时间", key: "time", sorter: (a, b) => (a.offer.valid_from ?? "").localeCompare(b.offer.valid_from ?? ""), width: 180, render: (_, { offer }) => <>{offer.valid_from && <div>{formatBusinessDateTime(offer.valid_from)}</div>}{!isOfferActive(offer, now) && <Tag>已失效</Tag>}</> },
        { title: "操作", key: "actions", width: 190, render: (_, item) => <Space direction="vertical"><Button type="link" disabled={!isOfferActive(item.offer, now)} onClick={() => reference(item.part, item.offer)}>用这条报价建询价单</Button>
          {item.offer.inquiry_id != null && <Button type="link" onClick={() => navigate(`/presales/inquiries/${item.offer.inquiry_id}`)}>查看来源单</Button>}<Button type="link" onClick={() => setSelected(item)}>报价详情{item.offer.more?.length ? `（更多 ${item.offer.more.length} 条）` : ""}</Button></Space> },
      ]} locale={{ emptyText: query.trim() ? "没有可见的有效报价" : "输入 PN 搜索有效报价" }} />
      <p className="presales-muted">共 {rows.length} 条主报价 · 价格保留原税口径 · 报价一直有效，直到报价人更新或撤回</p>
    </div>
    <Drawer title={selected ? `${selected.part.pn_std} · 报价详情` : "报价详情"} open={!!selected} onClose={() => setSelected(undefined)} width={680} destroyOnHidden>
      {selected && <><p>{selected.part.description}</p><PriceRefBadge partId={selected.part.part_id} />
        <Table aria-label={`${selected.part.pn_std} 更多报价`} rowKey="id" dataSource={detailOffers} tableLayout="fixed" pagination={{ pageSize: 5 }} columns={[
          { title: "报价", key: "offer", defaultSortOrder: "ascend", sorter: (a, b) => Number(a.unit_price) - Number(b.unit_price), render: (_, offer) => <div aria-label={`报价 ${offer.id}`}><OfferBadge offer={offer} />{offer.reporter?.name}<Button type="link" disabled={!isOfferActive(offer, now)} onClick={() => reference(selected.part, offer)}>用这条报价建询价单</Button></div> },
          { title: "详情", key: "details", render: (_, offer) => <Descriptions column={1} size="small" items={[
            ...("qty_available" in offer ? [{ key: "qty", label: "可供数量", children: offer.qty_available }] : []),
            ...("lead_time" in offer ? [{ key: "lead", label: "交期", children: offer.lead_time }] : []),
            ...("notes" in offer ? [{ key: "notes", label: "采购备注", children: offer.notes }] : []),
          ]} /> },
        ]} />
      </>}
    </Drawer>
  </section>;
}
