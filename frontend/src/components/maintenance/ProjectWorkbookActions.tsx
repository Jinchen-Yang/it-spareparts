import { useEffect, useRef, useState } from "react";
import { DownloadOutlined, UploadOutlined } from "@ant-design/icons";
import { Alert, Button, Descriptions, Space, Spin, Tag } from "antd";

import {
  applyMaintenanceProjectWorkbook,
  downloadMaintenanceProjectWorkbook,
  validateMaintenanceProjectWorkbook,
  type MaintenanceWorkbookValidation,
} from "../../api/maintenanceOperations";
import { readMaintenanceCapabilities } from "./maintenancePermissions";

const CHANGE_LABELS: Record<string, string> = {
  collection_append: "新增回款记录",
  collection_update: "更新回款记录",
  collection_void: "作废回款记录",
};

function safeFilenamePart(value: string): string {
  return value.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").trim() || "维保项目";
}

export default function ProjectWorkbookActions({
  projectId,
  projectCode,
  onApplied,
}: {
  projectId: string;
  projectCode: string;
  onApplied?: () => void | Promise<void>;
}) {
  const [{ canApplyRoundtrip, canDownloadRoundtrip }] = useState(
    readMaintenanceCapabilities,
  );
  const inputRef = useRef<HTMLInputElement>(null);
  const validationGeneration = useRef(0);
  const downloadInFlight = useRef(false);
  const applyInFlight = useRef(false);
  const [downloading, setDownloading] = useState(false);
  const [validating, setValidating] = useState(false);
  const [applying, setApplying] = useState(false);
  const [validation, setValidation] = useState<MaintenanceWorkbookValidation | null>(null);
  const [status, setStatus] = useState<{
    type: "success" | "error";
    message: string;
  } | null>(null);

  useEffect(() => () => { validationGeneration.current += 1; }, []);

  const download = async () => {
    if (downloadInFlight.current) return;
    downloadInFlight.current = true;
    setDownloading(true);
    setStatus(null);
    try {
      const { data } = await downloadMaintenanceProjectWorkbook(projectId);
      const url = URL.createObjectURL(data);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${safeFilenamePart(projectCode)}_维保项目全量四表.xlsx`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch {
      setStatus({ type: "error", message: "完整四表下载失败，请重试" });
    } finally {
      downloadInFlight.current = false;
      setDownloading(false);
    }
  };

  const chooseFile = async (file: File | undefined) => {
    if (!file) return;
    const request = ++validationGeneration.current;
    setValidating(true);
    setValidation(null);
    setStatus(null);
    try {
      const { data } = await validateMaintenanceProjectWorkbook(projectId, file);
      if (request === validationGeneration.current) setValidation(data);
    } catch {
      if (request === validationGeneration.current) {
        setStatus({ type: "error", message: "文件校验失败，系统尚未写入任何数据" });
      }
    } finally {
      if (request === validationGeneration.current) {
        setValidating(false);
        if (inputRef.current) inputRef.current.value = "";
      }
    }
  };

  const apply = async () => {
    if (!validation?.can_apply || applyInFlight.current) return;
    applyInFlight.current = true;
    setApplying(true);
    setStatus(null);
    try {
      const { data } = await applyMaintenanceProjectWorkbook(projectId, {
        validation_token: validation.validation_token,
        data_version: validation.data_version,
      });
      setValidation(null);
      setStatus({
        type: "success",
        message: `已应用 ${data.changed_rows.toLocaleString("zh-CN")} 行更新`,
      });
      await onApplied?.();
    } catch {
      setStatus({
        type: "error",
        message: "应用失败，可能已有更新写入；请重新下载后再上传",
      });
    } finally {
      applyInFlight.current = false;
      setApplying(false);
    }
  };

  const changeEntries = validation
    ? Object.entries(validation.changes).filter(([, count]) => Number(count) > 0)
    : [];

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Space wrap>
        {canDownloadRoundtrip && (
          <Button
            aria-label="下载完整四表"
            icon={<DownloadOutlined />}
            loading={downloading}
            onClick={() => void download()}
          >
            下载完整四表
          </Button>
        )}
        {canApplyRoundtrip && (
          <>
            <Button
              aria-label="上传月度更新"
              icon={<UploadOutlined />}
              onClick={() => inputRef.current?.click()}
              disabled={validating || applying}
            >
              上传月度更新
            </Button>
            <input
              ref={inputRef}
              hidden
              type="file"
              accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              aria-label="选择月度更新工作簿"
              onChange={(event) => void chooseFile(event.target.files?.[0])}
            />
            <Tag color="gold">仅 01_总览的回款表尾可追加</Tag>
          </>
        )}
      </Space>

      {validating && <Spin size="small" tip="正在校验整份工作簿"><span /></Spin>}
      {status && <Alert showIcon type={status.type} message={status.message} />}

      {validation && (
        <Alert
          showIcon
          type={validation.can_apply ? "success" : "error"}
          message={validation.can_apply
            ? "校验通过，尚未写入系统"
            : "校验未通过，未写入系统"}
          description={(
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              {changeEntries.length > 0 && (
                <Descriptions size="small" column={1} bordered>
                  {changeEntries.map(([key, count]) => (
                    <Descriptions.Item key={key} label={CHANGE_LABELS[key] || key}>
                      {count.toLocaleString("zh-CN")} 行
                    </Descriptions.Item>
                  ))}
                </Descriptions>
              )}
              {validation.warnings.map((warning) => (
                <Alert key={warning} type="warning" showIcon message={warning} />
              ))}
              {validation.errors.map((error) => (
                <Alert key={error} type="error" showIcon message={error} />
              ))}
              {canApplyRoundtrip && validation.can_apply && (
                <Button type="primary" loading={applying} onClick={() => void apply()}>
                  确认应用本次更新
                </Button>
              )}
            </Space>
          )}
        />
      )}
    </Space>
  );
}
