import { useEffect, useRef, useState } from "react";
import { Alert, Button, Input, Modal, Space, Table, Tag } from "antd";
import { catalogError, CommandIntent } from "../../api/pnCatalog";
import { useCatalogAccess } from "./access";

interface Attribute { value: unknown; deleted: boolean; version: number; protected: boolean; provenance: unknown }
interface Attributes { part_id: number; identity_revision: number; revision: number; items: Record<string, Attribute> }
const attributeAt = (base: Attributes, key: string): Attribute | undefined => Object.prototype.hasOwnProperty.call(base.items, key) ? base.items[key] : undefined;
interface Draft { key: string; json: string; operation: "set" | "delete"; base: Attributes; existing: boolean }

export default function AttributesEditor({ partId }: { partId: number }) {
  const { client, can, field } = useCatalogAccess();
  const [data, setData] = useState<Attributes>();
  const [draft, setDraft] = useState<Draft>();
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);
  const intent = useRef(new CommandIntent());
  const generation = useRef(0);
  const path = `/parts/${partId}/attributes`;
  const editable = can("part_master", "edit") && field("part_master", "attributes", true);
  useEffect(() => {
    const seq = ++generation.current;
    setData(undefined); setDraft(undefined); setError("");
    client.get<Attributes>(path).then(result => { if (seq === generation.current) setData(result.data); })
      .catch(e => { if (seq === generation.current) setError(catalogError(e).message); });
    return () => { ++generation.current; };
  }, [client, path]);
  const open = (key = "", operation: "set" | "delete" = "set") => {
    if (!data) return;
    setDraft({ key, json: key && !attributeAt(data, key)?.deleted ? JSON.stringify(attributeAt(data, key)?.value, null, 2) : "null", operation, base: data, existing: Boolean(key) });
    setConflict(false); setError(""); intent.current.clear();
  };
  const save = async () => {
    if (!draft || !editable) return;
    const key = draft.existing ? draft.key : draft.key.trim();
    if (!key) { setError("请输入参数名"); return; }
    if (!draft.existing && attributeAt(draft.base, key) && !attributeAt(draft.base, key)?.deleted) { setError("参数已存在，请从列表打开编辑"); return; }
    let change: { operation: "set"; value: unknown } | { operation: "delete" };
    try { change = draft.operation === "delete" ? { operation: "delete" } : { operation: "set", value: JSON.parse(draft.json) }; }
    catch { setError("请输入有效 JSON；文字需要双引号，null 表示空值"); return; }
    const body = { expected_identity_revision: draft.base.identity_revision,
      field_versions: { [key]: attributeAt(draft.base, key)?.version ?? 0 }, changes: { [key]: change } };
    const seq = generation.current; setBusy(true); setError("");
    try {
      const result = await client.write<Attributes>("patch", path, body, intent.current.key(path, body));
      if (seq !== generation.current) return;
      setData(result.data); setDraft(undefined); intent.current.clear();
    } catch (e) {
      if (seq !== generation.current) return;
      const failure = catalogError(e); setError(`${failure.message}。草稿已保留`);
      setConflict(failure.code === "request.precondition_failed" || failure.code.endsWith("conflict"));
    } finally { if (seq === generation.current) setBusy(false); }
  };
  const rebase = async () => {
    if (!draft) return;
    const seq = generation.current; setBusy(true);
    try {
      const { data: latest } = await client.get<Attributes>(path);
      if (seq !== generation.current) return;
      if (latest.identity_revision !== draft.base.identity_revision) throw new Error("PN 身份已变化，请重新打开；草稿仍保留");
      setData(latest); setDraft({ ...draft, base: latest }); setConflict(false); intent.current.clear(); setError("已更新当前值，请比较后保存草稿。");
    } catch (e) { if (seq === generation.current) setError(catalogError(e).message); }
    finally { if (seq === generation.current) setBusy(false); }
  };
  const rows = Object.entries(data?.items || {}).map(([key, value]) => ({ key, ...value }));
  return <Space direction="vertical" style={{ width: "100%" }}>
    <Alert type="info" message="自定义参数保留 JSON 类型。设置 null 是保存空值，删除会保留变更记录。" />
    {error && !draft && <Alert type="error" message={error} />}
    {editable && <Button onClick={() => open()} disabled={!data}>添加参数</Button>}
    <Table rowKey="key" dataSource={rows} loading={!data && !error} pagination={false} columns={[
      { title: "参数名", dataIndex: "key" },
      { title: "当前值", render: (_, item) => item.deleted ? <Tag>已删除</Tag> : <code style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify(item.value)}</code> },
      { title: "版本", dataIndex: "version" },
      { title: "操作", render: (_, item) => editable && <Space><Button onClick={() => open(item.key)}>{item.deleted ? "恢复并编辑" : "编辑"}</Button>{!item.deleted && <Button danger onClick={() => open(item.key, "delete")}>删除</Button>}</Space> },
    ]} />
    <Modal title={draft?.operation === "delete" ? "删除自定义参数" : "编辑自定义参数"} open={Boolean(draft)} destroyOnClose
      onCancel={() => { if (!busy) { setDraft(undefined); setError(""); } }} onOk={save} confirmLoading={busy} okText="保存" okButtonProps={{ disabled: conflict || !editable }} cancelButtonProps={{ disabled: busy }} closable={!busy} maskClosable={!busy}>
      {draft && <Space direction="vertical" style={{ width: "100%" }}>
        {error && <Alert type={conflict ? "warning" : "info"} message={error} action={conflict && <Button loading={busy} onClick={rebase}>更新基线并保留草稿</Button>} />}
        <Input aria-label="参数名" value={draft.key} disabled={draft.existing || busy} maxLength={256} onChange={e => setDraft({ ...draft, key: e.target.value })} />
        <div>当前值：<code>{attributeAt(draft.base, draft.key)?.deleted ? "已删除" : JSON.stringify(attributeAt(draft.base, draft.key)?.value) ?? "不存在"}</code></div>
        {draft.operation === "set" ? <Input.TextArea aria-label="JSON 参数值" rows={8} value={draft.json} disabled={busy} onChange={e => setDraft({ ...draft, json: e.target.value })} /> : <Alert type="warning" message="确认删除这个参数？历史值仍可通过审计追溯。" />}
      </Space>}
    </Modal>
  </Space>;
}
