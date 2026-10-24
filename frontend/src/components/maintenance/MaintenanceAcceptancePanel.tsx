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
import { useContextAdmission } from "../../authz/access";
import { actionRule, anyRule } from "../../authz/rules";
import { useContextTransport } from "../../authz/domainTransport";
import { withContextSurface, contextSurfaces } from "../../authz/ContextSurface";
import { authorizationErrorText } from "../../authz/requestLifecycle";

const { Text } = Typography;


function idempotencyKey(prefix: string): string {
  const suffix = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${suffix}`;
}


function readFailure(reason: unknown, fallback: string): string {
  const status = (reason as { response?: { status?: number } } | null)?.response?.status;
  if (status && [401, 403, 409, 503].includes(status)) return authorizationErrorText(reason, fallback);
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


function MaintenanceAcceptancePanelContent({
  projectId,
  onChanged,
}: {
  projectId: string;
  onChanged?: () => Promise<boolean>;
}) {
  const [record, setRecord] = useState<MaintenanceAcceptanceDeliverable | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const generation = useRef(0);
  const { canSubmitAcceptance: legacySubmit } = readMaintenanceCapabilities();
  const admission = useContextAdmission();
  const canReport = admission.surface("maintenance_acceptance.report", true) !== "deny";
  const canAttachments = admission.surface("maintenance_acceptance.attachments", true) !== "deny";
  const canRead = canReport || canAttachments;
  const allowed = (action: string, legacy: boolean) => admission.state(actionRule("maintenance_acceptance", action), legacy) !== "deny";
  const canUpload = allowed("attachment_upload", legacySubmit);
  const canSubmit = allowed("submit", legacySubmit);
  const canDelete = canAttachments && allowed("attachment_delete", legacySubmit);
  const canDownload = canAttachments && allowed("attachment_download", true);
  const transport = useContextTransport(projectId, true);
  const inFlight = useRef(false);

  const load = async () => {
    if (!canRead) { setRecord(null); setLoading(false); return true; }
    const request = ++generation.current;
    setRecord(null);
    setLoading(true);
    setError(null);
    try {
      const { data } = await getMaintenanceAcceptance(projectId, transport);
      if (request === generation.current && transport.isCurrent()) setRecord(data);
      return request === generation.current && transport.isCurrent();
    } catch (reason) {
      if (request === generation.current && transport.isCurrent()) {
        setRecord(null);
        setError(authorizationErrorText(reason, "验收报告状态加载失败，请重试。"));
      }
      return false;
    } finally {
      if (request === generation.current && transport.isCurrent()) setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    return () => { generation.current += 1; };
    // projectId is the full request identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, transport, canRead]);

  const refreshAfterMutation = async () => {
    const localReady = await load();
    if (!transport.isCurrent()) return false;
    let parentReady = true;
    try {
      parentReady = onChanged ? (await onChanged()) !== false : true;
    } catch {
      parentReady = false;
    }
    return localReady && parentReady;
  };

  const runMutation = async (
    action: () => Promise<unknown>,
    writeFailure: string,
  ) => {
    let committed = false;
    try {
      if (!transport.isCurrent()) return;
      await action();
      if (!transport.isCurrent()) return;
      committed = true;
      if (!(await refreshAfterMutation()) && transport.isCurrent()) {
        setError("操作已写入，但验收页面刷新失败；旧数据已失效，请重试。");
      }
    } catch (reason: unknown) {
      if (!transport.isCurrent()) return;
      setError(committed
        ? "操作已写入，但验收页面刷新失败；旧数据已失效，请重试。"
        : readFailure(reason, writeFailure));
    }
  };

  const upload = async (file: File | undefined) => {
    // 2026-08-25 客户口径：一个上传口——只传文件，无版本握手、无前置条件。
    if (!file || !canUpload || inFlight.current || !transport.isCurrent()) {
      if (inputRef.current) inputRef.current.value = "";
      return;
    }
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await runMutation(
        () => uploadMaintenanceAcceptanceAttachment(projectId, { file }, transport),
        "附件上传失败，请刷新核对附件列表后重试。",
      );
    } finally {
      if (!transport.isCurrent()) return;
      inFlight.current = false;
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  const removeAttachment = async (fileId: string) => {
    if (!canDelete || inFlight.current || !transport.isCurrent()) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await runMutation(
        () => deleteMaintenanceAcceptanceAttachment(projectId, fileId, transport),
        "附件删除失败，请刷新后重试。",
      );
    } finally {
      if (!transport.isCurrent()) return;
      inFlight.current = false;
      setBusy(false);
    }
  };

  const submit = async () => {
    // 2026-08-25 客户口径：提交亦无前端版本守卫——version=0 空载荷是合法起点
    // （与已修复的 upload 同款 version<1 残留）；expected_version 仅是服务端
    // 乐观锁契约，不再作前端前置条件。
    if (!record || !canSubmit || inFlight.current || !transport.isCurrent()) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await runMutation(
        () => submitMaintenanceAcceptance(projectId, {
          expected_version: record.version,
          idempotencyKey: idempotencyKey("acceptance-submit"),
        }, transport),
        "验收报告提交失败，请刷新后重试。",
      );
    } finally {
      if (!transport.isCurrent()) return;
      inFlight.current = false;
      setBusy(false);
    }
  };

  const download = async (fileId: string, filename: string) => {
    if (!canDownload || inFlight.current || !transport.isCurrent()) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const { data } = await downloadMaintenanceAcceptanceAttachment(fileId, transport);
      if (transport.isCurrent()) saveBlob(data, safeFilename(filename));
    } catch (reason: unknown) {
      if (transport.isCurrent()) setError(readFailure(reason, "附件下载被拒绝或完整性校验失败，请联系管理员核查。"));
    } finally {
      if (!transport.isCurrent()) return;
      inFlight.current = false;
      setBusy(false);
    }
  };

  if (canRead && loading && !record) return <Spin size="small" tip="正在读取验收报告状态"><span /></Spin>;
  if (canRead && !record) return <Alert type="error" showIcon message={error || "验收报告不可用"} />;

  // 2026-08-25 客户拍板：验收只是个上传的地方，没有截止日概念——
  // 随时可传随时可提交；截止日有则展示、无则"—"，不再作前置条件。
  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {error && <Alert type="error" showIcon message={error} />}
      {canReport && record && <Descriptions bordered size="small" column={{ xs: 1, md: 3 }}>
        <Descriptions.Item label="验收截止日">
          {record.due_date || "—"}
        </Descriptions.Item>
        <Descriptions.Item label="提交状态">
          {record.submission_status === "submitted"
            ? <Tag color="blue">已提交</Tag>
            : <Tag>未提交</Tag>}
        </Descriptions.Item>
        <Descriptions.Item label="验收状态">{acceptanceTag(record.approval_status)}</Descriptions.Item>
      </Descriptions>}
      {canReport && record?.rejection_reason && (
        <Alert type="error" showIcon message="驳回原因（历史）" description={record.rejection_reason} />
      )}
      <Space wrap>
        {canAttachments && record?.attachments.map((attachment) => (
          <Space key={attachment.file_id} size={4}>
            <Button
              icon={<DownloadOutlined />}
              disabled={!canDownload || busy} onClick={() => void download(attachment.file_id, attachment.original_filename)}
            >
              {attachment.original_filename}
            </Button>
            {/* 2026-08-26 客户口径：附件旁显示上传人姓名（无实名账号回退用户名） */}
            <Text type="secondary" style={{ fontSize: 12 }}>
              {attachment.uploaded_by_name || attachment.uploaded_by}
            </Text>
            {canDelete && (
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
        {canAttachments && record?.attachments.length === 0 && <Tag color="orange">验收附件待上传</Tag>}
      </Space>
      <Space wrap>
        {canUpload && (
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
          </>
        )}
        {canSubmit && record && (
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
        )}
        {!canUpload && !canSubmit && (
          // 参照验收清单「导入需授权」：无权限用户只见橙标无入口，补一句解释。
          <Text type="secondary">
            上传与提交需授权（验收报告提交与附件上传），请联系管理员开通
          </Text>
        )}
      </Space>
    </Space>
  );
}

export default withContextSurface(MaintenanceAcceptancePanelContent,
  anyRule(contextSurfaces("maintenance_acceptance.report", "maintenance_acceptance.attachments"),
    actionRule("maintenance_acceptance", "attachment_upload")), props => props.projectId);
