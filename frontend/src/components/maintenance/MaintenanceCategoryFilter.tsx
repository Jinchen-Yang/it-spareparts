import { useAuthorizedQuery } from "../../authz/access";
import { actionRule } from "../../authz/rules";
import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Select, Space } from "antd";
import {
  listMaintenancePartCategories,
  type MaintenanceCategoryFilterInput,
  type MaintenancePartCategory,
} from "../../api/maintenanceOperations";

/** 筛选仅使用当前主数据库品类，不重新判定明细的返还要求。 */
export default function MaintenanceCategoryFilter({ value, onChange, labelPrefix }: {
  value: MaintenanceCategoryFilterInput;
  onChange: (value: MaintenanceCategoryFilterInput) => void;
  labelPrefix: string;
}) {
  const query = useAuthorizedQuery({ rule: actionRule("part_lookup", "read"), legacy: { part_lookup: true },
    queryKey: ["maintenance-category-filter"], query: async capture => {
      const result = await listMaintenancePartCategories(capture);
      if (!Array.isArray(result.data.categories)) throw new Error("invalid_categories");
      return result.data.categories;
    },
  });
  const categories = query.data ?? [], loading = query.loading, failed = !!query.error;
  const load = query.retry;
  const unlinked = value.category_state === "unlinked";
  const minorDisabled = unlinked || !value.category_major || value.category_state === "unclassified";
  const minors = categories.find((category) => category.major === value.category_major)?.minors ?? [];

  return <Space direction="vertical" size={4} style={{ width: "100%" }}>
    <Space wrap size={8}>
      <Select
        aria-label={`${labelPrefix}主数据分类状态`}
        value={value.category_state}
        allowClear
        placeholder="全部分类状态"
        style={{ width: 165 }}
        options={[
          { label: "未分类", value: "unclassified" },
          { label: "未关联主数据", value: "unlinked" },
        ]}
        onChange={(state: MaintenanceCategoryFilterInput["category_state"]) => onChange({
          ...(state === "unlinked" ? {} : value),
          category_state: state,
          category_minor: state ? undefined : value.category_minor,
        })}
      />
      <Select
        aria-label={`${labelPrefix}一级品类`}
        value={value.category_major}
        allowClear
        showSearch
        optionFilterProp="label"
        placeholder="全部一级品类"
        loading={loading}
        disabled={unlinked || !query.allowed}
        style={{ width: 165 }}
        options={categories.map((category) => ({ label: category.major, value: category.major }))}
        onChange={(major: string | undefined) => onChange({ ...value, category_major: major, category_minor: undefined })}
      />
      <Select
        aria-label={`${labelPrefix}二级品类`}
        value={value.category_minor}
        allowClear
        showSearch
        optionFilterProp="label"
        placeholder="全部二级品类"
        disabled={minorDisabled || !query.allowed}
        style={{ width: 165 }}
        options={minors.map((minor) => ({ label: minor, value: minor }))}
        onChange={(minor: string | undefined) => onChange({ ...value, category_minor: minor })}
      />
    </Space>
    {failed ? <Alert type="warning" showIcon message="品类选项加载失败，仍可查看记录或筛选未分类、未关联主数据"
      action={<Button size="small" onClick={() => { void load(); }}>重试加载品类</Button>} /> : null}
  </Space>;
}
