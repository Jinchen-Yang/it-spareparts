import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Alert, App, Button, Card, Checkbox, Descriptions, Input, List, Modal, Select, Space, Table, Tag, Upload } from "antd";
import { InboxOutlined } from "@ant-design/icons";
import PageHeader from "../../components/PageHeader";
import ImportPrecheckPanel from "./ImportPrecheckPanel";
import ExpenseVoidPreviewPanel from "./ExpenseVoidPreviewPanel";
import WbddUploadFeedback from "../../components/maintenance/WbddUploadFeedback";
import { useContextAdmission } from "../../authz/access";
import { useContextQuery } from "../../authz/contextQuery";
import { domainApi, useContextTransport } from "../../authz/domainTransport";
import type { AuthorizationContext } from "../../authz/contextTypes";
import { actionRule, allRule, anyRule, surfaceRule, surfaces as surfaceDefinitions, type Rule } from "../../authz/rules";
import { AuthorizationCancelled } from "../../authz/requestLifecycle";
import { IMPORT_SOURCES, batchList, batchDetail, jobList, jobResponse, type ImportSource, type JobProjection } from "../../api/importProtocol";
import { precheckImportFiles, previewExpenseVoid, uploadImportBatch, downloadImportErrors, type ImportMode, type ImportPrecheckResult, type ExpenseVoidPreview } from "../../api/imports";
const SOURCE_LABEL: Record<ImportSource, string> = { purchase: "采购", sales: "销售", inventory: "库存", maintenance: "维保出库", expense: "报销" };
const surfaces = (surface: string) => anyRule(...IMPORT_SOURCES.map(source => surfaceRule(`import_${source}.${surface}`)));
/** Select display requirements from the server-selected actual source branch, never from file names. */
export function resolvedImportRule(entryId: string, context: AuthorizationContext): Rule {
  const needs: Rule[] = [];
  for (const [resource, decision] of Object.entries(context.resources)) {
    for (const [action, effect] of Object.entries(decision.decisions)) {
      if (effect.effect !== "allow") continue;
      const suffix = action === "precheck" ? "upload" : action.startsWith("submit_") ? "submit" : action === "preview" ? "preview"
        : action === "export_errors" ? "errors" : action === "read_report" ? "report" : entryId.includes("/jobs") ? "jobs" : "history";
      const surface = surfaceDefinitions[`${resource}.${suffix}`];
      if (!surface) return anyRule();
      // Preparation precheck belongs to its own input observation. Submit uses its own
      // actual action plus the explicit display switch, never a precheck context ticket.
      needs.push(allRule(actionRule(resource, ...surface.display_actions), actionRule(resource, action)));
    }
  }
  if (!needs.length) return anyRule();
  return entryId.startsWith("POST ") ? allRule(...needs) : anyRule(...needs);
}
const knownId = (value: string | null) => { const n = Number(value); return Number.isSafeInteger(n) && n > 0 ? n : null; };
function errorText(error: unknown) { const e = error as { response?: { data?: { detail?: { message?: string } | string } }; message?: string }; const d = e.response?.data?.detail; return typeof d === "string" ? d : d?.message || e.message || "请求失败，没有自动重放。"; }
const itemColumns = [{ title: "文件", dataIndex: "filename" }, { title: "来源", dataIndex: "source_domain" }, { title: "状态", dataIndex: "state" }, { title: "状态代码", dataIndex: "code", render: (v: string | null) => v ?? "—" }];
function JobView({ job, show }: { job: JobProjection; show: (source: ImportSource) => boolean }) {
  const items = job.items.filter(item => show(item.source_domain));
  const restricted = job.restricted || items.length !== job.items.length;
  return <>
  {restricted && <Alert type="warning" message="仅显示已授权来源的进度；整个作业的状态和数量不可见" />}
  <p>{restricted ? "当前展示文件" : "全部文件"} {items.length} · 完成 {items.filter(item => item.state === "success").length} · 失败 {items.filter(item => ["failed", "authorization_failed"].includes(item.state)).length}</p>
  <Tag>{restricted ? "仅部分进度可见" : job.status}</Tag>
  <Table rowKey="item_id" size="small" dataSource={items} columns={itemColumns} pagination={false} />
</>; }
export default function CanonicalImportPage() {
  const { message } = App.useApp(), admission = useContextAdmission(), [search] = useSearchParams();
  const locatorConflict = search.has("batch_id") && search.has("job_id");
  const can = (source: ImportSource, surface: string) => admission.surface(`import_${source}.${surface}`) !== "deny";
  const any = (surface: string) => IMPORT_SOURCES.some(source => can(source, surface));
  const [files, setFiles] = useState<File[]>([]), [mode, setMode] = useState<ImportMode>("skip"), [revision, setRevision] = useState(0);
  const [prepared, setPrepared] = useState<{ result: ImportPrecheckResult; previews: ExpenseVoidPreview[]; revision: number } | null>(null);
  const [confirmed, setConfirmed] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(locatorConflict ? "批次 ID 与作业 ID 不能同时定位，请选择一个目标。" : null);
  const [standalonePreviews, setStandalonePreviews] = useState<ExpenseVoidPreview[]>([]);
  const canVoid = admission.surface("import_expense_void.preview") !== "deny";
  const [receipt, setReceipt] = useState<{ job_id: number; total_files: number } | null>(null);
  const [batchId, setBatchId] = useState<number | null>(locatorConflict ? null : knownId(search.get("batch_id"))), [jobId, setJobId] = useState<number | null>(locatorConflict ? null : knownId(search.get("job_id")));
  const [locator, setLocator] = useState(locatorConflict ? "" : search.get("batch_id") || search.get("job_id") || ""), [locatorKind, setLocatorKind] = useState<"batch" | "job">(search.has("job_id") ? "job" : "batch");
  const [detailOpen, setDetailOpen] = useState(batchId !== null), [downloading, setDownloading] = useState(false);
  const live = useRef(true), inputGeneration = useRef(revision); inputGeneration.current = revision;
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  const inputTransport = useContextTransport(JSON.stringify([revision, mode]), false, resolvedImportRule);
  const downloadTransport = useContextTransport(JSON.stringify([batchId, detailOpen]), false, resolvedImportRule);
  const lists = useContextQuery({ entryId: "GET /api/import/batches", resolvedRule: resolvedImportRule, rule: surfaces("history"), queryKey: [], enabled: any("history"),
    query: async capture => batchList((await domainApi(capture).get("/import/batches")).data) });
  const jobs = useContextQuery({ entryId: "GET /api/import/jobs", resolvedRule: resolvedImportRule, rule: surfaces("jobs"), queryKey: [], enabled: any("jobs"),
    query: async capture => jobList((await domainApi(capture).get("/import/jobs")).data) });
  const detail = useContextQuery({ entryId: "GET /api/import/batches/{batch_id}", resolvedRule: resolvedImportRule, rule: anyRule(surfaces("history"), surfaces("report")), queryKey: [batchId, detailOpen],
    enabled: detailOpen && batchId !== null && (any("history") || any("report")),
    query: async capture => batchDetail((await domainApi(capture).get(`/import/batches/${batchId}`)).data) });
  const job = useContextQuery({ entryId: "GET /api/import/jobs/{job_id}", resolvedRule: resolvedImportRule, rule: surfaces("jobs"), queryKey: [jobId], enabled: jobId !== null && any("jobs"),
    query: async capture => jobResponse((await domainApi(capture).get(`/import/jobs/${jobId}`)).data) });
  useEffect(() => {
    if (!job.data || job.error || job.data.job.visible_status !== "processing") return;
    const timer = window.setTimeout(() => { void job.retry(); }, 1500);
    return () => window.clearTimeout(timer);
  }, [job.data, job.error]);
  const invalidate = () => { setRevision(v => v + 1); setPrepared(null); setStandalonePreviews([]); setConfirmed(false); setError(null); };
  const prepare = async () => {
    if (busy || !files.length || !any("upload")) return;
    const version = revision; setBusy(true); setError(null); setPrepared(null); setConfirmed(false);
    try {
      const result = await precheckImportFiles(files, mode, undefined, inputTransport, true);
      if (!inputTransport.isCurrent() || inputGeneration.current !== version) return;
      if (result.contract !== "v2" || !result.sourceDomains || result.sourceDomains.some(source => source !== null && !can(source, "upload"))) throw new Error("部分文件来源未获预检展示权限，不能继续准备。");
      const previews: ExpenseVoidPreview[] = [];
      for (const [index, file] of result.files.entries()) {
        if (!file.issues.some(issue => ["upsert_void_armed", "upsert_precheck_skipped"].includes(issue.code))) continue;
        if (admission.surface("import_expense_void.preview") === "deny") throw new Error("该报销输入需要作废预演权限；尚未提交。");
        previews.push(await previewExpenseVoid(files[index], mode, undefined, inputTransport, true));
        if (!inputTransport.isCurrent() || inputGeneration.current !== version) return;
      }
      setPrepared({ result, previews, revision: version });
    } catch (e) { if (inputTransport.isCurrent() && !(e instanceof AuthorizationCancelled)) setError(errorText(e)); }
    finally { if (live.current && inputGeneration.current === version) setBusy(false); }
  };
  const previewOnly = async () => {
    if (busy || !canVoid || files.length !== 1) return;
    const version = revision; setBusy(true); setError(null); setStandalonePreviews([]);
    try {
      const value = await previewExpenseVoid(files[0], mode, undefined, inputTransport, true);
      if (inputTransport.isCurrent()) setStandalonePreviews([value]);
    } catch (e) { if (inputTransport.isCurrent()) setError(errorText(e)); }
    finally { if (live.current && inputGeneration.current === version) setBusy(false); }
  };
  const sourceSet = prepared?.result.sourceDomains ?? [];
  const submitAllowed = !!prepared && prepared.revision === revision && !prepared.result.blocked && ["clean", "warning"].includes(prepared.result.decision) && sourceSet.length > 0 &&
    sourceSet.every(source => source !== null && admission.state(allRule(surfaceRule(`import_${source}.submit`), actionRule(`import_${source}`, `submit_${mode}`))) !== "deny") &&
    prepared.previews.every(preview => ["ready", "suppressed", "not_applicable"].includes(preview.status));
  const submit = async () => {
    if (busy || !submitAllowed || !prepared || prepared.result.decision === "warning" && !confirmed) return;
    const version = revision; setBusy(true); setError(null);
    try {
      const result = await uploadImportBatch(files, mode, prepared.previews.flatMap(v => v.preview_token ? [v.preview_token] : []), inputTransport, true);
      if (!inputTransport.isCurrent()) return;
      setReceipt(result); setPrepared(null); setFiles([]); setRevision(v => v + 1);
      // The accepted-files receipt is not a job status grant.
      if (sourceSet.some(source => source !== null && can(source, "jobs"))) setJobId(result.job_id);
      if (any("history")) void lists.retry(); if (any("jobs")) void jobs.retry();
    } catch (e) { if (inputTransport.isCurrent() && !(e instanceof AuthorizationCancelled)) setError(errorText(e)); }
    finally { if (live.current && inputGeneration.current === version) setBusy(false); }
  };
  const download = async () => {
    if (batchId === null || downloading || !any("errors")) return;
    setDownloading(true); setError(null);
    try { await downloadImportErrors(batchId, downloadTransport); }
    catch (e) { if (downloadTransport.isCurrent()) setError(errorText(e)); }
    finally { if (downloadTransport.isCurrent()) setDownloading(false); }
  };
  const selectBatch = (id: number) => { setBatchId(id); setDetailOpen(true); };
  const selected = detail.data;
  const showStatus = selected && can(selected.source_domain, "history") && selected.block_access.status.effect === "allow";
  const showReport = selected && can(selected.source_domain, "report") && selected.block_access.report.effect === "allow";
  return <Space direction="vertical" size="large" style={{ width: "100%" }}>
    <PageHeader title="数据导入" subtitle="预检、提交、状态和报告分别按实际来源授权" />
    {error && <Alert type="error" message={error} />}
    <Space wrap>
      <Select aria-label="定位类型" value={locatorKind} onChange={setLocatorKind} options={[{ value: "batch", label: "批次 ID" }, { value: "job", label: "作业 ID" }]} />
      <Input aria-label="已知导入 ID" value={locator} onChange={e => setLocator(e.target.value)} placeholder="输入已知 ID" />
      <Button disabled={knownId(locator) === null} onClick={() => { setError(null); if (locatorKind === "batch") selectBatch(knownId(locator)!); else setJobId(knownId(locator)); }}>打开目标</Button>
      {batchId !== null && any("errors") && <Button loading={downloading} onClick={download}>下载批次 #{batchId} 问题明细</Button>}
    </Space>
    {(any("upload") || canVoid) && <Card title="上传与预检" extra={<Select aria-label="导入模式" value={mode} disabled={busy} onChange={value => { invalidate(); setMode(value); }} options={[{ value: "skip", label: "跳过已有" }, { value: "upsert", label: "修复更新" }]} />}>
      <Upload.Dragger accept=".xlsx" multiple showUploadList={false} disabled={busy || files.length >= 20} beforeUpload={file => { invalidate(); setFiles(old => [...old, file]); return false; }}>
        <InboxOutlined /><p>选择 Excel 文件（最多 20 个）</p>
      </Upload.Dragger>
      <List dataSource={files} renderItem={(file, index) => <List.Item actions={[<Button key="remove" disabled={busy} onClick={() => { invalidate(); setFiles(old => old.filter((_, i) => i !== index)); }}>移除</Button>]}>{file.name}</List.Item>} />
      <Space>{any("upload") && <Button disabled={busy || !files.length || files.length > 20} onClick={prepare}>预检文件</Button>}{canVoid && <Button disabled={busy || files.length !== 1} onClick={previewOnly}>仅预演报销作废</Button>}<Button disabled={busy} onClick={() => { invalidate(); setFiles([]); }}>清空</Button></Space>
      <ExpenseVoidPreviewPanel previews={standalonePreviews} />
      {prepared && <>
        <ImportPrecheckPanel result={prepared.result} onOpenBatch={selectBatch} />
        <ExpenseVoidPreviewPanel previews={prepared.previews} />
        {prepared.result.decision === "warning" && <Checkbox checked={confirmed} onChange={e => setConfirmed(e.target.checked)}>已核对警告及作废预演清单</Checkbox>}
        {submitAllowed ? <Button type="primary" loading={busy} disabled={busy || prepared.result.decision === "warning" && !confirmed} onClick={submit}>确认提交</Button> : <Alert type="info" message="当前输入尚不满足提交条件或缺少该来源、模式的提交展示权限" />}
      </>}
    </Card>}
    {receipt && <Alert type="success" message={`作业 #${receipt.job_id} 已接受 ${receipt.total_files} 个本次文件`} description="这只是提交回执；后续状态和报告需分别授权读取。没有自动重新上传。" />}
    {jobId !== null && any("jobs") && <Card title={`作业 #${jobId}`}>
      {job.error && <Alert type="error" message={errorText(job.error)} action={<Button onClick={() => void job.retry()}>重新查询</Button>} />}
      {job.data && <JobView job={job.data.job} show={source => can(source, "jobs")} />}
    </Card>}
    {any("jobs") && <Card title="作业记录">
      {jobs.error && <Alert type="error" message={errorText(jobs.error)} action={<Button onClick={() => void jobs.retry()}>重试</Button>} />}
      <List loading={jobs.loading} dataSource={(jobs.data?.items ?? []).filter(item => item.items.some(row => can(row.source_domain, "jobs")))} renderItem={item => <List.Item><Button onClick={() => setJobId(item.job_id)}>作业 #{item.job_id}</Button><span>{item.restricted ? "部分来源可见" : item.status}</span></List.Item>} />
    </Card>}
    {any("history") && <Card title="导入历史">
      {lists.error && <Alert type="error" message={errorText(lists.error)} action={<Button onClick={() => void lists.retry()}>重试</Button>} />}
      {lists.data?.restricted && <Alert type="info" message="只显示已授权来源；未请求全库总数" />}
      <Table size="small" rowKey="batch_id" loading={lists.loading} dataSource={(lists.data?.items ?? []).filter(item => can(item.source_domain, "history"))} columns={[
        { title: "批次", dataIndex: "batch_id" }, { title: "来源", render: (_, item) => SOURCE_LABEL[item.source_domain] },
        { title: "文件", render: (_, item) => item.metadata.filename }, { title: "状态", render: (_, item) => item.metadata.status },
        { title: "操作", render: (_, item) => <Button onClick={() => selectBatch(item.batch_id)}>查看批次</Button> },
      ]} pagination={{ pageSize: 10 }} />
    </Card>}
    <Modal title={`批次 #${batchId ?? ""}`} open={detailOpen && (any("history") || any("report"))} footer={null} onCancel={() => { setDetailOpen(false); setDownloading(false); }} destroyOnHidden>
      {detail.error && <Alert type="error" message={errorText(detail.error)} action={<Button onClick={() => void detail.retry()}>重试</Button>} />}
      {showStatus && selected.metadata && <Descriptions column={1} items={Object.entries(selected.metadata).map(([key, value]) => ({ key, label: key, children: value === null ? "—" : String(value) }))} />}
      {showReport && <>
        {selected.source_domain === "maintenance" && selected.report && <WbddUploadFeedback report={{ ...selected.report, batch_id: selected.batch_id }} />}
        {Number(selected.report?.expense_rows_voided) > 0 && <Alert type="warning" message={`本次作废了 ${selected.report?.expense_rows_voided} 条旧报销行，请核对报告`} />}
        <Descriptions column={1} items={Object.entries(selected.report ?? {}).filter(([key]) => key !== "errors_preview").map(([key, value]) => ({ key, label: key, children: typeof value === "object" ? JSON.stringify(value) : String(value) }))} />
        <Table rowKey={(_, index) => String(index)} size="small" dataSource={selected.errors} columns={[{ title: "行号", dataIndex: "row_no" }, { title: "性质", dataIndex: "nature" }, { title: "类型", dataIndex: "error_type" }, { title: "明细", dataIndex: "detail" }]} locale={{ emptyText: selected.issue_count === 0 ? "无问题行" : "没有返回明细" }} />
      </>}
      {selected && !showReport && <Alert type="info" message="完整报告未授权展示" />}
    </Modal>
  </Space>;
}
