import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Card, Select, Space, Spin, Table, Typography } from "antd";
import {
  exportMaintenanceRows, getMaintenanceRowFields, listMaintenanceWorkbenchRows,
  readRowFilterError, serializeRowFilters,
} from "../../api/maintenanceRowFilters";
import type {
  MaintenanceRowFields, MaintenanceRowFilter, MaintenanceWorkbenchRow, RowExportFormat, RowRangeParams,
} from "../../api/maintenanceRowFilters";
import { saveBlob } from "../../api/maintenanceWorkbooks";
import { listMaintenanceProjects } from "../../api/maintenanceProjects";
import type { MaintenanceProject } from "../../api/maintenanceProjects";
import MaintenanceRowFilterBuilder from "./MaintenanceRowFilterBuilder";

export default function GlobalMaintenanceRowWorkbench({ rangeParams }: { rangeParams: RowRangeParams }) {
  const [catalog, setCatalog] = useState<MaintenanceRowFields | null>(null);
  const [projectId, setProjectId] = useState<string | undefined>();
  const [projects, setProjects] = useState<MaintenanceProject[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(false);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [fieldsError, setFieldsError] = useState<string | null>(null);
  const [filters, setFilters] = useState<MaintenanceRowFilter[]>([]);
  const [rows, setRows] = useState<MaintenanceWorkbenchRow[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<RowExportFormat | null>(null);
  const sequence = useRef(0);
  const fieldsSequence = useRef(0);
  const projectsSequence = useRef(0);
  const exportSequence = useRef(0);
  const query = useMemo(() => ({
    ...rangeParams,
    ...(projectId ? { project_id: projectId } : {}),
    ...(filters.length ? { filters: serializeRowFilters(filters) } : {}),
  }), [rangeParams.range, rangeParams.from, rangeParams.to, filters, projectId]);
  const loadProjects = useCallback(async () => {
    const current = ++projectsSequence.current;
    setProjectsLoading(true);
    setProjectsError(null);
    try {
      const all: MaintenanceProject[] = [];
      let pageNumber = 1;
      let directoryTotal = 0;
      do {
        const response = await listMaintenanceProjects({ page: pageNumber, page_size: 200, include_inactive: true });
        if (current !== projectsSequence.current) return;
        all.push(...response.data.rows);
        directoryTotal = response.data.total;
        if (!response.data.rows.length) break;
        pageNumber += 1;
      } while (all.length < directoryTotal);
      setProjects(all);
    } catch (err) {
      const text = await readRowFilterError(err, "项目目录加载失败");
      if (current !== projectsSequence.current) return;
      setProjects([]);
      setProjectsError(text);
    } finally {
      if (current === projectsSequence.current) setProjectsLoading(false);
    }
  }, []);
  useEffect(() => {
    void loadProjects();
    return () => { projectsSequence.current += 1; };
  }, [loadProjects]);
  const loadFields = useCallback(async () => {
    const current = ++fieldsSequence.current;
    setCatalog(null);
    setFieldsError(null);
    try {
      const response = await getMaintenanceRowFields(projectId);
      if (current === fieldsSequence.current) setCatalog(response);
    } catch (err) {
      const text = await readRowFilterError(err, "筛选字段加载失败");
      if (current === fieldsSequence.current) setFieldsError(text);
    }
  }, [projectId]);
  useEffect(() => {
    void loadFields();
    return () => { fieldsSequence.current += 1; };
  }, [loadFields]);
  useEffect(() => { setPage(1); }, [rangeParams.range, rangeParams.from, rangeParams.to]);
  const loadRows = useCallback(async () => {
    const current = ++sequence.current;
    setLoading(true);
    setError(null);
    setTotal(null);
    setRows([]);
    try {
      const response = await listMaintenanceWorkbenchRows(query, page);
      if (current !== sequence.current) return;
      setRows(response.rows);
      setTotal(response.total);
    } catch (err) {
      const text = await readRowFilterError(err, "备件行级表加载失败");
      if (current !== sequence.current) return;
      setRows([]);
      setTotal(null);
      setError(text);
    } finally {
      if (current === sequence.current) setLoading(false);
    }
  }, [query, page]);
  useEffect(() => {
    void loadRows();
    return () => { sequence.current += 1; };
  }, [loadRows]);
  useEffect(() => {
    exportSequence.current += 1;
    setExporting(null);
    setExportError(null);
    return () => { exportSequence.current += 1; };
  }, [query]);
  const download = async (format: RowExportFormat) => {
    const current = ++exportSequence.current;
    setExporting(format);
    setExportError(null);
    try {
      const blob = await exportMaintenanceRows(query, format);
      if (current === exportSequence.current) saveBlob(blob, `备件行级表-核对.${format}`);
    } catch (err) {
      const text = await readRowFilterError(err, "导出失败，请重试");
      if (current === exportSequence.current) setExportError(text);
    } finally {
      if (current === exportSequence.current) setExporting(null);
    }
  };
  return (
    <Card className="maintenance-row-workbench" size="small" title="全项目备件行级筛选">
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Select
          aria-label="行级表项目范围" placeholder="全部可见项目" allowClear showSearch optionFilterProp="label"
          style={{ width: "100%", maxWidth: 360 }} value={projectId} loading={projectsLoading}
          disabled={projectsLoading || Boolean(projectsError)}
          options={projects.map((project) => ({ value: project.project_id, label: project.display_name }))}
          onChange={(next: string | undefined) => {
            setCatalog(null);
            setFilters([]);
            setPage(1);
            setProjectId(next);
          }}
        />
        {projectsError ? <Alert type="error" showIcon message={projectsError}
          action={<Button onClick={() => void loadProjects()}>重试项目</Button>} /> : null}
        {fieldsError ? <Alert type="error" showIcon message={fieldsError}
          action={<Button onClick={() => void loadFields()}>重试字段</Button>} /> : null}
        {!catalog && !fieldsError ? <Spin tip="正在加载筛选字段"><div style={{ height: 40 }} /></Spin> : null}
        {catalog ? <MaintenanceRowFilterBuilder key={projectId ?? "all"} fields={catalog.fields} loading={loading}
          onApply={(next) => { setFilters(next); setPage(1); }} /> : null}
        {catalog && !catalog.amount_filter_allowed
          ? <Alert type="info" showIcon message="当前范围不可使用金额条件；可先限定到有金额资格的项目。" /> : null}
        <Space wrap>
          <Typography.Text>{total === null ? "行数待查询" : `共 ${total} 行`}</Typography.Text>
          <Button disabled={loading || total === null || exporting !== null} loading={exporting === "csv"}
            onClick={() => void download("csv")}>导出完整筛选集 CSV</Button>
          <Button disabled={loading || total === null || exporting !== null} loading={exporting === "xlsx"}
            onClick={() => void download("xlsx")}>导出完整筛选集 XLSX</Button>
        </Space>
        <Typography.Text type="secondary">核对导出不可回传；改价回传请使用原“全项目备件行级表”下载。</Typography.Text>
        {error ? <Alert type="error" showIcon message={error}
          action={<Button onClick={() => void loadRows()}>重试列表</Button>} /> : null}
        {exportError ? <Alert type="error" showIcon message={exportError} /> : null}
        <Table<MaintenanceWorkbenchRow>
          rowKey="line_id" size="small" loading={loading} dataSource={rows}
          columns={[
            { title: "项目", dataIndex: "project_name", width: 180 },
            { title: "维保单号", dataIndex: "order_no", width: 170 },
            { title: "日期", dataIndex: "order_date", width: 120 },
            { title: "PN", dataIndex: "pn_std", width: 220, render: (value: string | null) => (
              <span style={{ overflowWrap: "anywhere" }}>{value ?? "—"}</span>
            ) },
            { title: "描述", dataIndex: "description", width: 240 },
            { title: "需求数量", dataIndex: "qty", width: 100 },
            { title: "退货数量", dataIndex: "return_qty", width: 100 },
          ]}
          scroll={{ x: 1130 }}
          locale={{ emptyText: error ? "查询失败，请重试" : "当前筛选没有备件行" }}
          pagination={{ current: page, total: total ?? 0, pageSize: 20, showSizeChanger: false, onChange: setPage }}
        />
      </Space>
    </Card>
  );
}
