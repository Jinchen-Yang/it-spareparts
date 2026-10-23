import { useRef, useState } from "react";
import { Alert, Button, Checkbox, Modal, Select, Space, Table } from "antd";
import { catalogError, CommandIntent, type Capabilities, type Content, type Entry, type Grade, type Part } from "../../api/pnCatalog";
import { useCatalogAccess } from "./access";
import JobsPanel from "./JobsPanel";
import { completeGallery, type Job } from "./workflows";

interface ResearchPlan { expected_identity_revision: number; expected_profile_revision: number; expected_content_revisions: Record<string, number>; expected_gallery_revision?: number; capabilities: string[]; max_searches: number }
interface BundlePlan { identity: number; revision: number; entries: Entry[] }
export default function EnrichmentPanel({ partId, capabilities }: { partId: number; capabilities: Capabilities }) {
  const { client, can, field } = useCatalogAccess();
  const [text, setText] = useState(false); const [images, setImages] = useState(false);
  const [research, setResearch] = useState<ResearchPlan>(); const [bundle, setBundle] = useState<BundlePlan>();
  const [grade, setGrade] = useState<Grade>("good"); const [watermark, setWatermark] = useState(false); const [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false); const [latestJob, setLatestJob] = useState<Job>();
  const intent = useRef(new CommandIntent());
  const researchBase = can("part_master", "read") && can("part_master", "enrichment.research") && can("part_master", "job.read") && field("part_master", "candidates", true);
  const canText = researchBase && can("part_master", "enrichment.research.text") && can("part_master", "create_candidate") && can("part_master", "read_candidates") && can("part_content", "read") && (["good", "bad"] as const).every(g => ["title", "description"].some(k => field("part_content", `content.${g}.${k}`)));
  const canImages = researchBase && can("part_gallery", "enrichment.research.images") && can("part_gallery", "gallery.research") && can("part_gallery", "gallery.read") && field("part_gallery", "gallery", true);
  const canBundle = can("part_gallery", "enrichment.bundle") && can("part_gallery", "gallery.read") && can("part_gallery", "job.read") && field("part_gallery", "gallery");
  const prepareResearch = async () => {
    if (!(text && canText || images && canImages)) return;
    setBusy(true); setError("");
    try {
      const part = (await client.get<Part>(`/parts/${partId}`)).data;
      const plan: ResearchPlan = { expected_identity_revision: part.identity_revision, expected_profile_revision: part.profile_revision, expected_content_revisions: {}, capabilities: [], max_searches: 2 };
      if (text && canText) {
        const responses = await Promise.all([client.get<Content>(`/parts/${partId}/content/good`), client.get<Content>(`/parts/${partId}/content/bad`)]);
        if (responses.some(r => r.data.identity_revision !== part.identity_revision)) throw new Error("读取期间 PN 身份已变化，请重新核对");
        plan.expected_content_revisions = { good: responses[0].data.revision, bad: responses[1].data.revision }; plan.capabilities.push("text");
      }
      if (images && canImages) { const result = await client.get<{ revision: number }>(`/parts/${partId}/gallery`, { limit: 1 }); if (result.meta.identity_revision !== part.identity_revision) throw new Error("读取期间 PN 身份已变化，请重新核对"); plan.expected_gallery_revision = result.data.revision; plan.capabilities.push("images"); }
      setResearch(plan); intent.current.clear();
    } catch (e) { setError(catalogError(e).message); } finally { setBusy(false); }
  };
  const prepareBundle = async () => {
    setBusy(true); setError("");
    try { const result = await completeGallery(client, partId); setBundle({ identity: result.identityRevision, revision: result.gallery.revision, entries: result.gallery.entries }); setSelected(result.gallery.entries.filter(e => grade === "bad" || e.purpose === "common").map(e => e.entry_id)); intent.current.clear(); }
    catch (e) { setError(catalogError(e).message); } finally { setBusy(false); }
  };
  const submit = async (kind: "research" | "bundles") => {
    let body: unknown;
    if (kind === "research") { if (!research) return; body = research; }
    else { if (!bundle || !selected.length) return; body = { expected_identity_revision: bundle.identity, expected_gallery_revision: bundle.revision, grade, entry_ids: selected, watermark }; }
    const path = `/parts/${partId}/${kind}`; setBusy(true); setError("");
    try { const { data } = await client.write<Job>("post", path, body, intent.current.key(path, body)); setLatestJob(data); setResearch(undefined); setBundle(undefined); intent.current.clear(); }
    catch (e) { const failure = catalogError(e); setError(`${failure.message}。${failure.ambiguous ? "原操作编号已保留，可重试确认结果" : "请关闭后重新核对范围"}`); } finally { setBusy(false); }
  };
  const entries = bundle?.entries.filter(e => grade === "bad" || e.purpose === "common") || [];
  const bytes = entries.filter(e => selected.includes(e.entry_id)).reduce((sum, e) => sum + (e.size_bytes || 0), 0);
  return <Space direction="vertical" style={{ width: "100%" }}>
    {error && <Alert type="warning" message={error} />}
    {(canText || canImages) && <Space direction="vertical"><strong>手动研究</strong><span>研究结果先进入候选，由你核对采用。每次最多 2 次搜索。</span>
      {!capabilities.research && <Alert type="info" message="研究服务尚未配置" />}
      <Space>{canText && <Checkbox checked={text} disabled={busy} onChange={e => setText(e.target.checked)}>资料与好坏件文案</Checkbox>}{canImages && <Checkbox checked={images} disabled={busy} onChange={e => setImages(e.target.checked)}>候选图片</Checkbox>}</Space>
      <Button onClick={prepareResearch} disabled={busy || !capabilities.research || !(text || images)}>核对研究范围</Button>
    </Space>}
    {canBundle && <Space direction="vertical"><strong>导出图片 ZIP</strong>{!capabilities.bundle && <Alert type="info" message="打包服务尚未配置" />}<Button onClick={prepareBundle} disabled={busy || !capabilities.bundle}>选择打包图片</Button></Space>}
    <JobsPanel partId={partId} latestJob={latestJob} />
    <Modal title="确认开始研究" open={Boolean(research)} onCancel={() => setResearch(undefined)} onOk={() => submit("research")} okText="开始研究" confirmLoading={busy} cancelButtonProps={{ disabled: busy }} closable={!busy} maskClosable={!busy}>
      <p>本次范围：{research?.capabilities.map(c => c === "text" ? "主资料及好坏件文案" : "候选图片").join("、")}。正式内容需人工采用候选后才会更新。</p>{error && <Alert type="warning" message={error} />}
    </Modal>
    <Modal title="确认图片打包范围" open={Boolean(bundle)} width={740} onCancel={() => setBundle(undefined)} onOk={() => submit("bundles")} okText="开始打包" confirmLoading={busy} okButtonProps={{ disabled: !selected.length || bytes > 1024 ** 3 }} cancelButtonProps={{ disabled: busy }} closable={!busy} maskClosable={!busy}>
      {error && <Alert type="warning" message={error} />}
      <Space><Select aria-label="打包好坏件" value={grade} disabled={busy} options={[{ value: "good", label: "好件：通用图片" }, { value: "bad", label: "坏件：通用和缺陷图片" }]} onChange={v => { setGrade(v); setSelected((bundle?.entries || []).filter(e => v === "bad" || e.purpose === "common").map(e => e.entry_id)); }} /><Checkbox checked={watermark} disabled={busy} onChange={e => setWatermark(e.target.checked)}>添加水印</Checkbox></Space>
      <p>已选 {selected.length} 张，{(bytes / 1024 / 1024).toFixed(1)} MiB（单次上限 1000 张 / 1 GiB）</p>
      <Table<Entry> rowKey="entry_id" size="small" dataSource={entries} pagination={{ pageSize: 20 }} rowSelection={{ selectedRowKeys: selected, onChange: keys => setSelected(keys.map(String)), getCheckboxProps: () => ({ disabled: busy }) }} columns={[{ title: "文件", render: (_, e) => String(e.source.filename || e.entry_id) }, { title: "用途", render: (_, e) => e.purpose === "common" ? "通用" : "缺陷" }, { title: "大小", render: (_, e) => `${((e.size_bytes || 0) / 1024).toFixed(1)} KiB` }]} />
    </Modal>
  </Space>;
}
