// 销售分析筛选栏：日期预设（共用组件）+ 业务类型多选（候选取值带全时间计数）+
// 客户/销售人员模糊搜索 + 三金额多选（共用组件）+ 含已退货开关（D-40 §5，默认不含）。
// 客户权限（data_customer）受限时禁用客户搜索（后端对带 customer 的请求 fail-closed
// 403，这里先不给入口）；对账权限（data_reconciliation）受限时开关无意义，不渲染。
import { useEffect, useState } from "react";
import { Button, Input, Select, Switch, Tooltip } from "antd";
import DateRangePresets from "../../../components/DateRangePresets";
import AmountBasisSelect from "../../../components/AmountBasisSelect";
import type { SalesAnalyticsBusinessTypeOption } from "../../../api/salesAnalytics";
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
  /** data_customer=false：客户搜索不可用（后端 fail-closed 403）。 */
  customerRestricted: boolean;
  /** data_reconciliation=false：对账字段不可见，含退货开关不渲染。 */
  reconRestricted: boolean;
  onReset: () => void;
}

const multiSelectProps = {
  mode: "multiple" as const,
  maxTagCount: "responsive" as const,
  showSearch: true,
  allowClear: true,
  style: { minWidth: 180, maxWidth: 300 },
};

/** 模糊搜索框：输入是本地草稿，回车/清除才提交进 URL；URL 侧变化（重置、后退、
 * 深链更新）反向同步回输入框——非受控 defaultValue 在重置后会残留旧文本。 */
function DelayedSearchInput({
  value,
  onCommit,
  ariaLabel,
  placeholder,
  disabled,
}: {
  value: string | null;
  onCommit: (v: string | null) => void;
  ariaLabel: string;
  placeholder: string;
  disabled?: boolean;
}) {
  const [text, setText] = useState(value ?? "");
  useEffect(() => { setText(value ?? ""); }, [value]);
  return (
    <Input.Search
      aria-label={ariaLabel}
      placeholder={placeholder}
      value={text}
      onChange={(e) => setText(e.target.value)}
      onSearch={(v) => onCommit(v.trim() || null)}
      disabled={disabled}
      allowClear
      style={{ width: 180 }}
    />
  );
}

export default function FilterBar({
  state,
  patch,
  businessTypeOptions,
  customerRestricted,
  reconRestricted,
  onReset,
}: FilterBarProps) {
  const change = (next: Partial<SalesAnalyticsUrlState>) => patch(next, { resetPage: true });

  const customerInput = customerRestricted ? (
    <Tooltip title="当前账号无客户信息权限（data_customer），客户筛选不可用">
      <DelayedSearchInput
        value={null}
        onCommit={() => undefined}
        ariaLabel="客户"
        placeholder="客户信息不可见"
        disabled
      />
    </Tooltip>
  ) : (
    <DelayedSearchInput
      value={state.customer}
      onCommit={(v) => change({ customer: v })}
      ariaLabel="客户"
      placeholder="客户（模糊搜索，回车生效）"
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
      <Tooltip title="候选取值与计数来自全部已生效销售单（不随日期区间变化）">
        <Select
          {...multiSelectProps}
          aria-label="业务类型"
          placeholder="业务类型（多选）"
          value={state.businessType}
          optionFilterProp="label"
          options={businessTypeOptions.map((o) => ({
            // value=null 的「未填写」组无法作为筛选值传给后端（空串会被清洗丢弃），
            // 保留计数展示但禁选，不丢弃取值面
            value: o.value ?? "__null__",
            label: o.value == null ? `（未填写） ${o.orders}` : `${o.value}（${o.orders}）`,
            disabled: o.value == null,
          }))}
          onChange={(v: string[]) => change({ businessType: v.filter((x) => x !== "__null__") })}
        />
      </Tooltip>
      {customerInput}
      <DelayedSearchInput
        value={state.salesperson}
        onCommit={(v) => change({ salesperson: v })}
        ariaLabel="销售人员"
        placeholder="销售人员（模糊搜索，回车生效）"
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
      <Button onClick={onReset}>重置</Button>
    </div>
  );
}
