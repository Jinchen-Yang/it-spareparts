import { useState, type ReactNode } from "react";
import { Button, Descriptions, Drawer, Form, Input, Radio, Select, Space, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { InquiryLine, LineResponseInput, Offer, OfferInput } from "../../api/presales";
import PriceRefBadge from "../../components/pricing/PriceRefBadge";
import QuoteEditor from "./QuoteEditor";
import { OfferBadge, activeOfferGroups, flattenOffers } from "./shared";
import { formatBusinessDateTime } from "../../utils/date";
import "./presales.css";

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
  binding?: ReactNode;
  actions?: ReactNode;
}
interface Props {
  id: string;
  rows: QuoteGridRow[];
  now: number;
  proactive?: boolean;
  onChange: (lineId: number, value: OfferInput) => void;
  onResponse?: (lineId: number, patch: Partial<LineResponseInput>) => void;
}

export default function QuoteGrid({ id, rows, now, proactive = false, onChange, onResponse }: Props) {
  const [selected, setSelected] = useState<number>();
  const [binding, setBinding] = useState<number>();
  const row = rows.find((item) => item.line.id === selected);
  const bindingRow = rows.find((item) => item.line.id === binding);
  const offersFor = (item: QuoteGridRow) => flattenOffers(activeOfferGroups(item.existing ?? [], now));
  const state = (item: QuoteGridRow) => item.referenced ? "已引用报价，无需再次处理"
    : item.response?.mode === "skip" ? `不报：${item.response.reason || "待填写原因"}`
    : item.response?.mode === "reuse" ? `沿用报价 #${item.response.ref_offer_id ?? "待选择"}`
    : item.current?.status === "published" ? "已回复" : item.current?.status === "stale" ? "基于旧版本"
    : item.current?.status === "withdrawn" ? "已撤销" : item.current?.status === "draft" ? "已保存草稿" : "待填写";
  const columns: ColumnsType<QuoteGridRow> = [
    { title: "PN / 描述", key: "pn", render: (_, item) => <><div>{item.line.pn_raw}{item.line.require_exact_pn && <Tag>一致</Tag>}</div><div className="presales-muted">{item.line.description || "—"}</div></> },
    { title: "数量", key: "qty", width: 80, sorter: (a, b) => Number(a.line.qty) - Number(b.line.qty), render: (_, item) => item.line.qty },
    { title: "期望交期", key: "delivery", width: 115, render: (_, item) => item.line.expected_delivery || "—" },
    { title: "参考价", key: "reference", width: 160, render: (_, item) => item.line.part_id ? <PriceRefBadge partId={item.line.part_id} /> : "待建档" },
    { title: "已有有效报价", key: "existing", width: 130, render: (_, item) => <Button type="link" onClick={() => setSelected(item.line.id)}>{offersFor(item).length} 条</Button> },
    { title: "本次填写状态", key: "state", width: 170, render: (_, item) => <>{state(item)}{item.responseDraft && <Tag>草稿（未回复）</Tag>}</> },
    { title: "操作", key: "actions", width: 150, render: (_, item) => <Space wrap><Button type="link" aria-label={`填写 ${item.line.pn_raw} 报价`} onClick={() => setSelected(item.line.id)}>{item.disabled && item.responseDisabled ? "查看报价" : "填报价"}</Button>
      {item.binding && <Button type="link" aria-label={`建档 ${item.line.pn_raw}`} onClick={() => setBinding(item.line.id)}>建档</Button>}{!item.current && proactive && item.actions}</Space> },
  ];
  return <>
    <Table id={id} aria-label="采购报价明细" rowKey={(item) => item.line.id} dataSource={rows} columns={columns}
      tableLayout="fixed" pagination={{ defaultPageSize: 20, showSizeChanger: true }} />
    <Drawer title={row ? `${row.line.pn_raw} · 报价` : "填写报价"} open={!!row} width={640} onClose={() => setSelected(undefined)}
      extra={row?.actions} destroyOnHidden>
      {row && <Space direction="vertical" size="large" style={{ width: "100%" }}>
        <Descriptions column={1} items={[
          { key: "description", label: "描述", children: row.line.description || "—" },
          { key: "qty", label: "需求数量", children: row.line.qty },
          ...(row.line.notes != null ? [{ key: "notes", label: "行要求", children: row.line.notes }] : []),
          ...(row.line.field_values ?? []).map((field, index) => ({ key: `field-${index}`, label: field.name ?? field.new_name, children: String(field.value ?? "") })),
        ]} />
        {row.referenced ? row.line.ref_offer && <OfferBadge offer={row.line.ref_offer} /> : <>
          {!proactive && (row.responseDisabled ? <div>处理方式：{state(row)}</div> : <span role="radiogroup" aria-label={`${row.line.pn_raw}处理方式`}><Radio.Group value={row.response?.mode ?? "quote"}
            onChange={(event) => onResponse?.(row.line.id, { mode: event.target.value })}>
            <Radio.Button aria-label="报新价" value="quote">报价</Radio.Button><Radio.Button aria-label="沿用有效报价" value="reuse" disabled={!offersFor(row).length}>沿用</Radio.Button>
            <Radio.Button aria-label="无货 / 不报" value="skip">不报</Radio.Button>
          </Radio.Group></span>)}
          {!proactive && row.response?.mode === "skip" ? row.responseDisabled ? <p>不报原因：{row.response.reason}</p> : <Form layout="vertical"><Form.Item label="不报原因" required>
            <Input.TextArea aria-label={`${row.line.pn_raw}不报原因`} value={row.response.reason ?? ""} onChange={(event) => onResponse?.(row.line.id, { reason: event.target.value })} />
          </Form.Item></Form> : !proactive && row.response?.mode === "reuse" ? <>
            {!row.responseDisabled && <Select aria-label={`${row.line.pn_raw}沿用报价`} placeholder="选择有效报价" value={row.response.ref_offer_id} style={{ width: "100%" }}
              onChange={(ref_offer_id) => onResponse?.(row.line.id, { ref_offer_id })}
              options={offersFor(row).map((offer) => ({ value: offer.id, label: <span>报价 #{offer.id} <OfferBadge offer={offer} /></span> }))} />}
            {row.reused ? <div>所沿用报价：<OfferBadge offer={row.reused} /><div>原报价人：{row.reporterLabel ?? row.reused.reporter?.name ?? "其他采购"}</div>
              {row.reused.valid_from && <div>原报价时间：{formatBusinessDateTime(row.reused.valid_from)}</div>}</div> : row.response.ref_offer_id && <Tag>沿用报价当前不可见或已失效</Tag>}
          </> : <QuoteEditor label={row.line.pn_raw} value={row.value} disabled={row.disabled}
            allowNoStock={proactive || row.current?.status === "published"}
            existing={row.current?.status === "stale" || row.current?.status === "withdrawn" ? undefined : row.current}
            onChange={(value) => onChange(row.line.id, value)} />}
        </>}
        {row.details}
        <div><h3>本 PN 有效报价{!proactive && "（仅展示询价单所有人可见的候选）"}</h3>
          <Table aria-label={`${row.line.pn_raw}有效报价`} rowKey="id" dataSource={offersFor(row)} size="small" pagination={{ pageSize: 5 }}
            tableLayout="fixed" columns={[
              { title: "报价人", key: "reporter", width: 100, render: (_, offer) => offer.reporter?.name ?? "其他采购" },
              { title: "有效报价", key: "price", render: (_, offer) => <OfferBadge offer={offer} /> },
              { title: "原报价时间", key: "time", width: 150, render: (_, offer) => offer.valid_from ? formatBusinessDateTime(offer.valid_from) : "—" },
            ]} locale={{ emptyText: "暂无可见的有效报价" }} />
        </div>
      </Space>}
    </Drawer>
    <Drawer title={bindingRow ? `${bindingRow.line.pn_raw} · 建档` : "型号建档"} open={!!bindingRow?.binding} width={560} onClose={() => setBinding(undefined)} destroyOnHidden>
      {bindingRow?.binding}
    </Drawer>
  </>;
}
