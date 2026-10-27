import { type ReactNode, type Key } from "react";
import { Button, Input, Radio, Select, Space, Tag } from "antd";
import DataGrid, { type DataGridColumn } from "../../components/datagrid/DataGrid";
import type { InquiryLine, LineResponseInput, Offer, OfferInput } from "../../api/presales";
import PriceRefBadge from "../../components/pricing/PriceRefBadge";
import QuoteEditor from "./QuoteEditor";
import { OfferBadge, activeOfferGroups, flattenOffers, taxLabel } from "./shared";
import { formatBusinessDateTime } from "../../utils/date";
import { moneyExact } from "../../utils/format";
import "./quoting.css";

export interface QuoteGridRow {
  line: InquiryLine;
  value: OfferInput;
  current?: Offer;
  existing?: Offer[];
  response?: LineResponseInput;
  responseDraft?: boolean;
  reused?: Offer;
  reporterLabel?: string;
  disabled?: boolean;
  responseDisabled?: boolean;
  referenced?: boolean;
  details?: ReactNode;
  actions?: ReactNode;
}
interface Props {
  id: string;
  rows: QuoteGridRow[];
  now: number;
  proactive?: boolean;
  onChange: (lineId: number, value: OfferInput) => void;
  onResponse?: (lineId: number, patch: Partial<LineResponseInput>) => void;
  expandedRowKeys: Key[];
  onExpandedRowKeysChange: (keys: Key[]) => void;
}

export default function QuoteGrid({ id, rows, now, proactive = false, onChange, onResponse, expandedRowKeys, onExpandedRowKeysChange }: Props) {
  const showQuote = (row: QuoteGridRow) => !row.referenced && (proactive || (row.response?.mode ?? "quote") === "quote");
  const editor = (row: QuoteGridRow, section: "price" | "tax" | "location" | "details") => <QuoteEditor
    section={section} label={row.line.pn_raw} value={row.value} existing={row.current?.status === "stale" || row.current?.status === "withdrawn" ? undefined : row.current}
    disabled={row.disabled} allowNoStock={proactive || row.current?.status === "published"}
    onChange={(value) => onChange(row.line.id, value)} />;
  const groups = (row: QuoteGridRow) => activeOfferGroups(row.existing ?? [], now);
  const visible = (row: QuoteGridRow) => flattenOffers(groups(row));
  const columns: DataGridColumn<QuoteGridRow>[] = [
    { key: "pn", title: "PN", type: "code", width: 150, identity: true, value: (row) => row.line.pn_raw,
      render: (row) => <span title={row.line.pn_raw}>{row.line.pn_raw}{row.line.require_exact_pn && <span className="quote-muted"> · 一致</span>}</span> },
    { key: "description", title: "描述", type: "zh", width: 205, value: (row) => row.line.description },
    { key: "qty", title: "数量", type: "number", width: 60, value: (row) => row.line.qty },
    { key: "expected_delivery", title: "期望交期", width: 120, value: (row) => row.line.expected_delivery },
    { key: "reference", title: "参考价 采 / 售", width: 160, render: (row) => row.line.part_id ? <PriceRefBadge partId={row.line.part_id} /> : "待建档" },
    { key: "existing", title: "已有有效报价", type: "number", width: 165, value: (row) => visible(row).length,
      render: (row) => {
        const offers = visible(row);
        // 跨税基不比较金额；摘要只取含税报价的最低值。
        const inclusive = offers.filter((offer) => offer.is_tax_inclusive === true);
        const minimum = inclusive.length ? Math.min(...inclusive.map((offer) => Number(offer.unit_price))) : null;
        return offers.length ? <Button type="link" size="small" onClick={() => onExpandedRowKeysChange([...new Set([...expandedRowKeys, row.line.id])])}>
          {offers.length} 条{minimum !== null ? ` · 含税最低 ${moneyExact(minimum)}` : " · 展开查看"}</Button> : "暂无有效报价";
      } },
    { key: "mode", title: "处理方式", width: 156, render: (row) => row.referenced ? <Tag>已引用报价，无需再次处理</Tag> : proactive ? <Tag>主动报价</Tag> :
      <span role="radiogroup" aria-label={`${row.line.pn_raw}处理方式`}><Radio.Group size="small" value={row.response?.mode ?? "quote"} disabled={row.responseDisabled}
        onChange={(event) => onResponse?.(row.line.id, { mode: event.target.value })}>
        <Radio.Button aria-label="报新价" value="quote">报价</Radio.Button><Radio.Button aria-label="沿用有效报价" value="reuse" disabled={!visible(row).length}>沿用</Radio.Button>
        <Radio.Button aria-label="无货 / 不报" value="skip">不报</Radio.Button>
      </Radio.Group></span> },
    { key: "price", title: "单价 / 沿用 / 原因", width: 235, render: (row) => row.referenced ? row.line.ref_offer && <OfferBadge offer={row.line.ref_offer} />
      : showQuote(row) ? editor(row, "price") : row.response?.mode === "skip" ? <Input size="small" aria-label={`${row.line.pn_raw}不报原因`}
        placeholder="不报原因（必填）" value={row.response.reason ?? ""} disabled={row.responseDisabled}
        onChange={(event) => onResponse?.(row.line.id, { reason: event.target.value })} />
      : <Select size="small" aria-label={`${row.line.pn_raw}沿用报价`} placeholder="选择有效报价" value={row.response?.ref_offer_id}
        disabled={row.responseDisabled} onChange={(ref_offer_id) => onResponse?.(row.line.id, { ref_offer_id })}
        options={visible(row).map((offer) => ({ value: offer.id, label: <span>报价 #{offer.id} <OfferBadge offer={offer} /></span> }))} /> },
    { key: "tax", title: "含税", width: 94, render: (row) => showQuote(row) ? editor(row, "tax") : row.reused ? taxLabel(row.reused.is_tax_inclusive) : "—" },
    { key: "location", title: "货源 / 所在地", width: 140, render: (row) => showQuote(row) ? editor(row, "location") : row.reused?.stock_location ?? "—" },
    { key: "actions", title: "操作 / 状态", width: 174, render: (row) => <Space size={4}>
      {row.responseDraft && <Tag>草稿（未回复）</Tag>}
      {row.current?.status === "draft" && <Tag>已保存草稿</Tag>}{row.current?.status === "stale" && <Tag>基于旧版本</Tag>}
      {row.current?.status === "withdrawn" && <Tag>已撤销</Tag>}{row.actions}</Space> },
  ];
  return <DataGrid id={id} columns={columns} rows={rows} rowKey={(row) => row.line.id} className="quote-grid" ariaLabel="采购报价明细"
    expandedRowKeys={expandedRowKeys} onExpandedRowKeysChange={onExpandedRowKeysChange}
    renderExpanded={(row) => <div className="quote-expanded">
      <div className="quote-full-description">{row.line.description || "暂无描述"}</div>
      {row.line.notes && <div>{row.line.notes}</div>}
      {row.line.field_values?.map((field, index) => <div key={index}>{field.name ?? field.new_name}: {String(field.value ?? "")}</div>)}
      {row.reused && <div>所沿用报价：<OfferBadge offer={row.reused} /><span>原报价人：{row.reporterLabel ?? row.reused.reporter?.name ?? "其他采购"}</span>
        {row.reused.valid_from && <span> · 原报价时间：{formatBusinessDateTime(row.reused.valid_from)}</span>}</div>}
      {!row.reused && row.response?.mode === "reuse" && row.response.ref_offer_id && <Tag>沿用报价当前不可见或已失效</Tag>}
      {row.details}
      {showQuote(row) && editor(row, "details")}
      <div className="quote-muted">{proactive ? "本 PN 当前可见的有效报价" : "本 PN 有效报价（仅展示询价单所有人可见的候选）"}</div>
      <DataGrid id={`${id}-existing`} ariaLabel={`${row.line.pn_raw}有效报价`} rows={groups(row)} rowKey={(offer) => offer.id}
        columns={[
          { key: "reporter", title: "报价人", width: 130, type: "zh", value: (offer) => offer.reporter?.name ?? "其他采购", identity: true },
          { key: "price", title: "有效报价", width: 310, type: "money", value: (offer) => offer.unit_price, render: (offer) => <OfferBadge offer={offer} /> },
          { key: "time", title: "原报价时间", width: 175, type: "time", value: (offer) => offer.valid_from, render: (offer) => offer.valid_from ? formatBusinessDateTime(offer.valid_from) : "—" },
        ]} emptyText="暂无可见的有效报价" renderExpanded={(offer) => <DataGrid id={`${id}-more`} rows={offer.more ?? []} rowKey={(more) => more.id}
          ariaLabel="更多有效报价" columns={[
            { key: "id", title: "报价编号", width: 96, type: "number", identity: true, value: (more) => more.id },
            { key: "price", title: "报价", width: 360, type: "money", value: (more) => more.unit_price, render: (more) => <OfferBadge offer={more} /> },
          ]} />} />
    </div>} />;
}
