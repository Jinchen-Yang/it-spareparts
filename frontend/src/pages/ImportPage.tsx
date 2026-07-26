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
  downloadImportErrors, precheckImportFiles, uploadImportBatch,
  type ImportMode, type ImportPrecheckResult,
} from "../api/imports";
import ImportPrecheckPanel from "./import/ImportPrecheckPanel";

// 历史询价（inquiry）导入为合同 Step 4 规划，后端尚未实装（B7 去重口径待客户确认），实装后再加回
const FILE_TYPE: Record<string, string> = {
  purchase: "采购订单", sales: "销售订单", inventory: "产品库存", maintenance: "维保出库",
  expense: "报销明细", workbook: "项目工作簿",
};
const STATUS_COLOR: Record<string, string> = { success: "green", failed: "red", processing: "blue" };
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
      setSnapshot({ files, mode, revision, result });
      setPhase(result.decision === "clean" ? "clean_ready"
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
      const data = await uploadImportBatch(current.files, current.mode);
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
      setImportError(`${detail}。请先查看导入历史，确认未创建作业后再重试。`);
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
            setStaged((prev) => {
              const existing = prev.findIndex((f) => f.name === file.name && f.size === file.size);
              if (existing < 0) return [...prev, file];
              const next = [...prev];
              next[existing] = file;
              return next;
            });
            return false; // 阻止 antd 默认上传，自己批量提交
          }}
        >
          <p className="ant-upload-drag-icon"><InboxOutlined /></p>
          <p className="ant-upload-text">点击或拖拽 .xlsx 文件到此处（可多选批量导入）</p>
          <p className="ant-upload-hint">支持采购订单 / 销售订单 / 产品库存 / 维保出库 / 报销明细，自动识别类型；项目追踪工作簿可整本上传（只吃报销明细页，其余页自动跳过）</p>
        </Upload.Dragger>

        {staged.length > 0 && (
          <div style={{ marginTop: 16 }}>
            <List
              size="small" bordered dataSource={staged}
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
                    我已阅读并确认以上警告
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
            <Descriptions bordered column={2} size="small" style={{ marginBottom: 12 }}>
              {Object.entries(detail.report || {})
                .filter(([k]) => k !== "errors_preview" && k !== "missing_price_columns")
                .map(([k, v]) => (
                  <Descriptions.Item key={k} label={k}>{String(v)}</Descriptions.Item>
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
