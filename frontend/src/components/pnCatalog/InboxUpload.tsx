import { useRef, useState } from "react";
import { Alert, Button, Card, List, Popconfirm, Select, Space, Tag } from "antd";
import { catalogError, CommandIntent, sendUpload, type UploadForm, type UploadTicket } from "../../api/pnCatalog";
import { useCatalogAccess } from "./access";
import type { Inbox } from "./workflows";

interface Group { group_id: string; revision: number; ordered_item_ids: string[] }
interface Row { id: string; file: File; position: number; purpose: "common" | "defect"; ticket?: UploadTicket; error?: string; grouped: boolean; intent: CommandIntent }
export default function InboxUpload({ onChanged }: { onChanged: () => void }) {
  const { client, can, field } = useCatalogAccess();
  const [rows, setRows] = useState<Row[]>([]); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const [purpose, setPurpose] = useState<"common" | "defect">("common");
  const [mode, setMode] = useState<"same_item" | "batch">("batch");
  const groupKey = useRef(crypto.randomUUID());
  const group = useRef<Group>();
  const groupEnabled = can("part_gallery", "inbox.group.create") && can("part_gallery", "inbox.group.add") && field("part_gallery", "inbox", true);
  const update = (row: Row, values: Partial<Row>) => { Object.assign(row, values); setRows(old => old.map(r => r.id === row.id ? { ...row } : r)); };
  const run = async (row: Row) => {
    setBusy(true); update(row, { error: undefined });
    try {
      let ticket = row.ticket ? (await client.get<UploadTicket>(`/uploads/${row.ticket.upload_id}`)).data : undefined;
      if (!ticket) ticket = (await client.write<UploadTicket>("post", "/uploads", { client_request_id: row.id, filename: row.file.name, size_bytes: row.file.size, content_type: row.file.type, purpose: row.purpose }, row.id)).data;
      update(row, { ticket });
      if (groupEnabled && !row.grouped) {
        // A lost add response is recovered precisely, without replaying human sort positions.
        let existing: Inbox | undefined;
        try { existing = (await client.get<Inbox>(`/uploads/${ticket.upload_id}/inbox`)).data; }
        catch (e) { if (catalogError(e).code !== "request.not_found") throw e; }
        if (!existing?.group_id) {
          const groupBody = { client_group_id: groupKey.current, capture_mode: mode };
          group.current = (await client.write<Group>("post", "/inbox-groups", groupBody, groupKey.current)).data;
          const path = `/inbox-groups/${group.current.group_id}/items`;
          const body = { upload_id: ticket.upload_id, position: group.current.ordered_item_ids.length, client_order: row.position, expected_group_revision: group.current.revision };
          group.current = (await client.write<Group>("post", path, body, row.intent.key(path, body))).data; row.intent.clear();
        }
        update(row, { grouped: true }); onChanged();
      }
      if (ticket.status === "issued") {
        const form = ticket.upload_form || (await client.get<UploadForm>(`/uploads/${ticket.upload_id}/transport`)).data;
        await sendUpload(form, row.file);
        const path = `/uploads/${ticket.upload_id}/completion`; const body = { expected_revision: ticket.revision };
        ticket = (await client.write<UploadTicket>("post", path, body, row.intent.key(path, body))).data;
        update(row, { ticket }); row.intent.clear();
      }
      onChanged();
    } catch (e) { update(row, { error: catalogError(e).message }); } finally { setBusy(false); }
  };
  const cancel = async (row: Row) => {
    if (!row.ticket) { setRows(old => old.filter(r => r.id !== row.id)); return; }
    const path = `/uploads/${row.ticket.upload_id}/cancellation`; const body = { expected_revision: row.ticket.revision }; setBusy(true);
    try { update(row, { ticket: (await client.write<UploadTicket>("post", path, body, row.intent.key(path, body))).data, error: undefined }); row.intent.clear(); onChanged(); }
    catch (e) { update(row, { error: catalogError(e).message }); } finally { setBusy(false); }
  };
  const refresh = async (row: Row) => {
    if (!row.ticket) return; setBusy(true);
    try { update(row, { ticket: (await client.get<UploadTicket>(`/uploads/${row.ticket.upload_id}`)).data, error: undefined }); onChanged(); }
    catch (e) { update(row, { error: catalogError(e).message }); } finally { setBusy(false); }
  };
  return <Card title="添加收件箱图片" size="small"><Space direction="vertical" style={{ width: "100%" }}>
    {error && <Alert type="warning" message={error} />}
    <Select aria-label="上传图片用途" value={purpose} disabled={busy} onChange={setPurpose} options={[{ value: "common", label: "通用外观" }, { value: "defect", label: "缺陷图片" }]} />
    {groupEnabled && <Select aria-label="拍摄分组方式" value={mode} disabled={busy || rows.some(r => r.ticket)} onChange={setMode} options={[{ value: "batch", label: "多件图片，分别核对归档" }, { value: "same_item", label: "同件多图，保留拍摄顺序" }]} />}
    <input aria-label="选择收件箱图片" type="file" accept="image/jpeg,image/png,image/webp" multiple disabled={busy || rows.length >= 20} onChange={e => {
      const files = Array.from(e.target.files || []);
      if (files.length + rows.length > 20 || files.some(f => !["image/jpeg", "image/png", "image/webp"].includes(f.type) || f.size <= 0 || f.size > 20 * 1024 ** 2)) { setError("每批最多 20 张，每张为不超过 20 MiB 的 JPG、PNG 或 WebP"); return; }
      setError(""); setRows(old => [...old, ...files.map((file, index) => ({ id: crypto.randomUUID(), file, position: Math.max(-1, ...old.map(row => row.position)) + 1 + index, purpose, grouped: false, intent: new CommandIntent() }))]); e.target.value = "";
    }} />
    <List dataSource={rows} renderItem={row => <List.Item><Space wrap><span>{row.position + 1}. {row.file.name}</span><Tag>{row.ticket?.status || "等待上传"}</Tag>
      {(!row.ticket || row.ticket.status === "issued") && <Button onClick={() => run(row)} disabled={busy}>{row.ticket ? "重试当前上传" : "上传并校验"}</Button>}
      {row.ticket && <Button onClick={() => refresh(row)} disabled={busy}>刷新接收状态</Button>}
      {(!row.ticket || can("part_gallery", "uploads.cancel") && ["issued", "verifying"].includes(row.ticket.status)) && <Popconfirm title="取消这次图片上传？" onConfirm={() => cancel(row)}><Button disabled={busy}>取消上传</Button></Popconfirm>}
      {row.error && <Alert type="warning" message={row.error} />}
    </Space></List.Item>} />
    {rows.length > 0 && rows.every(row => row.ticket && ["ready", "committed", "failed", "cancelled"].includes(row.ticket.status)) && <Button disabled={busy} onClick={() => { setRows([]); group.current = undefined; groupKey.current = crypto.randomUUID(); }}>添加下一批图片</Button>}
    <small>校验完成后在收件箱核对、认领和归档；刷新页面后已接收的图片仍在服务器收件箱。</small>
  </Space></Card>;
}
