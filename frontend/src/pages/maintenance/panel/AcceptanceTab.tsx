import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Modal,
  Space,
  Table,
  Tag,
  Typography,
  Upload,
  message,
} from "antd";
import { DownloadOutlined, UploadOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";

import {
  applyMaintenanceAcceptanceChecklist,
  downloadAcceptanceChecklistTemplate,
  getMaintenanceAcceptanceChecklist,
  previewMaintenanceAcceptanceChecklist,
  type MaintenanceAcceptanceChecklist,
  type MaintenanceAcceptanceChecklistPreview,
} from "../../../api/maintenanceOperations";
import { readError } from "./panelUtils";
import { readMaintenanceCapabilities } from "../../../components/maintenance/maintenancePermissions";
import MaintenanceAcceptancePanel from "../../../components/maintenance/MaintenanceAcceptancePanel";

const { Text } = Typography;

function idempotencyKey(): string {
  const suffix = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `acceptance-checklist-${suffix}`;
}

/** 是否完成三态：null=未识别（理论上 apply 已拦，兜底显示）。 */
function doneTag(done: boolean | null) {
  if (done === true) return <Tag color="green">已完成</Tag>;
  if (done === false) return <Tag color="orange">待验收</Tag>;
  return <Tag color="red">未识别</Tag>;
}

const itemColumns: ColumnsType<{
  item_id: string;
  row_no: number;
  requirement: string;
  done: boolean | null;
}> = [
  { title: "#", dataIndex: "row_no", width: 56 },
  { title: "验收需求", dataIndex: "requirement" },
  {
    title: "是否完成",
    dataIndex: "done",
    width: 110,
    render: (done: boolean | null) => doneTag(done),
  },
];

/**
 * 验收 tab（2026-08-21 客户反馈）：
 * 上半区「验收需求清单」——两列 Excel 导入（预览确认 → 整表替换，历史留档）；
 * 下半区复活既有验收交付面板（附件上传 + 提交审批流，原孤儿组件）。
 */
export function AcceptanceTab({
  projectId,
  canImport,
}: {
  projectId: string;
  canImport: boolean;
}) {
  const [data, setData] = useState<MaintenanceAcceptanceChecklist | null>(null);
  const [loading, setLoading] = useState(true);
  const [importing, setImporting] = useState(false);
  const generation = useRef(0);

  const load = async () => {
    const seq = ++generation.current;
    setLoading(true);
    try {
      const resp = await getMaintenanceAcceptanceChecklist(projectId);
      if (seq === generation.current) setData(resp.data);
    } catch (err) {
      message.error(readError(err, "验收清单加载失败"));
    } finally {
      if (seq === generation.current) setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const downloadTemplate = async () => {
    try {
      const resp = await downloadAcceptanceChecklistTemplate(projectId);
      const url = URL.createObjectURL(resp.data);
      const link = document.createElement("a");
      link.href = url;
      link.download = "验收需求清单模板.xlsx";
      link.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      message.error(readError(err, "模板下载失败"));
    }
  };

  const confirmApply = (preview: MaintenanceAcceptanceChecklistPreview) => {
    Modal.confirm({
      title: "应用验收清单",
      width: 560,
      content: (
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <Text>
            本次导入 {preview.item_rows} 条（已完成 {preview.done_rows}、
            待验收 {preview.todo_rows}）。
          </Text>
          {preview.will_replace_rows > 0 ? (
            <Alert
              type="warning"
              showIcon
              message={`将整表替换现有 ${preview.will_replace_rows} 条清单`}
              description="旧版本会保留在历史记录里，可随时重新导入恢复。"
            />
          ) : (
            <Text type="secondary">当前项目还没有生效清单，本次导入即为首版。</Text>
          )}
        </Space>
      ),
      okText: "替换生效",
      cancelText: "取消",
      onOk: async () => {
        setImporting(true);
        try {
          await applyMaintenanceAcceptanceChecklist(preview.batch_id);
          message.success(`验收清单已生效（${preview.item_rows} 条）`);
          await load();
        } catch (err) {
          message.error(readError(err, "应用失败"));
        } finally {
          setImporting(false);
        }
      },
    });
  };

  const beforeUpload = async (file: File) => {
    setImporting(true);
    try {
      const resp = await previewMaintenanceAcceptanceChecklist(projectId, {
        file,
        idempotencyKey: idempotencyKey(),
      });
      const preview = resp.data;
      if (preview.issue_rows > 0) {
        Modal.error({
          title: `清单有 ${preview.issue_rows} 个问题行，未导入`,
          width: 560,
          content: (
            <ul style={{ paddingLeft: 18, margin: 0 }}>
              {preview.issues.map((issue) => (
                <li key={issue}><Text type="danger">{issue}</Text></li>
              ))}
            </ul>
          ),
        });
      } else {
        confirmApply(preview);
      }
    } catch (err) {
      message.error(readError(err, "清单解析失败"));
    } finally {
      setImporting(false);
    }
    return false; // 阻止 antd 自动上传
  };

  const current = data?.current ?? null;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card
        size="small"
        title="验收需求清单"
        extra={
          <Space>
            <Button
              size="small"
              icon={<DownloadOutlined />}
              onClick={downloadTemplate}
            >
              下载模板
            </Button>
            {canImport ? (
              <Upload
                accept=".xlsx"
                maxCount={1}
                showUploadList={false}
                beforeUpload={beforeUpload}
                disabled={importing}
              >
                <Button
                  size="small"
                  type="primary"
                  icon={<UploadOutlined />}
                  loading={importing}
                >
                  导入清单
                </Button>
              </Upload>
            ) : (
              <Text type="secondary">导入需授权（验收清单 Excel 导入）</Text>
            )}
          </Space>
        }
      >
        {current ? (
          <>
            <Space size={16} wrap style={{ marginBottom: 12 }}>
              <Text type="secondary">
                当前版本：{current.filename} · {current.applied_at ?? "—"} 由{" "}
                {current.applied_by} 应用
              </Text>
              <Tag color="green">已完成 {current.done_rows}</Tag>
              <Tag color="orange">待验收 {current.todo_rows}</Tag>
            </Space>
            <Table
              size="small"
              rowKey="item_id"
              columns={itemColumns}
              dataSource={current.items}
              pagination={current.items.length > 50 ? { pageSize: 50 } : false}
            />
          </>
        ) : (
          <Text type="secondary">
            {loading ? "清单加载中…" : "尚未导入验收清单——下载模板、填好「验收需求 / 是否完成」两列后导入。"}
          </Text>
        )}
        {data && data.history.length > 1 ? (
          <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
            历史版本 {data.history.length - 1} 个（最早{" "}
            {data.history[data.history.length - 1].applied_at ?? "—"}），如需回滚请重新导入对应文件。
          </Text>
        ) : null}
      </Card>

      {/* 验收交付（附件+审批）是 Beta 总闸后面的既有模块：没开 Beta 的账号
          不渲染，避免稳定版面板出现一块永远报错的区块。 */}
      {readMaintenanceCapabilities().canUseBeta ? (
        <MaintenanceAcceptancePanel projectId={projectId} />
      ) : null}
    </Space>
  );
}

export default AcceptanceTab;
