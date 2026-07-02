import { useEffect, useRef, useState } from "react";
import {
  Card, Upload, Descriptions, Tag, message, Space, Button, Modal, Progress,
  Switch, Tooltip, List, Alert,
} from "antd";
import { InboxOutlined, DeleteOutlined } from "@ant-design/icons";
import ResizableTable from "../components/ResizableTable";
import PageHeader from "../components/PageHeader";
import type { ColumnsType } from "antd/es/table";
import api from "../api";

// 历史询价（inquiry）导入为合同 Step 4 规划，后端尚未实装（B7 去重口径待客户确认），实装后再加回
const FILE_TYPE: Record<string, string> = {
  purchase: "采购订单", sales: "销售订单", inventory: "产品库存",
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

export default function ImportPage() {
  const [upsertMode, setUpsertMode] = useState(false);  // false=skip(默认), true=upsert(修复模式)
  const [staged, setStaged] = useState<File[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [detail, setDetail] = useState<any | null>(null);
  const pollRef = useRef<number | null>(null);
  const pollDeadlineRef = useRef<number>(0);
  const POLL_MAX_MS = 15 * 60 * 1000;   // 兜底：进程被杀等极端情况下作业卡在「进行中」，不无限轮询

  const loadBatches = () => api.get("/import/batches").then((r) => setBatches(r.data));
  useEffect(() => {
    loadBatches();
    return () => { if (pollRef.current) clearTimeout(pollRef.current); };
  }, []);

  const busy = submitting || job?.status === "processing";

  const poll = async (jobId: number) => {
    if (Date.now() > pollDeadlineRef.current) {
      message.warning("导入耗时异常，已停止刷新；请稍后到「导入历史」查看结果");
      await loadBatches();
      return;
    }
    try {
      const { data } = await api.get(`/import/jobs/${jobId}`);
      setJob(data);
      if (data.status === "processing") {
        pollRef.current = window.setTimeout(() => poll(jobId), 1500);
      } else {
        await loadBatches();
        if (data.status === "done") message.success("批量导入完成");
        else if (data.status === "partial") message.warning("部分文件未导入，见作业明细");
        else message.error("导入失败，见作业明细");
      }
    } catch {
      if (pollRef.current) clearTimeout(pollRef.current);
    }
  };

  const submitBatch = async () => {
    if (!staged.length) return;
    setSubmitting(true);
    try {
      // 导入前预检：识别文件类型 + 采购/销售是否含价格列。有问题 → 弹二次确认
      const pcForm = new FormData();
      staged.forEach((f) => pcForm.append("files", f));
      const { data: pc } = await api.post("/import/precheck", pcForm);
      const warned: any[] = (pc.files || []).filter((x: any) => x.warning);
      if (warned.length) {
        const proceed = await new Promise<boolean>((resolve) => {
          Modal.confirm({
            title: "导入前确认",
            width: 560,
            okText: "仍要导入",
            okButtonProps: { danger: true },
            cancelText: "取消",
            content: (
              <div>
                <p>以下文件检测到问题：</p>
                <ul style={{ paddingLeft: 18 }}>
                  {warned.map((x, i) => (
                    <li key={i} style={{ marginBottom: 6 }}>
                      <b>{x.filename}</b>：{x.warning}
                    </li>
                  ))}
                </ul>
                <p style={{ color: "var(--mb-text-3)", marginBottom: 0 }}>
                  若是导出视图选错，建议「取消」后用含价格列的视图重新导出再导入。确认无误可继续。
                </p>
              </div>
            ),
            onOk: () => resolve(true),
            onCancel: () => resolve(false),
          });
        });
        if (!proceed) { setSubmitting(false); return; }
      }

      // 实际导入
      const form = new FormData();
      staged.forEach((f) => form.append("files", f));
      const { data } = await api.post("/import/upload-batch", form, {
        params: { mode: upsertMode ? "upsert" : "skip" },
      });
      setJob({ id: data.job_id, status: "processing", mode: upsertMode ? "upsert" : "skip",
               total_files: data.total_files, done_files: 0, error_files: 0, note: null, batches: [] });
      setStaged([]);
      pollDeadlineRef.current = Date.now() + POLL_MAX_MS;
      poll(data.job_id);
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "提交失败");
    } finally {
      setSubmitting(false);
    }
  };

  const openDetail = async (id: number) => {
    const { data } = await api.get(`/import/batches/${id}`);
    setDetail(data);
  };

  // 软标记（非真错误，可忽略）→ 灰色；其余 → 红色
  const ERR_LABEL: Record<string, { label: string; color: string }> = {
    empty_pn_inactive: { label: "草稿/取消单·可忽略", color: "default" },
  };
  const errCols: ColumnsType<any> = [
    { title: "行号", dataIndex: "row_no", width: 80 },
    { title: "类型", dataIndex: "error_type", width: 170,
      render: (t: string) => {
        const e = ERR_LABEL[t];
        return <Tag color={e?.color || "red"}>{e?.label || t}</Tag>;
      } },
    { title: "明细", dataIndex: "detail" },
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
        subtitle="上传氚云导出的 Excel（采购 / 销售 / 库存），自动清洗入库并留痕"
      />
      <Card title="上传文件"
        extra={
          <Tooltip title="开启后：源系统改过字段的旧数据,重导会更新(而不是跳过)。日常导入用「跳过」即可。">
            <Space>
              <span style={{ color: upsertMode ? "var(--mb-danger)" : "var(--mb-text-3)" }}>修复模式(更新已存在)</span>
              <Switch checked={upsertMode} onChange={setUpsertMode} disabled={busy} />
            </Space>
          </Tooltip>
        }>
        <Upload.Dragger
          accept=".xlsx"
          multiple
          showUploadList={false}
          disabled={busy}
          beforeUpload={(file) => {
            setStaged((prev) =>
              prev.some((f) => f.name === file.name && f.size === file.size) ? prev : [...prev, file]);
            return false; // 阻止 antd 默认上传，自己批量提交
          }}
        >
          <p className="ant-upload-drag-icon"><InboxOutlined /></p>
          <p className="ant-upload-text">点击或拖拽 .xlsx 文件到此处（可多选批量导入）</p>
          <p className="ant-upload-hint">支持采购订单 / 销售订单 / 产品库存，自动识别类型</p>
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
                    onClick={() => setStaged((prev) => prev.filter((_, idx) => idx !== i))} />,
                ]}>
                  {f.name}
                  <span style={{ color: "var(--mb-text-3)", marginLeft: 8 }}>
                    {(f.size / 1024 / 1024).toFixed(2)} MB
                  </span>
                </List.Item>
              )}
            />
            <Space style={{ marginTop: 12 }}>
              <Button type="primary" loading={submitting} disabled={busy} onClick={submitBatch}>
                开始导入（{staged.length} 个）
              </Button>
              <Button disabled={busy} onClick={() => setStaged([])}>清空</Button>
            </Space>
          </div>
        )}
      </Card>

      {job && (
        <Card title={`导入作业 #${job.id}`}
          extra={<Tag color={JOB_STATUS[job.status]?.color}>{JOB_STATUS[job.status]?.label || job.status}</Tag>}>
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
            <ResizableTable storageKey="import-errors" rowKey={(_, i) => String(i)} size="small" columns={errCols}
              dataSource={detail.errors || []} pagination={{ pageSize: 10 }}
              locale={{ emptyText: "无异常行" }} />
          </>
        )}
      </Modal>
    </Space>
  );
}
