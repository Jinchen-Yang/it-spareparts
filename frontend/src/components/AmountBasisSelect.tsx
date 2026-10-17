// 金额口径多选（D-39 / Q7：可选一项或多项并排显示，页面本地状态，不碰管理员全站设置）。
// 命名保持通用（不带 purchase/sales 前缀）：采购分析三口径与销售分析三金额（PR-4b）共用。
import { Select } from "antd";

export interface AmountBasisOption {
  value: string;
  label: string;
}

export interface AmountBasisSelectProps {
  options: AmountBasisOption[];
  value: string[];
  onChange: (next: string[]) => void;
  /** 控件展示名（无障碍标签与占位文案用）。 */
  label?: string;
  /** 看不到金额的账号直接不渲染（数据面真值仍是后端脱敏，这里只是 UI 门控）。 */
  hidden?: boolean;
  disabled?: boolean;
}

export default function AmountBasisSelect({
  options,
  value,
  onChange,
  label = "金额口径",
  hidden,
  disabled,
}: AmountBasisSelectProps) {
  if (hidden) return null;
  const valid = new Set(options.map((o) => o.value));
  return (
    <Select
      aria-label={label}
      mode="multiple"
      style={{ minWidth: 190, maxWidth: 300 }}
      placeholder={`选择${label}（可多选）`}
      value={value.filter((v) => valid.has(v))}
      maxTagCount="responsive"
      disabled={disabled}
      options={options.map((o) => ({ value: o.value, label: o.label }))}
      onChange={(next) => {
        // 至少保留一项：全不选等于「没有金额列」，和业务预期不符（与后端恒返三口径对齐）
        const cleaned = next.filter((v) => valid.has(v));
        if (cleaned.length > 0) onChange(cleaned);
      }}
    />
  );
}
