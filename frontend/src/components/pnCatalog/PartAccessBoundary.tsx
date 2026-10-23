import { useEffect, useState, type ReactNode } from "react";
import { Alert, Button, Spin } from "antd";
import { catalogError } from "../../api/pnCatalog";
import { PartAccessContext, useCatalogAccess, type PartAccess } from "./access";

export default function PartAccessBoundary({ partId, children }: { partId: number; children: ReactNode }) {
  const { client, snapshot } = useCatalogAccess();
  const [state, setState] = useState<{ value?: PartAccess; error?: string; client?: typeof client; version?: string; partId?: number }>({});
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let alive = true; setState({});
    client.get<PartAccess>(`/parts/${partId}/access`).then(({ data }) => {
      if (data.part_id !== partId || ![data.allowed_actions, data.readable_fields, data.writable_fields].every(v => Array.isArray(v) && v.every(s => typeof s === "string"))) throw new Error("型号权限响应无效");
      if (alive) setState({ value: data, client, version: snapshot?.version, partId });
    }).catch(e => { if (alive) setState({ error: catalogError(e).message }); });
    return () => { alive = false; };
  }, [client, partId, snapshot?.version, retry]);
  if (state.error) return <Alert type="error" message={state.error} action={<Button onClick={() => setRetry(n => n + 1)}>重试型号权限</Button>} />;
  if (!state.value || state.client !== client || state.version !== snapshot?.version || state.partId !== partId) return <Spin tip="确认当前型号权限" />;
  return <PartAccessContext.Provider value={state.value}>{children}</PartAccessContext.Provider>;
}
