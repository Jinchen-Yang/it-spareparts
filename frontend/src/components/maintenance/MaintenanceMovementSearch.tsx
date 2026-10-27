import { useRef, useState } from "react";
import { Alert, Button, Card, Input, Select, Space, Table, Typography } from "antd";
import { Link } from "react-router-dom";
import { formatBusinessDate } from "../../utils/date";
import {
  searchMaintenanceMovements,
  type MaintenanceMovement,
  type MaintenanceMovementQuery,
  type MaintenanceMovementResult,
} from "../../api/maintenanceOperations";
import { readError } from "../../pages/maintenance/panel/panelUtils";

const ENTITIES = { issue: "领用", receipt: "返还实收", demand_order: "需求单", bad_return: "坏件返还" };
const FIELDS: Record<string, string> = {
  pn: "PN", serial_number: "SN", serial_numbers: "逐件 SN", source_serial_number: "导入源 SN",
  express_no: "快递单号", express_no2: "快递单号（第二列）", logistics_reference: "物流凭证",
  evidence_ref: "凭证号", note: "备注", remark: "现场备注", issue_no: "领用单号", display_no: "领用展示号",
};

export default function MaintenanceMovementSearch() {
  const [query, setQuery] = useState<MaintenanceMovementQuery>({ q: "", field: "auto", entity: "all", match_mode: "literal" });
  const [result, setResult] = useState<MaintenanceMovementResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);

  const change = (patch: Partial<MaintenanceMovementQuery>) => {
    generation.current += 1;
    setQuery((prev) => ({ ...prev, ...patch }));
    setResult(null);
    setError(null);
    setLoading(false);
  };
  const search = async (page = 1) => {
    if (!query.q.trim()) return;
    const seq = ++generation.current;
    setLoading(true);
    setError(null);
    try {
      const response = await searchMaintenanceMovements({ ...query, page, page_size: 20 });
      if (seq === generation.current) setResult(response.data);
    } catch (err) {
      if (seq === generation.current) {
        setResult(null);
        setError(readError(err, "搜索失败，请稍后重试"));
      }
    } finally {
      if (seq === generation.current) setLoading(false);
    }
  };

  return <Card title="领用与返还跨项目搜索" size="small">
    <Space direction="vertical" style={{ width: "100%" }}>
      <Space wrap>
        <Input aria-label="流转搜索词" placeholder="输入 PN、SN、快递单号或备注片段" maxLength={256}
          value={query.q} onChange={(event) => change({ q: event.target.value })}
          onPressEnter={() => void search()} style={{ width: 320, maxWidth: "100%" }} />
        <Select aria-label="搜索字段" value={query.field} style={{ width: 160 }}
          onChange={(field) => change({ field })} options={[
            { value: "auto", label: "自动" }, { value: "pn", label: "PN" }, { value: "sn", label: "SN" },
            { value: "evidence", label: "快递或凭证号" }, { value: "remark", label: "备注" },
          ]} />
        <Select aria-label="记录类型" value={query.entity} style={{ width: 130 }}
          onChange={(entity) => change({ entity })}
          options={[{ value: "all", label: "全部类型" }, ...Object.entries(ENTITIES).map(([value, label]) => ({ value, label }))]} />
        <Select aria-label="匹配方式" value={query.match_mode} style={{ width: 110 }}
          onChange={(match_mode) => change({ match_mode })}
          options={[{ value: "literal", label: "包含" }, { value: "exact", label: "精确" }]} />
        <Button type="primary" loading={loading} disabled={!query.q.trim()} onClick={() => void search()}>搜索流转</Button>
      </Space>
      <Typography.Text type="secondary">仅搜索当前权限范围；PN、SN、凭证号 1–2 字符仅精确匹配，备注包含至少两个字符。</Typography.Text>
      {error && <Alert type="error" showIcon message="流转搜索失败" description={error} />}
      {result?.unavailable_entities.includes("issue") && <Alert type="info" showIcon message="当前账号无法查询领用记录，其余类型仍可搜索" />}
      {result && <>
        <Table<MaintenanceMovement> size="small" loading={loading} pagination={false}
          rowKey={(row) => `${row.entity_type}:${row.record_id}`} dataSource={result.items}
          locale={{ emptyText: "当前权限范围内未找到" }} scroll={{ x: 1150 }} columns={[
            { title: "类型", width: 100, render: (_, row) => ENTITIES[row.entity_type] },
            { title: "项目", width: 150, render: (_, row) => <Link to={`/maintenance/projects/${encodeURIComponent(row.record_ref.project_id)}?tab=${row.record_ref.tab}`}>{row.project.name || row.project.code}</Link> },
            { title: "单号", width: 150, render: (_, row) => row.document.display_number && row.document.number.startsWith("LY-AUTO-") ? row.document.display_number : row.document.number },
            { title: "日期", width: 110, render: (_, row) => formatBusinessDate(row.document.date) },
            { title: "PN", dataIndex: "pn", width: 130 },
            { title: "SN", width: 150, render: (_, row) => <>{row.serials.join(" / ")}{row.source_serial_number && <div>导入源：{row.source_serial_number}</div>}</> },
            { title: "数量", dataIndex: "quantity", width: 90 },
            { title: "命中字段", width: 130, render: (_, row) => row.matched_fields.map((name) => FIELDS[name] || name).join("、") },
            { title: "摘要", width: 230, render: (_, row) => `${row.excerpt}${row.truncated ? "…" : ""}` },
          ]} />
        <Space>
          <Button disabled={loading || result.page <= 1} onClick={() => void search(result.page - 1)}>上一页</Button>
          <Typography.Text>第 {result.page} 页{result.has_more ? "，还有更多结果" : ""}</Typography.Text>
          <Button disabled={loading || !result.has_more} onClick={() => void search(result.page + 1)}>下一页</Button>
        </Space>
      </>}
    </Space>
  </Card>;
}
