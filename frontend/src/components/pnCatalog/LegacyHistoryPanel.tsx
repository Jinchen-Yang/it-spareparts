import { useEffect, useRef, useState } from "react";
import { Alert, Button, List, Space, Tag } from "antd";
import { catalogError } from "../../api/pnCatalog";
import { useCatalogAccess } from "./access";
interface HistoricalActivity { source_record_id: number; kind: string; source_id: string; legacy_status: string; state: string; event_at: string | null; attempts: number | null; parent_refs: Record<string, string>; continuation: string }
export default function LegacyHistoryPanel({ partId, inboxId }: { partId?: number; inboxId?: string }) {
  const { client, can } = useCatalogAccess(); const readable = can("part_master", "read_audit");
  const [rows, setRows] = useState<HistoricalActivity[]>([]); const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false); const sequence = useRef(0);
  const [downloads, setDownloads] = useState<{ url: string; filename: string; image_count: number }[]>([]);
  const download = async (recordId: number) => { const seq = sequence.current; setDownloads([]); setError(""); try { const result = await client.get<{ archives: { url: string; filename: string; image_count: number }[] }>(`/legacy-history/${recordId}/bundle-downloads`); if (seq === sequence.current) setDownloads(result.data.archives); } catch (e) { if (seq === sequence.current) setError(catalogError(e).message); } };
  const load = async (more = false) => {
    const seq = ++sequence.current; setBusy(true); setError("");
    try { const result = await client.get<HistoricalActivity[]>("/legacy-history", { part_id: partId, inbox_id: inboxId, limit: 20, ...(more && cursor ? { cursor } : {}) }); if (seq !== sequence.current) return; setRows(old => more ? [...old, ...result.data] : result.data); setCursor(result.meta.next_cursor || null); }
    catch (e) { if (seq === sequence.current) { setRows([]); setError(catalogError(e).message); } }
    finally { if (seq === sequence.current) setBusy(false); }
  };
  useEffect(() => { setRows([]); setDownloads([]); if (readable) void load(); return () => { ++sequence.current; }; }, [client, readable, partId, inboxId]);
  if (!readable) return null;
  return <Space direction="vertical" style={{ width: "100%" }}><Space><strong>我的迁入历史</strong><Button disabled={busy} onClick={() => load()}>刷新历史</Button></Space>
    <small>保留原任务编号与状态；历史记录不会自行重新执行。</small>{error && <Alert type="warning" message={error} />}
    <List loading={busy} dataSource={rows} renderItem={item => <List.Item><Space wrap><span>{item.kind}</span><Tag>{item.legacy_status}</Tag><small>{item.source_id}</small>{item.attempts !== null && <span>原尝试 {item.attempts} 次</span>}{item.continuation === "needs_recovery" && <span>等待迁移核对与恢复决定</span>}{item.kind === "bundle" && item.legacy_status === "done" && <Button onClick={() => download(item.source_record_id)}>读取历史打包文件</Button>}</Space></List.Item>} />
    {downloads.map(file => <a key={file.url} href={file.url} target="_blank" rel="noopener noreferrer">{file.filename} · {file.image_count} 张图片</a>)}
    {cursor && <Button disabled={busy} onClick={() => load(true)}>更多历史记录</Button>}
  </Space>;
}
