import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Checkbox, Input, Select, Space, Table, Tag } from "antd";
import { catalogError, CommandIntent } from "../../api/pnCatalog";
import { useCatalogAccess } from "./access";
import JobsPanel from "./JobsPanel";
import type { Job } from "./workflows";

type Operation = "merge" | "unmerge" | "alias_reassign";
interface IdentityPart { part_id: number; pn_std: string; status: string; merged_into_id: number | null }
interface Preview { operation: Operation; target_part_id: number; expected: Record<string, { identity_revision: number; profile_revision: number }>; parts: IdentityPart[] }
interface MergeLog { merge_log_id: number; source_part_id: number; target_part_id: number; source_pn: string; target_pn: string; relationship_active: boolean; created_at: string }
interface Alias { alias_id: number; pn_raw: string; pn_std: string; part_id: number | null; status: string | null; created_at: string }
interface Result { parts: IdentityPart[]; outcome: Record<string, unknown>; recalculation_job: Job }
interface Plan { preview: Preview; body: Record<string, unknown>; key: string }
const labels: Record<Operation, string> = { merge: "合并 PN", unmerge: "回滚合并", alias_reassign: "调整别名归属" };

function PnPicker({ label, disabled, onChange }: { label: string; disabled: boolean; onChange: (id?: number) => void }) {
  const { client } = useCatalogAccess(); const [q, setQ] = useState(""); const [rows, setRows] = useState<{ part_id: number; pn_std: string }[]>([]);
  const [id, setId] = useState<number>(); const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const seq = useRef(0);
  useEffect(() => () => { ++seq.current; }, [client]);
  const search = async () => { const current = ++seq.current; setBusy(true); setError(""); setId(undefined); onChange(); try { const result = await client.identities(q); if (current === seq.current) setRows(result); } catch (e) { if (current === seq.current) setError(catalogError(e).message); } finally { if (current === seq.current) setBusy(false); } };
  return <Space direction="vertical"><Input.Search aria-label={`搜索${label}`} placeholder={label} value={q} disabled={disabled} loading={busy} enterButton={`查找${label}`} onChange={e => { setQ(e.target.value); setId(undefined); onChange(); }} onSearch={search} />
    <Select aria-label={label} style={{ minWidth: 280 }} disabled={disabled || busy} value={id} options={rows.map(row => ({ label: row.pn_std, value: row.part_id }))} onChange={value => { setId(value); onChange(value); }} />{error && <Alert type="warning" message={error} />}</Space>;
}

export default function IdentityPanel({ initialOperation = "merge" }: { initialOperation?: Operation } = {}) {
  const { client, can, field } = useCatalogAccess();
  const [operation, setOperation] = useState<Operation>(initialOperation); const [source, setSource] = useState<number>(); const [target, setTarget] = useState<number>();
  const [log, setLog] = useState<MergeLog>(); const [alias, setAlias] = useState<Alias>(); const [query, setQuery] = useState("");
  const [logs, setLogs] = useState<MergeLog[]>([]); const [aliases, setAliases] = useState<Alias[]>([]); const [cursor, setCursor] = useState<string | null>(null);
  const [plan, setPlan] = useState<Plan>(); const [accepted, setAccepted] = useState(false); const [uncertain, setUncertain] = useState(false); const [result, setResult] = useState<Result>();
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false); const seq = useRef(0); const intent = useRef(new CommandIntent());
  const readable = can("part_identity", "read");
  const editable = readable && can("part_identity", "rebind_facts") && can("part_master", "read") && can("part_identity", operation === "alias_reassign" ? "review_alias" : operation) && field("part_identity", operation === "alias_reassign" ? "aliases" : "identity", true);
  const listAllowed = readable && field("part_identity", operation === "alias_reassign" ? "aliases" : "identity");
  const reset = () => { setPlan(undefined); setAccepted(false); setResult(undefined); setError(""); intent.current.clear(); };
  const load = async (more = false) => {
    if (!listAllowed) return; const current = ++seq.current; setBusy(true); setError("");
    try { const response = await client.get<MergeLog[] | Alias[]>(operation === "alias_reassign" ? "/identity/aliases" : "/identity/merge-logs", { limit: 20, ...(operation === "alias_reassign" ? { q: query } : {}), ...(more && cursor ? { cursor } : {}) }); if (current !== seq.current) return;
      if (operation === "alias_reassign") setAliases(old => more ? [...old, ...response.data as Alias[]] : response.data as Alias[]); else setLogs(old => more ? [...old, ...response.data as MergeLog[]] : response.data as MergeLog[]); setCursor(response.meta.next_cursor || null);
    } catch (e) { if (current === seq.current) { setError(catalogError(e).message); setLogs([]); setAliases([]); } } finally { if (current === seq.current) setBusy(false); }
  };
  useEffect(() => { setCursor(null); setLogs([]); setAliases([]); void load(); return () => { ++seq.current; }; }, [client, operation, listAllowed]);
  const prepare = async () => {
    if (!editable) return; const body = operation === "merge" ? { operation, source_part_id: source, target_part_id: target } : operation === "unmerge" ? { operation, merge_log_id: log?.merge_log_id } : { operation, alias_id: alias?.alias_id, target_part_id: target };
    setBusy(true); reset();
    try { const response = await client.write<Preview>("post", "/identity/preview", body, crypto.randomUUID()); if (response.data.operation !== operation) throw new Error("身份预览响应不一致"); const command = { ...body, expected: response.data.expected }; setPlan({ preview: response.data, body: command, key: intent.current.key("/identity/commands", command) }); }
    catch (e) { setError(catalogError(e).message); } finally { setBusy(false); }
  };
  const submit = async () => {
    if (!plan || !accepted) return; setBusy(true); setError("");
    try { setResult((await client.write<Result>("post", "/identity/commands", plan.body, plan.key)).data); setUncertain(false); setPlan(undefined); intent.current.clear(); void load(); }
    catch (e) { const failure = catalogError(e); setError(`${failure.message}。${failure.ambiguous ? "原操作与版本已冻结，请重试同一次确认" : "请重新读取预览并核对"}`); setUncertain(failure.ambiguous); if (!failure.ambiguous) { setPlan(undefined); setAccepted(false); } } finally { setBusy(false); }
  };
  const invalid = operation === "merge" ? !source || !target || source === target : operation === "unmerge" ? !log : !alias || !target;
  if (!readable) return <Alert type="warning" message="共享身份资料尚未启用或当前没有读取权限。" />;
  return <Space direction="vertical" style={{ width: "100%" }}>
    <Alert type="info" message="身份变更会协调共享资料、图库和 ITSP 业务归属，派生计算由任务跟踪；不会调整 Shop 库存数量。" />
    <Select aria-label="身份操作" value={operation} disabled={busy || uncertain} options={Object.entries(labels).map(([value, label]) => ({ value, label }))} onChange={value => { setOperation(value); setSource(undefined); setTarget(undefined); setLog(undefined); setAlias(undefined); reset(); }} />
    {error && <Alert type="warning" message={error} />}
    {!editable && <Alert type="warning" message="当前可查看；提交还需本项身份操作、事实归属治理及主资料读取权限。" />}
    <Space wrap>{operation === "merge" && <PnPicker key={`${operation}-source`} label="来源 PN" disabled={!editable || busy || uncertain} onChange={id => { setSource(id); reset(); }} />}
      {operation !== "unmerge" && <PnPicker key={`${operation}-target`} label="目标 PN" disabled={!editable || busy || uncertain} onChange={id => { setTarget(id); reset(); }} />}</Space>
    {operation === "alias_reassign" && <Input.Search aria-label="搜索别名" value={query} disabled={busy || uncertain} enterButton="查找别名" onChange={e => setQuery(e.target.value)} onSearch={() => { setAlias(undefined); reset(); void load(); }} />}
    {operation === "unmerge" && log && <Tag>已选合并：{log.source_pn} → {log.target_pn}（#{log.merge_log_id}）</Tag>}
    {operation === "alias_reassign" && alias && <Tag>已选别名：{alias.pn_raw} → {alias.pn_std}</Tag>}
    <Button disabled={!editable || invalid || busy || uncertain} onClick={prepare}>读取身份变更预览</Button>
    {plan && <Card title="确认涉及的 PN 与原版本"><Table rowKey="part_id" pagination={false} dataSource={plan.preview.parts} columns={[{ title: "PN", dataIndex: "pn_std" }, { title: "状态", dataIndex: "status" }, { title: "身份版本", render: (_, row) => plan.preview.expected[String(row.part_id)].identity_revision }, { title: "资料版本", render: (_, row) => plan.preview.expected[String(row.part_id)].profile_revision }]} />
      <Checkbox disabled={busy || uncertain} checked={accepted} onChange={e => setAccepted(e.target.checked)}>我已核对这次{labels[operation]}的目标和全部涉及型号</Checkbox>
      <Button danger type="primary" disabled={!accepted || busy} onClick={submit}>{uncertain ? "重试同一次确认" : "确认身份变更"}</Button>
    </Card>}
    {result && <Card title="身份变更已保存"><Tag>重算任务：{result.recalculation_job.status}</Tag><JobsPanel partId={result.recalculation_job.part_id || undefined} latestJob={result.recalculation_job} /></Card>}
    {listAllowed && <Card title={operation === "alias_reassign" ? "别名记录" : "合并记录"} extra={<Button disabled={busy || uncertain} onClick={() => load()}>刷新身份记录</Button>}>
      {operation === "alias_reassign" ? <Table rowKey="alias_id" pagination={false} dataSource={aliases} columns={[{ title: "原始写法", dataIndex: "pn_raw" }, { title: "当前 PN", dataIndex: "pn_std" }, { title: "状态", dataIndex: "status" }, { title: "操作", render: (_, row) => <Button disabled={!editable || busy || uncertain} onClick={() => { setAlias(row); reset(); }}>选择此别名</Button> }]} /> : <Table rowKey="merge_log_id" pagination={false} dataSource={logs} columns={[{ title: "来源", dataIndex: "source_pn" }, { title: "目标", dataIndex: "target_pn" }, { title: "当前关系", render: (_, row) => row.relationship_active ? "直接合并关系有效" : "关系已变化" }, ...(operation === "unmerge" ? [{ title: "操作", render: (_: unknown, row: MergeLog) => <Button disabled={!editable || busy || uncertain || !row.relationship_active} onClick={() => { setLog(row); reset(); }}>选择此合并记录</Button> }] : [])]} />}
      {cursor && <Button disabled={busy || uncertain} onClick={() => load(true)}>加载更多身份记录</Button>}
    </Card>}
  </Space>;
}
