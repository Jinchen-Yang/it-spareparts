import { Alert, Space, Typography } from "antd";
import type { WbddImportReport } from "../../api/maintenanceWbddImport";

const { Text } = Typography;

const FIELD_LABELS: Record<string, string> = {
  pn_raw: "原始 PN", pn_std: "标准 PN", part_id: "PN 关联",
  description: "描述", qty: "需求数量", return_qty: "退货数量", serial_numbers: "SN", line_note: "备注",
};

/** 聚合回执只解释本批处理结果，不用原始身份或字段值生成样例。 */
export default function WbddUploadFeedback({ report }: { report: Partial<WbddImportReport> }) {
  const counts: [string, number | null | undefined][] = [
    ["新增", report.fact_rows_inserted],
    ["匹配已有", report.fact_rows_updated],
    ["跳过", report.fact_rows_skipped],
    ["实际变更", report.fact_rows_semantic_updated],
    ["无变化", report.fact_rows_unchanged],
    ["人工保护", report.fact_rows_protected],
    ["历史保护待核对", report.fact_rows_legacy_review],
  ];
  const protectedFields = Object.entries(FIELD_LABELS).flatMap(([field, label]) => {
    const count = report.protected_field_counts?.[field];
    return typeof count === "number" && count > 0 ? [`${label} ${count} 行`] : [];
  });
  const hasMissing = (report.snapshot_diff?.missing_orders ?? 0) > 0
    || (report.snapshot_diff?.missing_lines ?? 0) > 0;
  return (
    <Alert
      showIcon
      type={hasMissing || (report.fact_rows_protected ?? 0) > 0 ? "warning" : "info"}
      message={`原件上传回执${report.batch_id != null ? `（批次 #${report.batch_id}）` : ""}`}
      description={(
        <Space direction="vertical" size={4} style={{ width: "100%", overflowWrap: "anywhere" }}>
          {report.import_mode ? (
            <Text>{report.import_mode === "skip" ? "跳过模式：已有单据和明细不覆盖" : "更新模式：更新合法源值并保留人工保护"}</Text>
          ) : null}
          <Text>新增或变更单头 {report.orders_semantic_updated ?? "—"} 张</Text>
          <Text>本批明细处理行数（不含单头状态、地址变化）</Text>
          <Space wrap>
            {counts.map(([label, count]) => (
              <Text key={label}>{label} {count ?? "—"} 行</Text>
            ))}
          </Space>
          {report.snapshot_diff ? (
            <Space wrap>
              <Text>缺失整单 {report.snapshot_diff.missing_orders} 张</Text>
              {typeof report.snapshot_diff.missing_lines === "number"
                ? <Text>缺失明细 {report.snapshot_diff.missing_lines} 行</Text> : null}
              <Text type="secondary">缺项需核对并确认，上传不会自动作废。</Text>
            </Space>
          ) : null}
          {protectedFields.length ? (
            <Space wrap><Text>受保护字段：</Text>{protectedFields.map((field) => <Text key={field}>{field}</Text>)}</Space>
          ) : null}
          <Text type="secondary">匹配已有不代表实际覆盖；未提供的统计不代表 0。人工保护与实际变更或无变化可出现在同一行，不能相加。</Text>
        </Space>
      )}
    />
  );
}
