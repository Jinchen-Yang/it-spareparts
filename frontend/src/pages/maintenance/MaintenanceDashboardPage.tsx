/**
 * 维保数据看板（R33 / D-0022 四视图 + R34 / D-0024 下单/消耗/试算）：
 * 回收与领用总览 / 项目对比 / PN 排名与估价 / 下单与每日消耗 / 需求单数量核对。
 *
 * - URL 即筛选状态（useUrlState）：tab/range/from/to/project/business_type/q/
 *   ratio/sort/page/ps 刷新/分享不丢上下文；默认近 7 天含今天（上海日历日）。
 * - 数量与金额分离：amounts_visible 由 /options 下发（服务端事实，不用本地
 *   权限快照猜测）；无金额权时金额卡/列/图/参数控件整块不渲染。
 * - 排序候选同样由 /options 下发：金额排序键已在无权时被服务端剔除，
 *   前端不自行增删。
 * - R34 价值保留比例（Sunny 01:20 裁定，覆盖旧「无默认」口径）：**默认 50%
 *   可调**，20%/30% 只是快捷选项。金额可见账号首次打开 / 无 ratio 深链 /
 *   刷新 / 导出一律按 0.5 试算；显式 0 保留 0（不被默认覆盖）；清空或恢复
 *   默认回到 50%。URL 持有显式值，不写全局存储；数量账号不带任何参数痕迹。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert, Button, Card, DatePicker, Input, InputNumber, Select, Skeleton, Space, Tabs, Typography,
} from "antd";
import { FundOutlined, ReloadOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { useUrlState, urlPageField } from "../../hooks/useUrlState";
import {
  fetchDashboardOptions,
  type DashboardFilters,
  type DashboardOptions,
  type DashboardRangePreset,
} from "../../api/maintenanceDashboard";
import {
  BOARD_BUSINESS_TYPE_CODES,
  BOARD_BUSINESS_TYPE_LABELS,
  type BoardBusinessTypeCode,
} from "../../api/maintenanceBossBoard";
import PageHeader from "../../components/PageHeader";
import DashboardOverviewTab from "./dashboard/DashboardOverviewTab";
import DashboardProjectsTab from "./dashboard/DashboardProjectsTab";
import DashboardPartsTab from "./dashboard/DashboardPartsTab";
import DashboardConsumptionTab from "./dashboard/DashboardConsumptionTab";
import DashboardQuantityChecksTab from "./dashboard/DashboardQuantityChecksTab";
import ReceiptsDrillDrawer, { type ReceiptsDrill } from "./dashboard/ReceiptsDrillDrawer";
import { apiErrorMessage } from "./dashboard/dashboardShared";

const { Text } = Typography;

const TAB_KEYS = ["overview", "projects", "parts", "consumption", "checks"] as const;
type TabKey = (typeof TAB_KEYS)[number];

const TAB_LABELS: Record<TabKey, string> = {
  overview: "回收与领用总览",
  projects: "项目对比",
  parts: "PN 排名与估价",
  consumption: "下单与每日消耗",
  checks: "需求单核对",
};

const RANGE_PRESETS = ["7d", "15d", "30d", "half_year", "year", "all", "custom"] as const;

const RANGE_LABELS: Record<DashboardRangePreset, string> = {
  "7d": "近 7 天（含今天）",
  "15d": "近 15 天",
  "30d": "近 30 天",
  half_year: "近半年",
  year: "近一年",
  all: "全部",
  custom: "自定义",
};

/** 各页签默认排序（与后端 Query 默认一致）；金额键默认仅出现在有权账号）。 */
const DEFAULT_SORTS: Record<TabKey, string> = {
  overview: "",
  projects: "bad_receipt_qty_desc",
  parts: "bad_receipt_qty_desc",
  consumption: "",
  checks: "difference_desc",
};

const PAGE_SIZE_CHOICES = ["20", "50", "100"];

/** URL CSV → 去重非空值。 */
function csvValues(spec: string): string[] {
  if (!spec) return [];
  return [...new Set(spec.split(",").map((value) => value.trim()).filter(Boolean))];
}

const isDate = (value: string) => /^\d{4}-\d{2}-\d{2}$/.test(value);

/** R34 试算比例：[0,1] 十进制串（"0" 合法且不被默认覆盖；空 = 用默认 0.5）。 */
const isRatio = (value: string) =>
  /^\d+(\.\d+)?$/.test(value) && Number(value) >= 0 && Number(value) <= 1;

/** Sunny 01:20 裁定：默认试算比例 50%（可调；20/30 仅快捷选项）。 */
const DEFAULT_RATIO = "0.5";

/** 十进制比例串 → 百分数显示（"0.5" → 50；四舍五入到两位小数避免 0.3×100 浮点噪声）。 */
function ratioToPercent(ratio: string): number {
  return Math.round(Number(ratio) * 10000) / 100;
}

/** 百分数输入 → 十进制比例串（50 → "0.5"；保留用户精度，去尾零）。 */
function percentToRatio(percent: number): string {
  return String(percent / 100);
}

/** R34 价值保留比例控件（仅金额可见账号渲染；数量账号不见任何参数痕迹）。
 * URL 无显式值时按默认 0.5 试算（不解释为「不试算」）；清空输入 / 恢复默认
 * 都回到 50%；显式 0 与默认严格区分。 */
function RetentionRatioControl({ ratio, usingDefault, quickOptions, onChange }: {
  ratio: string;
  usingDefault: boolean;
  quickOptions: string[];
  onChange: (next: string | null) => void;
}) {
  return (
    <Space wrap size={8} data-testid="retention-ratio-control">
      <Text type="secondary" style={{ fontSize: 12 }}>价值保留比例（试算）</Text>
      {quickOptions.map((option) => {
        const percent = ratioToPercent(option);
        const active = ratio === option;
        return (
          <Button
            key={option}
            size="small"
            type={active ? "primary" : "default"}
            aria-pressed={active}
            onClick={() => onChange(active ? null : option)}
          >
            {percent}%
          </Button>
        );
      })}
      <InputNumber
        aria-label="自定义价值保留比例（百分比）"
        placeholder="自定义 %"
        size="small"
        style={{ width: 120 }}
        min={0}
        max={100}
        step={5}
        value={ratioToPercent(ratio)}
        onChange={(value) => {
          // 清空输入回归默认 50%（不再解释为待试算）
          if (value === null) { onChange(null); return; }
          if (typeof value !== "number" || !Number.isFinite(value)) return;
          onChange(percentToRatio(Math.min(100, Math.max(0, value))));
        }}
      />
      <Button size="small" disabled={usingDefault} onClick={() => onChange(null)}>
        恢复默认（50%）
      </Button>
      <Text type="secondary" style={{ fontSize: 12 }} data-testid="retention-ratio-state">
        {usingDefault
          ? `默认试算比例 ${ratioToPercent(ratio)}%（${ratio}）— 可调整`
          : `当前试算比例 ${ratioToPercent(ratio)}%（${ratio}）`}
      </Text>
    </Space>
  );
}

export function MaintenanceDashboardPage() {
  const [state, patch] = useUrlState({
    tab: { defaultValue: "overview", isValid: (v) => (TAB_KEYS as readonly string[]).includes(v) },
    range: {
      defaultValue: "7d",
      isValid: (v) => (RANGE_PRESETS as readonly string[]).includes(v),
    },
    from: { defaultValue: "", isValid: isDate },
    to: { defaultValue: "", isValid: isDate },
    project: { defaultValue: "" },
    business_type: { defaultValue: "" },
    q: { defaultValue: "" },
    ratio: { defaultValue: "", isValid: isRatio },
    sort: { defaultValue: "" },
    page: urlPageField,
    ps: {
      defaultValue: "20",
      isValid: (v) => PAGE_SIZE_CHOICES.includes(v),
    },
  });

  const tab = state.tab as TabKey;
  const page = Number(state.page);
  const pageSize = Number(state.ps);

  const [options, setOptions] = useState<DashboardOptions | null>(null);
  const [optionsLoading, setOptionsLoading] = useState(true);
  const [optionsError, setOptionsError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  // M-AC09 返件明细下钻（R33 GET /receipts 的轻量入口）：Drawer 状态在壳层，
  // 各页签只上抛「求交后的筛选 + 标题」；关闭即回原视图，不写 URL。
  const [receiptsDrill, setReceiptsDrill] = useState<ReceiptsDrill | null>(null);
  const openReceiptsDrill = useCallback((drill: ReceiptsDrill) => {
    setReceiptsDrill(drill);
  }, []);
  const closeReceiptsDrill = useCallback(() => setReceiptsDrill(null), []);

  const loadOptions = useCallback(async () => {
    setOptionsLoading(true);
    setOptionsError(null);
    try {
      setOptions(await fetchDashboardOptions());
    } catch (err) {
      setOptions(null);
      setOptionsError(apiErrorMessage(err));
    } finally {
      setOptionsLoading(false);
    }
  }, []);

  useEffect(() => { void loadOptions(); }, [loadOptions]);

  const amountsVisible = options?.amounts_visible ?? false;

  // 筛选集 → 请求参数（自定义区间补日期；空筛选项省略）。
  // 试算比例：金额可见账号一律携带（URL 无显式值时用默认 0.5，Sunny 01:20 裁定；
  // 显式 "0" 为非空真值串，不被默认覆盖）；数量账号不携带任何金额参数。
  const ratio = state.ratio || DEFAULT_RATIO;
  const usingDefaultRatio = state.ratio === "";
  const params = useMemo<DashboardFilters>(() => ({
    range: state.range as DashboardRangePreset,
    ...(state.range === "custom" && state.from ? { date_from: state.from } : {}),
    ...(state.range === "custom" && state.to ? { date_to: state.to } : {}),
    ...(state.project ? { project: state.project } : {}),
    ...(state.business_type ? { business_type: state.business_type } : {}),
    ...(state.q.trim() ? { q: state.q.trim() } : {}),
    ...(amountsVisible ? { value_retention_ratio: ratio } : {}),
  }), [state.range, state.from, state.to, state.project, state.business_type,
      state.q, amountsVisible, ratio]);

  // 排序：候选来自 options（随权限收敛）；URL 里的非法/越权键回退页签默认。
  const tabSorts = options?.sorts[tab] ?? [];
  const sort = state.sort && tabSorts.includes(state.sort)
    ? state.sort
    : DEFAULT_SORTS[tab];

  const onSortChange = useCallback((next: string) => {
    patch({ sort: next === DEFAULT_SORTS[tab] ? null : next, page: null });
  }, [patch, tab]);

  const onPageChange = useCallback((nextPage: number, nextSize: number) => {
    patch({
      page: nextPage > 1 ? String(nextPage) : null,
      ps: nextSize !== 20 ? String(nextSize) : null,
    });
  }, [patch]);

  const onTabChange = useCallback((key: string) => {
    // 切页签：排序回新页签默认、页码回 1，避免跨页签的非法排序残留。
    patch({ tab: key === "overview" ? null : key, sort: null, page: null });
  }, [patch]);

  const [searchDraft, setSearchDraft] = useState(state.q);
  useEffect(() => { setSearchDraft(state.q); }, [state.q]);

  const projectIds = useMemo(() => csvValues(state.project), [state.project]);

  const tabProps = {
    params,
    amountsVisible,
    sorts: tabSorts,
    sort,
    page,
    pageSize,
    refreshKey,
    maxExportRows: options?.max_export_rows ?? 50000,
    onSortChange,
    onPageChange,
  };

  if (optionsLoading) {
    return (
      <Space direction="vertical" size={16} style={{ width: "100%" }}>
        <Skeleton.Button active size="small" style={{ width: 220 }} />
        <Card size="small"><Skeleton active paragraph={{ rows: 6 }} /></Card>
      </Space>
    );
  }

  if (optionsError || !options) {
    return (
      <Alert
        type="error" showIcon message="看板选项加载失败"
        description={optionsError ?? "未知错误"}
        action={(
          <Button onClick={() => {
            void loadOptions();
            setRefreshKey((value) => value + 1);
          }}>重试</Button>
        )}
      />
    );
  }

  const quickOptions = options.valuation_policy?.quick_options ?? ["0.5", "0.3", "0.2"];

  return (
    <Space direction="vertical" size={16} className="maintenance-dashboard-page" style={{ width: "100%" }}>
      <PageHeader
        title={(
          <Space>
            <FundOutlined />
            <span>维保数据看板</span>
          </Space>
        )}
        subtitle="期间下单、实际领用、已计消耗成本、返件实收与坏件试算（数量口径人人可见，金额随权限）"
        extra={(
          <Space>
            <Button onClick={() => {
              setSearchDraft("");
              patch({
                range: null, from: null, to: null, project: null,
                business_type: null, q: null, ratio: null, sort: null, page: null, ps: null,
              });
            }}>
              重置筛选
            </Button>
            <Button aria-label="刷新" icon={<ReloadOutlined />}
              onClick={() => setRefreshKey((value) => value + 1)}>
              刷新
            </Button>
          </Space>
        )}
      />

      <Card size="small">
        <Space wrap size={12}>
          <Select<DashboardRangePreset>
            aria-label="时间范围"
            value={state.range as DashboardRangePreset}
            style={{ width: 180 }}
            options={(RANGE_PRESETS as readonly DashboardRangePreset[]).map((value) => ({
              label: RANGE_LABELS[value], value,
            }))}
            onChange={(value) => patch({ range: value, page: null })}
          />
          {state.range === "custom" ? (
            <DatePicker.RangePicker
              value={[state.from ? dayjs(state.from) : null, state.to ? dayjs(state.to) : null]}
              onChange={(value) => patch({
                from: value?.[0] ? value[0].format("YYYY-MM-DD") : null,
                to: value?.[1] ? value[1].format("YYYY-MM-DD") : null,
                page: null,
              })}
              allowEmpty={[true, true]}
            />
          ) : null}
          <Select<string>
            aria-label="业务类型筛选"
            placeholder="业务类型（全部）"
            allowClear
            value={state.business_type || undefined}
            style={{ width: 180, maxWidth: "calc(100vw - 96px)" }}
            options={BOARD_BUSINESS_TYPE_CODES.map((code) => ({
              label: BOARD_BUSINESS_TYPE_LABELS[code], value: code,
            }))}
            onChange={(value) => patch({ business_type: value ?? null, page: null })}
          />
          <Select<string[]>
            mode="multiple"
            showSearch
            optionFilterProp="label"
            aria-label="项目筛选"
            placeholder="项目（全部授权范围）"
            allowClear
            maxCount={50}
            value={projectIds}
            style={{ width: 300, maxWidth: "calc(100vw - 96px)" }}
            loading={optionsLoading}
            options={options.projects.map((project) => ({
              value: project.project_id,
              label: project.project_code
                ? `${project.display_name}（${project.project_code}）`
                : project.display_name,
            }))}
            onChange={(values) => patch({
              project: values.length ? values.join(",") : null,
              page: null,
            })}
          />
          {tab !== "checks" && tab !== "consumption" ? (
            <Input.Search
              allowClear
              value={searchDraft}
              placeholder="搜 PN / 描述"
              style={{ width: 200 }}
              onChange={(event) => setSearchDraft(event.target.value)}
              onSearch={(value) => patch({ q: value.trim() || null, page: null })}
            />
          ) : null}
          <Text type="secondary" style={{ fontSize: 12 }}>
            默认近 7 天含今天（上海日历日）；下单按制单日期、领用按领用日期、返件按业务发生时间各自入期。
          </Text>
        </Space>
        {amountsVisible ? (
          <div style={{ marginTop: 8 }}>
            <RetentionRatioControl
              ratio={ratio}
              usingDefault={usingDefaultRatio}
              quickOptions={quickOptions}
              onChange={(next) => patch({ ratio: next, page: null })}
            />
          </div>
        ) : null}
      </Card>

      {!amountsVisible ? (
        <Alert type="info" showIcon data-testid="amounts-restricted-banner"
          message="当前账号无金额查看权限：本页仅展示数量口径，金额卡片/列/试算参数/导出列不出现（需 data_profit 授权）" />
      ) : null}

      <Tabs
        activeKey={tab}
        onChange={onTabChange}
        items={[
          // 按需挂载：切到哪页签才请求哪份数据（同维保数据分析页范式）。
          { key: "overview", label: TAB_LABELS.overview,
            children: tab === "overview"
              ? <DashboardOverviewTab {...tabProps} onReceiptsDrill={openReceiptsDrill} />
              : null },
          { key: "projects", label: TAB_LABELS.projects,
            children: tab === "projects"
              ? <DashboardProjectsTab {...tabProps} onReceiptsDrill={openReceiptsDrill} />
              : null },
          { key: "parts", label: TAB_LABELS.parts,
            children: tab === "parts"
              ? <DashboardPartsTab {...tabProps} onReceiptsDrill={openReceiptsDrill} />
              : null },
          { key: "consumption", label: TAB_LABELS.consumption,
            children: tab === "consumption" ? <DashboardConsumptionTab {...tabProps} /> : null },
          { key: "checks", label: TAB_LABELS.checks,
            children: tab === "checks" ? <DashboardQuantityChecksTab {...tabProps} /> : null },
        ]}
      />

      <ReceiptsDrillDrawer
        drill={receiptsDrill}
        onClose={closeReceiptsDrill}
        amountsVisible={amountsVisible}
        maxExportRows={options.max_export_rows}
      />
    </Space>
  );
}

export default MaintenanceDashboardPage;
