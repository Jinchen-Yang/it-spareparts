import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Select, Space, Spin, Table, Typography } from "antd";
import {
  exportMaintenanceRows, getMaintenanceRowFields, listMaintenanceWorkbenchRows,
  readRowFilterError, serializeRowFilters,
} from "../../api/maintenanceRowFilters";
import type {
  MaintenanceRowFilter, MaintenanceWorkbenchRow, RowExportFormat, RowRangeParams,
} from "../../api/maintenanceRowFilters";
import { saveBlob } from "../../api/maintenanceWorkbooks";
import { listMaintenanceProjects } from "../../api/maintenanceProjects";
import { useContextQuery } from "../../authz/contextQuery";
import { useContextTransport } from "../../authz/domainTransport";
import { useAccess } from "../../authz/access";
import { actionRule, surfaceRule, anyRule, allRule } from "../../authz/rules";
import { withContextSurface, contextSurfaces } from "../../authz/ContextSurface";
import { contextRefKey, type AuthorizationContext } from "../../authz/contextTypes";
import { authorizationErrorText } from "../../authz/requestLifecycle";
import { ContextChanged } from "../../authz/contextProtocol";
import MaintenanceRowFilterBuilder from "./MaintenanceRowFilterBuilder";

function GlobalMaintenanceRowWorkbench({ rangeParams }: { rangeParams: RowRangeParams }) {
  const [projectId, setProjectId] = useState<string | undefined>();
  const [filters, setFilters] = useState<MaintenanceRowFilter[]>([]);
  const [page, setPage] = useState(1);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<RowExportFormat | null>(null);
  const query = useMemo(() => ({ ...rangeParams,
    ...(projectId ? { project_id: projectId } : {}),
    ...(filters.length ? { filters: serializeRowFilters(filters) } : {}),
  }), [rangeParams.range, rangeParams.from, rangeParams.to, filters, projectId]);
  const directory = useContextQuery({
    entryId: "GET /api/maintenance/projects/stable",
    rule: surfaceRule("maintenance_project_directory.project_selectors"), queryKey: ["row-projects"], legacyAllowed: true,
    query: async capture => {
      const first = (await listMaintenanceProjects({ page: 1, page_size: 200, include_inactive: true }, capture)).data;
      const rows = [...first.rows];
      const firstContext = (first as typeof first & { authorization_context?: AuthorizationContext }).authorization_context;
      for (let nextPage = 2; rows.length < first.total; nextPage += 1) {
        const next = (await listMaintenanceProjects({ page: nextPage, page_size: 200, include_inactive: true }, capture)).data;
        const nextContext = (next as typeof next & { authorization_context?: AuthorizationContext }).authorization_context;
        if (firstContext && (!nextContext || contextRefKey(firstContext.context) !== contextRefKey(nextContext.context))) throw new ContextChanged();
        rows.push(...next.rows);
        if (!next.rows.length) break;
      }
      return { ...first, rows };
    },
  });
  const fieldQuery = useContextQuery({
    entryId: "GET /api/maintenance/spare-part-lines/fields",
    rule: anyRule(actionRule("maintenance_global_rows", "read_fields"), actionRule("maintenance_managed_rows", "read_fields")), queryKey: [projectId], legacyAllowed: true,
    query: capture => getMaintenanceRowFields(projectId, false, capture),
  });
  const rowQuery = useContextQuery({
    entryId: "GET /api/maintenance/spare-part-lines/rows",
    rule: contextSurfaces("maintenance_global_rows.global_rows", "maintenance_managed_rows.global_rows"), queryKey: [query, page], legacyAllowed: true,
    query: capture => listMaintenanceWorkbenchRows(query, page, capture),
  });
  const exportAccess = useAccess(anyRule(
    allRule(surfaceRule("maintenance_global_rows.global_rows"), actionRule("maintenance_global_rows", "export")),
    allRule(surfaceRule("maintenance_managed_rows.global_rows"), actionRule("maintenance_managed_rows", "export")),
  ), { legacy: { maintenance_global_rows: true, maintenance_managed_rows: true } });
  const transport = useContextTransport(JSON.stringify(query), true);
  const catalog = fieldQuery.data;
  const projects = directory.data?.rows ?? [];
  const projectsLoading = directory.loading, projectsError = directory.error ? authorizationErrorText(directory.error, "项目目录加载失败") : null;
  const fieldsError = fieldQuery.error ? authorizationErrorText(fieldQuery.error, "筛选字段加载失败") : null;
  const rows = rowQuery.data?.rows ?? [], total = rowQuery.data?.total ?? null;
  const loading = rowQuery.loading, error = rowQuery.error ? authorizationErrorText(rowQuery.error, "备件行级表加载失败") : null;
  const loadProjects = directory.retry, loadFields = fieldQuery.retry, loadRows = rowQuery.retry;
  useEffect(() => { setPage(1); }, [rangeParams.range, rangeParams.from, rangeParams.to]);
  useEffect(() => { setExporting(null); setExportError(null); }, [transport]);
  const download = async (format: RowExportFormat) => {
    if (exportAccess.entryState === "deny") return;
    setExporting(format); setExportError(null);
    try {
      const blob = await exportMaintenanceRows(query, format, transport);
      if (transport.isCurrent()) saveBlob(blob, `备件行级表-核对.${format}`);
    } catch (err) {
      const text = await readRowFilterError(err, "导出失败，请重试");
      if (transport.isCurrent()) setExportError(text);
    } finally { if (transport.isCurrent()) setExporting(null); }
  };
  return (
    <Card className="maintenance-row-workbench" size="small" title="全项目备件行级筛选">
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        {directory.entryState !== "deny" && <Select
          aria-label="行级表项目范围" placeholder="全部可见项目" allowClear showSearch optionFilterProp="label"
          style={{ width: "100%", maxWidth: 360 }} value={projectId} loading={projectsLoading}
          disabled={projectsLoading || Boolean(projectsError)}
          options={projects.map((project) => ({ value: project.project_id, label: project.display_name }))}
          onChange={(next: string | undefined) => {
            setFilters([]);
            setPage(1);
            setProjectId(next);
          }}
        />}
        {projectsError ? <Alert type="error" showIcon message={projectsError}
          action={<Button onClick={() => void loadProjects()}>重试项目</Button>} /> : null}
        {fieldsError ? <Alert type="error" showIcon message={fieldsError}
          action={<Button onClick={() => void loadFields()}>重试字段</Button>} /> : null}
        {fieldQuery.entryState !== "deny" && !catalog && !fieldsError ? <Spin tip="正在加载筛选字段"><div style={{ height: 40 }} /></Spin> : null}
        {catalog ? <MaintenanceRowFilterBuilder key={projectId ?? "all"} fields={catalog.fields} loading={loading}
          onApply={(next) => { setFilters(next); setPage(1); }} /> : null}
        {catalog && !catalog.amount_filter_allowed
          ? <Alert type="info" showIcon message="当前范围不可使用金额条件；可先限定到有金额资格的项目。" /> : null}
        <Space wrap>
          <Typography.Text>{total === null ? "行数待查询" : `共 ${total} 行`}</Typography.Text>
          {exportAccess.entryState !== "deny" && <Button disabled={loading || total === null || exporting !== null} loading={exporting === "csv"}
            onClick={() => void download("csv")}>导出完整筛选集 CSV</Button>}
          {exportAccess.entryState !== "deny" && <Button disabled={loading || total === null || exporting !== null} loading={exporting === "xlsx"}
            onClick={() => void download("xlsx")}>导出完整筛选集 XLSX</Button>}
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

export default withContextSurface(GlobalMaintenanceRowWorkbench, contextSurfaces("maintenance_global_rows.global_rows", "maintenance_managed_rows.global_rows"), props => props.rangeParams);
