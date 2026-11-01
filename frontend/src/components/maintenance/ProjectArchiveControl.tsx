import { useRef, useState } from "react";
import { Alert, App, Button, Input, Modal, Space } from "antd";
import { archiveMaintenanceProject, restoreMaintenanceProject, type MaintenanceProject } from "../../api/maintenanceProjects";
import { useSessionOwner, sessionStale } from "../../utils/session";

export default function ProjectArchiveControl({ project, onChanged }: {
  project: MaintenanceProject; onChanged: () => Promise<boolean>;
}) {
  const { message } = App.useApp();
  const owner = useSessionOwner();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<MaintenanceProject | null>(null);
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const current = saved && saved.version >= project.version ? saved : project;
  const label = current.is_active ? "归档项目" : "恢复项目";
  async function submit() {
    if (submitting.current || !reason.trim()) return;
    if (sessionStale(owner)) { setError("登录账号已变化，请刷新后继续"); return; }
    submitting.current = true; setBusy(true); setError(null);
    try {
      const action = current.is_active ? archiveMaintenanceProject : restoreMaintenanceProject;
      const { data } = await action(current.project_id, { version: current.version, reason: reason.trim() });
      if (sessionStale(owner)) return;
      setSaved(data); setOpen(false); setReason("");
      message.success(data.is_active ? "项目已恢复" : "项目主档已归档，历史事实已保留");
      let refreshed = false;
      try { refreshed = await onChanged(); } catch { /* Mutation already succeeded. */ }
      if (!sessionStale(owner) && !refreshed) setError("操作已成功，但页面刷新失败。请刷新查看，勿重复提交。");
    } catch (err) {
      if (!sessionStale(owner)) {
        const response = (err as { response?: { status?: number; data?: { detail?: unknown } } }).response;
        setError(response?.status === 409 ? "项目已被其他操作修改，请刷新后重新确认。"
          : typeof response?.data?.detail === "string" ? response.data.detail : "操作失败，请检查权限并重试。");
      }
    } finally { submitting.current = false; setBusy(false); }
  }
  return <Space direction="vertical" size={4}>
    <Button size="small" danger={current.is_active} onClick={() => { setReason(""); setError(null); setOpen(true); }}>{label}</Button>
    {error && !open && <Alert type="warning" message={error} />}
    <Modal title={label} open={open} onCancel={() => { if (!busy) setOpen(false); }}
      onOk={() => void submit()} okText={`确认${label}`} okButtonProps={{ disabled: !reason.trim(), danger: current.is_active }} confirmLoading={busy}>
      <p>{current.display_name}（{current.project_code}）</p>
      <Alert type="info" showIcon message={current.is_active
        ? "归档保留需求、领用、实收和财务历史。有有效需求的项目仍在统计中显示；重复项目须先核对归属。"
        : "恢复后可以继续维护项目，原有事实和审计记录保持。"} />
      <Input.TextArea aria-label="归档或恢复原因" value={reason} maxLength={1000} rows={3}
        style={{ marginTop: 12 }} onChange={event => setReason(event.target.value)} placeholder="填写原因" />
      {error && <Alert type="error" message={error} style={{ marginTop: 12 }} />}
    </Modal>
  </Space>;
}
