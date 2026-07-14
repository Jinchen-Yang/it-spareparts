/**
 * 最近采购 / 最近销售 订单块（订单粒度一单一行 + 展开 PN 行明细）。
 * - PN 首屏直出（pn_preview ≤3 + 展开全部）
 * - 点单号展开订单内容；点 PN 深链 /parts?part_id=；点池名进池分析
 * - 服务端分页/排序/搜索；受全局筛选（时间/PN/池/人员）作用
 * - 四态分离：loading / 接口失败(可重试) / 空数据 / 数据
 */
import { useEffect, useMemo, useState, type Key } from "react";
import { Alert, Button, Card, Input, Select, Table, Tag } from "antd";
import type { ColumnsType, TablePaginationConfig } from "antd/es/table";
import type { SorterResult } from "antd/es/table/interface";
import {
  dashboardPurchaseOrders, dashboardSales,
  type OrdersQuery, type OrdersResp, type PurchaseOrderRow, type SalesOrderRow,
} from "../../api";
import { EMPTY, moneyExact, qty } from "../../utils/format";
import PartsTable, { PartLink, type OrderSide } from "./PartsTable";
import {
  MUTED, fmtMoneyR, orderReferenceSummary, useGuardedFetch, type DateRange,
} from "./shared";

type AnyOrder = SalesOrderRow | PurchaseOrderRow;
type AnyPart = NonNullable<AnyOrder["parts"]>[number];

interface OrdersBlockProps {
  side: OrderSide;
  range: DateRange;
  partId: number | null;
  poolId: number | null;
  /** 销售块=销售员、采购块=采购员 */
  person: string | null;
  /** 全局筛选摘要（当前统计范围），显示在块头 */
  scopeNote: string;
  localProfitRestricted: boolean;
  localCostRestricted: boolean;
}

const PAGE_SIZE_OPTIONS = [10, 20, 50];

/** null = pre-v2 response did not include the field; [] = v2 explicitly has no lines. */
function orderParts(row: AnyOrder): AnyPart[] | null {
  return Array.isArray(row.parts) ? row.parts : null;
}

function orderPnCount(row: AnyOrder): number {
  return typeof row.pn_count === "number" ? row.pn_count : row.part_count;
}

function isV2OrderContract(data: OrdersResp<AnyOrder> | null): boolean {
  if (!data) return true;
  const n = Number(String(data.contract_version ?? "").replace(/^v/i, ""));
  // 新筛选参数会被旧 FastAPI 静默忽略，不能凭返回字段“猜版本”；只有
  // 后端显式声明 v2 才能展示带 PN/池/采购员筛选的结果（空结果也一样）。
  return Number.isFinite(n) && n >= 2;
}

export default function OrdersBlock({
  side, range, partId, poolId, person, scopeNote,
  localProfitRestricted, localCostRestricted,
}: OrdersBlockProps) {
  const isPurchase = side === "purchase";
  const [q, setQ] = useState<string | undefined>(undefined);
  const [status, setStatus] = useState<string>("");
  const [sort, setSort] = useState<string>("order_date");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [expanded, setExpanded] = useState<readonly Key[]>([]);

  // 全局筛选/搜索变化回第 1 页并收起展开（避免停在越界页/展开态错位）
  useEffect(() => { setPage(1); setExpanded([]); },
    [range.date_from, range.date_to, partId, poolId, person, q, status]);

  const query: OrdersQuery = useMemo(() => ({
    ...range,
    q, status: status || undefined,
    part_id: partId ?? undefined,
    pool_group_id: poolId ?? undefined,
    ...(isPurchase ? { purchaser: person ?? undefined } : { salesperson: person ?? undefined }),
    sort, order, page, page_size: pageSize,
  }), [range, q, status, partId, poolId, person, isPurchase, sort, order, page, pageSize]);

  const { data, loading, error, reload } = useGuardedFetch<OrdersResp<AnyOrder>>(
    () => (isPurchase
      ? dashboardPurchaseOrders(query) as Promise<{ data: OrdersResp<AnyOrder> }>
      : dashboardSales(query) as Promise<{ data: OrdersResp<AnyOrder> }>),
    [query]);

  // 权限旗标：本地先行 + 响应旗标并集（只收紧不放开）
  const profitRestricted = localProfitRestricted || (data?.profit_restricted ?? false);
  const costRestricted = localCostRestricted || (data?.cost_restricted ?? false);
  const partsRestricted = data?.parts_restricted ?? false;
  const manualRestricted = data?.manual_reference_restricted ?? false;
  const ordersRestricted = data?.orders_restricted ?? false;
  const needsV2Filter = partId != null || poolId != null || (isPurchase && !!person);
  const unsupportedLegacyFilter = needsV2Filter && !isV2OrderContract(data);

  const toggleExpand = (id: number) =>
    setExpanded((keys) => (keys.includes(id) ? keys.filter((k) => k !== id) : [...keys, id]));

  const sortProps = (key: string, disabled = false) => ({
    sorter: !disabled,
    sortOrder: data?.effective_sort === key && sort === key
      ? (order === "asc" ? "ascend" as const : "descend" as const) : null,
  });

  const pnPreviewCell = (r: AnyOrder) => {
    if (partsRestricted) return <Tag>无明细权限</Tag>;
    const parts = orderParts(r);
    const preview = Array.isArray(r.pn_preview)
      ? r.pn_preview
      : parts?.map((p) => p.pn_std).filter((pn): pn is string => !!pn).slice(0, 3);
    if (!parts && !preview) {
      return <Tag title="当前后端尚未提供 v2 PN 明细字段">旧版接口：暂无PN明细</Tag>;
    }
    if (!preview?.length) return <span style={MUTED}>{EMPTY}</span>;
    const byPn = new Map((parts ?? []).map((p) => [p.pn_std, p.part_id]));
    const pnCount = orderPnCount(r);
    const more = Math.max(0, pnCount - preview.length);
    return (
      <span style={{ display: "inline-flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
        {preview.map((pn) => (
          <PartLink key={pn} partId={byPn.get(pn) ?? null} pn={pn} />
        ))}
        {more > 0 && (
          <Button type="link" size="small" onClick={() => toggleExpand(r.order_id)}
            style={{ padding: 0, height: "auto" }} aria-label={`展开全部 ${pnCount} 个型号明细`}>
            +{more} 更多
          </Button>
        )}
      </span>
    );
  };

  const cols: ColumnsType<AnyOrder> = [
    { title: "日期", dataIndex: "order_date", key: "order_date", width: 112,
      ...sortProps("order_date"),
      render: (v, r) => <span>{v || EMPTY}{r.is_future && <Tag color="red" style={{ marginLeft: 4 }}>未来</Tag>}</span> },
    { title: isPurchase ? "采购单号" : "销售单号", dataIndex: "order_no", width: 140,
      render: (v, r) => (
        <Button type="link" size="small" onClick={() => toggleExpand(r.order_id)}
          style={{ padding: 0, height: "auto", fontFamily: "monospace", fontSize: 12 }}
          aria-expanded={expanded.includes(r.order_id)}
          aria-label={`订单 ${v}，${expanded.includes(r.order_id) ? "收起" : "展开"}明细`}>{v}</Button>) },
    { title: "PN", key: "pns", width: 210, render: (_, r) => pnPreviewCell(r) },
    { title: "型号数", key: "part_count", width: 78, align: "right",
      ...sortProps("part_count"), render: (_, r) => orderPnCount(r) },
    { title: "数量", dataIndex: "total_quantity", width: 72, align: "right", render: qty },
    ...(isPurchase ? [
      { title: "采购员", dataIndex: "purchaser", width: 84,
        render: (v: string | null) => v ?? <span style={MUTED}>{EMPTY}</span> },
      { title: "类型", dataIndex: "source_type", width: 92,
        render: (v: string | null) => (v ? <Tag>{v}</Tag> : EMPTY) },
      { title: "金额(未税)", dataIndex: "total_amount", key: "amount", width: 116, align: "right" as const,
        ...sortProps("amount", costRestricted),
        render: (v: number | null) => fmtMoneyR(v, costRestricted, "无成本权限") },
      { title: "关联销售单", dataIndex: "linked_sales_order", width: 128,
        render: (v: string | null) => v || <span style={MUTED}>{EMPTY}</span> },
    ] : [
      { title: "销售员", dataIndex: "salesperson", width: 84,
        render: (v: string | null) => partsRestricted
          ? <span style={MUTED}>无权限</span> : (v ?? <span style={MUTED}>{EMPTY}</span>) },
      { title: "客户", dataIndex: "customer", width: 130, ellipsis: true,
        render: (v: string | null) => partsRestricted
          ? <span style={MUTED}>无权限</span> : (v ?? <span style={MUTED}>{EMPTY}</span>) },
      { title: "营收(未税)", dataIndex: "total_revenue", key: "revenue", width: 116, align: "right" as const,
        ...sortProps("revenue"), render: (v: number | null) => v == null
          ? <span style={MUTED}>{EMPTY}</span> : moneyExact(v) },
      { title: "毛利", dataIndex: "total_gross_profit", key: "gross_profit", width: 108, align: "right" as const,
        ...sortProps("gross_profit", profitRestricted),
        render: (v: number | null) => profitRestricted ? <Tag>无利润权限</Tag>
          : v == null ? <Tag>无成本</Tag>
            : <span style={{ color: v < 0 ? "#c0524a" : undefined }}>{moneyExact(v)}</span> },
    ]),
    { title: "状态", dataIndex: "data_status", width: 82,
      render: (v) => (v ? <Tag>{v}</Tag> : EMPTY) },
    { title: "分析状态", key: "ref_summary", width: 130,
      render: (_, r) => orderReferenceSummary(orderParts(r) ?? undefined, partsRestricted) },
  ];

  const onChange = (pag: TablePaginationConfig, _f: unknown,
                    sorter: SorterResult<AnyOrder> | SorterResult<AnyOrder>[]) => {
    const s = Array.isArray(sorter) ? sorter[0] : sorter;
    setPage(pag.current || 1);
    setPageSize(pag.pageSize || pageSize);
    if (s?.order) {
      setSort(String(s.columnKey || "order_date"));
      setOrder(s.order === "ascend" ? "asc" : "desc");
    }
  };

  return (
    <Card size="small" style={{ marginBottom: 16 }}
      title={isPurchase ? "最近采购" : "最近销售"}
      extra={<span style={MUTED}>{scopeNote}</span>}>
      <div style={{ display: "flex", gap: 8, marginBottom: 8, flexWrap: "wrap" }}>
        <Input.Search allowClear placeholder="搜索 型号 / 单号 / 描述 / 品牌" style={{ width: 250 }}
          onSearch={(v) => setQ(v || undefined)} aria-label={`${isPurchase ? "采购" : "销售"}订单搜索`} />
        <Select style={{ width: 120 }} value={status} onChange={setStatus}
          aria-label="订单状态筛选"
          options={[{ label: "仅已生效", value: "" }, { label: "全部状态", value: "全部" }]} />
      </div>
      {error ? (
        <Alert type="error" showIcon message={`${isPurchase ? "采购" : "销售"}订单加载失败：${error}`}
          action={<Button size="small" onClick={reload}>重试</Button>} />
      ) : ordersRestricted ? (
        <Alert type="info" showIcon message="当前账号无逐单销售订单权限（仅聚合数据可见）。" />
      ) : unsupportedLegacyFilter ? (
        <Alert type="warning" showIcon
          message="服务升级中，当前 PN、互通池或采购员筛选暂不可用；为避免展示错误结果，本列表已暂停显示。" />
      ) : (
        <Table<AnyOrder>
          size="small" rowKey="order_id" loading={loading}
          dataSource={data?.items || []} columns={cols} scroll={{ x: 1150 }}
          locale={{ emptyText: "当前筛选范围内暂无订单" }}
          onChange={onChange}
          expandable={{
            expandedRowKeys: expanded,
            onExpandedRowsChange: (keys) => setExpanded(keys),
            expandedRowRender: (r) => {
              if (partsRestricted) {
                return <Alert type="info" showIcon message="当前账号无逐单明细查看权限（仅聚合可见）。" />;
              }
              const parts = orderParts(r);
              return parts == null
                ? <Alert type="warning" showIcon
                    message="当前服务版本尚未返回 PN 明细；订单概要仍可查看，请待后端升级完成后重试。" />
                : <PartsTable side={side} parts={parts} dateRange={range}
                    costRestricted={costRestricted} manualRestricted={manualRestricted} />;
            },
          }}
          pagination={{
            current: data?.page ?? 1, pageSize, total: data?.total ?? 0,
            showSizeChanger: true, pageSizeOptions: PAGE_SIZE_OPTIONS,
            showTotal: (t) => `共 ${t} 单`,
          }} />
      )}
    </Card>
  );
}
