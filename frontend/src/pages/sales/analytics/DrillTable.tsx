// 大单列表：按所选金额键倒序（后端固定 DESC NULLS LAST + id DESC，只倒序不切升序），
// 每单挂最新对账快照块（D-40 §4-§5）。排序以响应 sort 回显为准——金额键被
// data_purchase_cost 遮蔽时后端静默回退 revenue（行序防泄漏），前端如实提示并
// 预先禁用金额排序入口，不发注定被回退的新请求。
import { Alert, Table, Tag, Tooltip } from "antd";
import type { ColumnsType, TablePaginationConfig } from "antd/es/table";
import type { FilterValue, SorterResult } from "antd/es/table/interface";
import { fmtDateTime, fmtMoney } from "../shared";
import type {
  SalesAnalyticsOrderRow,
  SalesAnalyticsOrdersResp,
  SalesAnalyticsSortKey,
  SalesReconciliationBlock,
} from "../../../api/salesAnalytics";
import { AMOUNT_SORT_KEY, type AmountKey } from "./urlState";

export interface DrillTableProps {
  data: SalesAnalyticsOrdersResp | null;
  loading: boolean;
  /** 展示口径（D-41 §2：三金额多选并排；排序只能落已显示口径）。 */
  amounts: AmountKey[];
  /** 请求发出的排序键（默认不含税金额，不落 URL）。 */
  sort: SalesAnalyticsSortKey;
  /** 与抽屉同一开关：开时 reconciled_display = 已对账 + 退货已对账，列头随之改标。 */
  includeReturns: boolean;
  /** data_purchase_cost=false：金额排序入口预先禁用（行序是隐藏金额排名的侧信道）。 */
  costRestricted: boolean;
  /** data_reconciliation=false：对账字段不可见，按「无对账数据」展示（不显示 0）。 */
  reconRestricted: boolean;
  page: number;
  pageSize: number;
  onSortChange: (sort: SalesAnalyticsSortKey) => void;
  onPageChange: (page: number, pageSize: number) => void;
  onOpenOrder: (orderId: number) => void;
}

export default function DrillTable({
  data,
  loading,
  amounts,
  sort,
  includeReturns,
  costRestricted,
  reconRestricted,
  page,
  pageSize,
  onSortChange,
  onPageChange,
  onOpenOrder,
}: DrillTableProps) {
  // 生效排序以响应回显为准（被遮蔽时后端已回退 revenue）；未返回前按请求键显示
  const effectiveSort = data?.sort ?? sort;
  const sortFellBack = data != null && data.sort !== sort;
  // 本地预判：账号看不见金额却请求了金额键排序（权限快照与后端一致时后端必回退）
  const amountSortHidden = costRestricted && sort !== "revenue";
  // 金额列可排 = 该口径正显示 + 账号看得见金额（revenue 恒公开）
  const sortable = (key: AmountKey) =>
    amounts.includes(key) && (key === "revenue" || !costRestricted);

  const reconNoData = (b: SalesReconciliationBlock) => !b.has_data || reconRestricted;

  const amountColumn = (key: AmountKey, title: string) => {
    const sortKey = AMOUNT_SORT_KEY[key];
    return {
      title: (
        <Tooltip title={sortable(key) ? "按此金额倒序（缺失值沉底，同额按单号先后破并列）" : "金额不可见，金额排序不可用"}>
          {title}
        </Tooltip>
      ),
      key: sortKey,
      width: 120,
      align: "right" as const,
      dataIndex: sortKey,
      render: (v: number | null) => fmtMoney(v),
      sorter: sortable(key),
      // 后端只支持倒序：单一排序方向，点击=切换排序键，不提供升序/取消
      sortDirections: ["descend" as const],
      sortOrder: effectiveSort === sortKey ? ("descend" as const) : undefined,
    };
  };

  const columns: ColumnsType<SalesAnalyticsOrderRow> = [
    {
      title: "订单日期",
      dataIndex: "order_date",
      width: 122,
      render: (v: string | null, r) => (
        <span style={{ whiteSpace: "nowrap" }}>
          {v || "—"}
          {r.is_future && (
            <Tooltip title="订单日期晚于今天（本页不排除未来单，仅打标）">
              <Tag color="orange" style={{ marginLeft: 4, marginRight: 0, paddingInline: 4 }}>未来</Tag>
            </Tooltip>
          )}
        </span>
      ),
    },
    { title: "订单号", dataIndex: "order_no", width: 120 },
    { title: "业务类型", dataIndex: "business_type", width: 96, render: (v: string | null) => v || "—" },
    { title: "销售人员", dataIndex: "salesperson", width: 88, render: (v: string | null) => v || "—" },
    { title: "客户", dataIndex: "customer", ellipsis: true, render: (v: string | null) => v || "—" },
    ...(amounts.includes("ex") ? [amountColumn("ex", "不含税金额")] : []),
    ...(amounts.includes("inc") ? [amountColumn("inc", "含税订单金额")] : []),
    ...(amounts.includes("revenue") ? [amountColumn("revenue", "营收")] : []),
    {
      // 开「含已退货」时该列数值 = 已对账 + 退货已对账（D-40 §5），列头必须随之改标，
      // 否则一眼读数会把含退货合计误读为已对账金额本身（与抽屉内标注同一口径）
      title: (
        <Tooltip title="已对账金额 = 实际回款（D-40）。开「含已退货」时 = 已对账 + 退货已对账（相加，不扣减）。">
          {includeReturns ? "已对账金额（含退货）" : "已对账金额"}
        </Tooltip>
      ),
      key: "reconciled_display",
      width: includeReturns ? 140 : 110,
      align: "right",
      render: (_v, r) =>
        reconNoData(r.reconciliation)
          ? <span style={{ color: "var(--mb-text-3)" }}>无对账数据</span>
          : fmtMoney(r.reconciliation.reconciled_display),
    },
    {
      title: "待对账金额",
      key: "pending_amount",
      width: 104,
      align: "right",
      render: (_v, r) =>
        reconNoData(r.reconciliation) ? "—" : fmtMoney(r.reconciliation.pending_amount),
    },
    {
      title: "退货已对账金额",
      key: "return_reconciled_amount",
      width: 118,
      align: "right",
      render: (_v, r) =>
        reconNoData(r.reconciliation) ? "—" : fmtMoney(r.reconciliation.return_reconciled_amount),
    },
    {
      title: "数据截至时间",
      key: "reconciled_as_of",
      width: 128,
      render: (_v, r) =>
        reconNoData(r.reconciliation) ? "—" : fmtDateTime(r.reconciliation.reconciled_as_of),
    },
  ];

  const onTableChange = (
    pg: TablePaginationConfig,
    _filters: Record<string, FilterValue | null>,
    sorter: SorterResult<SalesAnalyticsOrderRow> | SorterResult<SalesAnalyticsOrderRow>[],
    extra: { action: "paginate" | "sort" | "filter" },
  ) => {
    // antd 5：翻页与排序都从 Table onChange 走（pagination 配置里的 onChange 只在
    // reset 路径触发），必须按 extra.action 分流——否则翻页会被排序分支吞掉
    if (extra.action === "paginate") {
      onPageChange(pg.current ?? 1, pg.pageSize ?? pageSize);
      return;
    }
    if (extra.action !== "sort") return;
    const s = Array.isArray(sorter) ? sorter[0] : sorter;
    if (!s || !s.column || s.order !== "descend") return; // 只认倒序（后端契约）
    onSortChange(s.field as SalesAnalyticsSortKey);
  };

  const pagination: TablePaginationConfig = {
    current: page,
    pageSize,
    total: data?.total ?? 0,
    showSizeChanger: true,
    pageSizeOptions: [20, 50, 100, 200], // 后端上限 200
    showTotal: (t) => `共 ${t} 单`,
  };

  return (
    <div>
      {(sortFellBack || amountSortHidden) && (
        <Alert
          style={{ marginBottom: 8 }}
          type="info"
          showIcon
          message={
            sortFellBack
              ? "金额字段不可见：已按营收（公开口径）倒序，行序不反映隐藏金额的排名。"
              : "金额字段不可见：行级金额列显示「—」，金额排序不可用；请求金额键排序时后端将按营收倒序。"
          }
        />
      )}
      <Table<SalesAnalyticsOrderRow>
        size="small"
        rowKey="order_id"
        loading={loading}
        dataSource={data?.items ?? []}
        columns={columns}
        pagination={pagination}
        onChange={onTableChange}
        locale={{ emptyText: "当前筛选下没有销售单" }}
        onRow={(r) => ({
          style: { cursor: "pointer" },
          onClick: () => onOpenOrder(r.order_id),
          // 键盘可达：行可聚焦，Enter/Space 打开详情（不覆写 role=button——会顶掉 tr 的行语义）
          tabIndex: 0,
          "aria-label": `查看销售单 ${r.order_no} 详情`,
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
