import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Space,
  Typography,
  Upload,
  message,
} from "antd";
import { InboxOutlined } from "@ant-design/icons";
import {
  getBoardHealth,
  type BoardHealth,
} from "../../../api/maintenanceBossBoard";
import {
  getWbddLatest,
  relinkDocProjects,
  uploadWbdd,
  type WbddImportReport,
  type WbddLatest,
} from "../../../api/maintenanceWbddImport";
import SourceHealthBar from "../../../components/maintenance/boss/SourceHealthBar";

const { Title, Text, Paragraph } = Typography;

function errorText(error: unknown): string {
  const payload = error as {
    response?: { data?: { detail?: { message?: string; code?: string } | string } };
  };
  const detail = payload.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  return "上传失败，请重试";
}

/**
 * 上传台（plan v1.3 §5.1）。
 *
 * WBDD 走专用端点（maintenance-only，不复用通用导入全家桶——铁律 6）；
 * 上传后返回精确对账报告（计数/快照差异/成本重算统计）。
 * 三单（发货/返库/入库）的导入仍在维保工作台既有入口，本页只展示其来源健康，
 * 并提供「重新关联项目」（上传顺序无关，M4-3）。
 */
export default function BossUploadConsolePage() {
  const [health, setHealth] = useState<BoardHealth | null>(null);
  const [latest, setLatest] = useState<WbddLatest | null>(null);
  const [report, setReport] = useState<WbddImportReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = () => {
    getBoardHealth().then((resp) => setHealth(resp.data)).catch(() => undefined);
    getWbddLatest().then((resp) => setLatest(resp.data)).catch(() => undefined);
  };

  useEffect(refresh, []);

  const handleUpload = async (file: File) => {
    setBusy(true);
    setError(null);
    try {
      const key = `wbdd-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
      const resp = await uploadWbdd(file, key);
      setReport(resp.data);
      message.success(
        resp.data.replayed ? "该文件已导入过，返回原对账报告" : "导入完成",
      );
      refresh();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
    return false;
  };

  const handleRelink = async () => {
    setBusy(true);
    try {
      const resp = await relinkDocProjects();
      message.success(
        `已重新关联 ${resp.data.relinked} 条；仍未关联 ${resp.data.still_unlinked} 条`,
      );
      refresh();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Title level={4} style={{ margin: 0 }}>
        维保数据上传
      </Title>
      <SourceHealthBar health={health} />

      <Card size="small" title="维保备件需求单（WBDD）">
        <Paragraph type="secondary" style={{ fontSize: 12 }}>
          支持氚云导出的 91 列（当前年度版）与 90 列（历史版）两种布局，按业务名定位列。
          只接受维保备件需求单；采购/销售/库存/报销文件会被拒绝且不写入任何数据。
        </Paragraph>
        <Upload.Dragger
          accept=".xlsx"
          multiple={false}
          showUploadList={false}
          disabled={busy}
          beforeUpload={handleUpload}
        >
          <p className="ant-upload-drag-icon">
            <InboxOutlined />
          </p>
          <p className="ant-upload-text">点击或拖拽 .xlsx 到此处上传</p>
        </Upload.Dragger>
        {latest ? (
          <Descriptions size="small" column={2} style={{ marginTop: 12 }}>
            <Descriptions.Item label="当前状态">
              {latest.readiness === "not_imported" ? "尚未导入" : "已接入"}
            </Descriptions.Item>
            <Descriptions.Item label="数据截至">{latest.as_of ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="需求单总数">
              {latest.orders_total ?? "—"}
            </Descriptions.Item>
            <Descriptions.Item label="最近布局">{latest.layout ?? "—"}</Descriptions.Item>
          </Descriptions>
        ) : null}
      </Card>

      {error ? <Alert type="error" showIcon message={error} /> : null}

      {report ? (
        <Card size="small" title="本次对账报告" data-testid="wbdd-report">
          <Descriptions size="small" column={2}>
            <Descriptions.Item label="布局">{report.layout} 列</Descriptions.Item>
            <Descriptions.Item label="批次">{report.batch_id}</Descriptions.Item>
            <Descriptions.Item label="单头 新增/更新">
              {report.orders_inserted ?? 0} / {report.orders_updated ?? 0}
            </Descriptions.Item>
            <Descriptions.Item label="明细 新增/更新">
              {report.fact_rows_inserted ?? 0} / {report.fact_rows_updated ?? 0}
            </Descriptions.Item>
            <Descriptions.Item label="无明细单头">
              {report.headless_orders ?? 0}
            </Descriptions.Item>
            <Descriptions.Item label="错误行">
              {report.fact_rows_error ?? 0}
            </Descriptions.Item>
            <Descriptions.Item label="本批未出现的历史单" span={2}>
              {report.snapshot_diff?.missing_orders ?? 0}
              <Text type="secondary" style={{ fontSize: 11.5 }}>
                （只报告，不删除、不隐藏）
              </Text>
            </Descriptions.Item>
          </Descriptions>
        </Card>
      ) : null}

      <Card size="small" title="单据关联维护">
        <Paragraph type="secondary" style={{ fontSize: 12 }}>
          发货单/返库单/入库单若在需求单或项目归属确认之前上传，其项目关联会暂缺；
          确认归属后系统会自动补齐，也可在此手动触发一次。
        </Paragraph>
        <Button onClick={handleRelink} loading={busy}>
          重新关联项目
        </Button>
      </Card>
    </Space>
  );
}
