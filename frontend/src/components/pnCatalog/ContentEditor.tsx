import { useEffect, useRef, useState } from "react";
import { Alert, Button, Form, Input, Space, Spin } from "antd";
import { catalogError, CommandIntent, type Content, type Grade } from "../../api/pnCatalog";
import { useCatalogAccess } from "./access";
import { normalized } from "./drafts";

export default function ContentEditor({ partId, grade }: { partId: number; grade: Grade }) {
  const { client, can, field } = useCatalogAccess();
  const [base, setBase] = useState<Content>();
  const [draft, setDraft] = useState<Record<string, string | null>>({});
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);
  const intent = useRef(new CommandIntent());
  const path = `/parts/${partId}/content/${grade}`;
  const initial = (data: Content) => Object.fromEntries(["title", "description"].filter(k => k in data).map(k => [k, data[k as "title" | "description"] ?? null]));
  useEffect(() => { let alive = true; client.get<Content>(path).then(({ data }) => { if (alive) { setBase(data); setDraft(initial(data)); } }).catch(e => { if (alive) setError(catalogError(e).message); }); return () => { alive = false; }; }, [client, path]);
  const changes = () => Object.fromEntries(Object.entries(draft).filter(([k, v]) => normalized(v) !== normalized(base?.[k as "title" | "description"])).map(([k, v]) => [k, normalized(v)]));
  const save = async () => {
    if (!base || !Object.keys(changes()).length) return;
    const body = { expected_identity_revision: base.identity_revision, expected_revision: base.revision, changes: changes() };
    setBusy(true);
    try { const { data } = await client.write<Content>("patch", path, body, intent.current.key(path, body)); setBase(data); setDraft(initial(data)); setError("已保存"); setConflict(false); intent.current.clear(); }
    catch (e) { const failure = catalogError(e); setError(`${failure.message}。草稿已保留`); setConflict(["request.precondition_failed", "catalog.field_conflict"].includes(failure.code)); }
    finally { setBusy(false); }
  };
  const rebase = async () => {
    if (!base) return; setBusy(true);
    try { const { data } = await client.get<Content>(path); if (data.identity_revision !== base.identity_revision) throw new Error("PN 身份已变化，请重新打开；草稿仍保留"); const dirty = changes(); if (Object.keys(dirty).some(k => !(k in data))) throw new Error("字段权限已变化"); setDraft({ ...initial(data), ...dirty }); setBase(data); setConflict(false); intent.current.clear(); setError("已更新基线，请检查后保存。"); }
    catch (e) { setError(catalogError(e).message); } finally { setBusy(false); }
  };
  return <Space direction="vertical" style={{ width: "100%" }}>
    <Alert type="info" message={`${grade === "good" ? "好件" : "坏件"}通用商品文案：同一 PN 共用，单件故障和检测结果由业务记录维护。`} />
    {error && <Alert type={conflict ? "warning" : "info"} message={error} action={conflict && <Button loading={busy} onClick={rebase}>更新基线并保留草稿</Button>} />}
    {!base ? !error && <Spin /> : <Form layout="vertical" disabled={busy || !can("part_content", "edit")}>
      {["title", "description"].filter(k => k in draft && field("part_content", `content.${grade}.${k}`)).map(k => <Form.Item key={k} label={k === "title" ? "商品标题" : "商品描述"}>
        <Input.TextArea disabled={busy || !can("part_content", "edit") || !field("part_content", `content.${grade}.${k}`, true)} aria-label={k === "title" ? "商品标题" : "商品描述"} rows={k === "title" ? 2 : 8} maxLength={k === "title" ? 200 : 20000} value={draft[k] || ""} onChange={e => setDraft(d => ({ ...d, [k]: e.target.value }))} />
      </Form.Item>)}
      {can("part_content", "edit") && <Button type="primary" loading={busy} disabled={conflict} onClick={save}>保存{grade === "good" ? "好件" : "坏件"}文案</Button>}
    </Form>}
  </Space>;
}
