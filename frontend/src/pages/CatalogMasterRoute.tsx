import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Alert, Button, Form, Input, Modal, Select, Space, Table, Tag } from "antd";
import { AuthzUnavailable, resourceUsesLegacy, useAuthz } from "../authz/AuthzProvider";
import { catalogError, CommandIntent, type Part, type PartSummary } from "../api/pnCatalog";
import { useCatalogAccess } from "../components/pnCatalog/access";
import { PROFILE_LABELS } from "../components/pnCatalog/drafts";
import CatalogPartPanel from "../components/pnCatalog/CatalogPartPanel";
import PageHeader from "../components/PageHeader";
import MasterDataPage from "./MasterDataPage";

export default function CatalogMasterRoute() {
  const { status, snapshot } = useAuthz();
  const { can } = useCatalogAccess();
  if (status !== "ready") return <AuthzUnavailable />;
  if (resourceUsesLegacy(snapshot, "part_master")) return <MasterDataPage />;
  if (!can("part_master", "read")) return <Alert type="warning" message="当前无主数据访问权限，或模块已暂停。" />;
  return <SharedMasterDataPage />;
}

export function SharedMasterDataPage() {
  const { client, can, field } = useCatalogAccess();
  const [params, setParams] = useSearchParams();
  const query = params.get("q") || "";
  const partId = Number(params.get("part_id")) || null;
  const [input, setInput] = useState(query);
  const [rows, setRows] = useState<PartSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [create, setCreate] = useState(false);
  const [pn, setPn] = useState("");
  const [draft, setDraft] = useState<Record<string, string | null>>({});
  const [createError, setCreateError] = useState("");
  const [confirmation, setConfirmation] = useState<{ token: string; candidates: { part_id: number; pn_std: string }[] }>();
  const [reason, setReason] = useState("");
  const [categories, setCategories] = useState<{ name: string; children: string[] }[]>([]);
  const seq = useRef(0);
  const intent = useRef(new CommandIntent());
  const patchParams = (values: Record<string, string | null>) => setParams(old => { const next = new URLSearchParams(old); Object.entries(values).forEach(([k, v]) => v ? next.set(k, v) : next.delete(k)); return next; });
  const load = async (more = false) => { const id = ++seq.current; setBusy(true); setError(""); try { const result = await client.get<PartSummary[]>("/parts", { q: query, limit: 20, ...(more && cursor ? { cursor } : {}) }); if (id !== seq.current) return; setRows(old => more ? [...old, ...result.data] : result.data); setCursor(result.meta.next_cursor || null); } catch (e) { if (id === seq.current) setError(catalogError(e).message); } finally { if (id === seq.current) setBusy(false); } };
  useEffect(() => { setInput(query); void load(); return () => { ++seq.current; }; }, [client, query]);
  const beginCreate = () => { setCreate(true); setPn(""); setDraft({}); setCreateError(""); setConfirmation(undefined); setReason(""); intent.current.clear(); client.categories().then(setCategories).catch(() => {}); };
  const submitCreate = async (confirmed = false) => {
    const body = { pn_std: pn.trim(), changes: Object.fromEntries(Object.entries(draft).map(([k, v]) => [k, v?.trim() || null])), ...(confirmed && confirmation ? { confirmation_token: confirmation.token, confirmation_reason: reason.trim() } : {}) };
    setBusy(true); setCreateError("");
    try { const result = await client.write<Part>("post", "/parts", body, intent.current.key("/parts", body)); intent.current.clear(); setCreate(false); patchParams({ part_id: String(result.data.part_id) }); await load(); }
    catch (e) { const failure = catalogError(e); setCreateError(failure.message); if (failure.code === "catalog.possible_duplicate") setConfirmation({ token: String(failure.details.confirmation_token || ""), candidates: failure.details.candidates as { part_id: number; pn_std: string }[] || [] }); }
    finally { setBusy(false); }
  };
  const update = (key: string, value: string | null) => { setDraft(d => ({ ...d, [key]: value, ...(key === "category_major" ? { category_minor: null } : {}) })); setConfirmation(undefined); };
  return <>
    <PageHeader title="备件主数据" subtitle="itsp 与 shop 共用 PN 资料、好坏件文案和图库" extra={can("part_master", "create") && <Button type="primary" onClick={beginCreate}>新建 PN</Button>} />
    <Space direction="vertical" style={{ width: "100%" }}>
      <Input.Search aria-label="搜索 PN 主数据" value={input} onChange={e => setInput(e.target.value)} onSearch={q => patchParams({ q: q.trim() || null })} placeholder="PN、描述、品牌、厂商" enterButton="搜索" />
      {error && <Alert type="error" message={error} action={<Button onClick={() => load()}>重试</Button>} />}
      <Table<PartSummary> loading={busy} rowKey="part_id" dataSource={rows} pagination={false} scroll={{ x: 700 }} columns={[
        { title: "PN", dataIndex: "pn_std", render: (value, row) => <Button type="link" onClick={() => patchParams({ part_id: String(row.part_id) })}>{value}</Button> },
        ...(field("part_master", "description") ? [{ title: "描述", dataIndex: "description_preview" }] : []),
        ...(field("part_master", "brand") ? [{ title: "品牌", dataIndex: "brand" }] : []),
        ...(field("part_master", "category_major") ? [{ title: "品类", dataIndex: "category_major" }] : []),
        { title: "状态", dataIndex: "availability", render: value => <Tag>{value === "active" ? "使用中" : "已停用"}</Tag> },
      ]} />
      {cursor && <Button loading={busy} onClick={() => load(true)}>加载更多型号</Button>}
    </Space>
    <Modal open={!!partId} title="共享 PN 维护" width={1000} onCancel={() => patchParams({ part_id: null })} footer={null} destroyOnClose>
      {partId && <CatalogPartPanel key={partId} partId={partId} onChanged={() => load()} />}
    </Modal>
    <Modal open={create} title="新建 PN" onCancel={() => setCreate(false)} footer={null} destroyOnClose>
      {createError && <Alert type="warning" message={createError} />}
      <Form layout="vertical" disabled={busy}>
        <Form.Item label="PN" required><Input aria-label="新 PN" value={pn} maxLength={128} onChange={e => { setPn(e.target.value); setConfirmation(undefined); }} /></Form.Item>
        {Object.entries(PROFILE_LABELS).filter(([key]) => field("part_master", key)).map(([key, label]) => <Form.Item key={key} label={label}>
          {key === "category_major" || key === "category_minor" || key === "machine_or_part" ? <Select aria-label={`新 PN ${label}`} allowClear value={draft[key]} options={(key === "category_major" ? categories.map(c => c.name) : key === "category_minor" ? categories.find(c => c.name === draft.category_major)?.children || [] : ["整机", "备件"]).map(value => ({ value, label: value }))} onChange={v => update(key, v ?? null)} /> : <Input.TextArea aria-label={`新 PN ${label}`} value={draft[key] || ""} maxLength={key === "description" ? 20000 : key === "manufacturer" ? 256 : key === "unit" ? 16 : 128} autoSize onChange={e => update(key, e.target.value)} />}
        </Form.Item>)}
        {!confirmation ? <Button type="primary" loading={busy} disabled={!pn.trim()} onClick={() => submitCreate()}>创建</Button> : <Space direction="vertical">
          <Alert type="warning" message="请先核对近似型号" description={confirmation.candidates.map(c => <div key={c.part_id}><Button type="link" onClick={() => { setCreate(false); patchParams({ part_id: String(c.part_id) }); }}>{c.pn_std}</Button></div>)} />
          {can("part_master", "confirm_distinct") && confirmation.token && <><Input.TextArea aria-label="独立 PN 确认理由" placeholder="说明为何这是不同的 PN" value={reason} maxLength={2000} onChange={e => setReason(e.target.value)} /><Button loading={busy} disabled={!reason.trim()} onClick={() => submitCreate(true)}>确认是不同 PN 并创建</Button></>}
        </Space>}
      </Form>
    </Modal>
  </>;
}
