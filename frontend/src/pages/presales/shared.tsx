import { useEffect, useState } from "react";
import { Space, Tag, Typography } from "antd";
import type { Offer } from "../../api/presales";
import { moneyExact } from "../../utils/format";
import { useApiError } from "../../components/ApiErrorFeedback";
import { formatBusinessDateTime } from "../../utils/date";

/** 服务端截止可带微秒；浏览器以毫秒为单位，向上对齐才不会提前失效。 */
export function offerDeadline(value: string | null | undefined): number {
  if (!value) return NaN;
  const millis = Date.parse(value);
  const fraction = value.match(/\.(\d+)(?:Z|[+-]\d{2}:?\d{2})$/)?.[1];
  return millis + (fraction && /[1-9]/.test(fraction.slice(3)) ? 1 : 0);
}

export function useNow(deadlines: (string | null | undefined)[] = []) {
  const [now, setNow] = useState(Date.now);
  const key = deadlines.join("|");
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const tick = () => {
      const current = Date.now();
      setNow(current);
      const future = key.split("|").map(offerDeadline).filter((at) => at > current);
      // 切回后台标签页后重新核时，边界时刻单独唤醒。
      timer = setTimeout(tick, Math.max(1, Math.min(1000, ...future.map((at) => at - current))));
    };
    tick();
    const visible = () => { clearTimeout(timer); tick(); };
    document.addEventListener("visibilitychange", visible);
    window.addEventListener("focus", visible);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", visible);
      window.removeEventListener("focus", visible);
    };
  }, [key]);
  return now;
}

/** D-0046：报价不设有效期，直到更新、撤回、标无货或被取代（服务端 expired）前一直有效。 */
export function isOfferActive(offer: Offer, now = Date.now()) {
  return !offer.original_withdrawn && !offer.expired && offer.status === "published" && !offer.no_stock
    && offer.unit_price != null && !!offer.valid_from && offerDeadline(offer.valid_from) <= now;
}

export function taxLabel(value: boolean | null | undefined) {
  return value === true ? "含税" : value === false ? "不含税" : "未声明税基";
}

/** D-0047：价格性质不受成本数据权限隐藏；存量未填视为售价。 */
export function basisLabel(offer: Pick<Offer, "price_basis">) {
  return offer.price_basis === "cost" ? "成本价" : "售价";
}

export function PriceBasisTag({ offer }: { offer: Pick<Offer, "price_basis"> }) {
  return <Tag color={offer.price_basis === "cost" ? "purple" : undefined}>{basisLabel(offer)}</Tag>;
}

export function OfferBadge({ offer }: { offer: Offer }) {
  const now = useNow([offer.valid_from]);
  const active = isOfferActive(offer, now);
  return <Space wrap size={4}><Tag color={active ? "green" : undefined} style={{ color: active ? undefined : "var(--mb-text-3)" }}>
    {active ? "有效报价" : "已失效"} {moneyExact(offer.unit_price == null ? null : Number(offer.unit_price))} · {taxLabel(offer.is_tax_inclusive)}
    {offer.reporter && ` · ${offer.reporter.name}`}
  </Tag><PriceBasisTag offer={offer} /><OfferUpdates offer={offer} /></Space>;
}

/** 更新提示只使用服务端已授权的 latest，不请求当前报价来补全。 */
export function OfferUpdates({ offer }: { offer: Offer }) {
  return <>
    {offer.latest && <><Typography.Text type="secondary">该报价已更新为 {moneyExact(offer.latest.unit_price == null ? null : Number(offer.latest.unit_price))} · {taxLabel(offer.latest.is_tax_inclusive)}</Typography.Text>
      {offer.latest.price_basis && <PriceBasisTag offer={offer.latest} />}</>}
    {offer.original_withdrawn && <Tag color="warning">原报价已撤销</Tag>}
  </>;
}

export function flattenOffers(offers: Offer[]): Offer[] {
  return offers.flatMap((offer) => [offer, ...(offer.more ?? [])]);
}

/** 主报价不可用（撤回、无货、被取代）时，仍有效的其他报价继续作为该报价人的候选。 */
export function activeOfferGroups(offers: Offer[], now: number): Offer[] {
  return offers.flatMap((group) => {
    const active = flattenOffers([group]).filter((offer) => isOfferActive(offer, now));
    if (!active.length) return [];
    const latest = isOfferActive(group, now) ? group : active.reduce((current, offer) => {
      const delta = Date.parse(offer.valid_from!) - Date.parse(current.valid_from!);
      return delta > 0 || (delta === 0 && offer.id > current.id) ? offer : current;
    });
    return [{ ...latest, more: active.filter((offer) => offer.id !== latest.id) }];
  });
}

export function usePresalesError() {
  return useApiError();
}

/** 型号最新一条有效报价（含同报价人的其他货源），用于检索候选的小标签。 */
export function latestActiveOffer(groups: Offer[], now = Date.now()): Offer | undefined {
  return flattenOffers(groups).filter((offer) => isOfferActive(offer, now))
    .reduce<Offer | undefined>((latest, offer) => !latest || Date.parse(offer.valid_from!) > Date.parse(latest.valid_from!) ? offer : latest, undefined);
}

export function LatestPriceTag({ offer }: { offer?: Offer }) {
  if (!offer) return null;
  return <Tag color="green" className="presales-price-tag" title={offer.reporter ? `${offer.reporter.name} 报价` : undefined}>
    最新 {moneyExact(offer.unit_price == null ? null : Number(offer.unit_price))} {taxLabel(offer.is_tax_inclusive)} · {basisLabel(offer)}
    {offer.valid_from && ` · ${formatBusinessDateTime(offer.valid_from).slice(5, 10)}`}
  </Tag>;
}
