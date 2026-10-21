import { ANALYTICS_PAGE_SIZES } from "../../../utils/analyticsUrlState";
// 订单列表：任意列升序 / 降序（D-44 §4，后端 NULLS LAST + id 同向破并列），每单挂
// 最新对账快照块（D-40 §4-§5）。排序以响应 sort 回显为准——被遮蔽的金额 / 毛利 /
// 客户 / 对账键后端一律回退 order_date（Q-A-04 行序防泄漏，契约 §10.3），前端如实
// 提示并预先禁用对应排序入口，不发注定被回退的新请求。所有实际可见数据列双向排序
// （A-AC07：订单号 / 业务类型 / 客户类型 / 对账块四列在内——含按最新快照原值排序的
// 退货已对账金额）。
import { Alert, Table, Tag, Tooltip } from "antd";
import type { ColumnsType, TablePaginationConfig } from "antd/es/table";
import type { FilterValue, SorterResult } from "antd/es/table/interface";
import { fmtDateTime, fmtMoney, isReconBlockEmpty } from "../shared";
import type {
  SalesAnalyticsOrderRow,
  SalesAnalyticsOrdersResp,
  SalesAnalyticsSortKey,
  SalesReconciliationBlock,
  SortOrder,
} from "../../../api/salesAnalytics";
import { pct } from "../../../utils/format";
import { AMOUNT_SORT_KEY, type AmountKey } from "./urlState";

export interface DrillTableProps {
  data: SalesAnalyticsOrdersResp | null;
  loading: boolean;
  /** 展示口径（D-41 §2：三金额多选并排；排序只能落已显示口径）。 */
  amounts: AmountKey[];
  /** 请求发出的排序键（默认不含税金额，不落 URL）。 */
  sort: SalesAnalyticsSortKey;
  order: SortOrder;
  /** data_profit=false：毛利列显示「—」，毛利排序不可用。 */
  profitRestricted?: boolean;
  /** data_customer=false：客户排序不可用。 */
  customerRestricted?: boolean;
  /** 选了商品大类 / 品牌（行级口径）：金额是匹配行合计，不含税为名义值。 */
  lineMode?: boolean;
  /** 与抽屉同一开关：开时 reconciled_display = 已对账 + 退货已对账，列头随之改标。 */
  includeReturns: boolean;
  /** data_purchase_cost=false：金额排序入口预先禁用（行序是隐藏金额排名的侧信道）。 */
  costRestricted: boolean;
  /** data_reconciliation=false：对账字段不可见，按「无对账数据」展示（不显示 0）。 */
  reconRestricted: boolean;
  page: number;
  pageSize: number;
  onSortChange: (sort: SalesAnalyticsSortKey, order: SortOrder) => void;
  onPageChange: (page: number, pageSize: number) => void;
  onOpenOrder: (orderId: number) => void;
}

export default function DrillTable({
  data,
  loading,
  amounts,
  sort,
  order,
  profitRestricted = false,
  customerRestricted = false,
  lineMode = false,
  includeReturns,
  costRestricted,
  reconRestricted,
  page,
  pageSize,
  onSortChange,
  onPageChange,
  onOpenOrder,
}: DrillTableProps) {
  // 生效排序以响应回显为准（被遮蔽时后端已回退 order_date）；未返回前按请求键显示
  const effectiveSort = data?.sort ?? sort;
  const effectiveOrder: SortOrder = data?.order ?? order;
  const sortFellBack = data != null && data.sort !== sort;
  const sortOrderOf = (key: SalesAnalyticsSortKey) =>
    effectiveSort === key ? (effectiveOrder === "asc" ? "ascend" as const : "descend" as const) : null;
  const sortableCol = (key: SalesAnalyticsSortKey, enabled: boolean) => ({
    key,
    sorter: enabled,
    sortDirections: ["descend" as const, "ascend" as const],
    sortOrder: sortOrderOf(key),
  });
  // 本地预判：账号看不见金额却请求了金额键排序（权限快照与后端一致时后端必回退）
  const amountSortHidden = costRestricted && (sort === "amount_ex_tax" || sort === "amount_inc_tax");
  // 金额列可排 = 该口径正显示 + 账号看得见金额（revenue 恒公开）
  const sortable = (key: AmountKey) =>
    amounts.includes(key) && (key === "revenue" || !costRestricted);

  const reconNoData = (b: SalesReconciliationBlock) => reconRestricted || isReconBlockEmpty(b);

  const amountColumn = (key: AmountKey, title: string) => {
    const sortKey = AMOUNT_SORT_KEY[key];
    return {
      title: (
        <Tooltip title={sortable(key) ? "点击切换降序 / 升序（缺失值恒沉底，同额按单号先后破并列）" : "金额不可见，金额排序不可用"}>
          {lineMode && key === "ex" ? `${title}（名义）` : title}
        </Tooltip>
      ),
      width: 120,
      align: "right" as const,
      dataIndex: sortKey,
      render: (v: number | null) => fmtMoney(v),
      ...sortableCol(sortKey, sortable(key)),
    };
  };

  const columns: ColumnsType<SalesAnalyticsOrderRow> = [
    {
      title: "订单日期",
      dataIndex: "order_date",
      width: 122,
      ...sortableCol("order_date", true),
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
    { title: "订单号", dataIndex: "order_no", width: 120, ...sortableCol("order_no", true) },
    { title: "业务类型", dataIndex: "business_type", width: 96, render: (v: string | null) => v || "—", ...sortableCol("business_type", true) },
    {
      title: "销售人员", dataIndex: "salesperson", width: 96,
      render: (v: string | null) => v || "—",
      ...sortableCol("salesperson", true),
    },
    {
      title: "客户", dataIndex: "customer", ellipsis: true,
      render: (v: string | null) => v || "—",
      ...sortableCol("customer", !customerRestricted),
    },
    ...(customerRestricted ? [] : [{
      // 客户类型列（A-AC07 可见即可排）：无 data_customer 时后端置 null，不渲染
      title: "客户类型", dataIndex: "customer_type", width: 96,
      render: (v: string | null) => v || "—",
      ...sortableCol("customer_type", !customerRestricted),
    }]),
    ...(amounts.includes("ex") ? [amountColumn("ex", "不含税金额")] : []),
    ...(amounts.includes("inc") ? [amountColumn("inc", "含税订单金额")] : []),
    ...(amounts.includes("revenue") ? [amountColumn("revenue", "营收")] : []),
    {
      title: (
        <Tooltip title="毛利 = 已配成本营收 − 成本（与老板看板同口径）；未配成本的单为空，不当 0">
          毛利
        </Tooltip>
      ),
      dataIndex: "gross_profit",
      width: 120,
      align: "right" as const,
      render: (v: number | null | undefined, r: SalesAnalyticsOrderRow) => (
        profitRestricted ? "—" : (
          <span>
            {fmtMoney(v ?? null)}
            {r.gross_margin != null && (
              <span style={{ color: "var(--mb-text-3)", marginLeft: 4, fontSize: 12 }}>{pct(r.gross_margin)}</span>
            )}
          </span>
        )
      ),
      ...sortableCol("gross_profit", !profitRestricted),
    },
    {
      // 开「含已退货」时该列数值 = 已对账 + 退货已对账（D-40 §5），列头必须随之改标，
      // 否则一眼读数会把含退货合计误读为已对账金额本身（与抽屉内标注同一口径）
      title: (
        <Tooltip title="已对账金额 = 实际回款（D-40）。开「含已退货」时 = 已对账 + 退货已对账（相加，不扣减）。">
          {includeReturns ? "已对账金额（含退货）" : "已对账金额"}
        </Tooltip>
      ),
      width: includeReturns ? 140 : 110,
      align: "right",
      render: (_v, r) =>
        reconNoData(r.reconciliation)
          ? <span style={{ color: "var(--mb-text-2)" }}>无对账数据</span>
          : fmtMoney(r.reconciliation.reconciled_display),
      // A-AC07：对账块可见数据列双向排序；无 data_reconciliation 时后端回退
      // order_date，前端预先禁用排序入口
      ...sortableCol("reconciled_display", !reconRestricted),
    },
    {
      title: "待对账金额",
      width: 104,
      align: "right",
      render: (_v, r) =>
        reconNoData(r.reconciliation) ? "—" : fmtMoney(r.reconciliation.pending_amount),
      ...sortableCol("pending_amount", !reconRestricted),
    },
    {
      title: "退货已对账金额",
      width: 118,
      align: "right",
      // 按最新快照的退货已对账原值排序，不随 include_returns（那只改 reconciled_display
      // 的展示定义，契约 §3）；无对账权限同回退 order_date
      ...sortableCol("return_reconciled_amount", !reconRestricted),
      render: (_v, r) =>
        reconNoData(r.reconciliation) ? "—" : fmtMoney(r.reconciliation.return_reconciled_amount),
    },
    {
      title: "数据截至时间",
      width: 128,
      render: (_v, r) =>
        reconNoData(r.reconciliation) ? "—" : fmtDateTime(r.reconciliation.reconciled_as_of),
      ...sortableCol("reconciled_as_of", !reconRestricted),
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
    if (!s || !s.column) return;
    const key = (s.columnKey ?? s.field) as SalesAnalyticsSortKey;
    // 取消排序（第三次点击）不提供：回到该列降序，后端永远有确定序
    onSortChange(key, s.order === "ascend" ? "asc" : "desc");
  };

  const pagination: TablePaginationConfig = {
    current: page,
    pageSize,
    total: data?.total ?? 0,
    showSizeChanger: true,
    pageSizeOptions: [...ANALYTICS_PAGE_SIZES], // 后端上限 200
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
              ? "所选排序字段不可见：已改按订单日期排序，行序不反映隐藏字段的排名。"
              : "金额字段不可见：行级金额列显示「—」，金额排序不可用；请求金额键排序时后端将按订单日期排序。"
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
