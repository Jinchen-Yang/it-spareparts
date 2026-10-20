import { useCallback, useEffect, useRef, useState } from "react";
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
  const [catalog, setCatalog] = useState<MaintenanceRowFields | null>(null);
  const [fieldsError, setFieldsError] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<RowExportFormat | null>(null);
  const fieldSequence = useRef(0);
  const exportSequence = useRef(0);
  const loadFields = useCallback(async () => {
    const current = ++fieldSequence.current;
    setCatalog(null);
    setFieldsError(null);
    try {
      const response = await getMaintenanceRowFields(projectId, true);
      if (current === fieldSequence.current) setCatalog(response);
    } catch (err) {
      const text = await readRowFilterError(err, "筛选字段加载失败");
      if (current === fieldSequence.current) setFieldsError(text);
    }
  }, [projectId]);
  useEffect(() => {
    void loadFields();
    return () => { fieldSequence.current += 1; };
  }, [loadFields]);
  useEffect(() => {
    exportSequence.current += 1;
    setExportError(null);
    setExporting(null);
    return () => { exportSequence.current += 1; };
  }, [projectId, query.filters, query.order_no, query.contract_no]);
  const download = async (format: RowExportFormat) => {
    const current = ++exportSequence.current;
    setExporting(format);
    setExportError(null);
    try {
      const blob = await exportProjectRows(projectId, query, format);
      if (current === exportSequence.current) saveBlob(blob, `项目备件-核对.${format}`);
    } catch (err) {
      const text = await readRowFilterError(err, "导出失败，请重试");
      if (current === exportSequence.current) setExportError(text);
    } finally {
      if (current === exportSequence.current) setExporting(null);
    }
  };
  return (
    <Space className="maintenance-row-workbench" direction="vertical" size={12}
      style={{ width: "100%", margin: "16px 0" }}>
      {fieldsError ? <Alert type="error" showIcon message={fieldsError}
        action={<Button onClick={() => void loadFields()}>重试字段</Button>} /> : null}
      {!catalog && !fieldsError ? <Spin tip="正在加载筛选字段"><div style={{ height: 40 }} /></Spin> : null}
      {catalog ? <>
        <MaintenanceRowFilterBuilder key={projectId} fields={catalog.fields} onApply={onApply} loading={loading} />
        {!catalog.amount_filter_allowed
          ? <Alert type="info" showIcon message="当前项目没有金额资格，金额字段及金额条件不可用。" /> : null}
      </> : null}
      <Space wrap>
        <Typography.Text>{total === null ? "行数待查询" : `共 ${total} 行`}</Typography.Text>
        {(["csv", "xlsx"] as const).map((format) => (
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
