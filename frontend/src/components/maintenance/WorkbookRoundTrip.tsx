import { useState } from "react";
import { Alert, Button, List, Modal, Space, Typography, Upload, message } from "antd";
import { DownloadOutlined, UploadOutlined } from "@ant-design/icons";
import type { UploadFile } from "antd/es/upload/interface";
import type { WillVoidRow, WorkbookApplyResult, WorkbookValidateResult } from "../../api/maintenanceWorkbooks";
import { saveBlob } from "../../api/maintenanceWorkbooks";

const { Text } = Typography;

/**
 * 「下载 → 改 → 上传覆盖」这一组动作的唯一复用件（REQUIREMENTS #38/#40）。
 *
 * 之所以把下载与上传绑在同一个组件里：口径是**在哪下载就在哪上传**。分开摆放
 * 迟早出现「在 A 处下载、到 B 处上传」，而两处的 sheet 集合并不相同。
 *
 * 总表 2.1（#265 契约三）：传入 onValidate 即启用两阶段回传——先预检，
 * 把「将被作废的行」逐条摆给用户确认后才真正落库；预检被拒（如行数骤减
 * 防呆）直接展示后端行级文案，绝不偷偷 apply。
 */
export interface WorkbookRoundTripProps {
  title: string;
  hint?: string;
  filename: string;
  onDownload: () => Promise<Blob>;
  onApply: (file: File) => Promise<WorkbookApplyResult>;
  /** 传入即启用「预检 → 作废预览 → 确认 → 落库」两阶段（项目总表 2.1）。 */
  onValidate?: (file: File) => Promise<WorkbookValidateResult>;
  /** 无上传动作键时只给下载（只读对账的人也该能把表拉下来核对）。 */
  canUpload: boolean;
  size?: "small" | "middle";
}

function describe(result: Partial<WorkbookApplyResult>): string {
  const parts: string[] = [];
  if (result.cost_refills || result.cost_overrides) {
    parts.push(`补价 ${result.cost_refills || result.cost_overrides} 行`);
  }
  if (result.line_creates) parts.push(`备件新增 ${result.line_creates} 行`);
  if (result.line_updates || result.qty_updates) {
    parts.push(`备件更新 ${result.line_updates || result.qty_updates} 行`);
  }
  if (result.line_voids) parts.push(`备件作废 ${result.line_voids} 行`);
  if (result.expense_creates) parts.push(`报销新增 ${result.expense_creates} 行`);
  if (result.expense_updates) parts.push(`报销更新 ${result.expense_updates} 行`);
  if (result.expense_voids) parts.push(`报销作废 ${result.expense_voids} 行`);
  if (result.collection_creates || result.collection_updates) {
    parts.push(`回款新增/覆盖 ${result.collection_creates || result.collection_updates} 条`);
  }
  if (result.collection_voids) parts.push(`回款作废 ${result.collection_voids} 条`);
  if (result.plan_creates) parts.push(`回款计划新增 ${result.plan_creates} 条`);
  if (result.plan_updates) parts.push(`回款计划更新 ${result.plan_updates} 条`);
  if (result.plan_voids) parts.push(`回款计划作废 ${result.plan_voids} 条`);
  if (result.site_return_flags) parts.push(`返还标记 ${result.site_return_flags} 行`);
  if (result.site_creates) parts.push(`领用新增 ${result.site_creates} 行`);
  if (result.site_updates) parts.push(`领用更新 ${result.site_updates} 行`);
  return parts.length ? parts.join("、") : "文件没有改动";
}

/** 作废预览的一行说人话：能定位到 sheet + 单号 + 原因即可，字段缺啥降级显示啥。 */
function describeVoidRow(row: WillVoidRow, index: number): string {
  const parts = [
    row.sheet ? String(row.sheet) : null,
    row.order_no ? `单号 ${String(row.order_no)}` : null,
    row.reason ? String(row.reason) : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : `第 ${index + 1} 行`;
}

export function WorkbookRoundTrip({
  title,
  hint,
  filename,
  onDownload,
  onApply,
  onValidate,
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

  const applyDirectly = async (file: File) => {
    const result = await onApply(file);
    message.success(describe(result) === "文件没有改动" ? "文件没有改动，未写入任何数据" : `已覆盖：${describe(result)}`);
  };

  /** 两阶段：预检 → 摆变更摘要和作废预览 → 用户确认才落库。 */
  const validateThenApply = async (file: File) => {
    const preview = await onValidate!(file);
    const voidRows = preview.will_void_rows ?? [];
    Modal.confirm({
      title: `确认回传${title}？`,
      width: 560,
      content: (
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Text>{describe(preview)}</Text>
          {voidRows.length ? (
            <Alert
              type="warning"
              showIcon
              message={`以下 ${voidRows.length} 行将被作废（不可撤销，需恢复请走需求单恢复）`}
              description={
                <List
                  size="small"
                  dataSource={voidRows}
                  renderItem={(row, index) => (
                    <List.Item style={{ padding: "4px 0" }}>
                      <Text style={{ fontSize: 12 }}>{describeVoidRow(row, index)}</Text>
                    </List.Item>
                  )}
                />
              }
            />
          ) : null}
        </Space>
      ),
      okText: "确认回传",
      cancelText: "取消",
      onOk: () => applyDirectly(file),
    });
  };

  const handleUpload = async (file: UploadFile) => {
    setApplying(true);
    try {
      if (onValidate) {
        await validateThenApply(file as unknown as File);
      } else {
        await applyDirectly(file as unknown as File);
      }
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
