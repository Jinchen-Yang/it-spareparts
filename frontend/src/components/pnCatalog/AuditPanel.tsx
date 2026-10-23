import { useEffect, useRef, useState } from "react";
import { Alert, Button, Space, Table } from "antd";
import { catalogError, type AuditEvent } from "../../api/pnCatalog";
import { useCatalogAccess } from "./access";

export default function AuditPanel({ partId }: { partId: number }) {
  const { client } = useCatalogAccess();
  const [rows, setRows] = useState<AuditEvent[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState("");
  const seq = useRef(0);
  const load = async (more = false) => { const id = ++seq.current; try { const result = await client.get<AuditEvent[]>(`/parts/${partId}/audit-events`, { limit: 50, ...(more && cursor ? { cursor } : {}) }); if (id !== seq.current) return; setRows(old => more ? [...old, ...result.data] : result.data); setCursor(result.meta.next_cursor || null); } catch (e) { if (id === seq.current) setError(catalogError(e).message); } };
  useEffect(() => { void load(); return () => { ++seq.current; }; }, [client, partId]);
  return <Space direction="vertical" style={{ width: "100%" }}>{error && <Alert type="error" message={error} />}<Button onClick={() => load()}>刷新审计</Button>
    <Table rowKey="audit_id" dataSource={rows} pagination={false} scroll={{ x: 650 }} columns={[{ title: "时间", dataIndex: "occurred_at" }, { title: "操作", dataIndex: "action" }, { title: "操作人", render: (_, row) => String(row.actor.display_name || row.actor.principal_id || "未知") }, { title: "执行方", render: (_, row) => String(row.actor.executor_id || "") }, { title: "来源", render: (_, row) => String((row.actor.provenance as { kind?: string })?.kind || "") }]} expandable={{ expandedRowRender: row => <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify({ changes: row.changes, actor: row.actor, context: row.context, request_id: row.request_id }, null, 2)}</pre> }} />
    {cursor && <Button onClick={() => load(true)}>加载更多审计</Button>}
  </Space>;
}
