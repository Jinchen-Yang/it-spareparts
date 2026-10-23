import { useEffect, useRef, useState } from "react";
import { Alert, Button, Form, Input, Select, Space, Spin, Tag } from "antd";
import { catalogError, CommandIntent, type Part } from "../../api/pnCatalog";
import { useCatalogAccess } from "./access";
import { PROFILE_LABELS, SPEC_LABELS, SPEC_UNITS, profileDraft, profilePatch, rebaseProfile, type ProfileDraft } from "./drafts";

export default function ProfileEditor({ partId, onChanged }: { partId: number; onChanged?: () => void }) {
  const { client, can, field } = useCatalogAccess();
  const [base, setBase] = useState<Part>();
  const [draft, setDraft] = useState<ProfileDraft>();
  const [categories, setCategories] = useState<{ name: string; children: string[] }[]>([]);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);
  const intent = useRef(new CommandIntent());
  const lifecycle = useRef(0);
  useEffect(() => {
    let alive = true; ++lifecycle.current;
    setBase(undefined); setDraft(undefined); setError("");
    client.get<Part>(`/parts/${partId}`).then(({ data }) => { if (alive) { setBase(data); setDraft(profileDraft(data)); } }).catch(e => { if (alive) setError(catalogError(e).message); });
    client.categories().then(data => { if (alive) setCategories(data); }).catch(() => {});
    return () => { alive = false; ++lifecycle.current; };
  }, [client, partId]);
  const disabled = (key: string) => busy || !can("part_master", "edit") || !field("part_master", key, true) || (key.startsWith("category_") && (!field("part_master", "category_major", true) || !field("part_master", "category_minor", true)));
  const update = (key: string, value: string | null) => setDraft(d => d && ({ ...d, fields: { ...d.fields, [key]: value, ...(key === "category_major" ? { category_minor: null } : {}) } }));
  const save = async () => {
    if (!base || !draft) return;
    const body = profilePatch(base, draft);
    if (!Object.keys(body.changes).length && !Object.keys(body.spec_updates).length) return;
    const generation = lifecycle.current;
    setBusy(true); setError("");
    try {
      const path = `/parts/${partId}/profile`;
      const result = await client.write<Part>("patch", path, body, intent.current.key(path, body));
      if (generation !== lifecycle.current) return;
      setBase(result.data); setDraft(profileDraft(result.data)); setConflict(false); intent.current.clear(); onChanged?.();
    } catch (e) { if (generation !== lifecycle.current) return; const failure = catalogError(e); setError(`${failure.message}。草稿已保留${failure.ambiguous ? "，再次保存会查询同一操作结果" : ""}`); setConflict(["request.precondition_failed", "catalog.field_conflict"].includes(failure.code)); }
    finally { if (generation === lifecycle.current) setBusy(false); }
  };
  const rebase = async () => {
    if (!base || !draft) return;
    const generation = lifecycle.current; setBusy(true);
    try { const { data } = await client.get<Part>(`/parts/${partId}`); if (generation !== lifecycle.current) return; setDraft(rebaseProfile(base, draft, data)); setBase(data); setConflict(false); setError("已更新基线并保留改动，请检查后保存。"); intent.current.clear(); }
    catch (e) { if (generation === lifecycle.current) setError(catalogError(e).message); } finally { if (generation === lifecycle.current) setBusy(false); }
  };
  return <Space direction="vertical" style={{ width: "100%" }}>
    {error && <Alert type={conflict ? "warning" : "info"} showIcon message={error} action={conflict && <Button onClick={rebase} loading={busy}>更新基线并保留草稿</Button>} />}
    {!base || !draft ? !error && <Spin /> : <>
      <Space><Tag>{base.pn_std}</Tag><Tag>{base.availability === "active" ? "使用中" : "已停用"}</Tag><span>资料版本 {base.profile_revision}</span></Space>
      <Form layout="vertical" disabled={busy || !can("part_master", "edit")}>
        {Object.entries(PROFILE_LABELS).filter(([key]) => key in base.profile && field("part_master", key)).map(([key, label]) => <Form.Item key={key} label={label}>
          {key === "category_major" ? <Select aria-label={label} disabled={disabled(key)} allowClear value={draft.fields[key]} options={categories.map(c => ({ label: c.name, value: c.name }))} onChange={v => update(key, v ?? null)} /> :
            key === "category_minor" ? <Select aria-label={label} disabled={disabled(key)} allowClear value={draft.fields[key]} options={(categories.find(c => c.name === draft.fields.category_major)?.children || []).map(v => ({ label: v, value: v }))} onChange={v => update(key, v ?? null)} /> :
              key === "machine_or_part" ? <Select aria-label={label} disabled={disabled(key)} allowClear value={draft.fields[key]} options={["整机", "备件"].map(v => ({ label: v, value: v }))} onChange={v => update(key, v ?? null)} /> :
                key === "description" ? <Input.TextArea aria-label={label} disabled={disabled(key)} value={draft.fields[key] || ""} maxLength={20000} rows={3} onChange={e => update(key, e.target.value)} /> :
                  <Input aria-label={label} disabled={disabled(key)} value={draft.fields[key] || ""} maxLength={key === "manufacturer" ? 256 : key === "unit" ? 16 : 128} onChange={e => update(key, e.target.value)} />}
        </Form.Item>)}
        {Object.entries(SPEC_LABELS).filter(([key]) => field("part_master", `specs.${key}`)).map(([key, label]) => <Form.Item label={label} key={key}>
          <Input aria-label={label} disabled={disabled(`specs.${key}`)} value={draft.specs[key]?.value || ""} maxLength={128} placeholder="留空并保存可删除该规格" onChange={e => setDraft(d => d && ({ ...d, specs: { ...d.specs, [key]: e.target.value.trim() ? { value: e.target.value, unit: SPEC_UNITS[key] ?? null, numeric_value: null } : null } }))} />
        </Form.Item>)}
        {can("part_master", "edit") && <Button type="primary" onClick={save} loading={busy} disabled={conflict || base.identity.status !== "active"}>保存资料</Button>}
      </Form>
    </>}
  </Space>;
}
