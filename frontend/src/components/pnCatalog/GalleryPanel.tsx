import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Empty, Image, Input, Popconfirm, Select, Space, Tag } from "antd";
import { catalogError, CommandIntent, sendUpload, type Capabilities, type Entry, type Gallery, type UploadForm, type UploadTicket } from "../../api/pnCatalog";
import { useCatalogAccess } from "./access";
import SuppressionsPanel from "./SuppressionsPanel";

function EntryImage({ entry }: { entry: Entry }) {
  const { client } = useCatalogAccess();
  const [links, setLinks] = useState<{ url: string; derivatives: { url: string }[] }>();
  const [error, setError] = useState("");
  const load = () => client.get<{ url: string; derivatives: { url: string }[] }>(`/gallery/entries/${entry.entry_id}/links`).then(({ data }) => { setLinks(data); setError(""); }).catch(e => setError(catalogError(e).message));
  useEffect(() => { let alive = true; client.get<{ url: string; derivatives: { url: string }[] }>(`/gallery/entries/${entry.entry_id}/links`).then(({ data }) => { if (alive) setLinks(data); }).catch(e => { if (alive) setError(catalogError(e).message); }); return () => { alive = false; }; }, [client, entry.entry_id, entry.revision]);
  return links && !error ? <Image width={180} height={150} style={{ objectFit: "contain" }} src={links.derivatives[0]?.url || links.url} preview={{ src: links.url }} onError={() => setError("刷新图片链接")} /> : <Button onClick={load}>{error || "读取图片"}</Button>;
}

export default function GalleryPanel({ partId, capabilities }: { partId: number; capabilities: Capabilities }) {
  const { client, can, field } = useCatalogAccess();
  const [status, setStatus] = useState("gallery");
  const [gallery, setGallery] = useState<Gallery>();
  const [identity, setIdentity] = useState<number>();
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [ticket, setTicket] = useState<UploadTicket>();
  const [file, setFile] = useState<File>();
  const [purpose, setPurpose] = useState<"common" | "defect">("common");
  const [restoreReason, setRestoreReason] = useState("");
  const intent = useRef(new CommandIntent());
  const uploadIntent = useRef(crypto.randomUUID());
  const sequence = useRef(0);
  const load = async (more = false) => {
    const seq = ++sequence.current;
    try {
      const result = await client.get<Gallery>(`/parts/${partId}/gallery`, { status, limit: 50, ...(more && cursor ? { cursor } : {}) });
      if (seq !== sequence.current) return;
      if (more && gallery && (gallery.group_id !== result.data.group_id || gallery.revision !== result.data.revision)) throw new Error("图库在分页期间已变化，请刷新后继续操作");
      setGallery(g => more && g ? { ...result.data, entries: [...g.entries, ...result.data.entries], ordered_entry_ids: [...g.ordered_entry_ids, ...result.data.ordered_entry_ids], primary_entry_id: result.data.primary_entry_id || g.primary_entry_id } : result.data);
      setIdentity(result.meta.identity_revision); setCursor(result.meta.next_cursor || null);
    } catch (e) { if (seq === sequence.current) setError(catalogError(e).message); }
  };
  useEffect(() => { setGallery(undefined); void load(); return () => { ++sequence.current; }; }, [client, partId, status]);
  const command = async (method: "post" | "patch" | "delete", path: string, body: unknown) => {
    setBusy(true); setError("");
    try { await client.write(method, path, body, intent.current.key(path, body)); intent.current.clear(); await load(); }
    catch (e) { setError(`${catalogError(e).message}。请刷新核对后再操作。`); } finally { setBusy(false); }
  };
  const upload = async () => {
    if (!file || identity === undefined) return;
    if (!new Set(["image/jpeg", "image/png", "image/webp"]).has(file.type) || file.size <= 0 || file.size > 20 * 1024 * 1024) { setError("请选择不超过 20 MiB 的 JPG、PNG 或 WebP 图片"); return; }
    setBusy(true); setError("");
    try {
      let current = ticket ? (await client.get<UploadTicket>(`/uploads/${ticket.upload_id}`)).data : undefined;
      if (current) setTicket(current);
      if (!current) {
        const body = { client_request_id: uploadIntent.current, filename: file.name, size_bytes: file.size, content_type: file.type, part_id: partId, expected_identity_revision: identity, purpose };
        current = (await client.write<UploadTicket>("post", "/uploads", body, uploadIntent.current)).data; setTicket(current);
      }
      if (current.status === "issued") {
        const form = current.upload_form || (await client.get<UploadForm>(`/uploads/${current.upload_id}/transport`)).data;
        await sendUpload(form, file);
        const path = `/uploads/${current.upload_id}/completion`; const body = { expected_revision: current.revision };
        current = (await client.write<UploadTicket>("post", path, body, intent.current.key(path, body))).data; setTicket(current); intent.current.clear();
      }
    } catch (e) { setError(catalogError(e).message); } finally { setBusy(false); }
  };
  const refreshTicket = async () => { if (!ticket) return; setBusy(true); try { setTicket((await client.get<UploadTicket>(`/uploads/${ticket.upload_id}`)).data); } catch (e) { setError(catalogError(e).message); } finally { setBusy(false); } };
  const commitUpload = async () => {
    if (!ticket || identity === undefined) return;
    setBusy(true);
    try {
      const path = `/parts/${partId}/gallery/entries`; const body = { expected_identity_revision: identity, upload_id: ticket.upload_id, purpose };
      await client.write<Entry>("post", path, body, intent.current.key(path, body)); intent.current.clear(); setTicket(undefined); setFile(undefined); uploadIntent.current = crypto.randomUUID(); await load();
    } catch (e) { setError(catalogError(e).message); } finally { setBusy(false); }
  };
  const arrange = (entry: Entry, primary: boolean) => {
    if (!gallery) return;
    const ordered = primary ? gallery.ordered_entry_ids : [entry.entry_id, ...gallery.ordered_entry_ids.filter(id => id !== entry.entry_id)];
    void command("patch", `/parts/${partId}/gallery`, { expected_gallery_revision: gallery.revision, primary_entry_id: primary ? entry.entry_id : gallery.primary_entry_id, ordered_entry_ids: ordered });
  };
  const galleryWrite = (action: string) => field("part_gallery", "gallery", true) && can("part_gallery", action);
  return <Space direction="vertical" style={{ width: "100%" }}>
    {!capabilities.media && <Alert type="info" message="图片服务尚未启用，上传和缩略图任务暂不可用。" />}
    {error && <Alert type="warning" message={error} />}
    <Space wrap><Select aria-label="图库状态" value={status} onChange={setStatus} options={[{ value: "gallery", label: "正式图库" }, { value: "candidate", label: "候选图片" }, { value: "deleted", label: "已删除" }]} /><Button onClick={() => load()} disabled={busy}>刷新图库</Button></Space>
    {capabilities.media && field("part_gallery", "uploads", true) && can("part_gallery", "uploads.create") && can("part_gallery", "uploads.complete") && <Card size="small" title="上传图片">
      <Space direction="vertical">
        <input aria-label="选择图片" type="file" accept="image/jpeg,image/png,image/webp" disabled={busy || !!ticket} onChange={e => { setFile(e.target.files?.[0]); uploadIntent.current = crypto.randomUUID(); }} />
        <Select aria-label="图片用途" value={purpose} disabled={!!ticket || busy} options={[{ value: "common", label: "通用外观（好坏件共用）" }, { value: "defect", label: "缺陷图（坏件使用）" }]} onChange={setPurpose} />
        {!ticket || ticket.status === "issued" ? <Button onClick={upload} disabled={!file} loading={busy}>{ticket ? "重试上传" : "上传并校验"}</Button> : <Tag>{ticket.status === "verifying" ? "正在校验图片" : ticket.status === "ready" ? "校验完成，待加入图库" : ticket.status}</Tag>}
        {ticket && <Space wrap><Button onClick={refreshTicket} disabled={busy}>刷新上传状态</Button>{ticket.status === "ready" && galleryWrite("gallery.archive") && <Button type="primary" onClick={commitUpload} loading={busy}>加入当前 PN 图库</Button>}
          {can("part_gallery", "uploads.cancel") && ["issued", "verifying", "ready"].includes(ticket.status) && <Button onClick={async () => { const path = `/uploads/${ticket.upload_id}/cancellation`; const body = { expected_revision: ticket.revision }; setBusy(true); try { setTicket((await client.write<UploadTicket>("post", path, body, intent.current.key(path, body))).data); intent.current.clear(); } catch (e) { setError(catalogError(e).message); } finally { setBusy(false); } }}>取消上传</Button>}
          {["cancelled", "failed", "committed"].includes(ticket.status) && <Button onClick={() => { setTicket(undefined); setFile(undefined); uploadIntent.current = crypto.randomUUID(); }}>开始另一张上传</Button>}</Space>}
        {ticket?.error_code && <Alert type="error" message={ticket.error_code} />}
      </Space>
    </Card>}
    {status === "deleted" && galleryWrite("gallery.restore") && <Input aria-label="恢复理由" placeholder="恢复图片前填写理由" value={restoreReason} onChange={e => setRestoreReason(e.target.value)} maxLength={2000} />}
    <Space wrap align="start">{gallery?.entries.map(entry => <Card key={entry.entry_id} size="small" style={{ width: 230 }}>
      {capabilities.media && status !== "deleted" && <EntryImage entry={entry} />}
      <Space direction="vertical" style={{ width: "100%" }}><Space><Tag>{entry.purpose === "defect" ? "缺陷图" : "通用图"}</Tag>{gallery.primary_entry_id === entry.entry_id && <Tag color="blue">主图</Tag>}</Space>
        <small>{entry.width} × {entry.height}</small>
        {entry.status === "candidate" && galleryWrite("gallery.adopt") && <Button disabled={busy} onClick={() => command("patch", `/gallery/entries/${entry.entry_id}`, { expected_revision: entry.revision, status: "gallery" })}>采用到正式图库</Button>}
        {entry.status === "gallery" && galleryWrite("gallery.arrange") && <Space><Button disabled={busy || !!cursor} onClick={() => arrange(entry, true)}>设为主图</Button><Button disabled={busy || !!cursor} onClick={() => arrange(entry, false)}>移到首位</Button></Space>}
        {entry.status !== "deleted" && galleryWrite("gallery.change_purpose") && <Button disabled={busy} onClick={() => command("patch", `/gallery/entries/${entry.entry_id}`, { expected_revision: entry.revision, purpose: entry.purpose === "common" ? "defect" : "common" })}>改为{entry.purpose === "common" ? "缺陷图" : "通用图"}</Button>}
        {capabilities.media && entry.status !== "deleted" && galleryWrite("gallery.derive") && !entry.derivatives.some(d => d.status !== "failed") && <Button disabled={busy} onClick={() => command("post", `/assets/${entry.asset_id}/derivatives`, { entry_id: entry.entry_id, expected_revision: entry.revision, recipe_id: "thumbnail-600-webp85-v1" })}>生成缩略图</Button>}
        {entry.status !== "deleted" && galleryWrite("gallery.delete") && <Popconfirm title="删除此图片？可在已删除列表中恢复。" onConfirm={() => command("delete", `/gallery/entries/${entry.entry_id}?expected_revision=${entry.revision}`, undefined)}><Button danger disabled={busy}>删除</Button></Popconfirm>}
        {entry.status === "deleted" && galleryWrite("gallery.restore") && <Button disabled={busy || !restoreReason.trim()} onClick={() => command("post", `/gallery/entries/${entry.entry_id}/restoration`, { expected_deleted_revision: entry.revision, reason: restoreReason.trim() })}>恢复</Button>}
      </Space>
    </Card>)}</Space>
    {gallery && !gallery.entries.length && <Empty description="暂无图片" />}
    {cursor && <Button onClick={() => load(true)} disabled={busy}>加载更多图片</Button>}
    {gallery?.group_id && <SuppressionsPanel key={`${partId}:${gallery.group_id}`} gallery={gallery} onChanged={() => load()} />}
  </Space>;
}
