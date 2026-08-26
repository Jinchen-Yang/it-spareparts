import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Popconfirm,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import { DeleteOutlined, DownloadOutlined, UploadOutlined } from "@ant-design/icons";

import {
  deleteMaintenanceAcceptanceAttachment,
  downloadMaintenanceAcceptanceAttachment,
  getMaintenanceAcceptance,
  submitMaintenanceAcceptance,
  uploadMaintenanceAcceptanceAttachment,
  type MaintenanceAcceptanceDeliverable,
} from "../../api/maintenanceOperations";
import { readMaintenanceCapabilities } from "./maintenancePermissions";
import { saveBlob } from "../../api/maintenanceWorkbooks";

const { Text } = Typography;


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
    // 2026-08-25 客户口径：一个上传口——只传文件，无版本握手、无前置条件。
    if (!file) {
      if (inputRef.current) inputRef.current.value = "";
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await uploadMaintenanceAcceptanceAttachment(projectId, { file });
      await refreshAfterMutation();
    } catch (reason: unknown) {
      setError(readFailure(reason, "附件上传失败，系统未写入。请检查格式后重试。"));
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  const removeAttachment = async (fileId: string) => {
    setBusy(true);
    setError(null);
    try {
      await deleteMaintenanceAcceptanceAttachment(projectId, fileId);
      await refreshAfterMutation();
    } catch (reason: unknown) {
      setError(readFailure(reason, "附件删除失败，请刷新后重试。"));
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    // 2026-08-25 客户口径：提交亦无前端版本守卫——version=0 空载荷是合法起点
    // （与已修复的 upload 同款 version<1 残留）；expected_version 仅是服务端
    // 乐观锁契约，不再作前端前置条件。
    if (!record) return;
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

  // 2026-08-25 客户拍板：验收只是个上传的地方，没有截止日概念——
  // 随时可传随时可提交；截止日有则展示、无则"—"，不再作前置条件。
  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {error && <Alert type="error" showIcon message={error} />}
      <Descriptions bordered size="small" column={{ xs: 1, md: 3 }}>
        <Descriptions.Item label="验收截止日">
          {record.due_date || "—"}
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
          <Space key={attachment.file_id} size={4}>
            <Button
              icon={<DownloadOutlined />}
              disabled={busy}
              onClick={() => void download(attachment.file_id, attachment.original_filename)}
            >
              {attachment.original_filename}
            </Button>
            {/* 2026-08-26 客户口径：附件旁显示上传人姓名（无实名账号回退用户名） */}
            <Text type="secondary" style={{ fontSize: 12 }}>
              {attachment.uploaded_by_name || attachment.uploaded_by}
            </Text>
            {canSubmitAcceptance && (
              <Popconfirm
                title="删除该附件？"
                description="删除后页面立即消失，可重新上传"
                okText="删除"
                okButtonProps={{ danger: true }}
                disabled={busy}
                onConfirm={() => void removeAttachment(attachment.file_id)}
              >
                <Button
                  danger
                  size="small"
                  icon={<DeleteOutlined />}
                  disabled={busy}
                  aria-label={`删除 ${attachment.original_filename}`}
                />
              </Popconfirm>
            )}
          </Space>
        ))}
        {record.attachments.length === 0 && <Tag color="orange">验收附件待上传</Tag>}
      </Space>
      <Space wrap>
        {canSubmitAcceptance && (
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
        {!canSubmitAcceptance && (
          // 参照验收清单「导入需授权」：无权限用户只见橙标无入口，补一句解释。
          <Text type="secondary">
            上传与提交需授权（验收报告提交与附件上传），请联系管理员开通
          </Text>
        )}
      </Space>
    </Space>
  );
}
