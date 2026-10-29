import type { Inquiry, InquiryLine, Offer } from "../../api/presales";
import { moneyExact } from "../../utils/format";
import { formatBusinessDateTime } from "../../utils/date";
import { taxLabel } from "./shared";

/** 每行对销售的答复：有效价优先，其次不报，否则待报价（D-0046 第 7 条，后端读模型上线前由前端按详情推导）。 */
export type LineAnswer =
  | { state: "quoted"; offer: Offer; reporter: string; at: string | null }
  | { state: "skip"; reason: string; reporter: string }
  | { state: "pending" };

function usable(offer: Offer) {
  return offer.status === "published" && !offer.no_stock && !offer.expired && !offer.original_withdrawn && offer.unit_price != null;
}

export function lineAnswer(inquiry: Inquiry, line: InquiryLine): LineAnswer {
  const responses = (inquiry.line_responses ?? []).filter((row) => row.line_id === line.id && !row.is_draft);
  const offers = [
    ...(line.ref_offer ? [line.ref_offer] : []),
    ...(inquiry.offers ?? []).filter((offer) => offer.line_id === line.id),
    ...responses.flatMap((row) => row.mode === "reuse" && row.offer ? [row.offer] : []),
  ].filter(usable);
  if (offers.length) {
    const offer = offers.reduce((latest, item) => Date.parse(item.valid_from ?? "") > Date.parse(latest.valid_from ?? "") ? item : latest);
    return { state: "quoted", offer, reporter: offer.reporter?.name ?? "采购", at: offer.valid_from };
  }
  const skip = responses.find((row) => row.mode === "skip");
  if (skip) return { state: "skip", reason: skip.reason || "无货源", reporter: skip.reporter_label ?? "采购" };
  const noStock = (inquiry.offers ?? []).find((offer) => offer.line_id === line.id && offer.status === "published" && offer.no_stock);
  if (noStock) return { state: "skip", reason: noStock.notes || "无货", reporter: noStock.reporter?.name ?? "采购" };
  return { state: "pending" };
}

export function priceText(offer: Pick<Offer, "unit_price" | "is_tax_inclusive">) {
  return `${moneyExact(offer.unit_price == null ? null : Number(offer.unit_price))} ${taxLabel(offer.is_tax_inclusive)}`;
}

/** D-0047：价格性质；存量与未填视为售价。 */
export function basisLabel(offer: Pick<Offer, "price_basis">) {
  return offer.price_basis === "cost" ? "成本价" : "售价";
}

/** 一句可以直接转述给客户的话；价格性质是内部标注，不写进这句话。 */
export function answerSentence(line: InquiryLine, answer: LineAnswer) {
  if (answer.state === "quoted") return `${line.pn_raw} ${priceText(answer.offer)}，${answer.reporter} ${formatBusinessDateTime(answer.at)} 报价`;
  if (answer.state === "skip") return `${line.pn_raw} 不报：${answer.reason}`;
  return `${line.pn_raw} 待报价`;
}

export function requiredLines(inquiry: Inquiry) {
  return (inquiry.lines ?? []).filter((line) => line.status !== "deleted");
}
