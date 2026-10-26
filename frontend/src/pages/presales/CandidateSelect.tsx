import { useEffect, useState } from "react";
import { Select, Typography } from "antd";
import type { PresalesCandidate } from "../../api/presales";

interface Props {
  label: string;
  value?: number | null;
  onChange: (id?: number) => void;
  load: (q: string, signal?: AbortSignal) => Promise<PresalesCandidate[]>;
  disabled?: boolean;
}

/** 销售、采购候选各走自己的权限接口；取消过期搜索，保留已选名称。 */
export default function CandidateSelect({ label, value, onChange, load, disabled }: Props) {
  const [query, setQuery] = useState("");
  const [options, setOptions] = useState<PresalesCandidate[]>([]);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    const timer = window.setTimeout(() => {
      load(query, controller.signal).then((rows) => {
        if (controller.signal.aborted) return;
        setOptions((previous) => {
          const selected = previous.find((row) => row.id === value);
          return selected && !rows.some((row) => row.id === value) ? [selected, ...rows] : rows;
        });
        setFailed(false);
      }).catch(() => { if (!controller.signal.aborted) setFailed(true); })
        .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, query ? 200 : 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [query, load, value]);
  const visibleOptions = value && !options.some((row) => row.id === value)
    ? [{ id: value, name: `#${value}` }, ...options] : options;
  return <>
    <Select aria-label={label} showSearch allowClear filterOption={false} disabled={disabled} loading={loading}
      style={{ width: "100%", minWidth: 200 }} value={value ?? undefined} onSearch={setQuery} onChange={onChange}
      options={visibleOptions.map((row) => ({ value: row.id, label: row.name }))} placeholder={`搜索并选择${label}`} />
    {failed && <Typography.Text type="secondary">候选加载失败，请重试；已有选择保留。</Typography.Text>}
  </>;
}
