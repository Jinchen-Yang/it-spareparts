// 采购分析页（D-39 客户需求 Round1，PR-4a）：日期区间 + 类型/采购员/销售/客户/分类多选，
// 三口径（含税/不含税/实付）并排显示（本地选择，不动管理员 TaxBasis），分组 → 订单下钻 → 明细抽屉。
// 全部状态写 URL（preset/from/to/筛选/group_by/group_key/sort/dir/page/basis/order_id），可分享。
// 权限：page_purchase_analytics（模板仅 boss；admin 恒放行）——路由/菜单由 nav.tsx 统一登记。
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import dayjs from "dayjs";
import { Alert, Card, message } from "antd";
import PageHeader from "../../../components/PageHeader";
import {
  fetchPurchaseAnalyticsOptions,
  fetchPurchaseAnalyticsOrders,
  fetchPurchaseAnalyticsSummary,
  type PurchaseAnalyticsOptions,
  type PurchaseAnalyticsOrdersResp,
  type PurchaseAnalyticsSummary,
} from "../../../api/purchaseAnalytics";
import FilterBar, { type CustomerOption } from "./FilterBar";
import SummaryView, { groupByTitle } from "./SummaryView";
import DrillTable from "./DrillTable";
import OrderDetailDrawer from "./OrderDetailDrawer";
import {
  DEFAULT_GROUP_BY,
  parseAnalyticsUrl,
  toAnalyticsApiFilters,
  useAnalyticsPatch,
  type AnalyticsSort,
} from "./urlState";

/** 登录时落到 localStorage 的 data_* 权限快照（boss/shared.tsx 同款；只用于首渲染 UI
 * 门控，数据面真值永远是后端脱敏 + 回显旗标）。 */
function readLocalPerms(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem("permissions") || "{}");
  } catch {
    return {};
  }
}

function apiErrorMessage(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  const status = (err as { response?: { status?: number } })?.response?.status;
  if (status === 403) return "没有访问权限（403）";
  if (status === 422) return "请求参数非法（422）";
  return "加载失败，请稍后重试";
}

export default function PurchaseAnalyticsPage() {
  const [sp, setSp] = useSearchParams();
  const state = parseAnalyticsUrl(sp);
  const patch = useAnalyticsPatch(sp, setSp);
  const today = dayjs();

  const isAdmin = (localStorage.getItem("role") || "") === "admin";
  const perms = useMemo(readLocalPerms, []);
  // admin 恒全开；其余角色按登录快照（boss 模板默认双开，显式关掉的按受限处理）
  const costRestricted = !isAdmin && perms.data_purchase_cost === false;
  const customerRestricted = !isAdmin && perms.data_customer === false;

  // 客户权限受限时：客户筛选/分组对后端是 403，其下钻层（group_key 是客户 id，
  // 换维度后必是空集）一并收敛——控件显示、渲染层、请求参数全部同源，
  // 不允许「下拉显示客户、表格按采购类型」或「采购类型：12 · 共 0 单」的自相矛盾 UI。
  const customerDimBlocked = customerRestricted && state.groupBy === "customer";
  const effectiveState = {
    ...state,
    groupBy: customerDimBlocked ? DEFAULT_GROUP_BY : state.groupBy,
    customerIds: customerRestricted ? [] : state.customerIds,
    groupKey: customerDimBlocked ? null : state.groupKey,
    orderId: customerDimBlocked ? null : state.orderId,
  };
  const effectiveGroupBy = effectiveState.groupBy;

  const [options, setOptions] = useState<PurchaseAnalyticsOptions | null>(null);
  const [summary, setSummary] = useState<PurchaseAnalyticsSummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [orders, setOrders] = useState<PurchaseAnalyticsOrdersResp | null>(null);
  const [ordersLoading, setOrdersLoading] = useState(false);
  const [ordersError, setOrdersError] = useState<string | null>(null);
  const [customerOptions, setCustomerOptions] = useState<CustomerOption[]>([]);
  const [customerOptionsLoading, setCustomerOptionsLoading] = useState(false);
  const [pageSize, setPageSize] = useState(50);
  const [retryTick, setRetryTick] = useState(0);
  const summarySeq = useRef(0);
  const ordersSeq = useRef(0);

  // 筛选取值（与筛选条件无关，一次拉取）
  useEffect(() => {
    let alive = true;
    fetchPurchaseAnalyticsOptions()
      .then(({ data }) => { if (alive) setOptions(data); })
      .catch((err) => { if (alive) message.error(`筛选取值加载失败：${apiErrorMessage(err)}`); });
    return () => { alive = false; };
  }, []);

  // 客户下拉取值：options 端点未提供客户清单（PR-3a 缺口），用无筛选的客户分组汇总
  // 反查（全历史已生效）。data_customer 受限时后端 403，直接不拉。
  useEffect(() => {
    if (customerRestricted || !options) return;
    let alive = true;
    setCustomerOptionsLoading(true);
    fetchPurchaseAnalyticsSummary({ group_by: "customer" })
      .then(({ data }) => {
        if (!alive) return;
        const opts: CustomerOption[] = [];
        for (const g of data.groups) {
          if (g.key === options.no_linked_label) continue;
          const id = Number.parseInt(g.key, 10);
          if (Number.isInteger(id) && id > 0) {
            opts.push({ value: id, label: g.customer || g.key });
          }
        }
        opts.sort((a, b) => a.label.localeCompare(b.label, "zh"));
        setCustomerOptions(opts);
      })
      .catch(() => { if (alive) message.error("客户筛选取值加载失败"); })
      .finally(() => { if (alive) setCustomerOptionsLoading(false); });
    return () => { alive = false; };
  }, [customerRestricted, options]);

  // KPI + 分组汇总。依赖是解析后的筛选键（口径多选不在内：切换口径不触发重新请求）。
  // todayKey 进依赖：跨零点后（重试/改每页条数等触发重拉）不会沿用前一天的窗口。
  const todayKey = today.format("YYYY-MM-DD");
  const apiFilters = useMemo(
    () => toAnalyticsApiFilters(effectiveState, today),
    // sp 变化时重算；实际重拉由 filterKey（筛选序列化键）控制，口径/抽屉参数变化不触发
    [sp.toString(), customerRestricted, todayKey],
  );
  const filterKey = JSON.stringify(apiFilters);

  useEffect(() => {
    const seq = ++summarySeq.current;
    setSummaryLoading(true);
    setSummaryError(null);
    fetchPurchaseAnalyticsSummary({ ...apiFilters, group_by: effectiveGroupBy })
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
  }, [filterKey, effectiveGroupBy, retryTick]);

  // 分组下钻订单列表（筛选随层传递）
  useEffect(() => {
    if (effectiveState.groupKey == null) {
      setOrders(null);
      setOrdersError(null);
      return;
    }
    const seq = ++ordersSeq.current;
    setOrdersLoading(true);
    setOrdersError(null);
    fetchPurchaseAnalyticsOrders({
      ...apiFilters,
      group_by: effectiveGroupBy,
      group_key: effectiveState.groupKey,
      sort: effectiveState.sort,
      order: effectiveState.dir,
      page: effectiveState.page,
      page_size: pageSize,
    })
      .then(({ data }) => {
        if (seq !== ordersSeq.current) return;
        setOrders(data);
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
  }, [filterKey, effectiveGroupBy, effectiveState.groupKey, effectiveState.sort, effectiveState.dir, effectiveState.page, pageSize, retryTick]);

  const basis = state.basis;
  const lineMode = (effectiveState.groupKey == null ? summary?.basis_mode : orders?.basis_mode) === "line";

  return (
    <>
      <PageHeader
        title="采购分析"
        subtitle={summary ? `三口径采购金额（含税 / 不含税 / 实付），数据截至 ${summary.as_of}` : "三口径采购金额（含税 / 不含税 / 实付）"}
      />
      <Card>
        <FilterBar
          state={effectiveState}
          patch={patch}
          sourceTypes={options?.source_types ?? []}
          purchasers={options?.purchasers ?? []}
          salespersons={options?.salespersons ?? []}
          categories={options?.categories ?? []}
          customerOptions={customerOptions}
          customerOptionsLoading={customerOptionsLoading}
          costRestricted={costRestricted}
          customerRestricted={customerRestricted}
          onReset={() => setSp(new URLSearchParams(), { replace: true })}
        />

        {summaryError && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 12 }}
            message="汇总加载失败"
            description={summaryError}
            action={<a onClick={() => setRetryTick((t) => t + 1)}>重试</a>}
          />
        )}
        {lineMode && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="分类口径：订单已拆到明细行，只统计匹配分类的行；行级实付 = 匹配行金额之和，只计单头所属批次的明细行（与整单「采购金额」口径不同）。"
          />
        )}
        {summary?.groups_ranking_restricted && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="金额字段不可见：分组按订单数排序（金额排名不适用）。"
          />
        )}

        {effectiveState.groupKey == null ? (
          <SummaryView
            summary={summary}
            loading={summaryLoading}
            basis={basis}
            groupBy={effectiveGroupBy}
            onDrill={(key) => patch({ groupKey: key, page: 1 })}
          />
        ) : ordersError ? (
          <Alert
            type="error"
            showIcon
            message="订单列表加载失败"
            description={ordersError}
            action={<a onClick={() => setRetryTick((t) => t + 1)}>重试</a>}
          />
        ) : (
          <DrillTable
            data={orders}
            loading={ordersLoading}
            basis={basis}
            costRestricted={costRestricted}
            groupByLabel={groupByTitle(effectiveGroupBy)}
            groupKey={effectiveState.groupKey}
            sort={effectiveState.sort}
            dir={effectiveState.dir}
            page={effectiveState.page}
            pageSize={pageSize}
            onSortChange={(sort: AnalyticsSort, dir) => patch({ sort, dir, page: 1 })}
            onPageChange={(page, size) => {
              setPageSize(size);
              patch({ page });
            }}
            onOpenOrder={(orderId) => patch({ orderId })}
            onBack={() => patch({ groupKey: null, orderId: null, page: 1 })}
          />
        )}
      </Card>

      <OrderDetailDrawer
        orderId={effectiveState.orderId}
        category={state.category}
        basis={basis}
        onClose={() => patch({ orderId: null })}
      />
    </>
  );
}
