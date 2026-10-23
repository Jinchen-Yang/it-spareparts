import { useRef, useState } from "react";
import { Alert, Button, List, Popconfirm, Space } from "antd";
import { catalogError, CommandIntent, type Envelope } from "../../api/pnCatalog";
import { useCatalogAccess } from "./access";
import type { Inbox } from "./workflows";

interface Group { group_id: string; revision: number; ordered_item_ids: string[] }
export default function InboxGroupEditor({ groupId, own, onChanged }: { groupId: string; own: boolean; onChanged: () => void }) {
  const { client } = useCatalogAccess(); const [base, setBase] = useState<Group>(); const [order, setOrder] = useState<string[]>([]);
  const [items, setItems] = useState<Record<string, Inbox>>({}); const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [conflict, setConflict] = useState(false);
  const intent = useRef(new CommandIntent());
  const load = async () => {
    setBusy(true); setError("");
    try {
      const group = (await client.get<Group>(`/inbox-groups/${groupId}`)).data;
      const found: Record<string, Inbox> = {}; let cursor: string | null = null; const seen = new Set<string>();
      do {
        const page: Envelope<Inbox[]> = await client.get<Inbox[]>("/inbox-items", { group_id: groupId, scope: own ? "mine" : "shared", status: "all", limit: 200, ...(cursor ? { cursor } : {}) });
        for (const item of page.data) found[item.inbox_id] = item;
        cursor = page.meta.next_cursor || null;
        if (Object.keys(found).length > 2000 || cursor && seen.has(cursor)) throw new Error("分组分页无效，请重新读取");
        if (cursor) seen.add(cursor);
      } while (cursor);
      const current = (await client.get<Group>(`/inbox-groups/${groupId}`)).data;
      if (current.revision !== group.revision) throw new Error("读取期间分组发生变化，请重新读取");
      if (group.ordered_item_ids.some(id => !found[id])) throw new Error("分组包含当前不可见的图片，暂不能排序");
      setBase(group); setOrder(group.ordered_item_ids); setItems(found); setConflict(false); intent.current.clear();
    } catch (e) { setError(catalogError(e).message); } finally { setBusy(false); }
  };
  const save = async () => {
    if (!base) return; const path = `/inbox-groups/${groupId}`; const body = { expected_revision: base.revision, ordered_item_ids: order }; setBusy(true); setError("");
    try { const { data } = await client.write<Group>("patch", path, body, intent.current.key(path, body)); setBase(data); setOrder(data.ordered_item_ids); intent.current.clear(); onChanged(); }
    catch (e) { const failure = catalogError(e); setError(`${failure.message}。本次排序草稿已保留。`); setConflict(failure.code.endsWith("conflict")); } finally { setBusy(false); }
  };
  const move = (index: number, delta: number) => setOrder(old => { const next = [...old]; [next[index], next[index + delta]] = [next[index + delta], next[index]]; return next; });
  return <Space direction="vertical" style={{ width: "100%" }}>
    {error && <Alert type="warning" message={error} />}
    {!base ? <Button onClick={load} loading={busy}>读取同组图片顺序</Button> : <>
      <List dataSource={order} renderItem={(id, index) => <List.Item><Space><span>{index + 1}. {items[id]?.filename}</span>{items[id]?.client_order != null && <small>原拍摄序号 {items[id].client_order! + 1}</small>}<Button disabled={busy || index === 0} onClick={() => move(index, -1)}>上移</Button><Button disabled={busy || index === order.length - 1} onClick={() => move(index, 1)}>下移</Button></Space></List.Item>} />
      <Space><Button type="primary" disabled={conflict || busy} onClick={save}>保存组顺序</Button><Popconfirm title="重新读取会放弃未保存的排序，继续？" onConfirm={load}><Button disabled={busy}>重新读取分组</Button></Popconfirm></Space>
    </>}
  </Space>;
}
