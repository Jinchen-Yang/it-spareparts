/**
 * 全局筛选栏：时间（今天/近7天/近30天/本月/自定义）+ PN 型号 + 互通池 + 采购员/销售员 + 清除。
 * 所有筛选写入 URL（push 进历史）：刷新/复制链接/前进后退均可恢复。
 */
import { useMemo, useState } from "react";
import { Button, DatePicker, Input, Segmented, Select, Tag } from "antd";
import { ClearOutlined } from "@ant-design/icons";
import dayjs, { type Dayjs } from "dayjs";
import PartPicker from "../../components/PartPicker";
import { dashboardPools, type PoolsResp } from "../../api";
import {
  MUTED, RANGE_OPTIONS, useGuardedFetch, type BoardFilters, type DateRange, type RangeKey,
} from "./shared";

const { RangePicker } = DatePicker;

interface FilterBarProps {
  filters: BoardFilters;
  dateRange: DateRange;
  patch: (next: Record<string, string | number | null | undefined>, opts?: { replace?: boolean }) => void;
  clearAll: () => void;
  hasFilter: boolean;
}

/** 人员筛选输入：Enter/清除时提交到 URL（不逐键触发请求） */
function PersonInput({ label, value, onCommit }: {
  label: string; value: string | null; onCommit: (v: string | null) => void;
}) {
  const [draft, setDraft] = useState(value ?? "");
  // URL 外部变化（后退/清除）时同步草稿
  const [lastValue, setLastValue] = useState(value);
  if (value !== lastValue) { setLastValue(value); setDraft(value ?? ""); }
  return (
    <Input
      allowClear size="small" style={{ width: 120 }}
      placeholder={label} aria-label={`按${label}筛选`}
      value={draft}
      onChange={(e) => {
        setDraft(e.target.value);
        if (!e.target.value) onCommit(null);   // 点清除按钮即生效
      }}
      onPressEnter={() => onCommit(draft.trim() || null)}
      onBlur={() => onCommit(draft.trim() || null)}
    />
  );
}

export default function FilterBar({ filters, dateRange, patch, clearAll, hasFilter }: FilterBarProps) {
  // 池选项：v2 指标排序路径（固定 3 条 SQL，不逐池 analyze）；生产 ~40 池，100 封顶足够
  const pools = useGuardedFetch<PoolsResp>(
    () => dashboardPools({ sort: "sales_total", page: 1, page_size: 100 }), []);
  const poolOptions = useMemo(() => (pools.data?.items || []).map((p) => ({
    value: p.group_id,
    label: p.name ? `${p.name}（#${p.group_id}）` : `池 #${p.group_id}`,
  })), [pools.data]);

  const customValue: [Dayjs, Dayjs] | null =
    filters.rangeKey === "custom" && filters.from && filters.to
      ? [dayjs(filters.from), dayjs(filters.to)] : null;

  return (
    <div role="search" aria-label="看板全局筛选"
      style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 4 }}>
      <Segmented size="small" value={filters.rangeKey}
        options={RANGE_OPTIONS}
        onChange={(v) => {
          const key = v as RangeKey;
          patch(key === "custom" ? { range: key } : { range: key, from: null, to: null });
        }} />
      {filters.rangeKey === "custom" && (
        <RangePicker size="small" value={customValue} allowClear={false}
          disabledDate={(d) => d.isAfter(dayjs(), "day")}
          onChange={(v) => {
            if (v && v[0] && v[1]) patch({ from: v[0].format("YYYY-MM-DD"), to: v[1].format("YYYY-MM-DD") });
          }} />
      )}
      <PartPicker size="small" style={{ width: 220 }}
        placeholder="PN 型号筛选"
        value={filters.partId}
        initialItem={filters.partId && filters.partPn
          ? { part_id: filters.partId, pn_std: filters.partPn, description: null } : null}
        onChange={(partId, item) =>
          patch({ part_id: partId ?? null, pn: partId ? (item?.pn_std ?? filters.partPn) : null })} />
      <Select size="small" allowClear showSearch style={{ width: 180 }}
        placeholder="互通池筛选" aria-label="按互通池筛选"
        value={filters.poolId ?? undefined}
        options={poolOptions}
        loading={pools.loading}
        optionFilterProp="label"
        onChange={(v) => patch({ pool: v ?? null })} />
      <PersonInput label="采购员" value={filters.purchaser}
        onCommit={(v) => { if (v !== filters.purchaser) patch({ buyer: v }); }} />
      <PersonInput label="销售员" value={filters.salesperson}
        onCommit={(v) => { if (v !== filters.salesperson) patch({ sp: v }); }} />
      {hasFilter && (
        <Button size="small" icon={<ClearOutlined />} onClick={clearAll} aria-label="清除全部筛选">
          清除筛选
        </Button>
      )}
      <span style={MUTED} aria-live="polite">
        统计范围：{dateRange.date_from} ~ {dateRange.date_to}
        {filters.partId && <Tag style={{ marginLeft: 6 }}>PN：{filters.partPn || `#${filters.partId}`}</Tag>}
        {filters.poolId && <Tag>池 #{filters.poolId}</Tag>}
        {filters.purchaser && <Tag>采购员：{filters.purchaser}</Tag>}
        {filters.salesperson && <Tag>销售员：{filters.salesperson}</Tag>}
      </span>
    </div>
  );
}
