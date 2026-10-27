import { useEffect, useRef, useState } from "react";
import { DownloadOutlined } from "@ant-design/icons";
import { Alert, Button, Checkbox, Modal, Space, Spin, Typography } from "antd";
import {
  workbookErrorMessage, type WorkbookColumn, type WorkbookSchema, type WorkbookVisibleColumns,
} from "../../api/workbooks";

export interface WorkbookExportButtonProps {
  schemaName: string;
  fetchSchema: (schemaName: string) => Promise<WorkbookSchema>;
  onExport: (visibleColumns: WorkbookVisibleColumns) => Promise<Blob | { blob: Blob; filename: string } | void>;
  filename?: string;
  disabled?: boolean;
}

function mandatory(column: WorkbookColumn): boolean {
  return column.mandatory === true || (column.edit === "locked" && column.type === "code")
    || ["_row_token", "entity_id", "base_version"].includes(column.key);
}

function saveWorkbook(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // WebKit 可能在 click 返回后才读取文件。
  globalThis.setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

export function WorkbookExportButton({ schemaName, fetchSchema, onExport, filename, disabled }: WorkbookExportButtonProps) {
  const [open, setOpen] = useState(false);
  const [schema, setSchema] = useState<WorkbookSchema | null>(null);
  const [selected, setSelected] = useState<WorkbookVisibleColumns>({});
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");
  const epoch = useRef(0);
  const session = localStorage.getItem("token");

  useEffect(() => {
    ++epoch.current;
    setOpen(false); setSchema(null); setSelected({}); setError("");
    setLoading(false); setDownloading(false);
    return () => { ++epoch.current; };
  }, [schemaName, session, disabled]);

  const load = async () => {
    const seq = ++epoch.current;
    const requestSession = localStorage.getItem("token");
    const current = () => seq === epoch.current && localStorage.getItem("token") === requestSession;
    setOpen(true); setLoading(true); setSchema(null); setSelected({}); setError("");
    try {
      const result = await fetchSchema(schemaName);
      if (!current()) return;
      if (result.name !== schemaName) throw new Error("模板类型不匹配，请重试");
      setSchema(result);
      setSelected(Object.fromEntries(result.sheets.map((sheet) => [sheet.key, sheet.columns.map((column) => column.key)])));
    } catch (loadError) {
      if (current()) setError(workbookErrorMessage(loadError, "导出列加载失败，请重试"));
    } finally {
      if (current()) setLoading(false);
    }
  };

  const download = async () => {
    if (!schema || downloading || disabled) return;
    const seq = epoch.current;
    const requestSession = localStorage.getItem("token");
    const current = () => seq === epoch.current && localStorage.getItem("token") === requestSession;
    const visible = Object.fromEntries(schema.sheets.map((sheet) => [sheet.key,
      sheet.columns.filter((column) => mandatory(column) || selected[sheet.key]?.includes(column.key)).map((column) => column.key),
    ]));
    setDownloading(true); setError("");
    try {
      const result = await onExport(visible);
      if (!current()) return;
      if (result instanceof Blob) saveWorkbook(result, filename || `${schemaName}.xlsx`);
      else if (result) saveWorkbook(result.blob, result.filename);
      setOpen(false);
    } catch (downloadError) {
      if (current()) setError(workbookErrorMessage(downloadError, "工作簿导出失败，请重试"));
    } finally {
      if (current()) setDownloading(false);
    }
  };

  return <>
    <Button aria-label="导出工作簿" icon={<DownloadOutlined aria-hidden />} disabled={disabled} onClick={() => void load()}>导出工作簿</Button>
    <Modal open={open} title="选择导出列" okText="下载 Excel" cancelText="取消" confirmLoading={downloading}
      okButtonProps={{ disabled: loading || !schema }} onOk={() => void download()}
      onCancel={() => { if (!downloading) { ++epoch.current; setOpen(false); } }}>
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Typography.Text type="secondary">身份列和锁定的编码列必须保留，用于上传时识别原行。</Typography.Text>
        {loading && <Spin />}
        {error && <Alert role="alert" type="error" message={error}
          action={!schema ? <Button aria-label="重试" onClick={() => void load()}>重试</Button> : undefined} />}
        {schema?.sheets.map((sheet) => <div key={sheet.key}>
          <Typography.Text strong>{sheet.title}</Typography.Text>
          <Space direction="vertical" style={{ display: "flex", marginTop: 8 }}>
            {sheet.columns.map((column) => <Checkbox key={column.key} disabled={downloading || mandatory(column)}
              checked={mandatory(column) || selected[sheet.key]?.includes(column.key)}
              onChange={(event) => setSelected((previous) => ({ ...previous, [sheet.key]: event.target.checked
                ? [...(previous[sheet.key] || []), column.key]
                : (previous[sheet.key] || []).filter((key) => key !== column.key) }))}>
              {column.header}{mandatory(column) ? "（必带）" : ""}
            </Checkbox>)}
          </Space>
        </div>)}
      </Space>
    </Modal>
  </>;
}

export default WorkbookExportButton;
