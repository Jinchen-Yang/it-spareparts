import { useState } from "react";
import { Alert, Button, Input, Modal, Space } from "antd";
import { useAuthz } from "../../authz/AuthzProvider";
import { accountAccess } from "../../authz/accountAccess";
import { useCapturedRunner, authorizationErrorText } from "../../authz/requestLifecycle";
import { api } from "../../api";
import { transportConfig } from "../../authz/transport";

/** A known username is sufficient; no directory read or manufactured Account is required. */
export default function AccountProfileModal({ username, initialName, onClose, onDone }: {
  username: string; initialName?: string | null; onClose: () => void; onDone: () => void;
}) {
  const { snapshot } = useAuthz();
  const [name, setName] = useState(initialName ?? ""), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const run = useCapturedRunner(JSON.stringify([username, name]));
  const allowed = accountAccess(snapshot).profile;
  const save = async () => {
    setBusy(true); setError("");
    try {
      await run({ admit: actor => accountAccess(actor).profile, queryKey: JSON.stringify([username, name]),
        request: capture => api.put(`/accounts/${encodeURIComponent(username)}`, {
          schema_version: 1, catalog_version: snapshot!.catalog_version, profile: { display_name: name.trim() || null },
        }, transportConfig(capture)),
        commit: () => { onDone(); onClose(); },
      });
    } catch (e) { setError(authorizationErrorText(e, "资料保存失败；没有自动重试")); }
    finally { setBusy(false); }
  };
  return <Modal open title={`修改显示资料 · ${username}`} onCancel={onClose} footer={null}>
    <Space direction="vertical" style={{ width: "100%" }}>
      <Alert type="info" message="此表单只保存显示名" description={initialName === undefined
        ? "未读取当前显示名。请输入要保存的新值；保存空值会清除显示名。" : "角色、销售归属和授权配置保持。"} />
      {error && <Alert type="error" message={error} />}
      <label>显示名<Input value={name} onChange={event => setName(event.target.value)} disabled={busy || !allowed} /></label>
      <Button type="primary" loading={busy} disabled={!allowed} onClick={save}>保存显示名</Button>
    </Space>
  </Modal>;
}
