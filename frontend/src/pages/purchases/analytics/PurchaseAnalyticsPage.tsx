// 采购分析页（D-39 客户需求 Round1，PR-4a）：日期区间 + 类型/采购员/销售/客户/分类多选，
// 三口径（含税/不含税/实付）并排显示（本地选择，不动管理员 TaxBasis），分组 → 订单下钻 → 明细抽屉。
// Q-A-01 补图表：日 / 月趋势柱图（同端点 group_by=day/month 二次请求）+ 当前分组维度
// 排行横条图（与表格同一次请求同源同下钻），复用三金额口径；不加新客 / 滑块 / 毛利。
// 全部状态写 URL（preset/from/to/筛选/group_by/group_key/sort/dir/page/basis/order_id），可分享。
// 权限：page_purchase_analytics（模板仅 boss；admin 恒放行）+ 本轮后端 admin/boss 角色硬门
// （403 由本页错误态承接）——路由/菜单由 nav.tsx 统一登记。
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Button, App, Alert, Card, ConfigProvider, Radio, Space, Spin } from "antd";
import PageHeader from "../../../components/PageHeader";
import { businessToday } from "../../../utils/date";
import { readSessionToken, SESSION_EXPIRED_MESSAGE, sessionStale, useSessionOwner } from "../../../utils/session";
import {
  downloadPurchaseOrdersCsv,
  fetchPurchaseAnalyticsOptions,
  fetchPurchaseAnalyticsOrders,
  fetchPurchaseAnalyticsSummary,
  fetchPurchasePnBoards,
  type PurchaseAnalyticsOptions,
  type PurchaseAnalyticsOrdersResp,
  type PurchaseAnalyticsSummary,
  type PurchasePnBoard,
} from "../../../api/purchaseAnalytics";
import {
  PurchaseRankBar,
  PurchaseTrendChart,
  type PurchaseChartMetric,
} from "../../../components/charts/PurchaseAnalyticsCharts";
import { analyticsControlTheme } from "../../sales/analytics/dashboardColors";
import FilterBar from "./FilterBar";
import SummaryView, { groupByTitle, groupLabelOf } from "./SummaryView";
import DrillTable from "./DrillTable";
import OrderDetailDrawer from "./OrderDetailDrawer";
import {
  DEFAULT_GROUP_BY,
  parseAnalyticsUrl,
  recommendTrendGranularity,
  resolveAnalyticsRange,
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

/** 排行图只画非时间维度（day / month 时表格本身就是趋势，不重复画）。 */
const RANKING_GROUP_BYS = ["source_type", "purchaser", "salesperson", "customer", "category"];

const BASIS_TO_CHART_METRIC: Record<string, PurchaseChartMetric> = {
  inc: "amount_inc",
  ex: "amount_ex",
  paid: "amount_paid",
};

// 趋势图口径单选：用短标签（含税/不含税/实付/单数），避免与 KPI 卡及分组列的
// 「XX金额」同名——页面测试与可读性都按文案区分元素。
const METRIC_RADIO_LABELS: Record<PurchaseChartMetric, string> = {
  amount_inc: "含税",
  amount_ex: "不含税",
  amount_paid: "实付",
  orders: "单数",
};

export default function PurchaseAnalyticsPage() {
  const { message } = App.useApp();
  const [sp, setSp] = useSearchParams();
  const state = parseAnalyticsUrl(sp);
  const patch = useAnalyticsPatch(sp, setSp);
  const today = businessToday();

  // 组件所属会话（DL-010 返工）：挂载时 token 是本页全部异步入口（候选 / 汇总 /
  // 趋势 / 订单列表 / 抽屉）的 owner。静默换 token 后旧会话的任何新请求一律拒发
  // （sessionExpired 清旧上下文），迟到响应仍由各处落地守卫作废。
  const ownerToken = useSessionOwner();
  const [sessionDead, setSessionDead] = useState(false);
  /** 页级过期通知（DL-010 落地清理）：子组件 / 迟到响应检测到 owner 失效时调用，
   * 幂等清空旧会话上下文（筛选候选 / 汇总与图表 / 订单列表）并置会话死亡标记。 */
  const notifyOwnerStale = () => {
    if (!sessionStale(ownerToken)) return;
    setSessionDead(true);
    setOptions(null);
    setCustomerOptionsLoading(false);
    setSummary(null);
    setSummaryLoading(false);
    setSummaryError(null);
    setOrders(null);
    setOrdersLoading(false);
    setOrdersError(null);
    setTrendSummary(null);
    setTrendLoading(false);
    setTrendError(null);
  };
  /** 所属会话失效：拒发请求并清旧会话上下文（筛选候选 / 汇总与图表 / 订单列表）。
   * 幂等；置死后数据面换专用提示，重试不再发请求。 */
  const sessionExpired = () => {
    if (!sessionStale(ownerToken)) return false;
    notifyOwnerStale();
    return true;
  };

  const isAdmin = (localStorage.getItem("role") || "") === "admin";
  const perms = useMemo(readLocalPerms, []);
  // admin 恒全开；其余角色按登录快照（boss 模板默认双开，显式关掉的按受限处理）
  const costRestricted = !isAdmin && perms.data_purchase_cost === false;
  const customerRestricted = !isAdmin && perms.data_customer === false;
  const supplierRestricted = !isAdmin && perms.data_supplier === false;

  // 客户权限受限时：客户筛选/分组对后端是 403，其下钻层（group_key 是客户 id，
  // 换维度后必是空集）一并收敛——控件显示、渲染层、请求参数全部同源，
  // 不允许「下拉显示客户、表格按采购类型」或「采购类型：12 · 共 0 单」的自相矛盾 UI。
  const customerDimBlocked = customerRestricted && state.groupBy === "customer";
  const effectiveState = {
    ...state,
    supplier: supplierRestricted ? "" : state.supplier,
    groupBy: customerDimBlocked ? DEFAULT_GROUP_BY : state.groupBy,
    customerIds: customerRestricted ? [] : state.customerIds,
    groupKey: customerDimBlocked ? null : state.groupKey,
    orderId: customerDimBlocked ? null : state.orderId,
  };
  const effectiveGroupBy = effectiveState.groupBy;
  // 分类分组下钻：抽屉按「所在行」的分类过滤与汇总（与该行行级口径一致），
  // 不是顶部多选筛选（多选时会把其他分类的行也算进来；未筛时则丢掉行级口径）
  const drawerCategory =
    effectiveGroupBy === "category" && effectiveState.groupKey
      ? [effectiveState.groupKey]
      : state.category;

  const [options, setOptions] = useState<PurchaseAnalyticsOptions | null>(null);
  const [summary, setSummary] = useState<PurchaseAnalyticsSummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [boards, setBoards] = useState<PurchasePnBoard[] | null>(null);
  const [boardsLoading, setBoardsLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [orders, setOrders] = useState<PurchaseAnalyticsOrdersResp | null>(null);
  const [ordersLoading, setOrdersLoading] = useState(false);
  const [ordersError, setOrdersError] = useState<string | null>(null);
  const [customerQuery, setCustomerQuery] = useState("");
  const [customerOptionsLoading, setCustomerOptionsLoading] = useState(false);
  // Q-A-01 趋势图状态：粒度与指标是图表视图开关（非筛选），不落 URL；初始粒度按区间
  // 推荐——日期要用预设解析后的实际区间（state.from/to 仅 preset=custom 才有值，
  // 「今天」等预设直接给 null 会错推成按月）
  const [trendGran, setTrendGran] = useState<"day" | "month">(() => {
    const range = resolveAnalyticsRange(state, today);
    return recommendTrendGranularity(range?.from, range?.to);
  });
  const [chartMetric, setChartMetric] = useState<PurchaseChartMetric>(
    () => BASIS_TO_CHART_METRIC[state.basis[0]] ?? "orders");
  const [trendSummary, setTrendSummary] = useState<PurchaseAnalyticsSummary | null>(null);
  const [trendLoading, setTrendLoading] = useState(false);
  const [trendError, setTrendError] = useState<string | null>(null);
  const pageSize = state.pageSize;
  const [retryTick, setRetryTick] = useState(0);
  const summarySeq = useRef(0);
  const ordersSeq = useRef(0);
  const trendSeq = useRef(0);
  // 仅合并本页所属会话中同筛选、分组与重试代次的在途请求；完成即删除，
  // 不缓存已完成结果，重试和后续刷新仍真实读取服务端。
  const summaryRequests = useRef(new Map<string, ReturnType<typeof fetchPurchaseAnalyticsSummary>>());

  // 客户已选ID额外补姓名最多200项；超出部分仍保留ID筛选，不扩大后端候选上限。
  const selectedCustomerKey = effectiveState.customerIds.slice(0, 200).join(",");
  // 客户候选直接来自 options 的有界只读查询；输入名称可搜索首批之外的客户。
  // 旧请求清理 alive 标志，快速输入时不让迟到响应覆盖新搜索；落地前比对
  // localStorage token（DL-010）：换账号后的迟到响应不进当前 UI。
  useEffect(() => {
    if (sessionExpired()) return;
    let alive = true;
    const token = readSessionToken();
    setCustomerOptionsLoading(true);
    fetchPurchaseAnalyticsOptions(customerRestricted ? "" : customerQuery,
      selectedCustomerKey ? selectedCustomerKey.split(",").map(Number) : [])
      .then(({ data }) => {
        if (!alive) return;
        if (sessionStale(token)) { notifyOwnerStale(); return; }
        setOptions(data);
      })
      .catch((err) => {
        if (!alive) return;
        if (sessionStale(token)) { notifyOwnerStale(); return; }
        message.error(`筛选取值加载失败：${apiErrorMessage(err)}`);
      })
      // finally 也守卫（DL-010 返工）：失效会话的收尾不触碰当前 UI 状态，并通知页级清旧上下文
      .finally(() => {
        if (!alive) return;
        if (sessionStale(token)) { notifyOwnerStale(); return; }
        setCustomerOptionsLoading(false);
      });
    return () => { alive = false; };
  }, [customerQuery, customerRestricted, selectedCustomerKey]);
  const customerOptions = customerRestricted ? [] : [...new Map(
    [...(options?.selected_customers ?? []), ...(options?.customers ?? [])]
      .map(c => [c.customer_id, { value: c.customer_id, label: c.customer || String(c.customer_id) }]),
  ).values()];

  // KPI + 分组汇总。依赖是解析后的筛选键（口径多选不在内：切换口径不触发重新请求）。
  // todayKey 进依赖：跨零点后（重试/改每页条数等触发重拉）不会沿用前一天的窗口。
  const todayKey = today.format("YYYY-MM-DD");
  const apiFilters = useMemo(
    () => toAnalyticsApiFilters(effectiveState, today),
    // sp 变化时重算；实际重拉由 filterKey（筛选序列化键）控制，口径/抽屉参数变化不触发
    [sp.toString(), customerRestricted, supplierRestricted, todayKey],
  );
  const filterKey = JSON.stringify(apiFilters);

  const requestSummary = (groupBy: string, groupsPage: number) => {
    const key = JSON.stringify([filterKey, groupBy, groupsPage, retryTick]);
    const existing = summaryRequests.current.get(key);
    if (existing) return existing;
    const pending = fetchPurchaseAnalyticsSummary({
      ...apiFilters,
      group_by: groupBy,
      ...(groupBy === "pn" && groupsPage > 1 ? { groups_page: groupsPage } : {}),
    });
    summaryRequests.current.set(key, pending);
    const clear = () => {
      if (summaryRequests.current.get(key) === pending) summaryRequests.current.delete(key);
    };
    void pending.then(clear, clear);
    return pending;
  };

  useEffect(() => {
    if (sessionExpired()) return;
    const seq = ++summarySeq.current;
    const token = readSessionToken();
    setSummaryLoading(true);
    setSummaryError(null);
    requestSummary(effectiveGroupBy, effectiveState.groupsPage)
      .then(({ data }) => {
        if (seq !== summarySeq.current) return;
        if (sessionStale(token)) { notifyOwnerStale(); return; }
        setSummary(data);
      })
      .catch((err) => {
        if (seq !== summarySeq.current) return;
        if (sessionStale(token)) { notifyOwnerStale(); return; }
        setSummary(null);
        setSummaryError(apiErrorMessage(err));
      })
      .finally(() => {
        if (seq !== summarySeq.current) return;
        if (sessionStale(token)) { notifyOwnerStale(); return; }
        setSummaryLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- apiFilters 以 filterKey 进入依赖
  }, [filterKey, effectiveGroupBy, effectiveState.groupsPage, retryTick]);

  useEffect(() => {
    if (sessionExpired() || effectiveState.groupKey != null) return;
    let alive = true;
    const token = readSessionToken();
    setBoards(null);
    setBoardsLoading(true);
    fetchPurchasePnBoards(apiFilters)
      .then(({ data }) => {
        if (!alive || sessionStale(token)) return;
        setBoards(data.boards);
      })
      .catch(() => {
        if (alive && !sessionStale(token)) setBoards(null);
      })
      .finally(() => {
        if (alive && !sessionStale(token)) setBoardsLoading(false);
      });
    return () => { alive = false; };
    // apiFilters 随 filterKey 变化
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterKey, effectiveState.groupKey, retryTick]);

  // Q-A-01 趋势图数据：同一 summary 端点按 day / month 二次分组（不新增后端）；
  // 与主汇总同一筛选键 + 粒度 + 重试驱动，令牌比对同 DL-010。
  useEffect(() => {
    if (sessionExpired()) return;
    const seq = ++trendSeq.current;
    const token = readSessionToken();
    setTrendLoading(true);
    setTrendError(null);
    requestSummary(trendGran, 1)
      .then(({ data }) => {
        if (seq !== trendSeq.current) return;
        if (sessionStale(token)) { notifyOwnerStale(); return; }
        setTrendSummary(data);
      })
      .catch((err) => {
        if (seq !== trendSeq.current) return;
        if (sessionStale(token)) { notifyOwnerStale(); return; }
        setTrendSummary(null);
        setTrendError(apiErrorMessage(err));
      })
      .finally(() => {
        if (seq !== trendSeq.current) return;
        if (sessionStale(token)) { notifyOwnerStale(); return; }
        setTrendLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- apiFilters 以 filterKey 进入依赖
  }, [filterKey, trendGran, retryTick]);

  // 分组下钻订单列表（筛选随层传递）
  useEffect(() => {
    if (sessionExpired()) return;
    if (effectiveState.groupKey == null) {
      setOrders(null);
      setOrdersError(null);
      return;
    }
    const seq = ++ordersSeq.current;
    const token = readSessionToken();
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
        if (sessionStale(token)) { notifyOwnerStale(); return; }
        setOrders(data);
      })
      .catch((err) => {
        if (seq !== ordersSeq.current) return;
        if (sessionStale(token)) { notifyOwnerStale(); return; }
        setOrders(null);
        setOrdersError(apiErrorMessage(err));
      })
      .finally(() => {
        if (seq !== ordersSeq.current) return;
        if (sessionStale(token)) { notifyOwnerStale(); return; }
        setOrdersLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- apiFilters 以 filterKey 进入依赖
  }, [filterKey, effectiveGroupBy, effectiveState.groupKey, effectiveState.sort, effectiveState.dir, effectiveState.page, pageSize, retryTick]);

  const basis = state.basis;
  const lineMode = (effectiveState.groupKey == null ? summary?.basis_mode : orders?.basis_mode) === "line";

  // 图表指标：三金额随成本权限（无成本时后端置 null，图表只剩单数口径）
  const metricOptions: PurchaseChartMetric[] = [
    ...(costRestricted ? [] : basis
      .map((b) => BASIS_TO_CHART_METRIC[b])
      .filter((m): m is PurchaseChartMetric => m != null)),
    "orders",
  ];
  const activeMetric = metricOptions.includes(chartMetric) ? chartMetric : metricOptions[0];
  const lineMessage = effectiveGroupBy === "pn"
    ? "PN 口径：订单已拆到明细行，只统计匹配 PN 的行；行级实付 = 匹配行金额之和，只计单头所属批次的明细行（与整单「采购金额」口径不同）。"
    : "分类口径：订单已拆到明细行，只统计匹配分类的行；行级实付 = 匹配行金额之和，只计单头所属批次的明细行（与整单「采购金额」口径不同）。";
  const onExport = () => {
    if (sessionExpired()) return;
    setExporting(true);
    const token = readSessionToken();
    const extra: Record<string, string | number> = {
      sort: effectiveState.sort,
      group_by: effectiveGroupBy,
      ...(effectiveState.dir !== "desc" ? { order: effectiveState.dir } : {}),
      ...(effectiveState.groupKey ? { group_key: effectiveState.groupKey } : {}),
    };
    downloadPurchaseOrdersCsv(apiFilters, extra)
      .then(({ data }) => {
        if (sessionStale(token)) { notifyOwnerStale(); return; }
        const url = URL.createObjectURL(data);
        const a = document.createElement("a");
        a.href = url;
        a.download = `采购单-${today.format("YYYYMMDD")}.csv`;
        a.click();
        URL.revokeObjectURL(url);
      })
      .catch((err) => {
        if (sessionStale(token)) { notifyOwnerStale(); return; }
        const status = (err as { response?: { status?: number } })?.response?.status;
        message.error(status === 413 ? "超过导出上限 10,000 单，请收窄区间或筛选" : `导出失败：${apiErrorMessage(err)}`);
      })
      .finally(() => setExporting(false));
  };

  // DL-025 局部控件主题（销售/采购分析共用常量）：不碰全站 theme.ts，只在本页树内
  // 覆盖 disabled/placeholder/hover 文字色；portal 下拉与触发器同 hashId 一并生效
  return (
    <ConfigProvider theme={analyticsControlTheme}>
      <PageHeader
        title="采购分析"
        subtitle={summary ? `三口径采购金额（含税 / 不含税 / 实付），数据截至 ${summary.as_of}` : "三口径采购金额（含税 / 不含税 / 实付）"}
        extra={<Button loading={exporting} onClick={onExport}>导出</Button>}
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
          onCustomerSearch={setCustomerQuery}
          customersHasMore={options?.customers_has_more ?? false}
          costRestricted={costRestricted}
          customerRestricted={customerRestricted}
          supplierRestricted={supplierRestricted}
          onReset={() => setSp(new URLSearchParams(), { replace: true })}
        />

        {sessionDead ? (
          // 所属会话失效（DL-010 返工）：旧会话的候选 / 汇总 / 图表 / 列表上下文已清，
          // 数据面整体让位给会话提示——重试无济于事，刷新后以当前账号重新挂载
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={SESSION_EXPIRED_MESSAGE}
            description="已停止本页所有请求并清空旧会话的筛选候选、汇总图表与订单列表；刷新页面后以当前账号继续（URL 中的筛选保持不变）。"
          />
        ) : (
          <>
        {summaryError && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 12 }}
            message="汇总加载失败"
            description={summaryError}
            action={<Button type="link" size="small" onClick={() => setRetryTick((t) => t + 1)}>重试</Button>}
          />
        )}
        {lineMode && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={lineMessage}
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
          <>
            {/* Q-A-01：趋势（day/month）+ 排行（当前分组维度）图表，与表格同源同下钻 */}
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 14 }}>
              <Card
                size="small"
                title="采购趋势"
                style={{ flex: "1 1 420px", minWidth: 0, maxWidth: "100%" }}
              >
                <Space size={12} wrap style={{ marginBottom: 12 }}>
                  <Radio.Group
                    aria-label="趋势粒度"
                    optionType="button"
                    size="small"
                    value={trendGran}
                    options={[{ value: "month", label: "按月" }, { value: "day", label: "按日" }]}
                    onChange={(e) => setTrendGran(e.target.value)}
                  />
                  <Radio.Group
                    aria-label="图表指标"
                    optionType="button"
                    size="small"
                    value={activeMetric}
                    options={metricOptions.map((m) => ({ value: m, label: METRIC_RADIO_LABELS[m] }))}
                    onChange={(e) => setChartMetric(e.target.value)}
                  />
                </Space>
                {/* 汇总失败时顶部已有权威错误 + 重试（同一端点同权限，重试会一并重发趋势），
                    避免同文案双 Alert 叠加 */}
                {trendError && !summaryError ? (
                  <Alert
                    type="error" showIcon message="趋势加载失败" description={trendError}
                    action={<Button type="link" size="small" onClick={() => setRetryTick((t) => t + 1)}>重试</Button>}
                  />
                ) : (
                  <PurchaseTrendChart
                    groups={trendSummary?.groups ?? []}
                    metric={activeMetric}
                    loading={trendLoading}
                    onBucketClick={(key) => {
                      // 与表格行点击同一 drill 语义：分组键进 URL，可分享可后退
                      patch({ groupBy: trendGran, groupKey: key, page: 1 }, { history: "push" });
                    }}
                  />
                )}
              </Card>
              {RANKING_GROUP_BYS.includes(effectiveGroupBy) && (
                <Card
                  size="small"
                  title={`分组排行（${groupByTitle(effectiveGroupBy)}）`}
                  style={{ flex: "1 1 360px", minWidth: 0, maxWidth: "100%" }}
                >
                  <PurchaseRankBar
                    groups={summary?.groups ?? []}
                    metric={activeMetric}
                    loading={summaryLoading}
                    labelOf={groupLabelOf}
                    onItemClick={(key) => patch({ groupKey: key, page: 1 }, { history: "push" })}
                  />
                </Card>
              )}
            </div>
            {effectiveState.groupKey == null && (boardsLoading || boards) && (
              boardsLoading ? (
                <div style={{ marginBottom: 12 }}><Spin size="small" /> PN 双榜加载中</div>
              ) : (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 12 }}>
                {(boards ?? []).map((board) => (
                  <Card key={board.key} size="small" title={`${board.source_type} · PN Top 20`}>
                    {board.empty_reason === "source_type_excluded" ? (
                      <span>{board.empty_message}</span>
                    ) : board.items.length === 0 ? (
                      <span>当前筛选下没有该类型的单头批次行</span>
                    ) : (
                      <ol style={{ margin: 0, paddingLeft: 18 }}>
                        {board.items.map((item) => (
                          <li key={item.key}>
                            {item.key} · {item.lines} 行
                            {item.amount_paid != null ? ` · 实付 ${item.amount_paid}` : ""}
                          </li>
                        ))}
                      </ol>
                    )}
                  </Card>
                ))}
              </div>
              )
            )}
            <SummaryView
              summary={summary}
              loading={summaryLoading}
              basis={basis}
              groupBy={effectiveGroupBy}
              groupsPage={effectiveState.groupsPage}
              onGroupsPageChange={(page) => patch({ groupsPage: page })}
              onDrill={(key) => patch({ groupKey: key, page: 1 }, { history: "push" })}
            />
          </>
        ) : ordersError ? (
          <Alert
            type="error"
            showIcon
            message="订单列表加载失败"
            description={ordersError}
            action={<Button type="link" size="small" onClick={() => setRetryTick((t) => t + 1)}>重试</Button>}
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
              patch({ page, pageSize: size });
            }}
            onOpenOrder={(orderId) => { notifyOwnerStale(); patch({ orderId }, { history: "push" }); }}
            onBack={() => patch({ groupKey: null, orderId: null, page: 1 }, { history: "push" })}
          />
        )}
          </>
        )}
      </Card>

      <OrderDetailDrawer
        orderId={effectiveState.orderId}
        category={drawerCategory}
        pn={effectiveGroupBy === "pn" ? effectiveState.groupKey : null}
        basis={basis}
        ownerToken={ownerToken}
        onOwnerStale={notifyOwnerStale}
        onClose={() => patch({ orderId: null })}
      />
    </ConfigProvider>
  );
}
