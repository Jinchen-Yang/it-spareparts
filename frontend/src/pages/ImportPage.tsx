import { useEffect, useRef, useState } from "react";
import {
  Card, Upload, Descriptions, Tag, message, Space, Button, Modal, Progress,
  Switch, Tooltip, List, Alert, Checkbox,
} from "antd";
import { InboxOutlined, DeleteOutlined } from "@ant-design/icons";
import ResizableTable from "../components/ResizableTable";
import PageHeader from "../components/PageHeader";
import type { ColumnsType } from "antd/es/table";
import api from "../api";
import {
  downloadImportErrors, precheckImportFiles, previewExpenseVoid, uploadImportBatch,
  type ExpenseVoidPreview, type ImportMode, type ImportPrecheckResult,
} from "../api/imports";
import ImportPrecheckPanel from "./import/ImportPrecheckPanel";
import ExpenseVoidPreviewPanel from "./import/ExpenseVoidPreviewPanel";

const MAX_IMPORT_FILES = 20;

// 历史询价（inquiry）导入为合同 Step 4 规划，后端尚未实装（B7 去重口径待客户确认），实装后再加回
const FILE_TYPE: Record<string, string> = {
  purchase: "采购订单", sales: "销售订单", inventory: "产品库存", maintenance: "维保出库",
  expense: "报销明细", workbook: "项目工作簿",
};
const STATUS_COLOR: Record<string, string> = { success: "green", failed: "red", processing: "blue" };
// 批次报告键的中文名。作废是**减钱**动作，其可见度必须高于插入统计，故除了在这里
// 有名字，还单独渲染成 warning（见详情弹窗）。未列出的键按原样显示。
const REPORT_LABEL: Record<string, string> = {
  source_rows_total: "源文件行数", fact_rows_inserted: "新增入库",
  fact_rows_updated: "更新", fact_rows_skipped: "跳过", fact_rows_error: "错误行",
  rows_inactive: "非生效行", rows_skipped_no_data: "空行跳过",
  import_mode: "导入模式",
  expense_rows_replaced: "修复模式覆盖范围（行）",
  expense_rows_voided: "作废旧行",
  expense_rows_void_protected: "免于作废（删除侧未生效）",
  expense_void_suppressed_reason: "删除侧未生效的原因",
  expense_rows_dropped_no_contract: "无销售订单被排除",
};
const VOID_SUPPRESSED_REASON: Record<string, string> = {
  dropped_no_contract: "本表有行缺销售订单被排除",
  multi_contract: "本表触及多个销售订单",
  unanchored: "本表不是带页级锚的项目工作簿报销页",
};
function reportValue(k: string, v: unknown): string {
  if (k === "expense_void_suppressed_reason") return VOID_SUPPRESSED_REASON[String(v)] ?? String(v);
  return String(v);
}
const JOB_STATUS: Record<string, { label: string; color: string }> = {
  processing: { label: "进行中", color: "blue" },
  done: { label: "全部完成", color: "green" },
  partial: { label: "部分成功", color: "orange" },
  failed: { label: "失败", color: "red" },
};

interface Batch {
  id: number; filename: string; file_type: string; status: string;
  uploaded_at: string; uploaded_by: string | null; rows_total: number; rows_inserted: number;
  rows_skipped: number; rows_error: number; rows_inactive: number;
}

interface JobBatch {
  id: number; filename: string; file_type: string; status: string;
  rows_total: number; rows_inserted: number; rows_skipped: number; rows_error: number;
}
interface Job {
  id: number; status: string; mode: string; total_files: number;
  done_files: number; error_files: number; note: string | null; batches: JobBatch[];
}

type ImportPhase = "dirty" | "prechecking" | "blocked" | "warning_ready"
  | "clean_ready" | "uploading" | "processing";

interface PrecheckSnapshot {
  files: File[];
  mode: ImportMode;
  revision: number;
  result: ImportPrecheckResult;
  // 修复模式下会真的作废的报销页（预检 upsert_void_armed）各一份服务端预演；
  // status=ready 的令牌随正式提交带回，服务端装载期复核。
  previews: ExpenseVoidPreview[];
}

export default function ImportPage() {
  const [upsertMode, setUpsertMode] = useState(false);  // false=skip(默认), true=upsert(修复模式)
  const [staged, setStaged] = useState<File[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [phase, setPhase] = useState<ImportPhase>("dirty");
  const [snapshot, setSnapshot] = useState<PrecheckSnapshot | null>(null);
  const [warningConfirmed, setWarningConfirmed] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [pollingInterrupted, setPollingInterrupted] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [detail, setDetail] = useState<any | null>(null);
  const [downloadingErrors, setDownloadingErrors] = useState(false);
  const pollRef = useRef<number | null>(null);
  const pollDeadlineTimerRef = useRef<number | null>(null);
  const pollDeadlineRef = useRef<number>(0);
  const pollGenerationRef = useRef(0);
  const mountedRef = useRef(false);
  const inputRevisionRef = useRef(0);
  const precheckActionLockRef = useRef(false);
  const precheckControllerRef = useRef<AbortController | null>(null);
  const uploadActionLockRef = useRef(false);
  const resumePollingLockRef = useRef(false);
  const downloadErrorsLockRef = useRef(false);
  const detailRequestRef = useRef<{
    key: string; fromPrecheck: boolean; controller: AbortController;
  } | null>(null);
  const POLL_MAX_MS = 15 * 60 * 1000;   // 兜底：进程被杀等极端情况下作业卡在「进行中」，不无限轮询

  const loadBatches = () => api.get("/import/batches").then((r) => {
    if (mountedRef.current) setBatches(r.data);
  });
  useEffect(() => {
    mountedRef.current = true;
    loadBatches();
    return () => {
      mountedRef.current = false;
      pollGenerationRef.current += 1;
      if (pollRef.current) clearTimeout(pollRef.current);
      if (pollDeadlineTimerRef.current) clearTimeout(pollDeadlineTimerRef.current);
      precheckControllerRef.current?.abort();
      detailRequestRef.current?.controller.abort();
    };
  }, []);

  const activeJob = job?.status === "processing";
  const busy = phase === "prechecking" || phase === "uploading"
    || (activeJob && !pollingInterrupted);

  const invalidatePrecheck = () => {
    inputRevisionRef.current += 1;
    precheckControllerRef.current?.abort();
    if (detailRequestRef.current?.fromPrecheck) {
      detailRequestRef.current.controller.abort();
      detailRequestRef.current = null;
    }
    setSnapshot(null);
    setWarningConfirmed(false);
    setImportError(null);
    setPhase("dirty");
  };

  const interruptPolling = (generation: number) => {
    if (generation !== pollGenerationRef.current) return;
    pollGenerationRef.current += 1;
    if (pollRef.current) clearTimeout(pollRef.current);
    if (pollDeadlineTimerRef.current) clearTimeout(pollDeadlineTimerRef.current);
    pollRef.current = null;
    pollDeadlineTimerRef.current = null;
    resumePollingLockRef.current = false;
    setPollingInterrupted(true);
    setPhase((current) => current === "processing" ? "dirty" : current);
    void loadBatches().catch(() => {});
  };

  const armPollingDeadline = (generation: number) => {
    if (pollDeadlineTimerRef.current) clearTimeout(pollDeadlineTimerRef.current);
    pollDeadlineTimerRef.current = window.setTimeout(
      () => interruptPolling(generation),
      Math.max(0, pollDeadlineRef.current - Date.now()),
    );
  };

  const poll = async (jobId: number, generation: number) => {
    if (!mountedRef.current || generation !== pollGenerationRef.current) return;
    if (Date.now() > pollDeadlineRef.current) {
      interruptPolling(generation);
      return;
    }
    try {
      const { data } = await api.get(`/import/jobs/${jobId}`);
      if (!mountedRef.current || generation !== pollGenerationRef.current) return;
      setJob(data);
      setPollingInterrupted(false);
      if (data.status === "processing") {
        pollRef.current = window.setTimeout(() => poll(jobId, generation), 1500);
      } else {
        resumePollingLockRef.current = false;
        if (pollDeadlineTimerRef.current) clearTimeout(pollDeadlineTimerRef.current);
        pollDeadlineTimerRef.current = null;
        setPhase((current) => current === "processing" ? "dirty" : current);
        await loadBatches();
        if (!mountedRef.current || generation !== pollGenerationRef.current) return;
        if (data.status === "done") message.success("批量导入完成");
        else if (data.status === "partial") message.warning("部分文件未导入，见作业明细");
        else message.error("导入失败，见作业明细");
      }
    } catch {
      if (!mountedRef.current || generation !== pollGenerationRef.current) return;
      if (Date.now() > pollDeadlineRef.current) {
        interruptPolling(generation);
      } else {
        pollRef.current = window.setTimeout(() => poll(jobId, generation), 1500);
      }
    }
  };

  const resumePolling = () => {
    if (resumePollingLockRef.current || !job || job.status !== "processing") return;
    resumePollingLockRef.current = true;
    setPollingInterrupted(false);
    if (!staged.length) setPhase("processing");
    pollDeadlineRef.current = Date.now() + POLL_MAX_MS;
    const generation = ++pollGenerationRef.current;
    armPollingDeadline(generation);
    poll(job.id, generation);
  };

  const runPrecheck = async () => {
    if (precheckActionLockRef.current) return;
    precheckActionLockRef.current = true;
    if (!staged.length) {
      precheckActionLockRef.current = false;
      return;
    }
    const files = [...staged];
    const mode: ImportMode = upsertMode ? "upsert" : "skip";
    const revision = inputRevisionRef.current;
    const controller = new AbortController();
    precheckControllerRef.current = controller;
    setImportError(null);
    setWarningConfirmed(false);
    setPhase("prechecking");
    try {
      const result = await precheckImportFiles(files, mode, controller.signal);
      if (!mountedRef.current || revision !== inputRevisionRef.current) return;
      // 预检说「会作废」的文件，逐个向服务端要作废预演（逐行清单 + 金额 + 令牌）。
      // 预演拿不到就不允许提交：宁可让用户重来，也不让他对着没有清单的「将作废」按确认。
      const armed = result.contract === "v2"
        ? result.files.filter((file) => file.issues.some((issue) => issue.code === "upsert_void_armed"))
        : [];
      const previews: ExpenseVoidPreview[] = [];
      for (const armedFile of armed) {
        const source = files.find((file) => file.name === armedFile.filename);
        if (!source) throw new Error(`「${armedFile.filename}」无法定位到已选文件，请重新预检`);
        previews.push(await previewExpenseVoid(source, mode, controller.signal));
        if (!mountedRef.current || revision !== inputRevisionRef.current) return;
      }
      const notReady = previews.filter((preview) => preview.status !== "ready");
      setSnapshot({ files, mode, revision, result, previews });
      setPhase(notReady.length ? "blocked"
        : result.decision === "clean" ? "clean_ready"
        : result.decision === "warning" ? "warning_ready" : "blocked");
    } catch (e: any) {
      if (!mountedRef.current || revision !== inputRevisionRef.current) return;
      const detail = e?.response?.data?.detail;
      if (e?.response?.status === 403) setImportError(`无权限${detail ? `：${detail}` : ""}`);
      else if (e?.code === "ECONNABORTED" || /timeout/i.test(e?.message || "")) {
        setImportError("预检超时：请检查文件大小后重新预检");
      } else if (!e?.response) setImportError("网络连接失败：请检查网络后重新预检");
      else setImportError(detail || "预检失败，请修正后重试");
      setPhase("dirty");
    } finally {
      if (precheckControllerRef.current === controller) {
        precheckControllerRef.current = null;
        precheckActionLockRef.current = false;
      }
    }
  };

  const submitBatch = async () => {
    if (uploadActionLockRef.current || activeJob) return;
    uploadActionLockRef.current = true;
    const current = snapshot;
    if (!current || current.revision !== inputRevisionRef.current
      || current.result.blocked || current.result.decision === "unknown"
      || (current.result.decision === "warning" && !warningConfirmed)) {
      uploadActionLockRef.current = false;
      return;
    }
    setSubmitting(true);
    setImportError(null);
    setPhase("uploading");
    try {
      const tokens = current.previews
        .filter((preview) => preview.status === "ready" && preview.preview_token)
        .map((preview) => preview.preview_token as string);
      // 没有令牌时保持既有调用形状（files, mode）；令牌只在会作废的形态下出现
      const data = tokens.length
        ? await uploadImportBatch(current.files, current.mode, tokens)
        : await uploadImportBatch(current.files, current.mode);
      if (!mountedRef.current) return;
      setJob({ id: data.job_id, status: "processing", mode: current.mode,
               total_files: data.total_files, done_files: 0, error_files: 0, note: null, batches: [] });
      pollDeadlineRef.current = Date.now() + POLL_MAX_MS;
      const generation = ++pollGenerationRef.current;
      armPollingDeadline(generation);
      poll(data.job_id, generation);
      if (current.revision === inputRevisionRef.current) {
        setStaged([]);
        inputRevisionRef.current += 1;
        setSnapshot(null);
        setWarningConfirmed(false);
        setPhase("processing");
      }
    } catch (e: any) {
      if (!mountedRef.current || current.revision !== inputRevisionRef.current) return;
      const detail = e?.response?.data?.detail || e?.message || "正式提交失败";
      const code = String(e?.response?.headers?.["x-error-code"] ?? "");
      setImportError(code.startsWith("void_")
        ? `${detail}。请点「返回修改」后重新预检，系统会重新生成作废预演。`
        : `${detail}。请先查看导入历史，确认未创建作业后再重试。`);
      setPhase(current.result.decision === "warning" ? "warning_ready" : "clean_ready");
    } finally {
      if (mountedRef.current) setSubmitting(false);
      uploadActionLockRef.current = false;
    }
  };

  const openDetail = async (id: number, fromPrecheck = false) => {
    const revision = inputRevisionRef.current;
    const key = `${fromPrecheck ? `precheck:${revision}` : "history"}:${id}`;
    if (detailRequestRef.current?.key === key) return;
    detailRequestRef.current?.controller.abort();
    const request = { key, fromPrecheck, controller: new AbortController() };
    detailRequestRef.current = request;
    try {
      const { data } = await api.get(`/import/batches/${id}`, {
        timeout: 30_000,
        signal: request.controller.signal,
      });
      if (detailRequestRef.current !== request) return;
      if (fromPrecheck && revision !== inputRevisionRef.current) return;
      if (mountedRef.current) setDetail(data);
    } catch (error: any) {
      if (!mountedRef.current || detailRequestRef.current !== request
        || request.controller.signal.aborted
        || (fromPrecheck && revision !== inputRevisionRef.current)) return;
      const timedOut = error?.code === "ECONNABORTED" || /timeout/i.test(error?.message || "");
      if (fromPrecheck && timedOut) {
        message.error("原批次详情加载超时，请重试");
      } else if (timedOut) {
        message.error("批次详情加载超时，请重试");
      } else if (fromPrecheck && error?.response?.status === 403) {
        message.error("无权查看原批次详情，请联系管理员开通数据导入权限");
      } else if (fromPrecheck && error?.response?.status === 404) {
        message.error("原批次不存在或已无法访问");
      } else if (fromPrecheck && !error?.response) {
        message.error("网络连接失败，请检查网络后重试查看原批次");
      } else if (fromPrecheck) {
        message.error("原批次详情加载失败，请稍后重试");
      } else {
        message.error("批次详情加载失败，请稍后重试");
      }
    } finally {
      if (detailRequestRef.current === request) detailRequestRef.current = null;
    }
  };

  const downloadErrors = async () => {
    if (downloadErrorsLockRef.current || !detail) return;
    downloadErrorsLockRef.current = true;
    setDownloadingErrors(true);
    try {
      await downloadImportErrors(detail.id);
    } catch (error: any) {
      if (!mountedRef.current) return;
      if (error?.response?.status === 403) {
        message.error("无权下载问题明细，请联系管理员开通数据导入权限");
      } else if (error?.response?.status === 404) {
        message.error("未找到批次，无法下载问题明细");
      } else if (!error?.response) {
        message.error("网络连接失败，请检查网络后重试下载");
      } else {
        message.error("问题明细下载失败，请稍后重试");
      }
    } finally {
      downloadErrorsLockRef.current = false;
      if (mountedRef.current) setDownloadingErrors(false);
    }
  };

  // 软标记（非真错误，可忽略）→ 灰色；其余 → 红色
  const ERR_LABEL: Record<string, { label: string; color: string }> = {
    empty_pn_inactive: { label: "草稿/取消单·可忽略", color: "default" },
    missing_date_in_progress: { label: "审批未完成·可忽略", color: "default" },
  };
  const errCols: ColumnsType<any> = [
    { title: "行号", dataIndex: "row_no", width: 80 },
    { title: "性质", dataIndex: "nature", width: 80 },
    { title: "问题类型", dataIndex: "error_type", width: 170,
      render: (t: string) => {
        const e = ERR_LABEL[t];
        return <Tag color={e?.color || "red"}>{e?.label || t}</Tag>;
      } },
    { title: "问题明细", dataIndex: "detail" },
  ];

  const jobBatchCols: ColumnsType<JobBatch> = [
    { title: "文件名", dataIndex: "filename", ellipsis: true },
    { title: "类型", dataIndex: "file_type", width: 100, render: (t) => FILE_TYPE[t] || t },
    { title: "状态", dataIndex: "status", width: 90, render: (s) => <Tag color={STATUS_COLOR[s]}>{s}</Tag> },
    { title: "总行", dataIndex: "rows_total", width: 70, align: "right" },
    { title: "入库", dataIndex: "rows_inserted", width: 70, align: "right" },
    { title: "跳过", dataIndex: "rows_skipped", width: 70, align: "right" },
    { title: "错误", dataIndex: "rows_error", width: 70, align: "right",
      render: (v) => (v ? <Tag color="red">{v}</Tag> : v) },
  ];

  const batchCols: ColumnsType<Batch> = [
    { title: "文件名", dataIndex: "filename", width: 240, fixed: "left", ellipsis: true },
    { title: "类型", dataIndex: "file_type", width: 100, render: (t) => FILE_TYPE[t] || t },
    { title: "状态", dataIndex: "status", width: 90, render: (s) => <Tag color={STATUS_COLOR[s]}>{s}</Tag> },
    { title: "总行", dataIndex: "rows_total", width: 80, align: "right" },
    { title: "入库", dataIndex: "rows_inserted", width: 80, align: "right" },
    { title: "跳过", dataIndex: "rows_skipped", width: 80, align: "right" },
    { title: "错误", dataIndex: "rows_error", width: 80, align: "right",
      render: (v) => (v ? <Tag color="red">{v}</Tag> : v) },
    { title: "未生效", dataIndex: "rows_inactive", width: 80, align: "right" },
    { title: "导入人", dataIndex: "uploaded_by", width: 110,
      render: (v) => v || <span style={{ color: "var(--mb-text-3)" }}>-</span> },
    { title: "时间", dataIndex: "uploaded_at", width: 180,
      render: (t) => new Date(t).toLocaleString("zh-CN") },
    { title: "", width: 70, render: (_, r) => <a onClick={() => openDetail(r.id)}>详情</a> },
  ];

  const jobPct = job && job.total_files
    ? Math.round(((job.done_files + job.error_files) / job.total_files) * 100) : 0;

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title="数据导入"
        subtitle="上传氚云导出的 Excel（采购 / 销售 / 库存 / 维保出库）或项目追踪工作簿（报销明细页），自动清洗入库并留痕"
      />
      <Card title="上传文件"
        extra={
          <Tooltip title="开启后：源系统改过字段的旧数据,重导会更新(而不是跳过)。日常导入用「跳过」即可。">
            <Space>
              <span style={{ color: upsertMode ? "var(--mb-danger)" : "var(--mb-text-3)" }}>修复模式(更新已存在)</span>
              <Switch checked={upsertMode} onChange={(checked) => {
                invalidatePrecheck();
                setUpsertMode(checked);
              }} disabled={busy} />
            </Space>
          </Tooltip>
        }>
        <Upload.Dragger
          accept=".xlsx"
          multiple
          showUploadList={false}
          disabled={busy}
          beforeUpload={(file) => {
            invalidatePrecheck();
            setStaged((prev) => [...prev, file]);
            return false; // 阻止 antd 默认上传，自己批量提交
          }}
        >
          <p className="ant-upload-drag-icon"><InboxOutlined /></p>
          <p className="ant-upload-text">点击或拖拽 .xlsx 文件到此处（可多选批量导入）</p>
          <p className="ant-upload-hint">每批最多 {MAX_IMPORT_FILES} 个文件；支持采购订单 / 销售订单 / 产品库存 / 维保出库 / 报销明细，自动识别类型；项目追踪工作簿可整本上传（只吃报销明细页，其余页自动跳过）</p>
        </Upload.Dragger>

        {staged.length > 0 && (
          <div style={{ marginTop: 16 }}>
            <List
              size="small" bordered dataSource={staged.slice(0, MAX_IMPORT_FILES)}
              header={<span>待导入 {staged.length} 个文件</span>}
              renderItem={(f, i) => (
                <List.Item actions={[
                  <Button key="rm" type="text" size="small" danger icon={<DeleteOutlined />}
                    disabled={busy}
                    onClick={() => {
                      invalidatePrecheck();
                      setStaged((prev) => prev.filter((_, idx) => idx !== i));
                    }} />,
                ]}>
                  {f.name}
                  <span style={{ color: "var(--mb-text-3)", marginLeft: 8 }}>
                    {(f.size / 1024 / 1024).toFixed(2)} MB
                  </span>
                </List.Item>
              )}
            />
            {staged.length > MAX_IMPORT_FILES && (
              <div style={{ marginTop: 8 }}>
                另有 {staged.length - MAX_IMPORT_FILES} 个文件未展示，请删除或清空后分批处理
              </div>
            )}
            <Space style={{ marginTop: 12 }} wrap>
              {phase === "dirty" && (
                <Button type="primary" disabled={busy} onClick={runPrecheck}>
                  预检文件（{staged.length} 个）
                </Button>
              )}
              {phase === "blocked" && (
                <Button type="primary" danger disabled>
                  {snapshot?.result.files.some((file) => file.blocked_reason === "exact_success_duplicate")
                    ? snapshot.result.files.some((file) => file.blocked_reason !== "exact_success_duplicate"
                      && file.severity === "error")
                      ? "请移除已导入文件并处理其他预检问题后重新预检"
                      : "请移除已导入文件后重新预检"
                    : "请修正后重新预检"}
                </Button>
              )}
              {phase === "clean_ready" && (
                <Button type="primary" loading={submitting} disabled={busy || activeJob} onClick={submitBatch}>开始导入</Button>
              )}
              {phase === "warning_ready" && (
                <>
                  <Checkbox checked={warningConfirmed} onChange={(event) => setWarningConfirmed(event.target.checked)}>
                    {snapshot?.previews.some((preview) => preview.status === "ready" && (preview.void?.rows ?? 0) > 0)
                      ? `我已逐行核对作废预演清单（共 ${snapshot.previews.reduce((n, preview) => n + (preview.void?.rows ?? 0), 0)} 行）并确认导入`
                      : "我已阅读并确认以上警告"}
                  </Checkbox>
                  <Button type="primary" loading={submitting} disabled={busy || activeJob || !warningConfirmed} onClick={submitBatch}>
                    确认警告并导入
                  </Button>
                </>
              )}
              {(phase === "blocked" || phase === "warning_ready" || phase === "clean_ready") && (
                <Button disabled={busy} onClick={invalidatePrecheck}>返回修改</Button>
              )}
              <Button disabled={busy} onClick={() => {
                invalidatePrecheck();
                setStaged([]);
              }}>清空</Button>
            </Space>
            {importError && <Alert type="error" showIcon message={importError} style={{ marginTop: 12 }} />}
            {snapshot && <ImportPrecheckPanel
              result={snapshot.result}
              onOpenBatch={(batchId) => { void openDetail(batchId, true); }}
            />}
            {snapshot && <ExpenseVoidPreviewPanel previews={snapshot.previews} />}
          </div>
        )}
      </Card>

      {job && (
        <Card title={`导入作业 #${job.id}`}
          extra={<Tag color={JOB_STATUS[job.status]?.color}>{JOB_STATUS[job.status]?.label || job.status}</Tag>}>
          {pollingInterrupted && (
            <Alert
              type="warning"
              showIcon
              message="作业查询已中断，可继续查询当前作业"
              action={<Button size="small" onClick={resumePolling}>继续查询</Button>}
              style={{ marginBottom: 12 }}
            />
          )}
          <Progress percent={jobPct}
            status={job.status === "processing" ? "active" : job.status === "failed" ? "exception" : "normal"} />
          <div style={{ color: "var(--mb-text-3)", margin: "4px 0 12px" }}>
            共 {job.total_files} · 成功 {job.done_files} · 失败/跳过 {job.error_files}
            {job.status === "processing" && " · 处理中，请稍候…"}
          </div>
          {job.note && <Alert type="warning" showIcon style={{ marginBottom: 12 }} message={job.note} />}
          {job.batches.length > 0 && (
            <ResizableTable storageKey="import-jobbatches" rowKey="id" size="small" columns={jobBatchCols} dataSource={job.batches}
              pagination={false} />
          )}
        </Card>
      )}

      <Card title="导入历史">
        <ResizableTable storageKey="import-batches" rowKey="id" size="small" columns={batchCols} dataSource={batches}
          scroll={{ x: 1080 }} pagination={{ pageSize: 10 }} />
      </Card>

      <Modal
        open={!!detail} width={760} footer={null} onCancel={() => setDetail(null)}
        title={detail ? `批次 #${detail.id} · ${detail.filename}` : ""}
      >
        {detail && (
          <>
            {detail.report?.missing_price_columns && (
              <Alert
                type="warning" showIcon style={{ marginBottom: 12 }}
                message="此文件未识别到价格列，金额为空"
                description="通常是导出视图选错。请用含 单价 / 金额 / 税 的视图重新导出，再用「修复模式」重导补上金额。"
              />
            )}
            {Number(detail.report?.expense_rows_voided) > 0 && (
              <Alert
                type="warning" showIcon style={{ marginBottom: 12 }}
                message={`修复模式作废了 ${detail.report.expense_rows_voided} 条旧报销行`}
                description="「以本表为准」会把本表触及的每个销售订单名下、未出现在本表里的报销行作废，成本随之从项目卡片上扣除。若本表只覆盖了部分月份/部分来源，请核对这些行是否确实该删。"
              />
            )}
            {Number(detail.report?.expense_rows_void_protected) > 0 && (
              <Alert
                type="warning" showIcon style={{ marginBottom: 12 }}
                message={`删除侧未生效：${detail.report.expense_rows_void_protected} 条旧报销行被保留`}
                description={detail.report.expense_void_suppressed_reason === "unanchored"
                  ? "本表没有页级「销售订单」锚，不是系统导出的项目工作簿报销页。「以本表为准」的删除只在那种页上执行——逐行手填的单合同表无法证明它完整覆盖了该合同。本次只做了同键覆盖（改金额照常生效），未作废任何旧行。要按本表删除旧行，请从对应项目下载工作簿、在报销页上修改后回传。"
                  : detail.report.expense_void_suppressed_reason === "multi_contract"
                  ? "本表触及多个销售订单。「以本表为准」的删除只在单合同的项目工作簿报销页上执行——多合同的全公司导出无法证明它完整覆盖了每个合同，按它删除会把这些合同名下本表未覆盖时段的历史报销一并作废。本次只做了同键覆盖（改金额照常生效），未作废任何旧行。要按本表删除旧行，请用对应项目的工作簿报销页逐个合同做。"
                  : "本表有行因缺少销售订单被排除，因此本表不能代表「以本表为准」的删除侧——这些旧行可能正对应被排除的那些行。本次只做了同键覆盖（改金额照常生效），未作废任何旧行。请勿为了「让删除生效」而把无销售订单的行删掉或补一个合同号后重导：报销单一单多行时，明细行的销售订单靠单头继承，按单元格是否为空来过滤会连带删掉这些继承行。要按本表删除旧行，请用对应项目的工作簿报销页（单合同、带页级锚）逐个合同做。"}
              />
            )}
            {Number(detail.report?.expense_rows_dropped_no_contract) > 0 && (
              <Alert
                type="info" showIcon style={{ marginBottom: 12 }}
                message={`${detail.report.expense_rows_dropped_no_contract} 行因无销售订单未入库`}
                description="这些行不挂任何销售订单（公司日常开销等），本次未入库，也未牵连其它行。明细见下方问题清单，含日期/金额/人员/事由。"
              />
            )}
            <Descriptions bordered column={2} size="small" style={{ marginBottom: 12 }}>
              {Object.entries(detail.report || {})
                .filter(([k, v]) => k !== "errors_preview" && k !== "missing_price_columns" && v != null)
                .map(([k, v]) => (
                  <Descriptions.Item key={k} label={REPORT_LABEL[k] ?? k}>{reportValue(k, v)}</Descriptions.Item>
                ))}
            </Descriptions>
            {detail.issue_count > 0 && (
              <>
                <Alert type="info" showIcon style={{ marginBottom: 12 }}
                  message="可能包含草稿/取消单等可忽略提示，不代表源数据错误" />
                <Button loading={downloadingErrors} disabled={downloadingErrors}
                  onClick={downloadErrors} style={{ marginBottom: 12 }}>
                  下载完整导入问题明细（{detail.issue_count} 条）
                </Button>
              </>
            )}
            <ResizableTable storageKey="import-errors" rowKey={(_, i) => String(i)} size="small" columns={errCols}
              dataSource={detail.errors || []} pagination={{ pageSize: 10 }}
              locale={{ emptyText: "无问题行" }} />
          </>
        )}
      </Modal>
    </Space>
  );
}
