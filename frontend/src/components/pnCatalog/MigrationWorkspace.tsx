import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Input, Modal, Select, Space, Table, Tag, Typography } from "antd";
import { catalogError, CommandIntent, type Content, type Gallery, type Part } from "../../api/pnCatalog";
import { useCatalogAccess } from "./access";

interface Snapshot { namespace: string; snapshot_id: string; source_schema: string; revision: number; verification_scope: string; created_at: string }
interface History { part_id?: number | null; inbox_id?: string | null }
interface SourceRecord { source_record_id: number; namespace: string; kind: string; source_id: string; source_sha256: string; state: string; revision: number; reason_code: string | null; target_ref: { history?: History; inbox_id?: string; [key: string]: unknown } | null }
type RecoveryKind = "retire_fenced" | "confirm_assignment" | "research" | "recognize" | "bundle";
const STATES: Record<string, string> = { pending: "等待转换", applied: "已接管", historical_terminal: "历史已留存", recovery_required: "等待恢复核对", dependency_blocked: "缺少明确绑定", conflict: "与现状冲突" };

export default function MigrationWorkspace() {
  const { client, can, field, token } = useCatalogAccess();
  const readable = can("part_master", "read_legacy") && field("part_master", "legacy_evidence");
  const writable = readable && can("part_master", "materialize_migration");
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]); const [snapshotCursor, setSnapshotCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<Snapshot>(); const [rows, setRows] = useState<SourceRecord[]>([]); const [cursor, setCursor] = useState<string | null>(null);
  const [progress, setProgress] = useState<Record<string, unknown>>(); const [state, setState] = useState<string>();
  const [sourceEvidence, setSourceEvidence] = useState<unknown>(); const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [review, setReview] = useState<SourceRecord>(); const [decision, setDecision] = useState<RecoveryKind>("retire_fenced");
  const [evidence, setEvidence] = useState(""); const [reason, setReason] = useState("");
  const [outcome, setOutcome] = useState<"no_external_effect" | "retained_effect_reviewed">("retained_effect_reviewed");
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false); const [uncertain, setUncertain] = useState(false);
  const evidenceSequence = useRef(0); const sequence = useRef(0); const intent = useRef(new CommandIntent()); const pending = useRef<{ path: string; body: unknown; key: string }>();
  const base = (value: Snapshot) => `/migration/snapshots/${encodeURIComponent(value.namespace)}/${value.snapshot_id}`;
  async function loadSnapshots(more = false) {
    const seq = ++sequence.current; setBusy(true); setError("");
    try { const result = await client.get<Snapshot[]>("/migration/snapshots", more && snapshotCursor ? { before: snapshotCursor } : undefined); if (seq !== sequence.current) return; setSnapshots(old => more ? [...old, ...result.data] : result.data); setSnapshotCursor(result.meta.next_cursor || null); }
    catch (e) { if (seq === sequence.current) { setSnapshots([]); setError(catalogError(e).message); } }
    finally { if (seq === sequence.current) setBusy(false); }
  }
  async function load(value: Snapshot, more = false, filter = state) {
    const seq = ++sequence.current; setBusy(true); setError("");
    try {
      const [records, report] = await Promise.all([client.get<SourceRecord[]>(`${base(value)}/records`, { ...(filter ? { state: filter } : {}), ...(more && cursor ? { after: Number(cursor) } : {}) }), client.get<Record<string, unknown>>(base(value))]);
      if (seq !== sequence.current) return; setRows(old => more ? [...old, ...records.data] : records.data); setCursor(records.meta.next_cursor || null); setProgress(report.data);
    } catch (e) { if (seq === sequence.current) { setRows([]); setProgress(undefined); setError(catalogError(e).message); } }
    finally { if (seq === sequence.current) setBusy(false); }
  }
  useEffect(() => { setSelected(undefined); setRows([]); setReview(undefined); setSourceEvidence(undefined); setEvidenceOpen(false); pending.current = undefined; if (readable) void loadSnapshots(); return () => { ++sequence.current; ++evidenceSequence.current; }; }, [client, readable, token]);
  async function inspect(row: SourceRecord) {
    const seq = ++evidenceSequence.current; setSourceEvidence(undefined); setEvidenceOpen(true);
    try { const result = await client.get<unknown>(`/migration/records/${row.source_record_id}/evidence`); if (seq === evidenceSequence.current) setSourceEvidence(result.data); }
    catch (e) { if (seq === evidenceSequence.current) { setEvidenceOpen(false); setError(catalogError(e).message); } }
  }
  async function convert(row: SourceRecord) {
    if (!selected) return; const path = `${base(selected)}/apply`; const body = { expected_snapshot_revision: selected.revision, record_ids: [row.source_record_id] };
    setBusy(true); setError(""); try { await client.write("post", path, body, intent.current.key(path, body)); intent.current.clear(); await load(selected); } catch (e) { setError(catalogError(e).message); } finally { setBusy(false); }
  }
  async function reviewBody(row: SourceRecord) {
    const common = { expected_revision: row.revision, source_sha256: row.source_sha256, evidence_ref: evidence.trim(), reason: reason.trim(), external_outcome: outcome };
    if (decision === "retire_fenced") return { ...common, decision };
    if (decision === "confirm_assignment") {
      const { data: item } = await client.get<{ part_id: number; revision: number; entry_id: string }>(`/inbox-items/${row.target_ref?.inbox_id}`);
      const [part, entry] = await Promise.all([client.get<Part>(`/parts/${item.part_id}`), client.get<{ revision: number }>(`/gallery/entries/${item.entry_id}`)]);
      return { ...common, decision, expected_identity_revision: part.data.identity_revision, expected_inbox_revision: item.revision, expected_entry_revision: entry.data.revision, assignment_key: crypto.randomUUID() };
    }
    const history = row.target_ref?.history;
    if (decision === "recognize") {
      if (!history?.inbox_id) throw new Error("原任务没有可确认的收件箱关联");
      const { data } = await client.get<{ revision: number }>(`/inbox-items/${history.inbox_id}`);
      return { ...common, decision: "restart_current", replacement: { kind: "recognize", command: { expected_revision: data.revision } } };
    }
    if (!history?.part_id) throw new Error("原任务尚未绑定共享 PN");
    const { data: part } = await client.get<Part>(`/parts/${history.part_id}`);
    if (decision === "bundle") {
      const { data: gallery } = await client.get<Gallery>(`/parts/${history.part_id}/gallery`);
      return { ...common, decision: "restart_current", replacement: { kind: "bundle", command: { expected_identity_revision: part.identity_revision, expected_gallery_revision: gallery.revision, grade: "good", watermark: false } } };
    }
    const [good, bad] = await Promise.all([client.get<Content>(`/parts/${history.part_id}/content/good`), client.get<Content>(`/parts/${history.part_id}/content/bad`)]);
    return { ...common, decision: "restart_current", replacement: { kind: "research", command: { expected_identity_revision: part.identity_revision, expected_profile_revision: part.profile_revision, expected_content_revisions: { good: good.data.revision, bad: bad.data.revision }, capabilities: ["text"], max_searches: 2 } } };
  }
  async function recover() {
    if (!review || !selected || !evidence.trim() || !reason.trim()) return;
    const row = review; setBusy(true); setError("");
    try {
      if (!pending.current) { const path = `/migration/records/${row.source_record_id}/recovery`; const body = await reviewBody(row); pending.current = { path, body, key: intent.current.key(path, body) }; }
      const { path, body, key } = pending.current;
      await client.write("post", path, body, key); pending.current = undefined; intent.current.clear(); setUncertain(false); setReview(undefined); await load(selected);
    } catch (e) { const failure = catalogError(e); setError(failure.message); setUncertain(failure.ambiguous); if (!failure.ambiguous) { pending.current = undefined; intent.current.clear(); } }
    finally { setBusy(false); }
  }
  if (!readable) return <Alert type="warning" message="迁移核对需要明确的历史证据读取权限。" />;
  return <Space direction="vertical" style={{ width: "100%" }}>
    <Typography.Title level={2}>共享 PN 迁移核对</Typography.Title>
    <Alert type="info" message="当前快照为隔离副本验证；此页面不冻结生产、不启动旧 worker、不自动重发旧外呼。" />
    {error && <Alert type="error" message={error} />}
    <Space><Select aria-label="选择冻结快照" style={{ minWidth: 360 }} placeholder="选择冻结快照" value={selected?.snapshot_id} options={snapshots.map(s => ({ value: s.snapshot_id, label: `${s.namespace} · ${s.snapshot_id}` }))} onChange={id => { const value = snapshots.find(s => s.snapshot_id === id); if (value) { setSelected(value); setRows([]); void load(value); } }} /><Button disabled={busy} onClick={() => loadSnapshots()}>刷新快照</Button>{snapshotCursor && <Button disabled={busy} onClick={() => loadSnapshots(true)}>更多快照</Button>}</Space>
    {selected && <><Card size="small" title="对账状态"><Space wrap><span>原始记录 {String(progress?.record_count ?? "—")}</span><span>未转换 {String(progress?.pending ?? "—")}</span><Tag color={progress?.live_complete ? "green" : "orange"}>{progress?.live_complete ? "隔离副本接管已核对" : "仍有待核对状态"}</Tag><Tag>未验收生产切换</Tag></Space></Card>
      <Space><Select allowClear aria-label="筛选迁移状态" placeholder="全部状态" value={state} options={Object.entries(STATES).map(([value, label]) => ({ value, label }))} onChange={value => { setState(value); void load(selected, false, value); }} /><Button disabled={busy} onClick={() => load(selected)}>刷新记录</Button></Space>
      <Table rowKey="source_record_id" pagination={false} loading={busy} dataSource={rows} columns={[{ title: "来源", render: (_, row) => `${row.kind} · ${row.source_id}` }, { title: "状态", render: (_, row) => <><Tag>{STATES[row.state] || row.state}</Tag>{row.reason_code && <small>{row.reason_code}</small>}</> }, { title: "操作", render: (_, row) => <Space><Button onClick={() => inspect(row)}>查看原始证据</Button>{writable && ["pending", "dependency_blocked", "conflict"].includes(row.state) && <Button onClick={() => convert(row)}>继续转换</Button>}{writable && row.state === "recovery_required" && <Button onClick={() => { setReview(row); setDecision(row.reason_code === "migration.assignment_receipt_recovery" ? "confirm_assignment" : "retire_fenced"); setEvidence(""); setReason(""); setUncertain(false); pending.current = undefined; }}>核对恢复决定</Button>}</Space> }]} />
      {cursor && <Button disabled={busy} onClick={() => load(selected, true)}>更多记录</Button>}
    </>}
    <Modal title="原始迁移证据（受限）" open={evidenceOpen} onCancel={() => { ++evidenceSequence.current; setEvidenceOpen(false); setSourceEvidence(undefined); }} footer={null} width={800}><Typography.Paragraph>包含历史来源与隔离证据，仅用于核对。来源快照不可修改。</Typography.Paragraph><pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: "60vh", overflow: "auto" }}>{sourceEvidence ? JSON.stringify(sourceEvidence, null, 2) : "读取中…"}</pre></Modal>
    <Modal title="核对旧状态并记录新的恢复决定" open={Boolean(review)} onCancel={() => { if (!busy && !uncertain) setReview(undefined); }} footer={null} closable={!busy && !uncertain} maskClosable={!busy && !uncertain}>
      <Space direction="vertical" style={{ width: "100%" }}>
        <Typography.Paragraph>仅在核对原任务、保留成果和旧租约隔离证据后提交。原事件与原操作者保持不变，新任务使用你的当前权限。</Typography.Paragraph>
        {uncertain && <Alert type="warning" message="结果尚未确认。保留原决定与请求号，再次核对将恢复原请求。" />}
        <Select aria-label="恢复方式" value={decision} disabled={busy || uncertain} onChange={setDecision} options={review?.reason_code === "migration.assignment_receipt_recovery" ? [{ value: "confirm_assignment", label: "确认现有图片归属，建立新的归档确认" }] : [
          { value: "retire_fenced", label: "结束旧执行并保留全部历史" },
          ...(["agent_job", "agent_run", "manual_research", "image_recovery"].includes(review?.kind || "") ? [{ value: "research", label: "按当前资料重新发起文字研究" }] : []),
          ...(review?.kind === "inbox_job" ? [{ value: "recognize", label: "按当前图片重新发起识别" }] : []),
          ...(review?.kind === "bundle" ? [{ value: "bundle", label: "按当前图库重新打包好件图片" }] : []),
        ]} />
        <Select aria-label="外部结果核对" value={outcome} disabled={busy || uncertain} onChange={setOutcome} options={[{ value: "retained_effect_reviewed", label: "已有外部成果已核对并保留" }, { value: "no_external_effect", label: "已核对没有遗留外部结果" }]} />
        <Input aria-label="核对证据引用" placeholder="核对证据引用" value={evidence} maxLength={256} disabled={busy || uncertain} onChange={e => setEvidence(e.target.value)} />
        <Input.TextArea aria-label="恢复决定理由" placeholder="说明核对结果和恢复理由" value={reason} maxLength={2000} disabled={busy || uncertain} onChange={e => setReason(e.target.value)} />
        <Button type="primary" disabled={!evidence.trim() || !reason.trim() || busy} onClick={recover}>{uncertain ? "核对原请求结果" : "确认并记录恢复决定"}</Button>
      </Space>
    </Modal>
  </Space>;
}
