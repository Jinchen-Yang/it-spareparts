// 采购分析筛选栏：日期预设（共用组件）+ 类型/采购员/销售/客户/分类多选 + 分组维度 + 口径多选。
// 客户维度被 data_customer 遮蔽时禁用客户筛选与客户分组（后端同款 403，这里先不给出入口）。
import { Button, Select, Tooltip } from "antd";
import DateRangePresets from "../../../components/DateRangePresets";
import AmountBasisSelect from "../../../components/AmountBasisSelect";
import {
  AMOUNT_BASIS_OPTIONS,
  DEFAULT_BASES,
  GROUP_BY_OPTIONS,
  type AnalyticsUrlState,
  type AmountBasis,
} from "./urlState";

export interface CustomerOption {
  value: number;
  label: string;
}

export interface FilterBarProps {
  state: AnalyticsUrlState;
  patch: (next: Partial<AnalyticsUrlState>, opts?: { resetPage?: boolean }) => void;
  sourceTypes: string[];
  purchasers: string[];
  salespersons: string[];
  categories: string[];
  customerOptions: CustomerOption[];
  customerOptionsLoading: boolean;
  onCustomerSearch?: (query: string) => void;
  customersHasMore?: boolean;
  /** data_purchase_cost=false：金额不可见 → 不渲染口径控件（后端金额也已置 null）。 */
  costRestricted: boolean;
  /** data_customer=false：客户筛选/分组不可用（后端 fail-closed 403）。 */
  customerRestricted: boolean;
  onReset: () => void;
}

const multiSelectProps = {
  mode: "multiple" as const,
  maxTagCount: "responsive" as const,
  showSearch: true,
  allowClear: true,
  style: { minWidth: 170, maxWidth: 280 },
};

export default function FilterBar({
  state,
  patch,
  sourceTypes,
  purchasers,
  salespersons,
  categories,
  customerOptions,
  customerOptionsLoading,
  onCustomerSearch,
  customersHasMore,
  costRestricted,
  customerRestricted,
  onReset,
}: FilterBarProps) {
  const change = (next: Partial<AnalyticsUrlState>) => patch(next, { resetPage: true });
  const customerSelect = (
    <Select
      {...multiSelectProps}
      aria-label="客户"
      placeholder={customerRestricted ? "客户信息不可见" : "客户（多选）"}
      value={state.customerIds}
      disabled={customerRestricted}
      loading={customerOptionsLoading}
      filterOption={false}
      onSearch={onCustomerSearch}
      notFoundContent={customerOptionsLoading ? "加载中…" : "无匹配客户"}
      popupRender={(menu) => <>{menu}{customersHasMore && <div style={{ padding: 8 }}>仅显示前 200 项，请输入客户名称缩小范围</div>}</>}
      optionFilterProp="label"
      options={customerOptions}
      onChange={(v: number[]) => change({ customerIds: v })}
    />
  );
  return (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 14 }}>
      <DateRangePresets
        preset={state.preset}
        from={state.from}
        to={state.to}
        onChange={({ preset, from, to }) => change({ preset, from, to })}
      />
      <Select
        {...multiSelectProps}
        aria-label="采购类型"
        placeholder="采购类型（多选）"
        value={state.sourceType}
        options={sourceTypes.map((v) => ({ value: v, label: v }))}
        onChange={(v: string[]) => change({ sourceType: v })}
      />
      <Select
        {...multiSelectProps}
        aria-label="采购员"
        placeholder="采购员（多选）"
        value={state.purchaser}
        options={purchasers.map((v) => ({ value: v, label: v }))}
        onChange={(v: string[]) => change({ purchaser: v })}
      />
      <Select
        {...multiSelectProps}
        aria-label="销售人员"
        placeholder="销售人员（多选）"
        value={state.salesperson}
        options={salespersons.map((v) => ({ value: v, label: v }))}
        onChange={(v: string[]) => change({ salesperson: v })}
      />
      {customerRestricted ? (
        <Tooltip title="当前账号无客户信息权限（data_customer），客户筛选不可用">
          {customerSelect}
        </Tooltip>
      ) : (
        customerSelect
      )}
      <Select
        {...multiSelectProps}
        aria-label="分类"
        placeholder="分类（多选）"
        value={state.category}
        options={categories.map((v) => ({ value: v, label: v }))}
        onChange={(v: string[]) => change({ category: v })}
      />
      <Select
        aria-label="分组维度"
        style={{ width: 116 }}
        value={state.groupBy}
        options={GROUP_BY_OPTIONS.map((o) => ({
          value: o.value,
          label: o.label,
          disabled: o.value === "customer" && customerRestricted,
        }))}
        onChange={(v: string) => change({ groupBy: v, groupKey: null })}
      />
      <AmountBasisSelect
        options={AMOUNT_BASIS_OPTIONS}
        value={costRestricted ? DEFAULT_BASES : state.basis}
        onChange={(v: string[]) => patch({ basis: v as AmountBasis[] })}
        hidden={costRestricted}
      />
      <Button onClick={onReset}>重置</Button>
    </div>
  );
}
