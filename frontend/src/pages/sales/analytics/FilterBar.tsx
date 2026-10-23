import { useAuthorizedQuery } from "../../../authz/access";
import { actionRule } from "../../../authz/rules";
// 销售分析筛选栏（D-41 + D-44）：日期预设（共用组件）+ 业务类型 / 客户 / 客户类型 /
// 销售人员 / 商品大类 / 品牌多选（精确匹配；客户按规范名，候选随输入向后端补全）+
// 金额区间拖动条（D-44 §7）+ 三金额多选（共用组件）+ 含已退货开关（D-40 §5）。
// 客户权限（data_customer）受限时禁用客户与客户类型（后端对带客户条件的请求
// fail-closed 403）；对账权限受限时含退货开关无意义，不渲染。旧链接里的客户 / 销售
// 模糊条件（customer / salesperson）只作为可关闭标签展示，不再提供输入。
// 控件主题（A3/DL-025 返工）由页面根的 ConfigProvider 统一提供，本组件不再自带。
import { useEffect, useRef, useState } from "react";
import { Button, Select, Switch, Tag, Tooltip } from "antd";
import DateRangePresets from "../../../components/DateRangePresets";
import AmountBasisSelect from "../../../components/AmountBasisSelect";
import CheckboxMultiSelect from "../../../components/CheckboxMultiSelect";
import { readSessionToken, sessionStale } from "../../../utils/session";
import {
  EMPTY_FILTER_VALUE,
  fetchSalesAnalyticsOptions,
  type SalesAmountBasis,
  type SalesAnalyticsBusinessTypeOption,
  type SalesAnalyticsOptions,
  type SalesAnalyticsValueOption,
} from "../../../api/salesAnalytics";
import AmountRangeFilter from "./AmountRangeFilter";
import {
  AMOUNT_OPTIONS,
  type AmountKey,
  type SalesAnalyticsUrlState,
} from "./urlState";

export interface FilterBarProps {
  state: SalesAnalyticsUrlState;
  patch: (next: Partial<SalesAnalyticsUrlState>, opts?: { resetPage?: boolean }) => void;
  /** /options 返回的业务类型取值（含全时间已生效计数；value=null 是「未填写」组）。 */
  businessTypeOptions: SalesAnalyticsBusinessTypeOption[];
  /** /options 的其余候选（D-44）；undefined = 尚未加载。 */
  options?: SalesAnalyticsOptions | null;
  /** data_customer=false：客户 / 客户类型不可用（后端 fail-closed 403）。 */
  customerRestricted: boolean;
  /** data_reconciliation=false：对账字段不可见，含退货开关不渲染。 */
  reconRestricted: boolean;
  /** 金额「显示」口径是否被遮蔽（revenue 恒公开；用于三金额显示多选）。 */
  isBasisHidden: (basis: SalesAmountBasis) => boolean;
  /** data_purchase_cost=false：金额区间整组不可用（含营收口径，A-AC03 无例外）。 */
  amountRangeHidden: boolean;
  /** 所属会话 owner（页级下传，DL-010 补证 owner-search/owner-debounce-switch）：
   * 客户搜索的 debounce / 请求 / 落地三处守卫都用它。 */
  ownerToken: string | null;
  /** 页级过期通知（DL-010 落地清理）：客户搜索检测到 owner 失效时回调。 */
  onOwnerStale: () => void;
  onReset: () => void;
}

const multiSelectProps = {
  mode: "multiple" as const,
  maxTagCount: "responsive" as const,
  showSearch: true,
  allowClear: true,
  style: { minWidth: 160, maxWidth: 280 },
};

/**
 * 候选取值 → Select options：空值组（value=null）可选——值用后端缺失哨兵
 * `__empty__`（A-AC06 / Q-A-03：缺失维度可明确筛选，且与任何真实名叫「未填」
 * 的对象不碰撞——真实值按字面精确匹配，永不等同哨兵串）。业务类型同样支持
 * 哨兵（契约 §0：business_type `__empty__`=筛缺失）。
 */
export function valueOptions(list: SalesAnalyticsValueOption[] | undefined, emptyLabel = "（未填）") {
  return (list ?? []).map((o) => ({
    value: o.value ?? EMPTY_FILTER_VALUE,
    label: o.value == null ? `${emptyLabel}（${o.orders}）` : `${o.value}（${o.orders}）`,
  }));
}

/**
 * 客户多选：候选随输入向后端补全（规范名子串，最多 50 条）；已选值始终保留在候选中。
 * 所属会话守卫（DL-010 补证）：本组件自有 300ms debounce timer 与 found 状态，
 * 不走页内统一 effect——守卫要单独接 owner，三处都覆盖：
 * 1) timer 到期请求前比对（debounce 窗口内静默换 token，旧 timer 不再以新身份发请求）；
 * 2) 落地前比对捕获 token（迟到的旧响应不进候选）；
 * 3) 任一处检测到失效即清旧 found，并通知页级清旧上下文。
 */
function CustomerSelect({
  value,
  initial,
  disabled,
  onChange,
  ownerToken,
  onOwnerStale,
}: {
  value: string[];
  initial: SalesAnalyticsValueOption[] | undefined;
  disabled: boolean;
  onChange: (v: string[]) => void;
  ownerToken: string | null;
  onOwnerStale: () => void;
}) {
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  const stale = sessionStale(ownerToken);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search]);
  useEffect(() => { if (stale) onOwnerStale(); }, [stale]);
  const query = useAuthorizedQuery({ rule: actionRule("sales_analysis", "read_options"),
    legacy: { sales_analysis: true }, onStale: onOwnerStale, queryKey: [debounced], enabled: !!debounced && !disabled && !stale,
    query: async context => (await fetchSalesAnalyticsOptions({}, debounced, context)).data });
  const found = search.trim() ? (query.data?.customers ?? []) : null;
  const onSearch = setSearch;
  const base = found ?? initial ?? [];
  const known = new Set(base.map((o) => o.value));
  const options = [
    ...value.filter((v) => !known.has(v)).map((v) => ({ value: v, label: v })),
    ...valueOptions(base),
    // 客户候选（后端）只返回非空规范名；「未填客户」桶可筛但无计数候选——
    // 固定补一项哨兵选项（A-AC06）
    { value: EMPTY_FILTER_VALUE, label: "（未填）" },
  ];
  const select = (
    <Select
      {...multiSelectProps}
      aria-label="客户"
      placeholder={disabled ? "客户信息不可见" : "客户（输入搜索，多选）"}
      value={disabled ? [] : value}
      disabled={disabled}
      filterOption={false}
      onSearch={onSearch}
      options={options}
      onChange={(v: string[]) => { setSearch(""); setDebounced(""); onChange(v); }}
    />
  );
  return disabled ? (
    <Tooltip title="当前账号无客户信息权限（data_customer），客户筛选不可用">{select}</Tooltip>
  ) : select;
}

export default function FilterBar({
  state,
  patch,
  businessTypeOptions,
  options,
  customerRestricted,
  reconRestricted,
  isBasisHidden,
  amountRangeHidden,
  ownerToken,
  onOwnerStale,
  onReset,
}: FilterBarProps) {
  const change = (next: Partial<SalesAnalyticsUrlState>) => patch(next, { resetPage: true });
  const range = options?.amount_range;
  const maxAmount = range ? range[`${state.amountBasis}_max` as keyof typeof range] : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 14 }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <DateRangePresets
          preset={state.preset}
          from={state.from}
          to={state.to}
          onChange={({ preset, from, to }) => change({ preset, from, to })}
        />
        <Tooltip title="候选取值与计数来自全部已生效销售单（不随日期区间变化）">
          <Select
            {...multiSelectProps}
            aria-label="业务类型"
            placeholder="业务类型（多选）"
            value={state.businessType}
            optionFilterProp="label"
            options={businessTypeOptions.map((o) => ({
              // 「未填写」组走后端哨兵 __empty__ 可筛（契约 §0），不再禁选
              value: o.value ?? EMPTY_FILTER_VALUE,
              label: o.value == null ? `（未填写）（${o.orders}）` : `${o.value}（${o.orders}）`,
            }))}
            onChange={(v: string[]) => change({ businessType: v })}
          />
        </Tooltip>
        <CustomerSelect
          value={state.customers}
          initial={options?.customers}
          disabled={customerRestricted}
          onChange={(v) => change({ customers: v })}
          ownerToken={ownerToken}
          onOwnerStale={onOwnerStale}
        />
        <Select
          {...multiSelectProps}
          aria-label="客户类型"
          placeholder={customerRestricted ? "客户信息不可见" : "客户类型（多选）"}
          disabled={customerRestricted}
          value={customerRestricted ? [] : state.customerTypes}
          optionFilterProp="label"
          options={valueOptions(options?.customer_types)}
          onChange={(v: string[]) => change({ customerTypes: v })}
        />
        <CheckboxMultiSelect<string>
          label="销售人员"
          value={state.salespeople}
          options={(options?.salespeople ?? []).map((person) => ({
            value: person.value ?? EMPTY_FILTER_VALUE,
            label: person.value ?? "（未填）",
            description: `${person.orders} 单`,
          }))}
          selectionUnit="人"
          disabled={sessionStale(ownerToken)}
          onChange={(v) => {
            // 确认时重新读取会话，避免打开面板后静默换账号提交旧草稿。
            if (sessionStale(ownerToken)) { onOwnerStale(); return; }
            change({ salespeople: v });
          }}
        />
        <Tooltip title="选了商品大类或品牌即进入行级口径（Q-A-02）：金额区间仍按原单头金额决定整单入选，行筛选只决定计入的明细行金额（整单入选、匹配行计额）；不含税为按 13% 推算的名义值">
          <Select
            {...multiSelectProps}
            aria-label="商品大类"
            placeholder="商品大类（多选）"
            value={state.categories}
            optionFilterProp="label"
            options={(options?.categories ?? []).map((o) => ({
              value: o.value ?? "未分类", label: `${o.value ?? "未分类"}（${o.orders}）`,
            }))}
            onChange={(v: string[]) => change({ categories: v })}
          />
        </Tooltip>
        <Select
          {...multiSelectProps}
          aria-label="品牌"
          placeholder="品牌（多选）"
          value={state.brands}
          optionFilterProp="label"
          options={[
            // 品牌候选（后端）剔除空值组；「未填品牌」可筛（哨兵），固定补项（A-AC06）
            { value: EMPTY_FILTER_VALUE, label: "（未填）" },
            ...valueOptions(options?.brands),
          ]}
          onChange={(v: string[]) => change({ brands: v })}
        />
      </div>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center" }}>
        <AmountRangeFilter
          amountBasis={state.amountBasis}
          amountMin={state.amountMin}
          amountMax={state.amountMax}
          includeNegative={state.includeNegative}
          includeEmpty={state.includeEmpty}
          maxAmount={maxAmount ?? null}
          hidden={amountRangeHidden}
          onChange={(next) => change(next)}
        />
        <AmountBasisSelect
          options={AMOUNT_OPTIONS}
          value={state.amounts}
          onChange={(v: string[]) => patch({ amounts: v as AmountKey[] })}
          label="金额口径"
        />
        {!reconRestricted && (
          <Tooltip title="含已退货 = 已对账 + 退货已对账（相加，不扣减）；不含 = 只看已对账。默认不含（D-40）。">
            <span style={{ display: "inline-flex", gap: 6, alignItems: "center", whiteSpace: "nowrap" }}>
              含已退货
              <Switch
                aria-label="含已退货"
                size="small"
                checked={state.includeReturns}
                onChange={(v) => patch({ includeReturns: v })}
              />
            </span>
          </Tooltip>
        )}
        {state.customer && !customerRestricted && (
          <Tag closable onClose={() => change({ customer: null })}>客户含「{state.customer}」</Tag>
        )}
        {state.salesperson && (
          <Tag closable onClose={() => change({ salesperson: null })}>销售含「{state.salesperson}」</Tag>
        )}
        <Button onClick={onReset}>重置</Button>
      </div>
    </div>
  );
}
