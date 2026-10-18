// 销售分析页（D-41 客户需求 Round1，PR-4b）：区间 KPI（销售单数 + 三金额合计）+
// 大单倒序列表（每单挂最新对账快照块，D-40）+ 订单详情抽屉（对账历史）。
// 筛选/日期预设/金额口径/排序/分页/含退货/抽屉全部写 URL，可分享可刷新。
// 权限：page_sales_analysis（模板仅 admin/boss）——路由/菜单由 nav.tsx 统一登记；
// data_* 受限只影响控件门控与「—」展示，数据面真值永远是后端脱敏 + sort 回显。
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Alert, Card, message } from "antd";
import PageHeader from "../../../components/PageHeader";
import { businessToday } from "../../../utils/date";
import {
  fetchSalesAnalyticsOptions,
  fetchSalesAnalyticsOrders,
  fetchSalesAnalyticsSummary,
  type SalesAnalyticsBusinessTypeOption,
  type SalesAnalyticsOrdersResp,
  type SalesAnalyticsSortKey,
  type SalesAnalyticsSummary,
} from "../../../api/salesAnalytics";
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
  const [sp, setSp] = useSearchParams();
  const state = parseSalesAnalyticsUrl(sp);
  const patch = useSalesAnalyticsPatch(sp, setSp);
  const today = businessToday();

  const isAdmin = (localStorage.getItem("role") || "") === "admin";
  const perms = useMemo(readLocalPerms, []);
  // admin 恒全开；其余按登录快照（undefined = 未显式关闭，按不受限处理）
  const costRestricted = !isAdmin && perms.data_purchase_cost === false;
  const reconRestricted = !isAdmin && perms.data_reconciliation === false;
  const customerRestricted = !isAdmin && perms.data_customer === false;

  // 客户筛选受限：后端对带 customer 的请求 fail-closed 403，请求参数先收敛
  // （URL 里的残值保留可分享，控件禁用并提示，不让入口发出注定 403 的请求）
  const effectiveState = {
    ...state,
    customer: customerRestricted ? null : state.customer,
  };

  // 请求排序键：D-41 §2 只按已显示金额倒序——排序键对应的金额口径未显示时
  // 收敛到首个已显示口径（纯展示切换不触发重拉，仅这一种边缘会换键重拉）
  const sortAmount = (Object.keys(AMOUNT_SORT_KEY) as AmountKey[]).find(
    (k) => AMOUNT_SORT_KEY[k] === state.sort,
  );
  const requestSort: SalesAnalyticsSortKey =
    sortAmount && state.amounts.includes(sortAmount)
      ? state.sort
      : AMOUNT_SORT_KEY[state.amounts[0]];

  const [businessTypeOptions, setBusinessTypeOptions] = useState<SalesAnalyticsBusinessTypeOption[]>([]);
  const [summary, setSummary] = useState<SalesAnalyticsSummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [orders, setOrders] = useState<SalesAnalyticsOrdersResp | null>(null);
  const [ordersLoading, setOrdersLoading] = useState(true);
  const [ordersError, setOrdersError] = useState<string | null>(null);
  const [pageSize, setPageSize] = useState(50);
  const [retryTick, setRetryTick] = useState(0);
  const summarySeq = useRef(0);
  const ordersSeq = useRef(0);

  // 业务类型候选取值（与筛选条件无关，一次拉取）
  useEffect(() => {
    let alive = true;
    fetchSalesAnalyticsOptions()
      .then(({ data }) => { if (alive) setBusinessTypeOptions(data.business_types); })
      .catch((err) => { if (alive) message.error(`筛选取值加载失败：${apiErrorMessage(err)}`); });
    return () => { alive = false; };
  }, []);

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

  useEffect(() => {
    const seq = ++summarySeq.current;
    setSummaryLoading(true);
    setSummaryError(null);
    fetchSalesAnalyticsSummary(apiFilters)
      .then(({ data }) => {
        if (seq !== summarySeq.current) return;
        setSummary(data);
      })
      .catch((err) => {
        if (seq !== summarySeq.current) return;
        setSummary(null);
        setSummaryError(apiErrorMessage(err));
      })
      .finally(() => {
        if (seq === summarySeq.current) setSummaryLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- apiFilters 以 filterKey 进入依赖
  }, [filterKey, retryTick]);

  // 大单倒序列表：筛选 + 生效排序 + 分页 + 含退货。排序键看响应回显（被遮蔽时
  // 后端已回退 revenue），DrillTable 据此高亮列头并如实提示。
  useEffect(() => {
    const seq = ++ordersSeq.current;
    setOrdersLoading(true);
    setOrdersError(null);
    fetchSalesAnalyticsOrders({
      ...apiFilters,
      sort: requestSort,
      page: state.page,
      page_size: pageSize,
      include_returns: state.includeReturns,
    })
      .then(({ data }) => {
        if (seq !== ordersSeq.current) return;
        setOrders(data);
        // 过期分享链的页码可能超出总页数（后端只校验 page≥1，不按 total 钳制）：
        // 空态文案与分页高亮都会失真。按 total 收敛回末页；updater 形式取最新
        // URL，避免迟到的响应用旧闭包覆盖用户刚改的其他键。state.page 变化自然
        // 触发重拉（收敛后 page≤maxPage，不会循环）。
        const maxPage = Math.max(1, Math.ceil(data.total / pageSize));
        if (state.page > maxPage) {
          setSp(
            (prev) => {
              const next = new URLSearchParams(prev);
              if (maxPage > 1) next.set("page", String(maxPage));
              else next.delete("page");
              return next;
            },
            { replace: true },
          );
        }
      })
      .catch((err) => {
        if (seq !== ordersSeq.current) return;
        setOrders(null);
        setOrdersError(apiErrorMessage(err));
      })
      .finally(() => {
        if (seq === ordersSeq.current) setOrdersLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- apiFilters 以 filterKey 进入依赖
  }, [filterKey, requestSort, state.page, state.includeReturns, pageSize, retryTick]);

  return (
    <>
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
      <Card>
        <FilterBar
          state={effectiveState}
          patch={patch}
          businessTypeOptions={businessTypeOptions}
          customerRestricted={customerRestricted}
          reconRestricted={reconRestricted}
          onReset={() => setSp(new URLSearchParams(), { replace: true })}
        />

        {summaryError ? (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 12 }}
            message="汇总加载失败"
            description={summaryError}
            action={<a onClick={() => setRetryTick((t) => t + 1)}>重试</a>}
          />
        ) : (
          <SummaryView summary={summaryLoading ? null : summary} amounts={state.amounts} />
        )}

        {ordersError ? (
          <Alert
            type="error"
            showIcon
            message="销售单列表加载失败"
            description={ordersError}
            action={<a onClick={() => setRetryTick((t) => t + 1)}>重试</a>}
          />
        ) : (
          <DrillTable
            data={orders}
            loading={ordersLoading}
            amounts={state.amounts}
            sort={requestSort}
            includeReturns={state.includeReturns}
            costRestricted={costRestricted}
            reconRestricted={reconRestricted}
            page={state.page}
            pageSize={pageSize}
            onSortChange={(sort) => patch({ sort, page: 1 })}
            onPageChange={(page, size) => {
              setPageSize(size);
              patch({ page });
            }}
            onOpenOrder={(orderId) => patch({ orderId })}
          />
        )}
      </Card>

      <DetailDrawer
        orderId={state.orderId}
        includeReturns={state.includeReturns}
        amounts={state.amounts}
        reconRestricted={reconRestricted}
        onClose={() => patch({ orderId: null })}
      />
    </>
  );
}
