import { useEffect, useState } from "react";
import {
  Card, Upload, Result, Descriptions, Table, Tag, message, Space, Button, Modal, Progress,
} from "antd";
import { InboxOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import api from "../api";

const FILE_TYPE: Record<string, string> = {
  purchase: "采购订单", sales: "销售订单", inventory: "产品库存", inquiry: "历史询价",
};
const STATUS_COLOR: Record<string, string> = { success: "green", failed: "red", processing: "blue" };

interface Batch {
  id: number; filename: string; file_type: string; status: string;
  uploaded_at: string; rows_total: number; rows_inserted: number;
  rows_skipped: number; rows_error: number; rows_inactive: number;
}

type Notice = { status: "warning" | "error"; title: string; sub?: string };

export default function ImportPage() {
  const [report, setReport] = useState<any | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [phase, setPhase] = useState<"idle" | "uploading" | "processing">("idle");
  const [batches, setBatches] = useState<Batch[]>([]);
  const [detail, setDetail] = useState<any | null>(null);

  const loadBatches = () => api.get("/import/batches").then((r) => setBatches(r.data));
  useEffect(() => {
    loadBatches();
  }, []);

  const doUpload = async (file: File) => {
    setUploading(true);
    setReport(null);
    setNotice(null);
    setProgress(0);
    setPhase("uploading");
    const form = new FormData();
    form.append("file", file);
    try {
      const { data } = await api.post("/import/upload", form, {
        onUploadProgress: (e) => {
          const pct = e.total ? Math.round((e.loaded * 100) / e.total) : 0;
          setProgress(pct);
          if (pct >= 100) setPhase("processing"); // 上传完，后端解析/入库/重算中
        },
      });
      setReport({ ...data.report, _status: data.status, _batch: data.batch_id, _name: file.name });
      message.success(`导入成功：${FILE_TYPE[data.file_type] || data.file_type}`);
      await loadBatches();
    } catch (e: any) {
      const code = e?.response?.status;
      const detailMsg = e?.response?.data?.detail || "导入失败";
      if (code === 409) {
        setNotice({ status: "warning", title: "该文件已导入过", sub: `${file.name}：${detailMsg}。系统按文件内容去重，重复文件不会再次入库。` });
      } else {
        setNotice({ status: "error", title: "导入失败", sub: `${file.name}：${detailMsg}` });
      }
      await loadBatches();
    } finally {
      setUploading(false);
      setPhase("idle");
    }
  };

  const openDetail = async (id: number) => {
    const { data } = await api.get(`/import/batches/${id}`);
    setDetail(data);
  };

  const errCols: ColumnsType<any> = [
    { title: "行号", dataIndex: "row_no", width: 80 },
    { title: "错误类型", dataIndex: "error_type", width: 160, render: (t) => <Tag color="red">{t}</Tag> },
    { title: "明细", dataIndex: "detail" },
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
    { title: "时间", dataIndex: "uploaded_at", width: 180,
      render: (t) => new Date(t).toLocaleString("zh-CN") },
    { title: "", width: 70, render: (_, r) => <a onClick={() => openDetail(r.id)}>详情</a> },
  ];

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card title="数据导入">
        <Upload.Dragger
          accept=".xlsx"
          multiple={false}
          showUploadList={false}
          disabled={uploading}
          beforeUpload={(file) => {
            doUpload(file);
            return false; // 阻止 antd 默认上传，自己处理
          }}
        >
          <p className="ant-upload-drag-icon"><InboxOutlined /></p>
          <p className="ant-upload-text">点击或拖拽 .xlsx 文件到此处导入</p>
          <p className="ant-upload-hint">支持采购订单 / 销售订单 / 产品库存 / 历史询价，自动识别类型</p>
        </Upload.Dragger>

        {phase !== "idle" && (
          <div style={{ marginTop: 16 }}>
            <Progress
              percent={progress}
              status={phase === "processing" ? "active" : "normal"}
            />
            <div style={{ textAlign: "center", color: "#888", marginTop: 4 }}>
              {phase === "uploading"
                ? `上传中 ${progress}%`
                : "上传完成，正在解析 / 入库 / 重算，请稍候…"}
            </div>
          </div>
        )}
      </Card>

      {notice && (
        <Card>
          <Result status={notice.status} title={notice.title} subTitle={notice.sub} />
        </Card>
      )}

      {report && (
        <Card title="导入报告">
          <Result
            status={report._status === "success" ? "success" : "error"}
            title={`批次 #${report._batch} · ${FILE_TYPE[report.file_type] || report.file_type}`}
          />
          <Descriptions bordered column={3} size="small">
            <Descriptions.Item label="解析总行">{report.source_rows_total}</Descriptions.Item>
            <Descriptions.Item label="入库">{report.fact_rows_inserted}</Descriptions.Item>
            <Descriptions.Item label="跳过(已存在)">{report.fact_rows_skipped}</Descriptions.Item>
            <Descriptions.Item label="错误">{report.fact_rows_error}</Descriptions.Item>
            <Descriptions.Item label="未生效">{report.rows_inactive}</Descriptions.Item>
            <Descriptions.Item label="新增型号">{report.new_parts ?? "-"}</Descriptions.Item>
            {report.orders_inserted != null && (
              <Descriptions.Item label="新增订单">{report.orders_inserted}</Descriptions.Item>
            )}
            {report.merged_pn_warehouse != null && (
              <Descriptions.Item label="合并(同PN仓库)">{report.merged_pn_warehouse}</Descriptions.Item>
            )}
          </Descriptions>
          {report.errors_preview?.length > 0 && (
            <Table
              style={{ marginTop: 16 }} rowKey="row_no" size="small"
              title={() => "异常行预览（前 10 条）"}
              columns={errCols} dataSource={report.errors_preview} pagination={false}
            />
          )}
        </Card>
      )}

      <Card title="导入历史">
        <Table rowKey="id" size="small" columns={batchCols} dataSource={batches} scroll={{ x: 1080 }} pagination={{ pageSize: 10 }} />
      </Card>

      <Modal
        open={!!detail} width={760} footer={null} onCancel={() => setDetail(null)}
        title={detail ? `批次 #${detail.id} · ${detail.filename}` : ""}
      >
        {detail && (
          <>
            <Descriptions bordered column={2} size="small" style={{ marginBottom: 12 }}>
              {Object.entries(detail.report || {}).filter(([k]) => k !== "errors_preview").map(([k, v]) => (
                <Descriptions.Item key={k} label={k}>{String(v)}</Descriptions.Item>
              ))}
            </Descriptions>
            <Table rowKey={(_, i) => String(i)} size="small" columns={errCols}
              dataSource={detail.errors || []} pagination={{ pageSize: 10 }}
              locale={{ emptyText: "无异常行" }} />
          </>
        )}
      </Modal>
    </Space>
  );
}
