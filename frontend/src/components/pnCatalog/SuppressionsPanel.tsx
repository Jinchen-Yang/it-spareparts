import { useEffect, useRef, useState } from "react";
import { Alert, Button, Collapse, Empty, Input, Space } from "antd";
import { catalogError, CommandIntent, type Gallery } from "../../api/pnCatalog";
import { useCatalogAccess } from "./access";

interface Suppression { suppression_id: string; group_id: string; content_hash: string; reason: string; revision: number; active: boolean }
interface Intent { method: "post" | "delete"; path: string; body?: unknown; key: string }

export default function SuppressionsPanel({ gallery, onChanged }: { gallery: Gallery; onChanged: () => Promise<unknown> }) {
  const { client, can, field } = useCatalogAccess();
  const [rows, setRows] = useState<Suppression[]>([]), [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false), [loaded, setLoaded] = useState(false), [error, setError] = useState("");
  const [entryId, setEntryId] = useState(""), [reason, setReason] = useState("");
  const [pending, setPending] = useState<Intent>();
  const intent = useRef(new CommandIntent()); const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const readable = can("part_gallery", "gallery.read") && field("part_gallery", "gallery");
  const writable = (action: string) => can("part_gallery", action) && field("part_gallery", "gallery", true);
  if (!readable || !gallery.group_id) return null;
  const root = `/gallery-groups/${gallery.group_id}/suppressions`;
  const load = async (more = false) => {
    setBusy(true); setError("");
    try { const result = await client.get<Suppression[]>(root, { limit: 20, ...(more && cursor ? { cursor } : {}) });
      if (alive.current) { setRows(old => more ? [...old, ...result.data] : result.data); setCursor(result.meta.next_cursor || null); setLoaded(true); }
    } catch (e) { if (alive.current) { setRows([]); setCursor(null); setLoaded(false); setError(catalogError(e).message); } }
    finally { if (alive.current) setBusy(false); }
  };
  const execute = async (command: Intent) => {
    setBusy(true); setError(""); setPending(command); let saved = false;
    try {
      await client.write(command.method, command.path, command.body, command.key);
      if (!alive.current) return;
      saved = true; setPending(undefined); intent.current.clear(); setEntryId(""); setReason("");
      await onChanged(); if (alive.current) await load();
    } catch (e) { if (alive.current) { const failure = catalogError(e); if (!failure.ambiguous) setPending(undefined);
      setError(saved ? "规则已保存，请刷新图库核对当前显示。" : failure.ambiguous ? "请求结果未知，原规则和版本已保留，请重试同一次操作。" : `${failure.message}。草稿已保留；请刷新图库后重新核对。`); }
    } finally { if (alive.current) setBusy(false); }
  };
  const hide = () => {
    const entry = gallery.entries.find(item => item.entry_id === entryId);
    if (!entry?.content_hash || !reason.trim()) return;
    const body = { content_hash: entry.content_hash, expected_gallery_revision: gallery.revision, reason: reason.trim() };
    void execute({ method: "post", path: root, body, key: intent.current.key(root, body) });
  };
  const restore = (row: Suppression) => {
    const path = `${root}/${row.suppression_id}?expected_revision=${row.revision}`;
    void execute({ method: "delete", path, key: intent.current.key(path, undefined) });
  };
  return <Collapse onChange={keys => { if (keys.length && !loaded && !pending && !busy) void load(); }} items={[{ key: "hidden", label: "隐藏相同图片与恢复显示", children: <Space direction="vertical" style={{ width: "100%" }}>
    <small>隐藏规则适用于共享图库所有来源与用途中的相同图片。原文件保留；恢复显示不会恢复已删除的图片。</small>
    {error && <Alert type="warning" message={error} />}
    {pending && <Button loading={busy} onClick={() => execute(pending)}>重试同一次操作</Button>}
    {writable("gallery.suppress") && <Space wrap><select aria-label="要隐藏的图片" value={entryId} disabled={busy || !!pending} onChange={e => setEntryId(e.target.value)}><option value="">选择已加载的正式图片</option>{gallery.entries.filter(e => e.status === "gallery").map((e, i) => <option key={e.entry_id} value={e.entry_id}>{typeof e.source.filename === "string" ? e.source.filename : `图片 ${i + 1}`}</option>)}</select>
      <Input aria-label="隐藏原因" placeholder="隐藏原因" value={reason} maxLength={2000} disabled={busy || !!pending} onChange={e => setReason(e.target.value)} />
      <Button disabled={busy || !!pending || !entryId || !reason.trim()} onClick={hide}>隐藏相同内容</Button></Space>}
    <Button disabled={busy || !!pending} onClick={() => load()}>刷新隐藏规则</Button>
    {loaded && !rows.length && <Empty description="暂无隐藏规则" />}
    {rows.map(row => <Space key={row.suppression_id} wrap><span style={{ overflowWrap: "anywhere" }}>{row.content_hash.slice(0, 12)}… · {row.reason}</span>{writable("gallery.unsuppress") && <Button disabled={busy || !!pending} onClick={() => restore(row)}>恢复显示</Button>}</Space>)}
    {cursor && <Button disabled={busy || !!pending} onClick={() => load(true)}>更多隐藏规则</Button>}
  </Space> }]} />;
}
