import { useCallback, useEffect, useState } from "react";
import { App, Space, Tag, Typography } from "antd";
import type { Offer } from "../../api/presales";
import { moneyExact } from "../../utils/format";

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

export function isOfferActive(offer: Offer, now = Date.now()) {
  return !offer.original_withdrawn && offer.status === "published" && !offer.no_stock && offer.unit_price != null
    && !!offer.valid_from && offerDeadline(offer.valid_from) <= now
    && offerDeadline(offer.valid_until) > now;
}

export function taxLabel(value: boolean | null | undefined) {
  return value === true ? "含税" : value === false ? "不含税" : "未声明税基";
}

export function expiryLabel(days: number, now = Date.now()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date(now));
  const value = (type: string) => Number(parts.find((part) => part.type === type)?.value);
  const end = new Date(Date.UTC(value("year"), value("month") - 1, value("day") + days - 1));
  return `将于 ${end.getUTCMonth() + 1}月${end.getUTCDate()}日 23:59 失效（上海时间）`;
}

export function OfferBadge({ offer }: { offer: Offer }) {
  const now = useNow([offer.valid_from, offer.valid_until]);
  const active = isOfferActive(offer, now);
  const days = Math.max(0, Math.ceil((offerDeadline(offer.valid_until) - now) / 86_400_000));
  return <Space wrap size={4}><Tag color={active ? "green" : undefined} style={{ color: active ? undefined : "var(--mb-text-3)" }}>
    {active ? "有效报价" : "已失效"} {moneyExact(offer.unit_price == null ? null : Number(offer.unit_price))} · {taxLabel(offer.is_tax_inclusive)}
    {active && ` · 剩 ${days} 天`}{offer.reporter && ` · ${offer.reporter.name}`}
  </Tag><OfferUpdates offer={offer} /></Space>;
}

/** 更新提示只使用服务端已授权的 latest，不请求当前报价来补全。 */
export function OfferUpdates({ offer }: { offer: Offer }) {
  return <>
    {offer.latest && <Typography.Text type="secondary">该报价已更新为 {moneyExact(offer.latest.unit_price == null ? null : Number(offer.latest.unit_price))} · {taxLabel(offer.latest.is_tax_inclusive)}</Typography.Text>}
    {offer.original_withdrawn && <Tag color="warning">原报价已撤销</Tag>}
  </>;
}

export function flattenOffers(offers: Offer[]): Offer[] {
  return offers.flatMap((offer) => [offer, ...(offer.more ?? [])]);
}

export function usePresalesError() {
  const { message } = App.useApp();
  return useCallback(async (error: unknown, refresh?: () => void | Promise<unknown>) => {
    const response = (error as { response?: { status?: number; data?: { detail?: unknown } } })?.response;
    if (response?.status === 409) {
      message.warning("已被他人修改/领取");
      await refresh?.();
      return;
    }
    const detail = response?.data?.detail;
    const text = typeof detail === "string" ? detail
      : detail && typeof detail === "object" && "message" in detail && typeof detail.message === "string" ? detail.message
      : "操作失败，请重试";
    message.error(text);
  }, [message]);
}
