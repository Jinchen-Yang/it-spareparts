import type { Inquiry, InquiryLine, Offer } from "../../api/presales";
import { moneyExact } from "../../utils/format";
import { formatBusinessDateTime } from "../../utils/date";
import { taxLabel } from "./shared";
export { basisLabel } from "./shared";

/** D-0048：服务端统一当前答复；历史快照只用于兼容读取，不能把旧价格冒充有效价格。 */
export type LineAnswer =
  | { state: "quoted"; offer: Offer; reporter: string; at: string | null }
  | { state: "skip"; reason: string; reporter: string }
  | { state: "pending" }
  | { state: "unavailable"; reason: string };

function usable(offer: Offer) {
  return offer.status === "published" && !offer.no_stock && !offer.expired && !offer.original_withdrawn && offer.unit_price != null;
}

export function lineAnswer(inquiry: Inquiry, line: InquiryLine): LineAnswer {
  const current = inquiry.line_answers?.find((answer) => answer.line_id === line.id);
  if (current) {
    if (current.state === "quoted" && current.offer && usable(current.offer)) return {
      state: "quoted", offer: current.offer, reporter: current.reporter ?? current.offer.reporter?.name ?? "采购", at: current.offer.valid_from,
    };
    if (current.state === "skip") return { state: "skip", reason: current.reason || "无货源", reporter: current.reporter ?? "采购" };
    if (current.responded || current.state === "unavailable") return { state: "unavailable", reason: current.reason || "已回复，当前无有效报价" };
    return { state: "pending" };
  }
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
  if (responses.length || (inquiry.offers ?? []).some((offer) => offer.line_id === line.id && offer.status !== "draft")) {
    return { state: "unavailable", reason: "已回复，当前无有效报价" };
  }
  return { state: "pending" };
}

export function priceText(offer: Pick<Offer, "unit_price" | "is_tax_inclusive">) {
  return `${moneyExact(offer.unit_price == null ? null : Number(offer.unit_price))} ${taxLabel(offer.is_tax_inclusive)}`;
}

/** 编辑只使用真实草稿或当前授权报价；历史引用快照不能提供编辑版本和字段。 */
export function ownedOfferForEdit(inquiry: Inquiry, line: InquiryLine, actor?: number | null): Offer | undefined {
  if (actor == null) return undefined;
  const own = (offer: Offer) => offer.reporter?.id === actor;
  const references = new Set([line.ref_offer_id, ...(line.linked_offer_ids ?? []), ...(inquiry.line_responses ?? [])
    .filter((row) => row.line_id === line.id && row.created_by === actor).map((row) => row.ref_offer_id)]);
  const source = (inquiry.offers ?? []).filter((offer) => own(offer) && (offer.line_id === line.id
    || references.has(offer.id)));
  const draft = source.filter((offer) => offer.status === "draft").sort((a, b) => b.id - a.id)[0];
  if (draft) return draft;
  const answer = inquiry.line_answers?.find((item) => item.line_id === line.id);
  const current = [...(answer?.offer ? [answer.offer] : []), ...(inquiry.line_responses ?? [])
    .flatMap((row) => row.line_id === line.id && row.current_offer ? [row.current_offer] : [])]
    .filter((offer) => own(offer) && offer.status === "published" && !offer.expired && !offer.original_withdrawn)
    .sort((a, b) => b.version - a.version || b.id - a.id)[0];
  return current ?? source.filter((offer) => offer.status === "published" && !offer.expired && !offer.original_withdrawn && !offer.latest)
    .sort((a, b) => b.id - a.id)[0];
}

/** 一句可以直接转述给客户的话；价格性质是内部标注，不写进这句话。 */
export function answerSentence(line: InquiryLine, answer: LineAnswer) {
  if (answer.state === "quoted") return `${line.pn_raw} ${priceText(answer.offer)}，${answer.reporter} ${formatBusinessDateTime(answer.at)} 报价`;
  if (answer.state === "skip") return `${line.pn_raw} 不报：${answer.reason}`;
  if (answer.state === "unavailable") return `${line.pn_raw} ${answer.reason}`;
  return `${line.pn_raw} 待报价`;
}

export function requiredLines(inquiry: Inquiry) {
  return (inquiry.lines ?? []).filter((line) => line.status !== "deleted");
}
