import { useAuthorizedRunner, AuthorizationCancelled } from "../../../authz/access";
import { actionRule, routeRule, ruleAllowed, surfaceRule } from "../../../authz/rules";
import { useAsyncData } from "./useAsyncData";
import { AuthzUnavailable, salesAccess, useAuthz } from "../../../authz/AuthzProvider";
// 销售分析页（D-41 客户需求 Round1，PR-4b；D-44 销售看板扩展）：常驻区间 KPI 条
// （单数 / 三金额 / 毛利 / 客户与新老客 / 上月对比）+ 五个页签：概览（迷你趋势 +
// Top 5）、多维紧凑图（九维并列条形榜）、分组排行、趋势、订单明细（任意列升降序 +
// 对账块 + 导出）+ 订单详情抽屉。
// 筛选 / 日期预设 / 金额区间 / 金额口径 / 排序 / 分页 / 页签 / 含退货 / 抽屉全部写 URL，
// 可分享可刷新；默认页签为订单明细（沿用 D-41 打开即大单列表）。
// 权限：page_sales_analysis（模板仅 admin/boss）——路由/菜单由 nav.tsx 统一登记；
// data_* 受限只影响控件门控与「—」展示，数据面真值永远是后端脱敏 + sort 回显。
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Button, App, Alert, Card, ConfigProvider, Space, Switch, Tabs, Tooltip } from "antd";
import { DownloadOutlined } from "@ant-design/icons";
import PageHeader from "../../../components/PageHeader";
import { businessToday } from "../../../utils/date";
import { readSessionToken, SESSION_EXPIRED_MESSAGE, sessionStale, useSessionOwner } from "../../../utils/session";
import {
  downloadSalesOrdersCsv,
  fetchSalesAnalyticsOptions,
  fetchSalesAnalyticsOrders,
  fetchSalesAnalyticsSummary,
  type SalesAmountBasis,
  type SalesAnalyticsBusinessTypeOption,
  type SalesAnalyticsOptions,
  type SalesAnalyticsOrdersResp,
  type SalesAnalyticsSortKey,
  type SalesAnalyticsSummary,
} from "../../../api/salesAnalytics";
import type { SalesChartMetric } from "../../../components/charts/SalesDashboardCharts";
import CompactTab from "./CompactTab";
import GroupsTab from "./GroupsTab";
import OverviewTab from "./OverviewTab";
import TrendTab from "./TrendTab";
import AmountHistogram from "./AmountHistogram";
import { analyticsControlTheme } from "./dashboardColors";
import FilterBar from "./FilterBar";
import SummaryView from "./SummaryView";
import DrillTable from "./DrillTable";
import DetailDrawer from "./DetailDrawer";
import {
  AMOUNT_SORT_KEY,
  parseSalesAnalyticsUrl,
  toSalesApiFilters,
  useSalesAnalyticsPatch,
  type AmountKey,
  type SalesTab,
} from "./urlState";

/** 登录时落到 localStorage 的 data_* 权限快照（PR-4a 同款）：只用于首渲染 UI
 * 门控（禁用控件/预先禁排），数据面真值永远是后端脱敏 + 回显。 */
function readLocalPerms(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem("permissions") || "{}");
  } catch {
    return {};
  }
}

function apiErrorMessage(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  // 422 有两种形态：业务校验（date_from>date_to）detail 是字符串；FastAPI 参数
  // 校验（非法 sort 等）detail 是数组——后者退到通用文案
  if (typeof detail === "string" && detail) return detail;
  const status = (err as { response?: { status?: number } })?.response?.status;
  if (status === 403) return "没有访问权限（403）";
  if (status === 422) return "请求参数非法（422）";
  return "加载失败，请稍后重试";
}

export default function SalesAnalyticsPage() {
  const authz = useAuthz();
  if (authz.status !== "ready" || !authz.snapshot) return <AuthzUnavailable />;
  const access = salesAccess(authz.snapshot);
  if (!access || !ruleAllowed(authz.snapshot, routeRule("/sales/analytics"), { sales_analysis: true }))
    return <Alert type="warning" message="没有销售分析访问权限" description={access?.reason || "权限协议无效"} />;
  return <SalesAnalyticsContent key={authz.snapshot.version} />;
}

function SalesAnalyticsContent() {
  const authz = useAuthz();
  const access = salesAccess(authz.snapshot);
  const v2 = access?.enabled === true;
  const visible = (id: string) => ruleAllowed(authz.snapshot, surfaceRule(`sales_analysis.${id}`), { sales_analysis: true });
  const canExport = visible("export");
  const runAuthorized = useAuthorizedRunner();

  const { message } = App.useApp();
  const [sp, setSp] = useSearchParams();
  const parsed = parseSalesAnalyticsUrl(sp);
  const availableTabs: SalesTab[] = (["overview", "compact", "groups", "trend", "orders"] as SalesTab[]).filter(tab => visible(tab));
  const state = { ...parsed, tab: availableTabs.includes(parsed.tab) ? parsed.tab : availableTabs[0] || parsed.tab };
  const patch = useSalesAnalyticsPatch(sp, setSp);
  const today = businessToday();

  // 组件所属会话（DL-010 返工）：挂载时 token 是本页全部异步入口（KPI/列表/候选/
  // 直方图/四视图/抽屉/导出）的 owner，下传给子视图保证同一所属会话。静默换 token
  // 后旧会话的任何新请求一律拒发（sessionExpired），迟到响应仍由各处落地守卫作废。
  const ownerToken = useSessionOwner();
  const [sessionDead, setSessionDead] = useState(false);
  /** 所属会话失效：拒发请求并清旧会话上下文（金额汇总 / 筛选候选 / 订单列表与
   * 排序回显 / 导出中状态）。幂等；置死后面板换专用提示，重试不再发请求。 */
  const sessionExpired = () => {
    if (!sessionStale(ownerToken)) return false;
    notifyOwnerStale();
    return true;
  };
  /** 页级过期清理（DL-010 落地矩阵）：任何异步入口（页内 effect / useAsyncData /
   * 直方图 / 抽屉 / 客户搜索 debounce / 行点击下钻）检测到 owner 失效时都回调
   * 这里——检测到过期只 return 丢弃迟到响应还不够，旧会话数据必须从数据面清掉。
   * 幂等：仅在所属会话真失效（localStorage 已换 token）时动作，合法新会话的
   * 重新挂载不受影响。 */
  const notifyOwnerStale = () => {
    if (!sessionStale(ownerToken)) return;
    setSessionDead(true);
    setExporting(false);
  };

  const isAdmin = (localStorage.getItem("role") || "") === "admin";
  const perms = useMemo(readLocalPerms, []);
  // admin 恒全开；其余按登录快照（undefined = 未显式关闭，按不受限处理）
  const costRestricted = v2 ? !access.field_permissions.includes("data_purchase_cost") : !isAdmin && perms.data_purchase_cost === false;
  const reconRestricted = v2 ? !access.field_permissions.includes("data_reconciliation") : !isAdmin && perms.data_reconciliation === false;
  const customerRestricted = v2 ? !access.field_permissions.includes("data_customer") : !isAdmin && perms.data_customer === false;
  const profitRestricted = v2 ? !access.field_permissions.includes("data_profit") : !isAdmin && perms.data_profit === false;
  // 金额「显示」可见性：revenue 恒公开；不含税 / 含税随 data_purchase_cost（与后端同组）。
  // 金额「区间筛选」另按 costRestricted 整组门控——三口径一视同仁，营收无例外
  // （A-AC03 / Q-A-03 修订：区间结果的单数本身就泄漏金额分布）。
  const isBasisHidden = (basis: SalesAmountBasis) => basis !== "revenue" && costRestricted;
  const amountRangeHidden = costRestricted;
  const [trendMetric, setTrendMetric] = useState<SalesChartMetric>("amount_ex_tax");

  // 客户筛选受限：后端对带 customer 的请求 fail-closed 403，请求参数先收敛
  // （URL 里的残值保留可分享，控件禁用并提示，不让入口发出注定 403 的请求）
  const effectiveState = {
    ...state,
    customer: customerRestricted ? null : state.customer,
    customers: customerRestricted ? [] : state.customers,
    customerTypes: customerRestricted ? [] : state.customerTypes,
    // 无成本权限：金额区间整组收敛（含营收口径，A-AC03 无营收例外）
    ...(amountRangeHidden ? {
      amountMin: null, amountMax: null, includeNegative: true, includeEmpty: true,
    } : {}),
  };

  // 请求排序键：D-41 §2 只按已显示金额倒序——排序键对应的金额口径未显示时
  // 收敛到首个已显示口径（纯展示切换不触发重拉，仅这一种边缘会换键重拉）
  // 非金额排序键（毛利 / 日期 / 客户 / 销售，D-44 §4）不受口径显示影响
  const sortAmount = (Object.keys(AMOUNT_SORT_KEY) as AmountKey[]).find(
    (k) => AMOUNT_SORT_KEY[k] === state.sort,
  );
  const requestSort: SalesAnalyticsSortKey =
    !sortAmount || state.amounts.includes(sortAmount)
      ? state.sort
      : AMOUNT_SORT_KEY[state.amounts[0]];

  const [exporting, setExporting] = useState(false);
  const pageSize = state.pageSize;
  const [retryTick, setRetryTick] = useState(0);

  // KPI。依赖是解析后的筛选键（金额口径多选不在内：切换口径是纯展示，不重新请求；
  // 含退货开关只影响列表 reconciled_display，也不触发 KPI 重拉）。todayKey 进依赖：
  // 跨零点后（重试/翻页等触发重拉）不会沿用前一天的「今天」窗口。
  const todayKey = today.format("YYYY-MM-DD");
  const apiFilters = useMemo(
    () => toSalesApiFilters(effectiveState, today),
    // sp 变化时重算；实际重拉由 filterKey（筛选序列化键）控制，口径/抽屉参数不触发
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 以 sp.toString() 为依赖
    [sp.toString(), customerRestricted, todayKey],
  );
  const filterKey = JSON.stringify(apiFilters);

  const optionsQuery = useAsyncData<SalesAnalyticsOptions>(
    context => fetchSalesAnalyticsOptions(apiFilters, "", context), [filterKey], apiErrorMessage,
    !sessionDead, ownerToken, notifyOwnerStale, "read_options");
  const options = optionsQuery.data;
  const businessTypeOptions = options?.business_types ?? [];
  const summaryVisible = visible("summary_counts") || visible("summary_amounts") || visible("summary_profit");
  const summaryQuery = useAsyncData<SalesAnalyticsSummary>(
    context => fetchSalesAnalyticsSummary(apiFilters, context), [filterKey, retryTick], apiErrorMessage,
    summaryVisible && !sessionDead, ownerToken, notifyOwnerStale, "read_summary");
  const { data: summary, loading: summaryLoading, error: summaryError } = summaryQuery;
  const ordersQuery = useAsyncData<SalesAnalyticsOrdersResp>(
    context => fetchSalesAnalyticsOrders({ ...apiFilters, sort: requestSort, order: state.order,
      page: state.page, page_size: pageSize, include_returns: state.includeReturns }, context),
    [filterKey, requestSort, state.order, state.page, state.includeReturns, pageSize, retryTick], apiErrorMessage,
    visible("orders") && state.tab === "orders" && !sessionDead, ownerToken, notifyOwnerStale, "read_orders");
  const { data: orders, loading: ordersLoading, error: ordersError } = ordersQuery;
  useEffect(() => {
    if (!orders) return;
    const maxPage = Math.max(1, Math.ceil(orders.total / pageSize));
    if (state.page > maxPage) setSp(prev => {
      const next = new URLSearchParams(prev);
      if (maxPage > 1) next.set("page", String(maxPage)); else next.delete("page");
      return next;
    }, { replace: true });
  }, [orders, pageSize, state.page, setSp]);

  const onExport = () => {
    if (!canExport) return;
    // 所属会话失效（静默换 token / 登出）：拒发并以页面级提示替代旧会话数据——
    // 不让旧会话的导出按钮以当前账号身份发请求（DL-010 返工）
    if (sessionExpired()) return;
    setExporting(true);
    void runAuthorized({
      rule: surfaceRule("sales_analysis.export"), legacy: { sales_analysis: true },
      queryKey: JSON.stringify([apiFilters, requestSort, state.order]),
      request: context => downloadSalesOrdersCsv(apiFilters, requestSort, state.order, context),
      commit: ({ data }) => {
        const url = URL.createObjectURL(data);
        const a = document.createElement("a");
        a.href = url; a.download = `销售单-${today.format("YYYYMMDD")}.csv`;
        a.click(); URL.revokeObjectURL(url); setExporting(false);
      },
    }).catch(err => {
      if (err instanceof AuthorizationCancelled) { notifyOwnerStale(); return; }
      if (sessionStale(ownerToken)) { notifyOwnerStale(); return; }
      setExporting(false);
      const status = err?.response?.status;
      message.error(status === 413 ? "超过导出上限 10,000 单，请收窄区间或筛选" : `导出失败：${apiErrorMessage(err)}`);
    });
  };

  const ordersPane = (
    ordersError ? (
          <Alert
            type="error"
            showIcon
            message="销售单列表加载失败"
            description={ordersError}
            action={<Button type="link" size="small" onClick={() => setRetryTick((t) => t + 1)}>重试</Button>}
          />
        ) : (
          <DrillTable
            data={orders}
            loading={ordersLoading}
            amounts={state.amounts}
            sort={requestSort}
            order={state.order}
            profitRestricted={profitRestricted}
            customerRestricted={customerRestricted}
            lineMode={summary?.line_mode === true}
            includeReturns={state.includeReturns}
            costRestricted={costRestricted}
            reconRestricted={reconRestricted}
            page={state.page}
            pageSize={pageSize}
            onSortChange={(sort, order) => patch({ sort, order, page: 1 })}
            onPageChange={(page, size) => {
              patch({ page, pageSize: size });
            }}
            onOpenOrder={(orderId) => {
              // 页级下钻入口（DL-010 旧行点击路径）：静默换 token 后点旧行，抽屉
              // 自身守卫会拒发并提示，但主页面旧数据也要一并清掉——不能只靠抽屉
              notifyOwnerStale();
              if (!visible("order_detail") || sessionExpired()) return;
              patch({ orderId }, { history: "push" });
            }}
          />
        )
  );

  const tabPatch = (next: Partial<typeof state>, opts?: { history?: "push" | "replace" }) => patch(next, opts);

  return (
    // A3/DL-025 返工：整页套看板控件局部主题（placeholder / disabled / 主色与
    // 链接 hover 文字），页内控件与弹层（portal 同 token 集）同享；详见
    // dashboardColors.analyticsControlTheme 注释。行为与权限语义不变。
    <ConfigProvider theme={analyticsControlTheme}>
      <PageHeader
        title="销售分析"
        subtitle={
          summary
            // as_of 是服务端今天（date-only，未来单打标基准），不是数据截止时点——
            // 「数据截至时间」是 D-40 术语（reconciled_as_of），此处不得撞名
            ? `销售单数与三金额（不含税 / 含税订单金额 / 营收），统计基准日 ${summary.as_of}`
            : "销售单数与三金额（不含税 / 含税订单金额 / 营收）"
        }
      />
      {v2 && <Alert style={{ marginBottom: 12 }} showIcon
        type={access.scope === "assigned_customers" && access.customer_count === 0 ? "warning" : "info"}
        message={access.scope === "all" ? "当前数据范围：全部客户" : access.customer_count === null ? "当前数据范围：负责客户" : `当前数据范围：负责的 ${access.customer_count} 个客户`}
        description={access.scope === "assigned_customers" && access.customer_count === 0
          ? "尚未分配客户，当前没有可见订单。请联系管理员设置客户归属。" : undefined} />}
      <Card>
        <FilterBar
          state={effectiveState}
          patch={patch}
          businessTypeOptions={businessTypeOptions}
          options={options}
          customerRestricted={customerRestricted}
          reconRestricted={reconRestricted}
          isBasisHidden={isBasisHidden}
          amountRangeHidden={amountRangeHidden}
          ownerToken={ownerToken}
          onOwnerStale={notifyOwnerStale}
          onReset={() => setSp(new URLSearchParams(), { replace: true })}
        />

        {visible("amount_histogram") && !amountRangeHidden && !sessionDead && (
          <AmountHistogram apiFilters={apiFilters} filterKey={filterKey} toMessage={apiErrorMessage}
            ownerToken={ownerToken} onOwnerStale={notifyOwnerStale} />
        )}

        {sessionDead ? (
          // 所属会话失效（DL-010 返工）：旧会话的汇总 / 候选 / 列表 / 导出上下文已清，
          // 数据面整体让位给会话提示——重试无济于事，刷新后以当前账号重新挂载
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={SESSION_EXPIRED_MESSAGE}
            description="已停止本页所有请求并清空旧会话的汇总、筛选候选、订单列表与导出状态；刷新页面后以当前账号继续（URL 中的筛选保持不变）。"
          />
        ) : summaryError ? (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 12 }}
            message="汇总加载失败"
            description={summaryError}
            action={<Button type="link" size="small" onClick={() => setRetryTick((t) => t + 1)}>重试</Button>}
          />
        ) : summaryVisible ? (
          <SummaryView
            summary={summaryLoading || !summary ? null : state.compare ? summary : { ...summary, compare: null, compare_available: undefined }}
            amounts={state.amounts}
            profitRestricted={profitRestricted || !visible("summary_profit")}
            showCounts={visible("summary_counts")} showAmounts={visible("summary_amounts")} />
        ) : null}

        {sessionDead ? null : (
        <Tabs
          activeKey={state.tab}
          onChange={(k) => patch({ tab: k as SalesTab })}
          tabBarExtraContent={(
            <Space>
              <Tooltip title="对比期 = 所选区间整体前移一个自然月；区间长于一个月时不提供（D-44 §9）">
                <span style={{ display: "inline-flex", gap: 6, alignItems: "center", whiteSpace: "nowrap" }}>
                  对比上月
                  <Switch size="small" aria-label="对比上月" checked={state.compare}
                    onChange={(v) => patch({ compare: v })} />
                </span>
              </Tooltip>
              {state.tab === "orders" && canExport && (
                <Tooltip title="按当前筛选与排序导出订单 CSV（上限 10,000 单；看不到的字段整列不导出）">
                  <Button size="small" icon={<DownloadOutlined />} loading={exporting} onClick={onExport}>
                    导出
                  </Button>
                </Tooltip>
              )}
            </Space>
          )}
          items={[
            {
              key: "overview", label: "概览",
              children: state.tab === "overview" && (
                <OverviewTab state={state} apiFilters={apiFilters} filterKey={filterKey} patch={tabPatch}
                  costRestricted={costRestricted} customerRestricted={customerRestricted} toMessage={apiErrorMessage}
                  ownerToken={ownerToken} onOwnerStale={notifyOwnerStale} />
              ),
            },
            {
              key: "compact", label: "多维紧凑图",
              children: state.tab === "compact" && (
                <CompactTab state={state} apiFilters={apiFilters} filterKey={filterKey} patch={tabPatch}
                  costRestricted={costRestricted} profitRestricted={profitRestricted}
                  customerRestricted={customerRestricted} toMessage={apiErrorMessage} ownerToken={ownerToken}
                  onOwnerStale={notifyOwnerStale} />
              ),
            },
            {
              key: "groups", label: "分组排行",
              children: state.tab === "groups" && (
                <GroupsTab state={state} apiFilters={apiFilters} filterKey={filterKey} patch={tabPatch}
                  costRestricted={costRestricted} profitRestricted={profitRestricted}
                  customerRestricted={customerRestricted} toMessage={apiErrorMessage} ownerToken={ownerToken}
                  onOwnerStale={notifyOwnerStale} />
              ),
            },
            {
              key: "trend", label: "趋势",
              children: state.tab === "trend" && (
                <TrendTab state={state} apiFilters={apiFilters} filterKey={filterKey} patch={tabPatch}
                  costRestricted={costRestricted} profitRestricted={profitRestricted}
                  toMessage={apiErrorMessage} metric={trendMetric} onMetricChange={setTrendMetric}
                  ownerToken={ownerToken} onOwnerStale={notifyOwnerStale} />
              ),
            },
            { key: "orders", label: "订单明细", children: state.tab === "orders" && ordersPane },
          ].filter(item => availableTabs.includes(item.key as SalesTab))}
        />
        )}
      </Card>

      {visible("order_detail") && <DetailDrawer
        showReconciliation={visible("reconciliation")}
        orderId={state.orderId}
        includeReturns={state.includeReturns}
        amounts={state.amounts}
        reconRestricted={reconRestricted || !visible("reconciliation")}
        costRestricted={costRestricted}
        ownerToken={ownerToken}
        onOwnerStale={notifyOwnerStale}
        onClose={() => patch({ orderId: null })}
      />}
    </ConfigProvider>
  );
}
