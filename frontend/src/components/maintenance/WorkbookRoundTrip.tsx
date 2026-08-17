import { useState } from "react";
import { Button, Space, Typography, Upload, message } from "antd";
import { DownloadOutlined, UploadOutlined } from "@ant-design/icons";
import type { UploadFile } from "antd/es/upload/interface";
import type { WorkbookApplyResult } from "../../api/maintenanceWorkbooks";
import { saveBlob } from "../../api/maintenanceWorkbooks";

const { Text } = Typography;

/**
 * 「下载 → 改 → 上传覆盖」这一组动作的唯一复用件（REQUIREMENTS #38/#40）。
 *
 * 之所以把下载与上传绑在同一个组件里：口径是**在哪下载就在哪上传**。分开摆放
 * 迟早出现「在 A 处下载、到 B 处上传」，而两处的 sheet 集合并不相同。
 */
export interface WorkbookRoundTripProps {
  title: string;
  hint?: string;
  filename: string;
  onDownload: () => Promise<Blob>;
  onApply: (file: File) => Promise<WorkbookApplyResult>;
  /** 无上传动作键时只给下载（只读对账的人也该能把表拉下来核对）。 */
  canUpload: boolean;
  size?: "small" | "middle";
}

function describe(result: WorkbookApplyResult): string {
  const parts: string[] = [];
  if (result.cost_refills) parts.push(`补价 ${result.cost_refills} 行`);
  if (result.expense_updates) parts.push(`报销 ${result.expense_updates} 行`);
  if (result.collection_creates) parts.push(`回款新增/覆盖 ${result.collection_creates} 条`);
  if (result.collection_voids) parts.push(`回款作废 ${result.collection_voids} 条`);
  if (result.site_return_flags) parts.push(`返还标记 ${result.site_return_flags} 行`);
  return parts.length ? `已覆盖：${parts.join("、")}` : "文件没有改动，未写入任何数据";
}

export function WorkbookRoundTrip({
  title,
  hint,
  filename,
  onDownload,
  onApply,
  canUpload,
  size = "middle",
}: WorkbookRoundTripProps) {
  const [downloading, setDownloading] = useState(false);
  const [applying, setApplying] = useState(false);

  const handleDownload = async () => {
    setDownloading(true);
    try {
      saveBlob(await onDownload(), filename);
    } catch (error) {
      message.error(readError(error, "下载失败"));
    } finally {
      setDownloading(false);
    }
  };

  const handleUpload = async (file: UploadFile) => {
    setApplying(true);
    try {
      const result = await onApply(file as unknown as File);
      message.success(describe(result));
    } catch (error) {
      // 后端整份拒绝时把原文 message 显示出来：告诉用户**哪一行**不合法，
      // 而不是一句「上传失败」——这份表是人工编辑的，定位靠这句话。
      message.error(readError(error, "上传失败"));
    } finally {
      setApplying(false);
    }
    return false;
  };

  return (
    <Space direction="vertical" size={2}>
      <Space size={8}>
        <Button
          size={size}
          icon={<DownloadOutlined />}
          loading={downloading}
          onClick={handleDownload}
        >
          下载{title}
        </Button>
        {canUpload ? (
          <Upload
            accept=".xlsx"
            maxCount={1}
            showUploadList={false}
            beforeUpload={(file) => handleUpload(file as unknown as UploadFile)}
          >
            <Button size={size} icon={<UploadOutlined />} loading={applying}>
              上传覆盖
            </Button>
          </Upload>
        ) : null}
      </Space>
      {hint ? (
        <Text type="secondary" style={{ fontSize: 11.5 }}>
          {hint}
        </Text>
      ) : null}
    </Space>
  );
}

function readError(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response
    ?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    return String((detail as { message: unknown }).message);
  }
  return fallback;
}

export default WorkbookRoundTrip;
