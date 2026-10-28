import { useState, useEffect } from "react";
import { Alert, Button, Space, Table } from "antd";
import type { InquiryLine, Offer } from "../../api/presales";
import type { AudienceRule, AudienceRuleRead } from "../../api/audience";
import { getFieldTemplate, listFieldDefinitions, type CustomFieldValue, type FieldDefinition, type FieldLevel } from "../../api/fields";
import CustomFieldsEditor from "../../components/fields/CustomFieldsEditor";
import { isOfferActive as activeOffer, OfferBadge, flattenOffers } from "./shared";
import { formatMoney } from "../../utils/format";
import { parseApiError } from "../../utils/apiError";
import { ApiErrorFeedback } from "../../components/ApiErrorFeedback";

export type ReadField = { field_def_id?: number; new_name?: string; name?: string; value: unknown; rule?: AudienceRuleRead };
export type DraftLine = {
  key: string; line_key?: string; part_id: number | null; pn_raw: string; description?: string | null;
  brand?: string | null; qty: number; require_exact_pn: boolean; ref_offer_id?: number | null;
  ref_offer_revision_no?: number | null; ref_offer?: Offer;
  notes?: string | null; notes_audience?: AudienceRule; notesVisible: boolean; field_values: CustomFieldValue[];
  savedNotesAudience?: AudienceRuleRead;
  originalFields: ReadField[]; expectedDelivery: string;
};
export const DELIVERY_FIELD = "期望交期";
export const fieldKey = (value: { field_def_id?: number; new_name?: string }) => value.field_def_id ? `id:${value.field_def_id}` : `new:${value.new_name}`;
export const editableFields = (values: ReadField[] = [], excludeDelivery = false): CustomFieldValue[] => values.flatMap((value) => {
  if ((excludeDelivery && value.name === DELIVERY_FIELD) || value.value == null) return [];
  return [{ ...(value.field_def_id ? { field_def_id: value.field_def_id } : { new_name: value.new_name || value.name || "" }), value: String(value.value) } as CustomFieldValue];
});

export function errorText(error: unknown, fallback: string) {
  return parseApiError(error, fallback).message;
}

/** 字段池只负责编辑；模板按 PN anchor 拉取，不携带上一张单据的值。 */
export function Fields({ level, partId, value, savedValues, onChange, disabled = false }: {
  level: FieldLevel; partId?: number | null; value: CustomFieldValue[];
  savedValues?: ReadField[];
  onChange: (values: CustomFieldValue[]) => void; disabled?: boolean;
}) {
  const [template, setTemplate] = useState<FieldDefinition[]>([]);
  const [definitions, setDefinitions] = useState<FieldDefinition[]>([]);
  const [error, setError] = useState<ReturnType<typeof parseApiError>>();
  const [reload, setReload] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError(undefined);
    void Promise.all([getFieldTemplate("presales_inquiry", level, partId ? `part:${partId}` : undefined, controller.signal),
      listFieldDefinitions("presales_inquiry", level, controller.signal)]).then(([rows, pool]) => {
      if (!controller.signal.aborted) {
        setTemplate(rows.filter((field) => field.name !== DELIVERY_FIELD || level !== "line"));
        setDefinitions(pool.filter((field) => field.name !== DELIVERY_FIELD || level !== "line"));
      }
    }).catch((cause) => { if (!controller.signal.aborted) setError(parseApiError(cause, "字段池加载失败")); });
    return () => controller.abort();
  }, [level, partId, reload]);
  return <>
    {error && <Alert type="warning" message="字段池加载失败，已有填写内容仍会保留。" description={<ApiErrorFeedback info={error} />}
      action={<Button onClick={() => setReload((value) => value + 1)}>重新加载字段</Button>} />}
    <CustomFieldsEditor subjectType="presales_inquiry" level={level} template={template} definitions={definitions}
      value={value} savedValues={savedValues} onChange={onChange} disabled={disabled || !!error} />
  </>;
}

export function EffectiveOffers({ offers, now, onReference }: { offers: Offer[]; now: number; onReference?: (offer: Offer) => void }) {
  const active = flattenOffers(offers).filter((offer) => activeOffer(offer, now));
  if (!active.length) return <span className="presales-muted">无有效报价</span>;
  const first = active[0];
  return <Space direction="vertical" style={{ width: "100%" }}><span>{formatMoney(first.unit_price, { prefix: "¥" })} · 共 {active.length} 条有效报价</span>
    {onReference && <Button onClick={() => onReference(first)}>直接引用</Button>}
    <Table aria-label="可引用报价" size="small" rowKey="id" dataSource={active} tableLayout="fixed" pagination={{ pageSize: 5 }} columns={[
      { title: "有效报价", key: "offer", render: (_, offer) => <OfferBadge offer={offer} /> },
      { title: "操作", key: "actions", width: 140, render: (_, offer) => onReference && <Button type="link" onClick={() => onReference(offer)}>引用报价 #{offer.id}</Button> },
    ]} />
  </Space>;
}
