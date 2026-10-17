// 下钻层：分组值 → 订单列表（服务端分页 + 排序；筛选条件随层传递）。
// 金额排序禁用要「预判」而非被动回显：后端 ranking_restricted 只在本次请求真的
// 试图按金额排时才为 true（默认 order_date 下受限账号也返回 false），若只看响应旗标，
// 受限账号首屏金额列仍可点击并发出注定被回退的请求。因此本地 costRestricted 一并参与。
import { Alert, Button, Table, Tooltip } from "antd";
import type { ColumnsType, TablePaginationConfig } from "antd/es/table";
import type { FilterValue, SorterResult } from "antd/es/table/interface";
import { fmt, fmtMoney } from "../shared";
import type {
  PurchaseAnalyticsOrderRow,
  PurchaseAnalyticsOrdersResp,
} from "../../../api/purchaseAnalytics";
import type { AnalyticsSort } from "./urlState";

export interface DrillTableProps {
  data: PurchaseAnalyticsOrdersResp | null;
  loading: boolean;
  basis: string[];
  /** 金额不可见账号（data_purchase_cost=false）：金额排序入口预先禁用。 */
  costRestricted: boolean;
  groupByLabel: string;
  groupKey: string;
  sort: AnalyticsSort;
  dir: "asc" | "desc";
  page: number;
  pageSize: number;
  onSortChange: (sort: AnalyticsSort, dir: "asc" | "desc") => void;
  onPageChange: (page: number, pageSize: number) => void;
  onOpenOrder: (orderId: number) => void;
  onBack: () => void;
}

type AmountSortKey = "amount_inc" | "amount_ex" | "amount_paid";

export default function DrillTable({
  data,
  loading,
  basis,
  costRestricted,
  groupByLabel,
  groupKey,
  sort,
  dir,
  page,
  pageSize,
  onSortChange,
  onPageChange,
  onOpenOrder,
  onBack,
}: DrillTableProps) {
  const restricted = costRestricted || data?.ranking_restricted === true;

  const amountColumn = (key: AmountSortKey, title: string) => ({
    title: (
      <Tooltip title={restricted ? "金额不可见，金额排序不可用（已回退按日期排序）" : undefined}>
        {title}
      </Tooltip>
    ),
    key,
    width: 120,
    align: "right" as const,
    dataIndex: key,
    render: (v: number | null) => fmtMoney(v),
    // 受限时不可排：行序本身是隐藏金额排名的侧信道（后端已回退，前端不再给出入口）
    sorter: !restricted,
    sortOrder: sort === key ? (dir === "desc" ? ("descend" as const) : ("ascend" as const)) : undefined,
  });

  const columns: ColumnsType<PurchaseAnalyticsOrderRow> = [
    {
      title: "采购日期",
      dataIndex: "order_date",
      width: 110,
      sorter: true,
      sortOrder: sort === "order_date" ? (dir === "desc" ? ("descend" as const) : ("ascend" as const)) : undefined,
      render: (v: string | null) => v || "—",
    },
    { title: "采购单号", dataIndex: "order_no", width: 120 },
    { title: "采购类型", dataIndex: "source_type", width: 100, render: (v: string | null) => v || "—" },
    { title: "采购员", dataIndex: "purchaser", width: 90, render: (v: string | null) => v || "—" },
    { title: "销售人员", dataIndex: "salesperson", width: 90, render: (v: string | null) => v || "—" },
    { title: "客户", dataIndex: "customer", ellipsis: true, render: (v: string | null) => v || "—" },
    { title: "明细行数", dataIndex: "lines", width: 84, align: "right", render: (v: number | null) => fmt(v) },
    ...(basis.includes("inc") ? [amountColumn("amount_inc", "含税金额")] : []),
    ...(basis.includes("ex") ? [amountColumn("amount_ex", "不含税金额")] : []),
    ...(basis.includes("paid") ? [amountColumn("amount_paid", "实付金额")] : []),
  ];

  const onTableChange = (
    _pg: TablePaginationConfig,
    _filters: Record<string, FilterValue | null>,
    sorter: SorterResult<PurchaseAnalyticsOrderRow> | SorterResult<PurchaseAnalyticsOrderRow>[],
  ) => {
    const s = Array.isArray(sorter) ? sorter[0] : sorter;
    if (!s || !s.column || !s.order) return;
    const key = s.field as AnalyticsSort;
    onSortChange(key, s.order === "ascend" ? "asc" : "desc");
  };

  const pagination: TablePaginationConfig = {
    current: page,
    pageSize,
    total: data?.total ?? 0,
    showSizeChanger: true,
    pageSizeOptions: [20, 50, 100, 200],
    showTotal: (t) => `共 ${t} 单`,
    onChange: (p, ps) => onPageChange(p, ps),
  };

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
        <Button onClick={onBack}>← 返回分组</Button>
        <span style={{ fontWeight: 500 }}>
          {groupByLabel}：<span style={{ color: "var(--mb-text)" }}>{groupKey}</span>
          {data ? <span style={{ color: "var(--mb-text-3)", fontWeight: 400 }}> · 共 {data.total} 单</span> : null}
        </span>
      </div>
      {restricted && (
        <Alert
          style={{ marginBottom: 8 }}
          type="info"
          showIcon
          message="金额字段不可见：已按采购日期排序（金额排序不可用，行序不会反映隐藏金额的排名）。"
        />
      )}
      <Table<PurchaseAnalyticsOrderRow>
        size="small"
        rowKey="order_id"
        loading={loading}
        dataSource={data?.items ?? []}
        columns={columns}
        pagination={pagination}
        onChange={onTableChange}
        locale={{ emptyText: "该分组下没有符合条件的采购单" }}
        onRow={(r) => ({
          style: { cursor: "pointer" },
          onClick: () => onOpenOrder(r.order_id),
          // 键盘可达：行可聚焦，Enter/Space 打开明细（不覆写 role=button——会顶掉 tr 的行语义）
          tabIndex: 0,
          "aria-label": `查看采购单 ${r.order_no} 明细`,
          onKeyDown: (e: React.KeyboardEvent) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onOpenOrder(r.order_id);
            }
          },
        })}
      />
    </div>
  );
}
