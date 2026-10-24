import { useEffect, useRef, useState } from "react";
import { useContextQuery } from "../../authz/contextQuery";
import { useContextTransport } from "../../authz/domainTransport";
import { useContextAdmission } from "../../authz/access";
import { actionRule, allRule, anyRule, surfaceRule } from "../../authz/rules";
import { Alert, Button, Space, Spin, Typography } from "antd";
import { exportProjectRows, getMaintenanceRowFields, readRowFilterError } from "../../api/maintenanceRowFilters";
import type {
  MaintenanceRowFields, MaintenanceRowFilter, ProjectRowFilterQuery, RowExportFormat,
} from "../../api/maintenanceRowFilters";
import { saveBlob } from "../../api/maintenanceWorkbooks";
import MaintenanceRowFilterBuilder from "./MaintenanceRowFilterBuilder";

export default function ProjectMaintenanceRowFilters({
  projectId, query, total, loading, onApply,
}: {
  projectId: string;
  query: ProjectRowFilterQuery;
  total: number | null;
  loading: boolean;
  onApply: (filters: MaintenanceRowFilter[]) => void;
}) {
  const rule = anyRule(surfaceRule("maintenance_managed_rows.project_rows"), surfaceRule("maintenance_row_workbench.project_rows"));
  const fields = useContextQuery({ entryId: "GET /api/maintenance/projects/stable/{project_id}/master-workbook/row-fields",
    rule, queryKey: [projectId], legacyAllowed: true, query: capture => getMaintenanceRowFields(projectId, true, capture) });
  const catalog = fields.data, fieldsError = fields.error ? "筛选字段加载失败" : null;
  const loadFields = fields.retry;
  const admission = useContextAdmission();
  const canExport = admission.state(anyRule(...["maintenance_managed_rows", "maintenance_row_workbench"].map(id =>
    allRule(surfaceRule(`${id}.project_rows`), actionRule(id, "export")))), true) !== "deny";
  const capture = useContextTransport(JSON.stringify([projectId, query]), true);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<RowExportFormat | null>(null);
  const exportSequence = useRef(0);
  useEffect(() => {
    exportSequence.current += 1;
    setExportError(null);
    setExporting(null);
    return () => { exportSequence.current += 1; };
  }, [projectId, query.filters, query.order_no, query.contract_no]);
  const download = async (format: RowExportFormat) => {
    if (!canExport || !capture.isCurrent()) return;
    const current = ++exportSequence.current;
    setExporting(format);
    setExportError(null);
    try {
      const blob = await exportProjectRows(projectId, query, format, capture);
      if (current === exportSequence.current && capture.isCurrent()) saveBlob(blob, `项目备件-核对.${format}`);
    } catch (err) {
      const text = await readRowFilterError(err, "导出失败，请重试");
      if (current === exportSequence.current && capture.isCurrent()) setExportError(text);
    } finally {
      if (current === exportSequence.current && capture.isCurrent()) setExporting(null);
    }
  };
  if (fields.entryState === "deny" && !canExport) return null;
  return (
    <Space className="maintenance-row-workbench" direction="vertical" size={12}
      style={{ width: "100%", margin: "16px 0" }}>
      {fieldsError ? <Alert type="error" showIcon message={fieldsError}
        action={<Button onClick={() => void loadFields()}>重试字段</Button>} /> : null}
      {fields.entryState !== "deny" && !catalog && !fieldsError ? <Spin tip="正在加载筛选字段"><div style={{ height: 40 }} /></Spin> : null}
      {catalog ? <>
        <MaintenanceRowFilterBuilder key={projectId} fields={catalog.fields} onApply={onApply} loading={loading} />
        {!catalog.amount_filter_allowed
          ? <Alert type="info" showIcon message="当前项目没有金额资格，金额字段及金额条件不可用。" /> : null}
      </> : null}
      <Space wrap>
        <Typography.Text>{total === null ? "行数待查询" : `共 ${total} 行`}</Typography.Text>
        {canExport && (["csv", "xlsx"] as const).map((format) => (
          <Button key={format} loading={exporting === format}
            disabled={loading || total === null || exporting !== null}
            onClick={() => void download(format)}>导出完整筛选集 {format.toUpperCase()}</Button>
        ))}
      </Space>
      <Typography.Text type="secondary">
        核对导出不可回传；“下载备件成本”和页头总表保留完整回传范围。
      </Typography.Text>
      {exportError ? <Alert type="error" showIcon message={exportError} /> : null}
    </Space>
  );
}
