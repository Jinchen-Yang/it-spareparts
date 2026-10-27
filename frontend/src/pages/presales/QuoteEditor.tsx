import { Checkbox, Form, Input, InputNumber, Select, Space, Typography } from "antd";
import type { Offer, OfferInput } from "../../api/presales";
import type { AudienceRule } from "../../api/audience";
import AudienceLock from "../../components/audience/AudienceLock";
import { PUBLIC_AUDIENCE } from "../../components/audience/AudiencePicker";
import { expiryLabel, useNow } from "./shared";
import { presales } from "../../api/presales";
import { readPermissionMap } from "../../nav";
import { changedAudience } from "../../components/audience/rules";
import CandidateSelect from "./CandidateSelect";

const PRIVATE_AUDIENCE: AudienceRule = { mode: "private", user_ids: [], group_ids: [] };
const editableFields = ["unit_price", "is_tax_inclusive", "no_stock", "qty_available", "stock_location",
  "lead_time", "source_key", "supplier_id", "validity_days", "notes"] as const;

export function offerInput(offer?: Offer): OfferInput {
  if (!offer) return { no_stock: false, validity_days: 1, supplier_audience: PRIVATE_AUDIENCE,
    reporter_audience: PUBLIC_AUDIENCE, notes_audience: PUBLIC_AUDIENCE };
  // 返回中缺失的字段保持缺失，避免保存时把不可见内容清空或改为公开。
  return Object.fromEntries(editableFields.filter((field) => field in offer)
    .map((field) => [field, offer[field]]));
}

export function quoteComplete(value: Pick<OfferInput, "no_stock" | "unit_price" | "notes">): boolean {
  return value.no_stock === true ? !!value.notes?.trim() : (value.unit_price != null && value.unit_price !== ""
    && Number.isFinite(Number(value.unit_price)) && Number(value.unit_price) >= 0);
}

interface Props {
  value: OfferInput;
  onChange: (value: OfferInput) => void;
  existing?: Offer;
  disabled?: boolean;
  label?: string;
  allowNoStock?: boolean;
}

export default function QuoteEditor({ value, onChange, existing, disabled = false, label = "报价", allowNoStock = true }: Props) {
  const now = useNow();
  const permissions = readPermissionMap();
  const supplierVisible = localStorage.getItem("role") === "admin"
    || (permissions.data_supplier !== false && !!(permissions.action_presales_quote || permissions.action_presales_offer));
  const change = (patch: Partial<OfferInput>) => onChange({ ...value, ...patch });
  const fieldVisible = (field: keyof Offer) => !existing || field in existing;
  const lock = (field: "supplier_audience" | "reporter_audience" | "notes_audience", title: string) => !existing || existing[field] ? (
    <AudienceLock key={`${existing?.id ?? "new"}:${existing?.version ?? 0}:${field}`} label={title} disabled={disabled}
      savedRule={existing?.[field]} value={value[field] ?? (!existing ? field === "supplier_audience" ? PRIVATE_AUDIENCE : PUBLIC_AUDIENCE : undefined)}
      onChange={(rule) => change({ [field]: changedAudience(rule, existing?.[field]) })} />
  ) : null;

  return <Form layout="vertical" component="div" disabled={disabled}>
    {allowNoStock && <Form.Item><Checkbox checked={value.no_stock} disabled={disabled}
      onChange={(event) => change({ no_stock: event.target.checked })}>无货</Checkbox></Form.Item>}
    <Space wrap align="start" style={{ width: "100%" }}>
      <Form.Item label="单价" required={!value.no_stock}>
        <InputNumber aria-label={`${label}单价`} min={0} max={999999999999.99} precision={2}
          disabled={disabled || value.no_stock} value={value.unit_price == null ? null : Number(value.unit_price)}
          onChange={(next) => change({ unit_price: next })} />
      </Form.Item>
      <Form.Item label="含税口径">
        <Select aria-label={`${label}含税口径`} value={value.is_tax_inclusive === true ? "inc" : value.is_tax_inclusive === false ? "ex" : "unknown"}
          style={{ width: 150 }} disabled={disabled || value.no_stock}
          options={[{ value: "unknown", label: "未声明" }, { value: "inc", label: "含税" }, { value: "ex", label: "不含税" }]}
          onChange={(next) => change({ is_tax_inclusive: next === "unknown" ? null : next === "inc" })} />
      </Form.Item>
      <Form.Item label="可供数量">
        <InputNumber aria-label={`${label}可供数量`} min={0} max={99999999999.999} precision={3}
          disabled={disabled || value.no_stock} value={value.qty_available == null ? null : Number(value.qty_available)}
          onChange={(next) => change({ qty_available: next })} />
      </Form.Item>
      <Form.Item label="货物所在地">
        <Input aria-label={`${label}货物所在地`} disabled={disabled} maxLength={256} value={value.stock_location ?? ""}
          onChange={(event) => change({ stock_location: event.target.value })} />
      </Form.Item>
      <Form.Item label="交期">
        <Input aria-label={`${label}交期`} disabled={disabled} maxLength={256} value={value.lead_time ?? ""}
          onChange={(event) => change({ lead_time: event.target.value })} />
      </Form.Item>
    </Space>
    <Form.Item label="有效天数（自然日）" required>
      <Space wrap>
        <Select aria-label={`${label}有效天数预设`} style={{ width: 150 }} disabled={disabled}
          value={[1, 3, 7, 15, 30].includes(value.validity_days ?? 1) ? value.validity_days ?? 1 : "custom"}
          options={[1, 3, 7, 15, 30].map((days) => ({ value: days as number | string, label: `${days} 天` }))
            .concat([{ value: "custom", label: "自定义" }])}
          onChange={(next) => change({ validity_days: next === "custom" ? 2 : Number(next) })} />
        <InputNumber aria-label={`${label}有效天数`} min={1} max={36500} precision={0} disabled={disabled}
          value={value.validity_days ?? 1} onChange={(next) => change({ validity_days: next ?? 1 })} />
        <Typography.Text type="secondary" role="status">{expiryLabel(value.validity_days ?? 1, now)}</Typography.Text>
      </Space>
    </Form.Item>
    {supplierVisible && fieldVisible("supplier_id") && <Form.Item label={<Space>供应商{lock("supplier_audience", "供应商")}</Space>}>
      <CandidateSelect label={`${label}供应商`} load={presales.suppliers} disabled={disabled} value={value.supplier_id}
        onChange={(next) => change({ supplier_id: next ?? null })} />
    </Form.Item>}
    {fieldVisible("source_key") && <Form.Item label="货源标识（可选）">
      <Input aria-label={`${label}货源标识`} value={value.source_key ?? ""} disabled={disabled} maxLength={128}
        onChange={(event) => change({ source_key: event.target.value })} />
    </Form.Item>}
    {(!existing || "reporter" in existing) && <Form.Item label={<Space>报价人{lock("reporter_audience", "报价人")}</Space>}>
      <Typography.Text>{existing?.reporter?.name ?? "当前填写人"}</Typography.Text>
    </Form.Item>}
    {fieldVisible("notes") && <Form.Item required={value.no_stock} label={<Space>{value.no_stock ? "无货原因（必填）" : "采购备注"}{lock("notes_audience", "采购备注")}</Space>}>
      <Input.TextArea aria-label={`${label}备注`} value={value.notes ?? ""} disabled={disabled}
        placeholder={value.no_stock ? "请填写无货原因" : undefined}
        onChange={(event) => change({ notes: event.target.value })} />
    </Form.Item>}
    {existing && <Typography.Text type="secondary">未调整的可见性沿用已有设置。</Typography.Text>}
  </Form>;
}
