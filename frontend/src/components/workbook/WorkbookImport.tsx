import { useEffect, useRef, useState } from "react";
import { UploadOutlined } from "@ant-design/icons";
import { Alert, Button, Space, Typography, Upload } from "antd";
import {
  applyWorkbook, previewWorkbook, workbookErrorMessage,
  type WorkbookPreviewResult, type WorkbookReceipt,
} from "../../api/workbooks";
import WorkbookPreview from "./WorkbookPreview";

export interface WorkbookImportProps {
  schemaName: string;
  onApplied?: (response: unknown) => void | Promise<void>;
  disabled?: boolean;
}

export function WorkbookImport({ schemaName, onApplied, disabled }: WorkbookImportProps) {
  const [preview, setPreview] = useState<WorkbookPreviewResult | null>(null);
  const [receipt, setReceipt] = useState<WorkbookReceipt | null>(null);
  const [confirmDeletes, setConfirmDeletes] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState("");
  const [refreshWarning, setRefreshWarning] = useState("");
  const epoch = useRef(0);
  const session = localStorage.getItem("token");

  useEffect(() => {
    ++epoch.current;
    setPreview(null); setReceipt(null); setConfirmDeletes([]); setError(""); setRefreshWarning("");
    setLoading(false); setApplying(false);
    return () => { ++epoch.current; };
  }, [schemaName, session, disabled]);

  const upload = async (file: File) => {
    if (disabled || loading || applying) return;
    const seq = ++epoch.current;
    const requestSession = localStorage.getItem("token");
    const current = () => seq === epoch.current && localStorage.getItem("token") === requestSession;
    setPreview(null); setReceipt(null); setConfirmDeletes([]); setError(""); setRefreshWarning("");
    if (!file.name.toLowerCase().endsWith(".xlsx")) { setError("请上传 .xlsx 工作簿"); return; }
    if (file.size > 10 * 1024 * 1024) { setError("工作簿不能超过 10 MB"); return; }
    setLoading(true);
    try {
      const result = await previewWorkbook(schemaName, file);
      if (!current()) return;
      if (result.replayed) setReceipt({ operation_id: result.operation_id, replayed: true, response: result.response });
      else {
        setPreview(result);
        if (result.status === "failed") setError("该次应用已失败，请修正文件后重新上传");
      }
    } catch (uploadError) {
      if (current()) setError(workbookErrorMessage(uploadError, "工作簿预览失败，请重试"));
    } finally {
      if (current()) setLoading(false);
    }
  };

  const apply = async () => {
    if (!preview || applying || disabled || preview.status === "failed") return;
    const seq = epoch.current;
    const requestSession = localStorage.getItem("token");
    const current = () => seq === epoch.current && localStorage.getItem("token") === requestSession;
    setApplying(true); setError(""); setRefreshWarning("");
    try {
      const result = await applyWorkbook(preview.operation_id, preview.plan_digest, confirmDeletes);
      if (!current()) return;
      setReceipt(result); setPreview(null);
      try { await onApplied?.(result.response); }
      catch { if (current()) setRefreshWarning("工作簿已应用，但页面刷新失败，请手动刷新。"); }
    } catch (applyError) {
      if (current()) setError(workbookErrorMessage(applyError, "工作簿应用失败，请重新预览"));
    } finally {
      if (current()) setApplying(false);
    }
  };

  const hasErrors = !!preview && (preview.plan.errors.length > 0 || preview.plan.rows.some((row) => row.errors.length > 0));
  const hasConflicts = !!preview && (preview.plan.counts.conflict > 0 || preview.plan.rows.some((row) => row.status === "conflict"));
  return <Space direction="vertical" size={14} style={{ width: "100%" }}>
    <Upload accept=".xlsx" showUploadList={false} disabled={disabled || loading || applying}
      beforeUpload={(file) => { void upload(file); return Upload.LIST_IGNORE; }}>
      <Button aria-label="上传工作簿" icon={<UploadOutlined aria-hidden />} loading={loading} disabled={disabled || applying}>上传工作簿</Button>
    </Upload>
    {error && <Alert role="alert" type="error" showIcon message={error} />}
    {refreshWarning && <Alert type="warning" message={refreshWarning} />}
    {preview && <>
      <Typography.Title level={5}>变更预览</Typography.Title>
      <WorkbookPreview plan={preview.plan} confirmDeletes={confirmDeletes} onConfirmDeletes={setConfirmDeletes} disabled={applying} />
      <Button type="primary" loading={applying} disabled={disabled || hasErrors || hasConflicts || preview.status === "failed"} onClick={() => void apply()}>确认应用</Button>
    </>}
    {receipt && <>
      <Alert type={receipt.replayed ? "info" : "success"} showIcon
        message={receipt.replayed ? "该文件已应用过，显示首次结果" : "工作簿已应用"} />
      <Typography.Title level={5}>回执</Typography.Title>
      <pre aria-label="工作簿回执" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", margin: 0 }}>
        {typeof receipt.response === "string" ? receipt.response : JSON.stringify(receipt.response, null, 2)}
      </pre>
    </>}
  </Space>;
}

export default WorkbookImport;
