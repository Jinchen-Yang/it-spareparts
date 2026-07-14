import { useMemo, useRef, useState } from "react";
import { Select, Tag } from "antd";
import type { SelectProps } from "antd";
import { unifiedSearch } from "../api/search";
import type { UnifiedSearchItem } from "../api/search";

/**
 * 可复用型号选择器（统一搜索入口）：远程按统一规则搜索（精确即唯一、别名折叠、
 * 相似降级），选中后**只回传 part_id**——身份用数字主键传给接口，绝不把展示文本
 * 回传后端再猜。经营看板等页面直接 <PartPicker value={partId} onChange={...} />。
 *
 * 交互：300ms 防抖 + 代次守卫（慢响应不覆盖新输入的结果，与 PoolManagementPage 同范式）。
 * 精确命中排第一并带"精确"徽标，相似候选跟在其后（分组展示）。
 */
export interface PartPickerProps {
  value?: number | null;
  onChange?: (partId: number | null, item?: UnifiedSearchItem) => void;
  placeholder?: string;
  style?: React.CSSProperties;
  disabled?: boolean;
  allowClear?: boolean;
  size?: SelectProps["size"];
  /** 初始已选项的展示信息（编辑场景回填标签用；只影响显示，不触发请求） */
  initialItem?: Pick<UnifiedSearchItem, "part_id" | "pn_std" | "description"> | null;
}

export default function PartPicker({
  value, onChange, placeholder = "搜索型号 (PN) / 别名 / 描述",
  style, disabled, allowClear = true, size,
  initialItem = null,
}: PartPickerProps) {
  const [items, setItems] = useState<UnifiedSearchItem[]>([]);
  const [similar, setSimilar] = useState<UnifiedSearchItem[]>([]);
  const [exact, setExact] = useState(false);
  const [loading, setLoading] = useState(false);
  // 已选项标签缓存：搜索结果换掉后已选 option 不失名
  const [picked, setPicked] = useState<UnifiedSearchItem | null>(null);
  const gen = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const onSearch = (kw: string) => {
    if (timer.current) clearTimeout(timer.current);
    const key = kw.trim();
    if (!key) { setItems([]); setSimilar([]); setExact(false); return; }
    timer.current = setTimeout(async () => {
      const g = ++gen.current;
      setLoading(true);
      try {
        const data = await unifiedSearch(key, { pageSize: 20 });
        if (g !== gen.current) return;   // 代次守卫：旧响应作废
        setItems(data.items || []);
        setSimilar(data.similar_items || []);
        setExact(!!data.exact);
      } catch {
        if (g === gen.current) { setItems([]); setSimilar([]); setExact(false); }
      } finally {
        if (g === gen.current) setLoading(false);
      }
    }, 300);
  };

  const optionOf = (it: UnifiedSearchItem) => ({
    value: it.part_id,
    // Select 搜索/回显用的文本（filterOption=false 时仅作展示兜底）
    label: (
      <span>
        <span style={{ fontFamily: "monospace" }}>{it.pn_std}</span>
        {(it.match_type === "exact_pn" || it.match_type === "exact_alias") && (
          <Tag color="green" style={{ marginLeft: 6 }}>精确</Tag>
        )}
        {it.pool_name && <Tag color="geekblue" style={{ marginLeft: 6 }}>{it.pool_name}</Tag>}
        <span style={{ color: "var(--mb-text-3)", marginLeft: 8, fontSize: 12 }}>
          {(it.description || "").slice(0, 40)}
        </span>
      </span>
    ),
    item: it,
  });

  const options = useMemo(() => {
    const seen = new Set<number>();
    const primary = items.filter((it) => it.part_id && !seen.has(it.part_id) && seen.add(it.part_id));
    const sims = similar.filter((it) => it.part_id && !seen.has(it.part_id) && seen.add(it.part_id));
    const groups: NonNullable<SelectProps["options"]> = [];
    if (primary.length) {
      groups.push(exact
        ? { label: "精确匹配", options: primary.map(optionOf) }
        : { label: "搜索结果", options: primary.map(optionOf) });
    }
    if (sims.length) groups.push({ label: "相似型号（非精确）", options: sims.map(optionOf) });
    // 已选项不在当前结果里 → 附加隐藏组保标签（antd 无匹配 option 时会显示裸 value 数字）
    const current = picked || (initialItem
      ? { ...initialItem, brand: null, category: null, category_major: null,
          needs_review: false, is_excluded: false, pool_group_id: null, pool_name: null,
          description: initialItem.description ?? null } as UnifiedSearchItem
      : null);
    if (current?.part_id && !seen.has(current.part_id) && current.part_id === value) {
      groups.push({ label: "已选", options: [optionOf(current)] });
    }
    return groups;
  }, [items, similar, exact, picked, initialItem, value]);

  return (
    <Select
      showSearch
      filterOption={false}
      value={value ?? undefined}
      placeholder={placeholder}
      style={{ minWidth: 260, ...style }}
      disabled={disabled}
      allowClear={allowClear}
      size={size}
      loading={loading}
      onSearch={onSearch}
      notFoundContent={loading ? "搜索中…" : "输入型号/别名/描述搜索"}
      onChange={(v, opt: any) => {
        const it: UnifiedSearchItem | undefined = Array.isArray(opt) ? opt[0]?.item : opt?.item;
        setPicked(it || null);
        onChange?.(typeof v === "number" ? v : null, it);
      }}
      options={options}
      optionLabelProp="label"
    />
  );
}
