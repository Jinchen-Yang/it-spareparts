import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Image, Input, Modal, Popconfirm, Select, Space, Tag } from "antd";
import { catalogError, CommandIntent } from "../../api/pnCatalog";
import { useCatalogAccess, type PartAccess } from "./access";
import useCapabilities from "./useCapabilities";
import InboxUpload from "./InboxUpload";
import JobsPanel from "./JobsPanel";
import InboxGroupEditor from "./InboxGroupEditor";
import { STATUS_LABELS, type Inbox, type Job } from "./workflows";
interface InboxLinks { url: string; derivatives: { url: string }[]; expires_at: string }

export default function InboxWorkspace() {
  const { client, can, field } = useCatalogAccess(); const { capabilities, configurationError } = useCapabilities();
  const [scope, setScope] = useState("mine"); const [status, setStatus] = useState("ready");
  const [rows, setRows] = useState<Inbox[]>([]); const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false); const [selected, setSelected] = useState<Inbox>();
  const sequence = useRef(0);
  const load = async (more = false) => {
    const seq = ++sequence.current; setBusy(true); setError("");
    try { const result = await client.get<Inbox[]>("/inbox-items", { scope, status, limit: 20, ...(more && cursor ? { cursor } : {}) }); if (seq !== sequence.current) return; setRows(old => more ? [...old, ...result.data] : result.data); setCursor(result.meta.next_cursor || null); }
    catch (e) { if (seq === sequence.current) { setRows([]); setError(catalogError(e).message); } } finally { if (seq === sequence.current) setBusy(false); }
  };
  useEffect(() => { void load(); return () => { ++sequence.current; }; }, [client, scope, status]);
  const uploadAllowed = capabilities.media && field("part_gallery", "uploads", true) && ["uploads.create", "uploads.complete", "uploads.read"].every(a => can("part_gallery", a));
  return <Space direction="vertical" style={{ width: "100%" }}>
    <Alert type="info" message="收件箱图片先核对与认领，再归档到明确的 PN；识图提示不会自动绑定型号。" />
    {configurationError && <Alert type="warning" message="辅助服务配置暂不可读，上传和识图入口已关闭。" />}
    {uploadAllowed && <InboxUpload onChanged={() => load()} />}
    <Space wrap><Select aria-label="收件箱范围" value={scope} onChange={v => { setScope(v); if (v === "shared" && status === "receiving") setStatus("ready"); }} options={[{ value: "mine", label: "我上传的图片" }, ...(can("part_gallery", "inbox.shared.read") ? [{ value: "shared", label: "共享收件箱" }] : [])]} /><Select aria-label="收件箱状态" value={status} onChange={setStatus} options={[{ value: "ready", label: "待归档" }, { value: "all", label: "全部" }, { value: "assigned", label: "已归档" }, { value: "deleted", label: "已删除" }, ...(scope === "mine" ? [{ value: "receiving", label: "接收中" }] : [])]} /><Button onClick={() => load()} loading={busy}>刷新收件箱</Button></Space>
    {error && <Alert type="warning" message={error} />}
    <Space wrap align="start">{rows.map(item => <Card key={item.inbox_id} title={item.filename} size="small" style={{ width: 260 }}>
      <Space direction="vertical"><Tag>{STATUS_LABELS[item.status]}</Tag><span>{item.uploaded_by_me ? "我上传的" : item.uploaded_by.principal_id} · {(item.size_bytes / 1024).toFixed(1)} KiB</span>
        {item.claimed_by && <span>{item.claimed_by_me ? "由我认领" : `认领人：${item.claimed_by}`}</span>}
        {item.recognition && <span>识图：{STATUS_LABELS[item.recognition.status]}</span>}
        <Button onClick={() => setSelected(item)}>查看、核对与归档</Button>
      </Space>
    </Card>)}</Space>
    {!busy && !rows.length && <span>暂无符合条件的图片</span>}
    {cursor && <Button onClick={() => load(true)} disabled={busy}>加载更多图片</Button>}
    <Modal open={Boolean(selected)} title={selected?.filename} width={850} footer={null} destroyOnClose onCancel={() => setSelected(undefined)}>
      {selected && <InboxDetail key={selected.inbox_id} initial={selected} mediaEnabled={capabilities.media} recognitionEnabled={capabilities.recognition} onChanged={() => load()} />}
    </Modal>
  </Space>;
}

function InboxDetail({ initial, mediaEnabled, recognitionEnabled, onChanged }: { initial: Inbox; mediaEnabled: boolean; recognitionEnabled: boolean; onChanged: () => void }) {
  const { client, can, field } = useCatalogAccess();
  const [item, setItem] = useState(initial); const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  const [links, setLinks] = useState<InboxLinks>();
  const [reason, setReason] = useState(""); const [query, setQuery] = useState(""); const [targets, setTargets] = useState<{ part_id: number; pn_std: string }[]>([]);
  const [targetId, setTargetId] = useState<number>(); const [target, setTarget] = useState<{ id: number; pn: string; identity: number }>();
  const [purpose, setPurpose] = useState<"common" | "defect">(initial.purpose); const [pendingAssignment, setPendingAssignment] = useState(false); const [latestJob, setLatestJob] = useState<Job>();
  const assignmentKey = useRef(crypto.randomUUID()); const assignmentDraft = useRef<{ path: string; body: unknown; key: string }>(); const intent = useRef(new CommandIntent()); const sequence = useRef(0);
  const allowed = (action: string) => can("part_gallery", action) && field("part_gallery", "inbox", true);
  const ownOrManager = item.uploaded_by_me || allowed("inbox.manage");
  const ownedClaim = item.claimed_by_me && Boolean(item.claim_expires_at && Date.parse(item.claim_expires_at) > Date.now());
  const refresh = async () => { setBusy(true); setLinks(undefined); try { const current = (await client.get<Inbox>(`/inbox-items/${item.inbox_id}`)).data; setItem(current); if (current.status === "assigned") { setPendingAssignment(false); assignmentDraft.current = undefined; } onChanged(); } catch (e) { setError(catalogError(e).message); } finally { setBusy(false); } };
  const preview = async () => { setLinks(undefined); try { const result = await client.get<InboxLinks>(`/inbox-items/${item.inbox_id}/links`); if (![result.data.url, ...result.data.derivatives.map(d => d.url)].every(url => new URL(url).protocol === "https:") || !Number.isFinite(Date.parse(result.data.expires_at))) throw new Error("图片链接无效"); setLinks(result.data); } catch (e) { setError(catalogError(e).message); } };
  useEffect(() => { void refresh(); return () => { ++sequence.current; }; }, [client, initial.inbox_id]);
  useEffect(() => { if (!links) return; const timeout = setTimeout(() => setLinks(undefined), Math.max(0, Date.parse(links.expires_at) - Date.now())); return () => clearTimeout(timeout); }, [links]);
  const command = async (method: "post" | "delete", path: string, body: unknown) => {
    setBusy(true); setError("");
    try { const result = await client.write<Inbox>(method, path, body, intent.current.key(path, body)); setItem(result.data); intent.current.clear(); setLinks(undefined); onChanged(); }
    catch (e) { setError(`${catalogError(e).message}。当前操作已保留，可重试或刷新后核对。`); } finally { setBusy(false); }
  };
  const search = async () => { const seq = ++sequence.current; setBusy(true); setTarget(undefined); setTargetId(undefined); try { const results = await client.identities(query.trim()); if (seq === sequence.current) setTargets(results); } catch (e) { if (seq === sequence.current) setError(catalogError(e).message); } finally { if (seq === sequence.current) setBusy(false); } };
  const prepare = async () => {
    if (!targetId) return; setBusy(true); setError("");
    try {
      const [access, gallery] = await Promise.all([client.get<PartAccess>(`/parts/${targetId}/access`), client.get<unknown>(`/parts/${targetId}/gallery`, { limit: 1 })]);
      if (access.data.part_id !== targetId || !["inbox.assign", "gallery.archive"].every(a => access.data.allowed_actions.includes(a)) || !["inbox", "gallery"].every(f => access.data.writable_fields.includes(f))) throw new Error("当前没有这个 PN 的归档权限");
      if (gallery.meta.identity_revision === undefined) throw new Error("目标 PN 缺少身份版本");
      setTarget({ id: targetId, pn: targets.find(t => t.part_id === targetId)!.pn_std, identity: gallery.meta.identity_revision }); assignmentKey.current = crypto.randomUUID(); assignmentDraft.current = undefined; intent.current.clear();
    } catch (e) { setError(catalogError(e).message); } finally { setBusy(false); }
  };
  const assign = async () => {
    if (!target) return;
    const path = `/inbox-items/${item.inbox_id}/assignment`;
    const body = { claim_revision: item.claim_revision, part_id: target.id, expected_identity_revision: target.identity, assignment_key: assignmentKey.current, purpose };
    assignmentDraft.current ??= { path, body, key: intent.current.key(path, body) };
    const frozen = assignmentDraft.current;
    setBusy(true); setError(""); setPendingAssignment(true);
    try { const result = await client.write<Inbox>("post", frozen.path, frozen.body, frozen.key); setItem(result.data); setPendingAssignment(false); assignmentDraft.current = undefined; intent.current.clear(); onChanged(); }
    catch (e) { const failure = catalogError(e); setError(`${failure.message}。${failure.ambiguous ? "归档意图已锁定，请重试确认同一操作结果" : "请刷新后重新核对"}`); if (!failure.ambiguous) { setPendingAssignment(false); assignmentDraft.current = undefined; setTarget(undefined); } }
    finally { setBusy(false); }
  };
  const recognize = async () => {
    const path = `/inbox/${item.inbox_id}/recognition`; const body = { expected_revision: item.revision }; setBusy(true); setError("");
    try { setLatestJob((await client.write<Job>("post", path, body, intent.current.key(path, body))).data); intent.current.clear(); }
    catch (e) { setError(catalogError(e).message); } finally { setBusy(false); }
  };
  return <Space direction="vertical" style={{ width: "100%" }}>
    {error && <Alert type="warning" message={error} />}
    <Space wrap><Tag>{STATUS_LABELS[item.status]}</Tag><Button onClick={refresh} disabled={busy}>刷新图片状态</Button>
      {mediaEnabled && ["ready", "assigned"].includes(item.status) && <Button onClick={preview} disabled={busy}>查看／刷新图片链接</Button>}
    </Space>
    {links && <Image width={300} src={links.derivatives[0]?.url || links.url} preview={{ src: links.url }} onError={() => { setLinks(undefined); setError("图片链接已失效，请刷新链接"); }} />}
    {item.recognition?.result && <Alert type="info" message="识别提示，请人工核对" description={<Space direction="vertical">{item.recognition.result.candidates.map(hint => <Button key={hint.pn} disabled={pendingAssignment || busy} onClick={() => { setQuery(hint.pn); setTarget(undefined); setTargetId(undefined); }}>{hint.pn} · {(hint.confidence * 100).toFixed(0)}%</Button>)}<span>{item.recognition.result.item_category}</span></Space>} />}
    {item.status === "ready" && <>
      {allowed("enrichment.recognize") && can("part_gallery", "job.read") && <Button onClick={recognize} disabled={busy || pendingAssignment || !recognitionEnabled}>手动识别图片{!recognitionEnabled && "（服务未配置）"}</Button>}
      {allowed("inbox.claim") && !ownedClaim && <Space wrap><Button disabled={busy || Boolean(item.claimed_by && !item.claimed_by_me && (!allowed("inbox.takeover") || !reason.trim()))} onClick={() => command("post", `/inbox-items/${item.inbox_id}/claim`, { expected_revision: item.revision, ...(item.claimed_by && !item.claimed_by_me ? { takeover_reason: reason.trim() } : {}) })}>认领图片</Button>
        {item.claimed_by && !item.claimed_by_me && allowed("inbox.takeover") && <Input aria-label="接手理由" placeholder="接手前填写理由" value={reason} onChange={e => setReason(e.target.value)} />}
      </Space>}
      {ownedClaim && !pendingAssignment && allowed("inbox.release") && <Button disabled={busy} onClick={() => command("post", `/inbox-items/${item.inbox_id}/release`, { expected_claim_revision: item.claim_revision })}>释放我的认领</Button>}
      {(ownedClaim || pendingAssignment) && allowed("inbox.assign") && can("part_gallery", "gallery.archive") && <Space direction="vertical" style={{ width: "100%" }}>
        <Input.Search disabled={busy || pendingAssignment} aria-label="搜索归档 PN" placeholder="先查找，再选择明确的型号" value={query} onChange={e => { setQuery(e.target.value); setTarget(undefined); setTargetId(undefined); setTargets([]); }} onSearch={search} enterButton="查找型号" loading={busy} />
        <Select disabled={busy || pendingAssignment} aria-label="归档 PN" style={{ minWidth: 300 }} value={targetId} options={targets.map(t => ({ value: t.part_id, label: t.pn_std }))} onChange={v => { setTargetId(v); setTarget(undefined); }} />
        <Select aria-label="归档图片用途" value={purpose} disabled={busy || pendingAssignment} onChange={v => { setPurpose(v); assignmentKey.current = crypto.randomUUID(); assignmentDraft.current = undefined; intent.current.clear(); }} options={[{ value: "common", label: "通用外观" }, { value: "defect", label: "缺陷图片" }]} />
        {!target ? <Button disabled={busy || !targetId} onClick={prepare}>核对目标 PN 权限和版本</Button> : <Popconfirm title={`确认将图片归档到 ${target.pn}？`} onConfirm={assign}><Button type="primary" disabled={busy}>归档到 {target.pn}</Button></Popconfirm>}
      </Space>}
    </>}
    {item.status === "assigned" && <Alert type="success" message={`已归档到 PN #${item.part_id}；图片删除和调整请在该 PN 图库操作。`} />}
    {!["assigned", "deleted"].includes(item.status) && ownOrManager && !pendingAssignment && allowed("inbox.delete") && <Popconfirm title="删除收件箱图片？" onConfirm={() => command("delete", `/inbox-items/${item.inbox_id}?expected_revision=${item.revision}`, undefined)}><Button danger disabled={busy}>删除收件箱图片</Button></Popconfirm>}
    {item.status === "deleted" && ownOrManager && allowed("inbox.restore") && <Space><Input aria-label="收件箱恢复理由" placeholder="恢复理由" value={reason} onChange={e => setReason(e.target.value)} /><Button disabled={busy || !reason.trim()} onClick={() => command("post", `/inbox-items/${item.inbox_id}/restoration`, { expected_deleted_revision: item.revision, reason: reason.trim() })}>恢复图片</Button></Space>}
    {item.group_id && ownOrManager && allowed("inbox.group.arrange") && <InboxGroupEditor groupId={item.group_id} own={item.uploaded_by_me} onChanged={onChanged} />}
    <JobsPanel inboxId={item.inbox_id} latestJob={latestJob} />
  </Space>;
}
