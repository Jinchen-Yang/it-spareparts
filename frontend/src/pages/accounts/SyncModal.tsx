/** 模板同步预览确认：dry-run 逐账号 diff → 指纹执行。全成或全败。 */
import { useEffect, useState } from "react";
import { Alert, Button, Checkbox, Modal, Space, Table, Tag, message } from "antd";
import type { BulkPreview, BulkPreviewItem, TemplateInfo } from "../../api/accounts";
import { explainApiError, syncTemplate } from "../../api/accounts";

export default function SyncModal({ template, onClose, onDone }: {
  template: TemplateInfo | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const [preview, setPreview] = useState<BulkPreview | null>(null);
  const [clearOverrides, setClearOverrides] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = async (tpl: TemplateInfo, clear: boolean) => {
    setBusy(true); setError(null); setPreview(null);
    try {
      const r = await syncTemplate(tpl.code, { dry_run: true, clear_overrides: clear });
      setPreview(r.data as BulkPreview);
    } catch (e) {
      setError(explainApiError(e, "同步预览失败"));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (template) { setClearOverrides(false); load(template, false); }
  }, [template]);

  const execute = async () => {
    if (!template || !preview) return;
    setBusy(true); setError(null);
    try {
      const r = await syncTemplate(template.code, {
        dry_run: false, clear_overrides: clearOverrides, fingerprint: preview.fingerprint,
      });
      const applied = (r.data as { applied: number }).applied;
      message.success(`已同步 ${applied} 个账号到模板 v${template.version}，相关账号需重新登录`);
      onDone();
      onClose();
    } catch (e) {
      const status = (e as { response?: { status?: number } })?.response?.status;
      setError(explainApiError(e, status === 409
        ? "预览后模板或账号被他人修改，请重新预览" : "同步失败（未做任何修改）"));
      if (status === 409 && template) load(template, clearOverrides);
    } finally {
      setBusy(false);
    }
  };

  const columns = [
    { title: "账号", dataIndex: "username",
      render: (v: string, r: BulkPreviewItem) => <span>{r.display_name || v} <span style={{ color: "#999" }}>({v})</span></span> },
    { title: "快照版本", key: "ver",
      render: (_: unknown, r: BulkPreviewItem) => <span>v{r.from_version ?? "—"} → v{r.to_version}</span> },
    { title: "权限变化", dataIndex: "changed_keys",
      render: (cks: BulkPreviewItem["changed_keys"]) => cks.length === 0
        ? <span style={{ color: "#999" }}>无变化（仅升版本号）</span>
        : <Space size={4} wrap>{cks.map((c) => (
            <Tag key={c.key} color={c.to ? "green" : "orange"}>
              {c.label}：{c.from ? "开" : "关"} → {c.to ? "开" : "关"}
            </Tag>
          ))}</Space> },
  ];

  return (
    <Modal
      title={template ? `同步账号 · 模板「${template.name}」v${template.version}` : ""}
      open={!!template}
      onCancel={onClose}
      width={760}
      footer={null}
      destroyOnClose
    >
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
      <Space style={{ marginBottom: 12 }}>
        <Checkbox
          checked={clearOverrides}
          onChange={(e) => {
            setClearOverrides(e.target.checked);
            if (template) load(template, e.target.checked);
          }}
        >同步时清除各账号的个别调整（回到纯模板口径）</Checkbox>
      </Space>
      {preview && (
        <>
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={`将同步 ${preview.affected} 个账号（${preview.changed} 个有实际权限变化）`}
            description="全成功或全失败；有变化的账号旧登录立即失效。个别调整默认保留，除非勾选上方清除。"
          />
          <Table
            size="small"
            rowKey="username"
            columns={columns as never}
            dataSource={preview.preview}
            pagination={preview.preview.length > 10 ? { pageSize: 10 } : false}
            scroll={{ x: 480 }}
          />
        </>
      )}
      <Space style={{ display: "flex", justifyContent: "flex-end", marginTop: 12 }}>
        <Button onClick={onClose}>取消</Button>
        <Button type="primary" danger loading={busy} disabled={!preview} onClick={execute}>
          确认同步
        </Button>
      </Space>
    </Modal>
  );
}
