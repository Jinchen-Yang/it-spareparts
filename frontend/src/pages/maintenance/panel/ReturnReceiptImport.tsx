import { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Descriptions, Input, Modal, Space, Table, Tag, Typography, message } from "antd";
import {
  type ReturnImportAction, type ReturnImportJob, type ReturnImportRow,
  applyReturnReceiptImport, cancelReturnReceiptImport, getReturnReceiptImport,
  retryReturnReceiptImport, uploadReturnReceiptImport, downloadReturnReceiptOriginal,
} from "../../../api/maintenanceReturnReceiptImports";
import { raw, readError } from "./panelUtils";
import { saveBlob } from "../../../api/maintenanceWorkbooks";

const ACTIONS: Record<ReturnImportAction, string> = {
  create: "新增", unchanged: "无变化", change: "变更", pending: "待关联", excluded: "不纳入", invalid: "无效",
};
const STATES: Record<ReturnImportJob["status"], string> = {
  queued: "等待解析", processing: "解析中", ready: "预览完成", failed: "导入失败", cancelled: "已取消", applied: "已应用",
};
const KIND = { part: "备件", machine: "整机（计 1 台）", component: "附属明细（不计数）" };
const newKey = () => `return-import-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;

const COMPARISON_FIELDS = [
  ["project_id", "项目"], ["source_order_id", "维保需求单"], ["pn", "返件 PN"],
  ["description", "备件描述"], ["qty", "数量"], ["condition", "件况"],
  ["receipt_kind", "返件层级"], ["review_required", "数量审核"], ["note", "备注"],
  ["evidence_ref", "来源凭据"], ["head_no", "来源单号"], ["occurred_at", "收货时间"],
  ["line_status", "有效状态"], ["source_metadata", "来源单据信息"],
] as const;

function comparisonValue(row: ReturnImportRow, field: string, side: "before" | "after"): string {
  const snapshot = row[side];
  if (!snapshot || !(field in snapshot)) return "—";
  const value = snapshot[field];
  if (value == null || value === "") return field === "source_order_id" ? "未关联需求单" : "未填写";
  if (field === "project_id" && value === row.project_id && row.project_name) return row.project_name;
  if (field === "source_order_id" && value === row.after?.source_order_id && row.wbdd_no) return row.wbdd_no;
  if (field === "receipt_kind") return KIND[side === "after" ? row.kind : value as ReturnImportRow["kind"]] ?? String(value);
  if (field === "review_required") return value ? "待审（保留源数量）" : "无需待审";
  if (field === "line_status") return value === "active" ? "有效，计入返还" : value === "voided" ? "已作废，不计入返还" : String(value);
  if (field === "source_metadata" && typeof value === "object") {
    const source = value as Record<string, unknown>;
    return [source.category ? `入库类别：${source.category}` : null,
      source.sn ? `SN：${source.sn}` : null,
      source.machine_source_qty != null ? `整机原始数量：${source.machine_source_qty}` : null,
    ].filter(Boolean).join("；") || "未填写";
  }
  if (field === "occurred_at") {
    const date = new Date(String(value));
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
  }
  return String(value);
}

/** 专用后台解析任务；原件重试、业务更正和重新预览分别保留明确入口。 */
export default function ReturnReceiptImport({ onApplied }: { onApplied: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [job, setJob] = useState<ReturnImportJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [duplicatesConfirmed, setDuplicatesConfirmed] = useState(false);
  const [reason, setReason] = useState("");
  const [page, setPage] = useState(1);
  const [action, setAction] = useState<ReturnImportAction | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const attempt = useRef<{ file: File; key: string } | null>(null);
  const generation = useRef(0);
  // Mutations own the job state until their response settles. Reads may
  // supersede other reads, but cannot invalidate an in-flight command.
  const operationLock = useRef(false);
  const downloadGeneration = useRef(0);
  const downloadLock = useRef(false);
  useEffect(() => () => { generation.current += 1; downloadGeneration.current += 1; }, []);

  const refresh = async (id: string, targetPage = page) => {
    if (operationLock.current) return;
    const seq = ++generation.current;
    try {
      const response = await getReturnReceiptImport(id, targetPage);
      if (seq !== generation.current) return;
      setJob(response.data);
      setError(null);
    } catch (err) {
      if (seq === generation.current) setError(readError(err, "读取导入进度失败；可重试查询，不必重新上传"));
    }
  };

  useEffect(() => {
    if (!open || busy || !job || !["queued", "processing"].includes(job.status) || error) return;
    const timer = window.setTimeout(() => { void refresh(job.batch_id, 1); }, 1500);
    return () => window.clearTimeout(timer);
    // Each poll result schedules one successor; a failed read exposes a manual retry.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, job, error, busy]);

  const upload = async () => {
    if (!attempt.current || operationLock.current) return;
    operationLock.current = true;
    const seq = ++generation.current;
    setBusy(true); setError(null); setConfirmed(false); setDuplicatesConfirmed(false); setReason(""); setPage(1); setAction(null);
    try {
      const response = await uploadReturnReceiptImport(attempt.current.file, attempt.current.key);
      if (seq !== generation.current) return;
      setJob(response.data);
      // An idempotent replay may return a ready job without its preview payload.
      if (response.data.status === "ready" && !response.data.summary) {
        const detail = await getReturnReceiptImport(response.data.batch_id, 1);
        if (seq === generation.current) setJob(detail.data);
      }
    } catch (err) {
      if (seq === generation.current) setError(readError(err, "上传失败；重试上传会使用同一个登记键"));
    } finally {
      operationLock.current = false;
      if (seq === generation.current) setBusy(false);
    }
  };

  const command = async (kind: "cancel" | "retry" | "apply") => {
    if (!job || operationLock.current) return;
    operationLock.current = true;
    const seq = ++generation.current;
    setBusy(true); setError(null);
    try {
      let response;
      if (kind === "cancel") response = await cancelReturnReceiptImport(job.batch_id);
      else if (kind === "retry") {
        // A lost retry response can still start a new preview; approvals must
        // be cleared before the request, never carried into that next plan.
        setConfirmed(false); setDuplicatesConfirmed(false); setReason(""); setPage(1);
        response = await retryReturnReceiptImport(job.batch_id);
      } else {
        if (!job.plan_hash || !job.preview_token) throw new Error("预览凭证缺失，请重新预览");
        response = await applyReturnReceiptImport(job.batch_id, {
          plan_hash: job.plan_hash, preview_token: job.preview_token,
          confirm_changes: confirmed, confirm_possible_duplicates: duplicatesConfirmed, ...(reason.trim() ? { reason: reason.trim() } : {}),
        });
      }
      if (seq !== generation.current) return;
      setJob(response.data);
      if (kind === "apply") {
        message.success("入库返件已写入台账");
        await onApplied();
      }
    } catch (err) {
      if (seq !== generation.current) return;
      const detail = readError(err, "操作失败，请查询任务状态后重试");
      // A lost response may still have committed. Always query before offering another apply.
      try {
        const response = await getReturnReceiptImport(job.batch_id, page);
        if (seq !== generation.current) return;
        setJob(response.data);
        if (response.data.status === "applied") {
          await onApplied();
          message.success("已确认导入成功，台账已刷新");
        } else setError(detail);
      } catch {
        if (seq === generation.current) setError(detail);
      }
    } finally {
      operationLock.current = false;
      if (seq === generation.current) setBusy(false);
    }
  };

  const downloadOriginal = async () => {
    if (!job || downloadLock.current) return;
    downloadLock.current = true;
    const seq = downloadGeneration.current;
    setDownloading(true);
    try {
      const response = await downloadReturnReceiptOriginal(job.batch_id);
      if (seq === downloadGeneration.current) saveBlob(response.data, job.filename ?? attempt.current?.file.name ?? "维保返件入库原件.xlsx");
    } catch (err) {
      if (seq === downloadGeneration.current) message.error(readError(err, "原件下载失败，请稍后重试"));
    } finally {
      downloadLock.current = false;
      if (seq === downloadGeneration.current) setDownloading(false);
    }
  };

  const counts = job?.summary;
  const changeRequired = (counts?.change ?? 0) > 0;
  const duplicatesCount = job?.possible_duplicates_count ?? 0;
  const running = job?.status === "queued" || job?.status === "processing";
  const active = running || job?.status === "ready";
  const rows = job?.rows ?? [];
  return <>
    <Button size="small" onClick={() => setOpen(true)}>导入入库单</Button>
    <Modal open={open} title="导入维保入库返件" width={1040} footer={null}
      onCancel={() => { if (!operationLock.current) { setOpen(false); generation.current += 1; } }}
      maskClosable={!busy} closable={!busy}>
      <Space direction="vertical" style={{ width: "100%" }} size={12}>
        <Typography.Text type="secondary">使用维保部门入库单导出。拆旧返件、旧库退返按项目归属计入；整机附属明细仅展示，小数数量保留原值并待审。</Typography.Text>
        <input ref={inputRef} type="file" accept=".xlsx" aria-label="选择入库单 Excel" hidden disabled={busy || active}
          onChange={(event) => {
            const file = event.target.files?.[0]; event.target.value = "";
            if (!file || operationLock.current) return;
            attempt.current = { file, key: newKey() }; setJob(null); void upload();
          }} />
        <Space wrap>
          <Button disabled={busy || active} onClick={() => inputRef.current?.click()}>选择入库单 Excel</Button>
          {!job && error && attempt.current ? <Button loading={busy} onClick={() => { void upload(); }}>重试上传</Button> : null}
          {job ? <><Tag>{STATES[job.status]}</Tag><span>{job.filename ?? attempt.current?.file.name}</span></> : null}
          {job ? <Button loading={downloading} onClick={() => { void downloadOriginal(); }}>下载归档原件</Button> : null}
          {active ? <Button danger disabled={busy} onClick={() => { void command("cancel"); }}>取消本次导入</Button> : null}
          {job && ["failed", "cancelled", "ready"].includes(job.status) ? <Button loading={busy} onClick={() => { void command("retry"); }}>重新预览原件</Button> : null}
        </Space>
        {running ? <Alert type="info" showIcon message="后台正在校验完整原件与项目归属，可稍后查看进度。关闭窗口后任务继续。" /> : null}
        {error || job?.error ? <Alert type="error" showIcon message={error ?? job?.error?.message}
          action={job ? <Button disabled={busy} onClick={() => { void refresh(job.batch_id); }}>重新查询状态</Button> : undefined} /> : null}
        {job?.excluded_reasons && Object.keys(job.excluded_reasons).length ? <Alert type="info" showIcon message="不纳入返还的源单据" description={Object.entries(job.excluded_reasons).map(([category, count]) => `${category} ${count} 单`).join("；")} /> : null}
        {counts ? <>
          <Space wrap>
            <Button size="small" type={action === null ? "primary" : "default"} onClick={() => setAction(null)}>本页全部</Button>
            {(Object.keys(ACTIONS) as ReturnImportAction[]).map((key) => <Button key={key} size="small" type={action === key ? "primary" : "default"} onClick={() => setAction(key)}>{ACTIONS[key]} {counts[key]}</Button>)}
          </Space>
          {counts.blocking_errors > 0 ? <Alert type="warning" showIcon message={`有 ${counts.blocking_errors} 项阻断问题，本批暂不能应用。请处理项目归属或源数据后重新预览原件。`} /> : null}
          <Table<ReturnImportRow> rowKey="row_key" size="small" dataSource={action ? rows.filter((row) => row.action === action) : rows}
            scroll={{ x: 1050 }} pagination={false} locale={{ emptyText: "本页没有该分类明细" }}
            expandable={{ expandedRowRender: (row) => <Space direction="vertical" style={{ width: "100%" }}>
              {row.possible_duplicates?.length ? <>
                <Alert type="warning" showIcon message="疑似重复的手工登记，请核对是否为同一批返件" />
                <Table size="small" rowKey="receipt_id" pagination={false} dataSource={row.possible_duplicates} scroll={{ x: 600 }} columns={[
                  { title: "手工登记编号", dataIndex: "receipt_id" },
                  { title: "手工登记数量", dataIndex: "qty" },
                  { title: "收货日期", render: (_value, item) => raw(item.receipt_date ?? item.occurred_at) },
                ]} />
              </> : null}
              <Table size="small" rowKey="field" pagination={false} scroll={{ x: 600 }}
                dataSource={COMPARISON_FIELDS.filter(([field]) => field in (row.before ?? {}) || field in (row.after ?? {})).map(([field, label]) => ({
                  field, label, before: comparisonValue(row, field, "before"), after: comparisonValue(row, field, "after"),
                  changed: JSON.stringify(row.before?.[field]) !== JSON.stringify(row.after?.[field]),
                }))}
                columns={[
                  { title: "核对项目", dataIndex: "label", width: 120 },
                  { title: "当前台账", dataIndex: "before", render: (value: string) => <span style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{value}</span> },
                  { title: "拟导入内容", dataIndex: "after", render: (value: string, item) => <Space align="start"><span style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{value}</span>{item.changed ? <Tag color="orange">变更</Tag> : null}</Space> },
                ]}
              />
              <Descriptions size="small" column={1}>
                <Descriptions.Item label="原始明细身份">{row.row_key}</Descriptions.Item>
                {row.parent_row_key ? <Descriptions.Item label="父级整机身份">{row.parent_row_key}</Descriptions.Item> : null}
              </Descriptions>
            </Space> }}
            columns={[
              { title: "分类", dataIndex: "action", render: (value: ReturnImportAction, row) => <Space direction="vertical" size={0}><Tag>{ACTIONS[value]}</Tag>{row.possible_duplicates?.length ? <Tag color="orange">疑似手工重复</Tag> : null}</Space> },
              { title: "入库单", dataIndex: "head_no" }, { title: "PN", dataIndex: "pn" },
              { title: "数量", dataIndex: "qty", render: (value: string, row) => <>{value} {row.review_required ? <Tag color="gold">待审</Tag> : null}</> },
              { title: "层级", dataIndex: "kind", render: (value: ReturnImportRow["kind"]) => KIND[value] },
              { title: "件况", dataIndex: "condition", render: raw },
              { title: "项目", render: (_v, row) => raw(row.project_name ?? row.project_id) },
              { title: "需求单", dataIndex: "wbdd_no", render: raw },
              { title: "说明", dataIndex: "reason", width: 230 },
            ]} />
          <Space wrap>
            <Button disabled={busy || page <= 1} onClick={() => { if (operationLock.current) return; setPage(page - 1); if (job) void refresh(job.batch_id, page - 1); }}>上一页</Button>
            <span>第 {page} 页 · 全部 {job?.rows_total ?? Object.values(counts).reduce((sum, count) => sum + count, 0) - counts.blocking_errors} 行（每页 100 行，分类筛选仅作用于本页）</span>
            <Button disabled={busy || page * 100 >= (job?.rows_total ?? 0)} onClick={() => { if (operationLock.current) return; setPage(page + 1); if (job) void refresh(job.batch_id, page + 1); }}>下一页</Button>
          </Space>
        </> : null}
        {job?.status === "ready" ? <>
          {duplicatesCount > 0 ? <>
            <Alert type="warning" showIcon message={`本批 ${duplicatesCount} 条拟新增记录与现有手工登记相似，请展开对应行核对。`} description="确认后将另行新增这些收货记录，现有手工登记保留。可取消本次导入后处理重复来源。" />
            <Checkbox checked={duplicatesConfirmed} disabled={busy} onChange={(event) => setDuplicatesConfirmed(event.target.checked)}>已核对全部疑似手工重复，确认仍另行新增收货记录</Checkbox>
          </> : null}
          {changeRequired ? <>
            <Checkbox checked={confirmed} disabled={busy} onChange={(event) => setConfirmed(event.target.checked)}>已核对全部变更，明确更正既有台账（包括来源取消）</Checkbox>
            <Input.TextArea value={reason} disabled={busy} onChange={(event) => setReason(event.target.value)} placeholder="更正原因（必填，保留原操作人与前后值）" maxLength={256} />
          </> : null}
          <Button type="primary" loading={busy} disabled={!!error || !counts || counts.blocking_errors > 0 || !job.plan_hash || !job.preview_token || (changeRequired && (!confirmed || !reason.trim())) || (duplicatesCount > 0 && !duplicatesConfirmed)}
            onClick={() => { void command("apply"); }}>确认导入有效返件</Button>
        </> : null}
        {job?.status === "applied" ? <Alert type="success" showIcon message="导入已完成；无变化行不会重复计数。" /> : null}
      </Space>
    </Modal>
  </>;
}
