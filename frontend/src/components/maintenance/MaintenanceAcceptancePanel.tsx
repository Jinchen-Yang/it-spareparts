import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import { DownloadOutlined, UploadOutlined } from "@ant-design/icons";

import {
  downloadMaintenanceAcceptanceAttachment,
  getMaintenanceAcceptance,
  submitMaintenanceAcceptance,
  uploadMaintenanceAcceptanceAttachment,
  type MaintenanceAcceptanceDeliverable,
} from "../../api/maintenanceOperations";
import { readMaintenanceCapabilities } from "./maintenancePermissions";
import { saveBlob } from "../../api/maintenanceWorkbooks";


function idempotencyKey(prefix: string): string {
  const suffix = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${suffix}`;
}


function readFailure(reason: unknown, fallback: string): string {
  const detail = (reason as { response?: { data?: { detail?: unknown } } } | null)
    ?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    return String((detail as { message: unknown }).message);
  }
  return fallback;
}


function safeFilename(value: string): string {
  return value.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_");
}


// 2026-08-24 起提交即生效（免独立审批）：approved=已验收；
// rejected 是历史存量驳回，重新提交后即转 approved。
function acceptanceTag(status: string) {
  if (status === "approved") return <Tag color="green">已验收</Tag>;
  if (status === "rejected") return <Tag color="red">已驳回（历史）</Tag>;
  return <Tag color="gold">待提交</Tag>;
}


export default function MaintenanceAcceptancePanel({
  projectId,
  onChanged,
}: {
  projectId: string;
  onChanged?: () => void;
}) {
  const [record, setRecord] = useState<MaintenanceAcceptanceDeliverable | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const generation = useRef(0);
  const { canSubmitAcceptance } = readMaintenanceCapabilities();

  const load = async () => {
    const request = ++generation.current;
    setLoading(true);
    setError(null);
    try {
      const { data } = await getMaintenanceAcceptance(projectId);
      if (request === generation.current) setRecord(data);
    } catch {
      if (request === generation.current) setError("验收报告状态加载失败，请重试。");
    } finally {
      if (request === generation.current) setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    return () => { generation.current += 1; };
    // projectId is the full request identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const refreshAfterMutation = async () => {
    await load();
    onChanged?.();
  };

  const upload = async (file: File | undefined) => {
    if (!file || !record || record.version < 1) return;
    setBusy(true);
    setError(null);
    try {
      await uploadMaintenanceAcceptanceAttachment(projectId, {
        expected_version: record.version,
        file,
        idempotencyKey: idempotencyKey("acceptance-upload"),
      });
      await refreshAfterMutation();
    } catch (reason: unknown) {
      setError(readFailure(reason, "附件上传失败，系统未写入。请检查格式并刷新后重试。"));
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  const submit = async () => {
    if (!record || record.version < 1) return;
    setBusy(true);
    setError(null);
    try {
      await submitMaintenanceAcceptance(projectId, {
        expected_version: record.version,
        idempotencyKey: idempotencyKey("acceptance-submit"),
      });
      await refreshAfterMutation();
    } catch (reason: unknown) {
      setError(readFailure(reason, "验收报告提交失败，请刷新后重试。"));
    } finally {
      setBusy(false);
    }
  };

  const download = async (fileId: string, filename: string) => {
    setBusy(true);
    setError(null);
    try {
      const { data } = await downloadMaintenanceAcceptanceAttachment(fileId);
      saveBlob(data, safeFilename(filename));
    } catch (reason: unknown) {
      setError(readFailure(reason, "附件下载被拒绝或完整性校验失败，请联系管理员核查。"));
    } finally {
      setBusy(false);
    }
  };

  if (loading && !record) return <Spin size="small" tip="正在读取验收报告状态"><span /></Spin>;
  if (!record) return <Alert type="error" showIcon message={error || "验收报告不可用"} />;

  const configured = record.configuration_state === "configured" && !!record.due_date;
  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {error && <Alert type="error" showIcon message={error} />}
      <Descriptions bordered size="small" column={{ xs: 1, md: 3 }}>
        <Descriptions.Item label="验收截止日">
          {record.due_date || <Typography.Text type="secondary">待月度全量表补齐</Typography.Text>}
        </Descriptions.Item>
        <Descriptions.Item label="提交状态">
          {record.submission_status === "submitted"
            ? <Tag color="blue">已提交</Tag>
            : <Tag>未提交</Tag>}
        </Descriptions.Item>
        <Descriptions.Item label="验收状态">{acceptanceTag(record.approval_status)}</Descriptions.Item>
      </Descriptions>
      {record.rejection_reason && (
        <Alert type="error" showIcon message="驳回原因（历史）" description={record.rejection_reason} />
      )}
      <Space wrap>
        {record.attachments.map((attachment) => (
          <Button
            key={attachment.file_id}
            icon={<DownloadOutlined />}
            disabled={busy}
            onClick={() => void download(attachment.file_id, attachment.original_filename)}
          >
            {attachment.original_filename}
          </Button>
        ))}
        {record.attachments.length === 0 && <Tag color="orange">验收附件待上传</Tag>}
      </Space>
      <Space wrap>
        {canSubmitAcceptance && configured && (
          <>
            <Button
              icon={<UploadOutlined />}
              loading={busy}
              onClick={() => inputRef.current?.click()}
            >
              上传验收附件
            </Button>
            <input
              ref={inputRef}
              hidden
              type="file"
              accept=".pdf,.docx,.xlsx,.png,.jpg,.jpeg"
              aria-label="选择验收报告附件"
              onChange={(event) => void upload(event.target.files?.[0])}
            />
            <Button
              type="primary"
              loading={busy}
              disabled={record.attachments.length === 0}
              onClick={() => void submit()}
            >
              {record.approval_status === "approved"
                ? "重新提交验收报告"
                : record.approval_status === "rejected"
                  ? "重新提交验收报告"
                  : "提交验收报告"}
            </Button>
          </>
        )}
      </Space>
      {!configured && (
        <Typography.Text type="secondary">截止日未配置时，系统会保留项目卡片，但关闭附件与提交通道。</Typography.Text>
      )}
    </Space>
  );
}
