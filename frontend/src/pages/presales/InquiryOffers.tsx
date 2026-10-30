import type { ReactNode } from "react";
import { Button, Descriptions, Table, Tag } from "antd";
import type { Inquiry, InquiryLine, Offer } from "../../api/presales";
import { OfferBadge, OfferUpdates, PriceBasisTag, taxLabel } from "./shared";
import { answerSentence, lineAnswer } from "./answers";
import { formatBusinessDateTime } from "../../utils/date";
import { formatMoney } from "../../utils/format";

export function hasValue(value: unknown): boolean {
  return value !== undefined && value !== null && value !== "";
}

export function displayValue(value: unknown): ReactNode {
  if (typeof value === "boolean") return value ? "是" : "否";
  return hasValue(value) ? String(value) : null;
}

export function FieldValues({ values }: { values?: Inquiry["field_values"] }) {
  if (!values?.length) return null;
  return <Descriptions size="small" column={1} items={values.filter((field) => hasValue(field.value)).map((field) => ({
    key: String(field.field_def_id), label: field.name, children: displayValue(field.value),
  }))} />;
}

export function OfferPrice({ offer }: { offer: Offer }) {
  if (offer.no_stock) return <Tag>无货</Tag>;
  return hasValue(offer.unit_price) ? <>{formatMoney(offer.unit_price, { prefix: "¥" })} · {taxLabel(offer.is_tax_inclusive)} <PriceBasisTag offer={offer} /></> : null;
}

export function CurrentAnswer({ inquiry, line }: { inquiry: Inquiry; line: InquiryLine }) {
  const answer = lineAnswer(inquiry, line);
  return <>{answerSentence(line, answer)} {answer.state === "quoted" && <PriceBasisTag offer={answer.offer} />}</>;
}

/** 引用、沿用按本行钉住快照显示，绝不补查当前报价覆盖旧价格。 */
export function offersForLine(line: InquiryLine, inquiry: Inquiry): Offer[] {
  const pinned = new Map([...(line.linked_offers ?? []), ...(line.ref_offer ? [line.ref_offer] : []),
    ...(inquiry.line_responses ?? []).flatMap((response) => !response.is_draft && response.line_id === line.id && response.mode === "reuse" && response.offer ? [response.offer] : [])]
    .map((offer) => [offer.id, offer]));
  return [...pinned.values(), ...(inquiry.offers ?? []).filter((offer) => !pinned.has(offer.id))].filter((offer) =>
    (offer.line_id === line.id || offer.id === line.ref_offer_id || pinned.has(offer.id) || line.linked_offer_ids?.includes(offer.id))
    && offer.status !== "withdrawn" && !offer.original_withdrawn);
}

export function OfferDetails({ line, offers, onHistory }: { line: InquiryLine; offers: Offer[]; onHistory: (offer: Offer) => void }) {
  return <Table aria-label={`${line.pn_raw} 报价详情`} rowKey="id" dataSource={offers} tableLayout="fixed" pagination={{ pageSize: 5 }} columns={[
    { title: "报价", key: "price", render: (_, offer) => <><OfferBadge offer={offer} /><OfferUpdates offer={offer} /></> },
    { title: "供货详情", key: "supply", render: (_, offer) => <Descriptions size="small" column={1} items={historyFields.filter((field) => !["unit_price", "is_tax_inclusive", "price_basis", "no_stock"].includes(field.key) && hasValue(offer[field.key]))
      .map((field) => ({ key: field.key, label: field.label, children: field.key === "valid_from" ? formatBusinessDateTime(String(offer[field.key])) : displayValue(offer[field.key]) }))} /> },
    { title: "操作", key: "actions", width: 90, render: (_, offer) => <Button type="link" aria-label={`查看报价 ${offer.id} 历史`} onClick={() => onHistory(offer)}>历史</Button> },
  ]} />;
}

export const historyFields: { key: keyof Offer; label: string }[] = [
  { key: "unit_price", label: "单价" }, { key: "is_tax_inclusive", label: "含税口径" },
  { key: "price_basis", label: "价格性质" },
  { key: "no_stock", label: "无货" }, { key: "qty_available", label: "可供数量" },
  { key: "stock_location", label: "货物所在地" }, { key: "lead_time", label: "交期" },
  { key: "supplier_id", label: "供应商" }, { key: "source_key", label: "货源" },
  { key: "notes", label: "采购备注" }, { key: "valid_from", label: "报价时间" },
];

