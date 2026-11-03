import { useEffect, useRef, useState } from "react";
import { Alert, Button, Modal, Space, Table, Upload } from "antd";
import { applyReceiptWorkbook, downloadReceiptWorkbook, previewReceiptWorkbook, type ReceiptWorkbookPreview } from "../../../api/maintenanceReceiptWorkbook";
import { sessionStale, useSessionOwner } from "../../../utils/session";

function errorText(error: unknown) {
  const detail = (error as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail && typeof detail.message === "string") return detail.message;
  return "操作未取得成功回执，请重试；重复确认同一文件不会重复修改。";
}

export default function ReceiptWorkbookEditor({ projectId, onChanged }: {
  projectId: string; onChanged: () => Promise<boolean>;
}) {
  const owner = useSessionOwner();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [plan, setPlan] = useState<ReceiptWorkbookPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const seq = useRef(0);
  const applying = useRef(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; seq.current++; }; }, []);
  const current = (key: number) => alive.current && key === seq.current && !sessionStale(owner);
  function allowed() {
    if (!sessionStale(owner)) return true;
    setPlan(null); setFile(null); setError("登录账号已变化，请刷新后继续"); return false;
  }
  function finish(key: number) {
    if (!alive.current || key !== seq.current) return;
    setBusy(false);
    allowed();
  }
  async function download() {
    if (!allowed()) return;
    const key = ++seq.current; setBusy(true); setError(null);
    try {
      const { data } = await downloadReceiptWorkbook(projectId);
      if (!current(key)) return;
      const url = URL.createObjectURL(data); const link = document.createElement("a");
      link.href = url; link.download = `返还收货编辑-${projectId}.xlsx`; link.click(); URL.revokeObjectURL(url);
    } catch (err) { if (current(key)) setError(errorText(err)); }
    finally { finish(key); }
  }
  async function preview(selected: File) {
    if (!allowed() || applying.current) return false;
    const key = ++seq.current; setBusy(true); setPlan(null); setFile(null); setDone(null); setError(null);
    try {
      const { data } = await previewReceiptWorkbook(projectId, selected);
      if (current(key)) { setFile(selected); setPlan(data); }
    } catch (err) { if (current(key)) setError(errorText(err)); }
    finally { finish(key); }
    return false;
  }
  async function apply() {
    if (!allowed() || !file || !plan || applying.current) return;
    applying.current = true; setBusy(true); setError(null);
    const key = ++seq.current;
    try {
      const { data } = await applyReceiptWorkbook(projectId, file, plan.digest);
      if (!current(key)) return;
      setPlan(null); setFile(null);
      setDone(`${data.replayed ? "已返回首次成功回执" : "已保存"}：修改 ${data.updates} 行，作废 ${data.voids} 行。`);
      let refreshed = false;
      try { refreshed = await onChanged(); } catch { /* committed success */ }
      if (current(key) && !refreshed) setError("保存已成功，列表刷新失败，请刷新页面查看。无需再次上传。");
    } catch (err) { if (current(key)) setError(errorText(err)); }
    finally { applying.current = false; finish(key); }
  }
  return <>
    <Button size="small" onClick={() => setOpen(true)}>返还收货Excel编辑</Button>
    <Modal title="返还收货Excel编辑" open={open} width={850} onCancel={() => { if (!busy) setOpen(false); }} footer={null}>
      <Space direction="vertical" style={{ width: "100%" }}>
        <Alert type="info" showIcon message="下载现有收货，修改后预览，再确认保存。全文件任一行不通过则全部不保存。"
          description="可更正数量、件况、收到时间、SN和备注，也可填写作废及原因。来源、PN、项目和需求关联保持只读；不能删行。本文件须在此上传，不能上传到项目总表。" />
        <Space wrap>
          <Button disabled={busy} onClick={() => void download()}>下载收货编辑模板</Button>
          <Upload accept=".xlsx" showUploadList={false} beforeUpload={preview} disabled={busy}>
            <Button loading={busy} disabled={busy}>选择文件并预览</Button>
          </Upload>
        </Space>
        {error && <Alert type="error" showIcon message={error} />}
        {done && <Alert type="success" showIcon message={done} />}
        {plan && <>
          <Alert type="warning" showIcon message={plan.replayed ? "此文件已保存，确认后返回首次回执，不重复修改。"
            : `将修改 ${plan.updates} 行、作废 ${plan.voids} 行，请核对下列变化。`} />
          <Table rowKey="receipt_id" size="small" dataSource={plan.rows} pagination={{ pageSize: 20 }} columns={[
            { title: "Excel行", dataIndex: "row_no", width: 80 }, { title: "操作", dataIndex: "action", width: 70 },
            { title: "变化", render: (_, row) => row.changes?.map(change => <div key={change.field}>{change.label}：{String(change.before || "（空）")} → {String(change.after || "（空）")}</div>) || "首次回执" },
            { title: "原因", dataIndex: "reason" },
          ]} />
          <Button type="primary" danger={plan.voids > 0} loading={busy} onClick={() => void apply()}>确认应用此文件</Button>
        </>}
      </Space>
    </Modal>
  </>;
}
