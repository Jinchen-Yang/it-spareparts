import { Select } from "antd";

/**
 * 生产快照里的常见采购类型只是输入建议，不是枚举边界。
 * tags 单选受控模式允许用户录入以后新增的氚云类型，URL 保存原文。
 */
export const PURCHASE_TYPE_SUGGESTIONS = [
  "销售订单",
  "维保需求",
  "指定采购",
  "其他采购",
  "批量采购",
  "委外维修",
  "回收",
  "采购申请",
];

export default function PurchaseTypeSelect({
  value,
  onChange,
  mobile = false,
}: {
  value?: string | null;
  onChange: (value: string | undefined) => void;
  mobile?: boolean;
}) {
  return (
    <span aria-label="采购类型筛选" style={{ display: "inline-block",
      width: mobile ? "100%" : "min(100%, 220px)", maxWidth: "100%" }}>
      <Select
        aria-label="采购类型"
        mode="tags"
        allowClear
        showSearch
        placeholder="采购类型"
        value={value ? [value] : []}
        options={PURCHASE_TYPE_SUGGESTIONS.map((item) => ({ label: item, value: item }))}
        onChange={(values) => {
          const next = values[values.length - 1]?.trim();
          onChange(next || undefined);
        }}
        maxTagCount={1}
        style={{ width: "100%", maxWidth: "100%" }}
      />
    </span>
  );
}
