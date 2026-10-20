import { useState } from "react";
import { Alert, Button, Input, InputNumber, Select, Space, Typography } from "antd";
import type { MaintenanceRowField, MaintenanceRowFilter, RowFilterOperator } from "../../api/maintenanceRowFilters";
import "./maintenanceRowFilters.css";

const LABELS: Record<RowFilterOperator, string> = {
  contains: "包含", eq: "等于", prefix: "前缀", is_empty: "为空", in: "属于", range: "范围（含边界）",
};

export default function MaintenanceRowFilterBuilder({
  fields, onApply, loading = false,
}: {
  fields: MaintenanceRowField[];
  onApply: (filters: MaintenanceRowFilter[]) => void;
  loading?: boolean;
}) {
  const [drafts, setDrafts] = useState<MaintenanceRowFilter[]>([]);
  const [error, setError] = useState<string | null>(null);
  const patch = (index: number, filter: MaintenanceRowFilter) => {
    setDrafts((previous) => previous.map((item, i) => i === index ? filter : item));
    setError(null);
  };
  const apply = () => {
    if (drafts.some((filter) => filter.op === "range" && !filter.min && !filter.max)) {
      setError("范围至少填写一个边界");
      return;
    }
    if (drafts.some((filter) => filter.op !== "is_empty" && filter.op !== "range"
      && (!filter.value || (Array.isArray(filter.value) && !filter.value.length)))) {
      setError("请填写筛选条件的值");
      return;
    }
    setError(null);
    onApply(drafts.map((filter) => {
      const base = { field: filter.field, op: filter.op };
      if (filter.op === "is_empty") return base;
      if (filter.op === "range") return {
        ...base,
        ...(filter.min ? { min: filter.min } : {}),
        ...(filter.max ? { max: filter.max } : {}),
      };
      return { ...base, value: filter.value };
    }));
  };
  return (
    <div className="maintenance-row-filters" role="region" aria-label="行级筛选条件">
      <Typography.Text type="secondary">同时满足以下全部条件</Typography.Text>
      {drafts.map((filter, index) => {
        const field = fields.find((item) => item.field === filter.field);
        return (
          <div className="maintenance-row-filter-condition" key={index}>
            <Select
              aria-label={`条件 ${index + 1} 的字段`}
              value={filter.field}
              options={fields.map((item) => ({ value: item.field, label: item.label }))}
              showSearch optionFilterProp="label"
              onChange={(name) => {
                const next = fields.find((item) => item.field === name)!;
                patch(index, { field: name, op: next.operators[0] });
              }}
            />
            <Select
              aria-label={`条件 ${index + 1} 的运算`}
              value={filter.op}
              options={field?.operators.map((op) => ({ value: op, label: LABELS[op] }))}
              onChange={(op) => patch(index, { field: filter.field, op })}
            />
            {filter.op === "is_empty" ? <Typography.Text type="secondary">无需填写值</Typography.Text>
              : filter.op === "range" ? (
                <div className="maintenance-row-range">
                  {(["min", "max"] as const).map((bound) => {
                    const label = `条件 ${index + 1} 的${bound === "min" ? "最小值" : "最大值"}`;
                    return field?.type === "number" ? (
                      <InputNumber<string> key={bound} stringMode aria-label={label}
                        placeholder={bound === "min" ? "最小值（可留空）" : "最大值（可留空）"}
                        value={filter[bound]} style={{ width: "100%" }}
                        onChange={(value) => patch(index, { ...filter, [bound]: value ?? undefined })} />
                    ) : (
                      <Input key={bound} type="date" aria-label={label} value={filter[bound] ?? ""}
                        onChange={(event) => patch(index, { ...filter, [bound]: event.target.value })} />
                    );
                  })}
                </div>
              ) : filter.op === "in" ? (
                <Select mode="multiple" aria-label={`条件 ${index + 1} 的值`}
                  placeholder="选择一个或多个值" options={field?.options} maxTagCount="responsive"
                  value={Array.isArray(filter.value) ? filter.value : []}
                  onChange={(value: string[]) => patch(index, { ...filter, value })} />
              ) : field?.type === "number" ? (
                <InputNumber<string> stringMode aria-label={`条件 ${index + 1} 的值`}
                  value={typeof filter.value === "string" ? filter.value : undefined}
                  onChange={(value) => patch(index, { ...filter, value: value ?? undefined })} />
              ) : (
              <Input
                type={field?.type === "date" ? "date" : "text"}
                aria-label={`条件 ${index + 1} 的值`}
                value={typeof filter.value === "string" ? filter.value : ""}
                onChange={(event) => patch(index, { ...filter, value: event.target.value })}
              />
            )}
            <Button aria-label={`删除条件 ${index + 1}`} onClick={() => setDrafts((old) => old.filter((_, i) => i !== index))}>删除</Button>
          </div>
        );
      })}
      {error ? <Alert type="error" showIcon message={error} /> : null}
      <Space wrap>
        <Button disabled={!fields.length} onClick={() => {
          const field = fields[0];
          setDrafts((old) => [...old, { field: field.field, op: field.operators[0] }]);
        }}>添加筛选条件</Button>
        <Button type="primary" loading={loading} onClick={apply}>应用筛选</Button>
        <Button onClick={() => { setDrafts([]); setError(null); onApply([]); }}>清空筛选</Button>
      </Space>
    </div>
  );
}
