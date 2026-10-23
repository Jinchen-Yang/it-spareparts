import { useEffect, useRef, useState } from "react";
import { Alert, Button, List, Space, Tag } from "antd";
import { catalogError, CommandIntent } from "../../api/pnCatalog";
import { useCatalogAccess } from "./access";
import { JOB_LABELS, STATUS_LABELS, jobGuard, type Job } from "./workflows";

interface Archive { number: number; filename: string; url: string; image_count: number; size_bytes: number }
export default function JobsPanel({ partId, inboxId, latestJob }: { partId?: number; inboxId?: string; latestJob?: Job }) {
  const { client, can } = useCatalogAccess();
  const [rows, setRows] = useState<Job[]>([]); const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  const [downloads, setDownloads] = useState<{ jobId: string; archives: Archive[]; expires: number }>();
  const sequence = useRef(0); const intent = useRef(new CommandIntent());
  const allowed = (job: Job, action: string) => can(job.kind === "enrichment.research" ? "part_master" : job.kind.startsWith("identity.") ? "part_identity" : "part_gallery", `job.${action}`);
  const readable = can("part_master", "job.read") || can("part_gallery", "job.read") || can("part_identity", "job.read");
  const load = async (more = false) => {
    const seq = ++sequence.current; setBusy(true); setError("");
    try { const result = await client.get<Job[]>("/jobs", { part_id: partId, inbox_id: inboxId, limit: 20, ...(more && cursor ? { cursor } : {}) }); if (seq !== sequence.current) return; setRows(old => more ? [...old, ...result.data] : result.data); setCursor(result.meta.next_cursor || null); }
    catch (e) { if (seq === sequence.current) { setRows([]); setError(catalogError(e).message); } }
    finally { if (seq === sequence.current) setBusy(false); }
  };
  useEffect(() => { if (readable) void load(); return () => { ++sequence.current; }; }, [client, partId, inboxId, latestJob?.job_id, readable]);
  useEffect(() => { if (!downloads) return; const timeout = setTimeout(() => setDownloads(undefined), Math.max(0, downloads.expires - Date.now())); return () => clearTimeout(timeout); }, [downloads]);
  const action = async (job: Job, operation: "cancellation" | "retry") => {
    const path = `/jobs/${job.job_id}/${operation}`; const body = jobGuard(job); setBusy(true); setError("");
    try { const { data } = await client.write<Job>("post", path, body, intent.current.key(path, body)); setRows(old => old.map(v => v.job_id === data.job_id ? data : v)); intent.current.clear(); }
    catch (e) { setError(`${catalogError(e).message}。请刷新任务状态后核对。`); } finally { setBusy(false); }
  };
  const sign = async (job: Job) => {
    setBusy(true); setDownloads(undefined); setError("");
    try { const { data } = await client.get<{ archives: Archive[]; expires_in: number }>(`/jobs/${job.job_id}/bundle-downloads`); if (data.archives.some(a => new URL(a.url).protocol !== "https:")) throw new Error("下载地址无效"); setDownloads({ jobId: job.job_id, archives: data.archives, expires: Date.now() + data.expires_in * 1000 }); }
    catch (e) { setError(catalogError(e).message); } finally { setBusy(false); }
  };
  if (!readable) return null;
  return <Space direction="vertical" style={{ width: "100%" }}>
    <Space><strong>我发起的任务</strong><Button disabled={busy} onClick={() => load()}>刷新任务状态</Button></Space>
    <small>刷新只读取状态，不会触发识别、研究或重新打包。</small>
    {error && <Alert type="warning" message={error} />}
    <List loading={busy} dataSource={rows} renderItem={job => <List.Item><Space direction="vertical" style={{ width: "100%" }}>
      <Space wrap><strong>{JOB_LABELS[job.kind] || "后台处理"}</strong><Tag>{STATUS_LABELS[job.status]}</Tag><span>{new Date(job.created_at).toLocaleString()}</span>
        {["pending", "running"].includes(job.status) && allowed(job, "cancel") && <Button onClick={() => action(job, "cancellation")} disabled={busy}>取消任务</Button>}
        {["failed", "cancelled"].includes(job.status) && allowed(job, "retry") && <Button onClick={() => action(job, "retry")} disabled={busy}>重试任务</Button>}
        {job.kind === "enrichment.bundle" && job.status === "succeeded" && can("part_gallery", "enrichment.bundle.read") && <Button disabled={busy} onClick={() => sign(job)}>获取当前下载链接</Button>}
      </Space>
      {job.error_code && <small>处理提示：{job.error_code}</small>}
      {job.kind === "enrichment.research" && job.status === "succeeded" && <span>研究已结束，请到资料候选和候选图片中核对结果；没有资料会自动覆盖正式内容。</span>}
      {downloads?.jobId === job.job_id && downloads.archives.map(a => <a key={a.number} href={a.url} target="_blank" rel="noopener noreferrer">{a.filename} · {a.image_count} 张 · {(a.size_bytes / 1024 / 1024).toFixed(1)} MiB</a>)}
    </Space></List.Item>} />
    {cursor && <Button onClick={() => load(true)} disabled={busy}>加载更多任务</Button>}
  </Space>;
}
