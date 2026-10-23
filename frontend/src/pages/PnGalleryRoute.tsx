import { useEffect, useRef, useState } from "react";
import { Alert, Button, Input, Modal, Space, Table } from "antd";
import { AuthzUnavailable, useAuthz } from "../authz/AuthzProvider";
import { catalogError } from "../api/pnCatalog";
import { useCatalogAccess } from "../components/pnCatalog/access";
import CatalogPartPanel from "../components/pnCatalog/CatalogPartPanel";
import PageHeader from "../components/PageHeader";

export default function PnGalleryRoute() {
  const { status } = useAuthz(); const { can, field } = useCatalogAccess();
  if (status !== "ready") return <AuthzUnavailable />;
  if (!can("part_gallery", "gallery.read") || !field("part_gallery", "gallery")) return <Alert type="warning" message="PN 图库尚未启用或当前无读取权限。" />;
  return <GalleryFinder />;
}

function GalleryFinder() {
  const { client } = useCatalogAccess(); const [q, setQ] = useState("");
  const [rows, setRows] = useState<{ part_id: number; pn_std: string }[]>([]); const [partId, setPartId] = useState<number>();
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false); const sequence = useRef(0);
  const search = async () => { const seq = ++sequence.current; setBusy(true); setError(""); try { const items = await client.identities(q.trim()); if (seq === sequence.current) setRows(items); } catch (e) { if (seq === sequence.current) setError(catalogError(e).message); } finally { if (seq === sequence.current) setBusy(false); } };
  useEffect(() => () => { ++sequence.current; }, []);
  return <><PageHeader title="PN 图库" subtitle="通过型号身份打开共享图库，资料字段按当前权限显示" />
    <Space direction="vertical" style={{ width: "100%" }}><Input.Search aria-label="查找 PN 图库" value={q} onChange={e => setQ(e.target.value)} onSearch={search} enterButton="查找型号" loading={busy} />
      {error && <Alert type="warning" message={error} />}<span>每次显示前 20 个匹配型号；可补充关键词缩小范围。</span>
      <Table rowKey="part_id" dataSource={rows} pagination={false} loading={busy} columns={[{ title: "PN", dataIndex: "pn_std" }, { title: "操作", render: (_, row) => <Button onClick={() => setPartId(row.part_id)}>打开共享图库</Button> }]} />
    </Space>
    <Modal title="共享 PN 维护" open={Boolean(partId)} width={1000} footer={null} destroyOnClose onCancel={() => setPartId(undefined)}>{partId && <CatalogPartPanel key={partId} partId={partId} />}</Modal>
  </>;
}
