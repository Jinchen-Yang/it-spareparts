import { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Input, Modal, Space, Table, Tag } from "antd";
import { catalogError, CommandIntent, type Candidate, type Content, type Part } from "../../api/pnCatalog";
import { useCatalogAccess } from "./access";
import { PROFILE_LABELS, SPEC_LABELS } from "./drafts";

export default function CandidatesPanel({ partId }: { partId: number }) {
  const { client, can, field } = useCatalogAccess();
  const [rows, setRows] = useState<Candidate[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [active, setActive] = useState<Candidate>();
  const [base, setBase] = useState<Part | Content>();
  const [selected, setSelected] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const intent = useRef(new CommandIntent());
  const seq = useRef(0);
  const editorSeq = useRef(0);
  const load = async (more = false) => { const id = ++seq.current; try { const result = await client.get<Candidate[]>(`/parts/${partId}/candidates`, { limit: 50, ...(more && cursor ? { cursor } : {}) }); if (id !== seq.current) return; setRows(old => more ? [...old, ...result.data] : result.data); setCursor(result.meta.next_cursor || null); } catch (e) { if (id === seq.current) setError(catalogError(e).message); } };
  useEffect(() => { void load(); return () => { ++seq.current; }; }, [client, partId]);
  const open = async (candidate: Candidate) => {
    const request = ++editorSeq.current;
    setActive(candidate); setSelected([]); setReason(""); setBase(undefined); setBusy(true); setError("");
    try { const result = candidate.resource === "profile" ? await client.get<Part>(`/parts/${partId}`) : await client.get<Content>(`/parts/${partId}/content/${candidate.grade}`); if (request === editorSeq.current) setBase(result.data); }
    catch (e) { if (request === editorSeq.current) setError(catalogError(e).message); } finally { if (request === editorSeq.current) setBusy(false); }
  };
  const act = async (adopt: boolean) => {
    if (!active || !base) return;
    const generation = editorSeq.current;
    const body = adopt ? { expected_candidate_revision: active.revision, expected_identity_revision: base.identity_revision, expected_target_revision: "profile_revision" in base ? base.profile_revision : base.revision, fields: selected, ...(active.resource === "profile" ? { field_versions: Object.fromEntries(selected.flatMap(k => k.startsWith("category_") ? ["category_major", "category_minor"] : [k]).map(k => [k, base.field_versions[k] || 0])) } : {}), reason: reason || null } : { expected_candidate_revision: active.revision, reason };
    const path = `/candidates/${active.id}/${adopt ? "adoption" : "rejection"}`;
    setBusy(true);
    try { await client.write("post", path, body, intent.current.key(path, body)); if (generation !== editorSeq.current) return; intent.current.clear(); setActive(undefined); await load(); }
    catch (e) { if (generation === editorSeq.current) setError(`${catalogError(e).message}。选择和理由已保留，请刷新核对当前值。`); } finally { if (generation === editorSeq.current) setBusy(false); }
  };
  const proposals = active ? { ...active.proposal.changes, ...Object.fromEntries(Object.entries(active.proposal.spec_updates || {}).map(([key, v]) => [`specs.${key}`, v])) } : {};
  const writable = (key: string) => active?.resource === "profile" ? can("part_master", "edit") && field("part_master", key, true) : can("part_content", "edit") && field("part_content", `content.${active?.grade}.${key}`, true);
  return <Space direction="vertical" style={{ width: "100%" }}>
    <Alert type="info" message="AI 与导入结果先作为候选。选定字段并确认采用后，才更新共享资料。" />
    {error && !active && <Alert type="warning" message={error} />}
    <Button onClick={() => load()}>刷新候选</Button>
    <Table rowKey="id" dataSource={rows} pagination={false} columns={[{ title: "内容", render: (_, r) => r.resource === "profile" ? "主资料" : `${r.grade === "good" ? "好件" : "坏件"}文案` }, { title: "来源", render: (_, r) => String(r.provenance.kind || "") }, { title: "状态", dataIndex: "status", render: value => <Tag>{value}</Tag> }, { title: "操作", render: (_, r) => <Button onClick={() => open(r)}>查看与处理</Button> }]} />
    {cursor && <Button onClick={() => load(true)}>加载更多候选</Button>}
    <Modal open={!!active} title="核对候选字段" onCancel={() => { ++editorSeq.current; setActive(undefined); setBusy(false); }} footer={null} width={760} destroyOnClose>
      {error && <Alert type="warning" message={error} />}
      <Space direction="vertical" style={{ width: "100%" }}>
        <pre style={{ whiteSpace: "pre-wrap" }}>来源：{JSON.stringify(active?.provenance, null, 2)}</pre>
        <Table rowKey="key" pagination={false} dataSource={Object.entries(proposals).map(([key, value]) => ({ key, value }))} columns={[
          { title: "采用", render: (_, row) => <Checkbox aria-label={`采用 ${row.key}`} checked={selected.includes(row.key)} disabled={busy || !writable(row.key) || !can("part_master", "adopt_candidate") || active?.adopted_fields.includes(row.key)} onChange={e => setSelected(s => e.target.checked ? [...s, row.key] : s.filter(k => k !== row.key))} /> },
          { title: "字段", render: (_, row) => PROFILE_LABELS[row.key] || SPEC_LABELS[row.key.replace("specs.", "")] || row.key },
          { title: "当前值", render: (_, row) => JSON.stringify(base && ("profile" in base ? row.key.startsWith("specs.") ? base.specs[row.key.slice(6)]?.value : base.profile[row.key] : base[row.key as "title" | "description"])) },
          { title: "候选值", render: (_, row) => JSON.stringify(row.value) },
        ]} />
        <Input.TextArea aria-label="候选处理理由" placeholder="处理理由（拒绝时必填）" value={reason} maxLength={2000} onChange={e => setReason(e.target.value)} />
        <Space wrap>{can("part_master", "adopt_candidate") && <Button type="primary" loading={busy} disabled={!selected.length || !base || !["pending", "partially_adopted"].includes(active?.status || "")} onClick={() => act(true)}>确认采用所选字段</Button>}
          {can("part_master", "reject_candidate") && <Button danger disabled={busy || !reason.trim() || !["pending", "partially_adopted"].includes(active?.status || "")} onClick={() => act(false)}>拒绝候选</Button>}
          <Button disabled={busy} onClick={async () => { if (!active) return; try { const current = (await client.get<Candidate>(`/candidates/${active.id}`)).data; const target = current.resource === "profile" ? await client.get<Part>(`/parts/${partId}`) : await client.get<Content>(`/parts/${partId}/content/${current.grade}`); setActive(current); setBase(target.data); setError("已刷新，请重新核对后确认。"); intent.current.clear(); } catch (e) { setError(catalogError(e).message); } }}>刷新当前值</Button>
        </Space>
      </Space>
    </Modal>
  </Space>;
}
