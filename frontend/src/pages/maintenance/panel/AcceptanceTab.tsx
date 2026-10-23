import { useEffect, useRef, useState } from "react";
import { App, Alert, Button, Card, Modal, Space, Table, Tag, Typography, Upload } from "antd";
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
import MaintenanceAcceptancePanel from "../../../components/maintenance/MaintenanceAcceptancePanel";
import { saveBlob } from "../../../api/maintenanceWorkbooks";

const { Text } = Typography;

function idempotencyKey(): string {
  const suffix = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
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
 * 下半区复活既有验收交付面板（附件上传 + 提交即生效免审批，原孤儿组件）。
 */
export function AcceptanceTab({
  projectId,
  canImport,
  onChanged,
}: {
  projectId: string;
  canImport: boolean;
  onChanged?: () => Promise<boolean>;
}) {
  const { message } = App.useApp();
  const [data, setData] = useState<MaintenanceAcceptanceChecklist | null>(null);
  const [loading, setLoading] = useState(true);
  const [importing, setImporting] = useState(false);
  const [feedback, setFeedback] = useState<{ type: "error" | "warning"; text: string } | null>(null);
  const [issues, setIssues] = useState<string[]>([]);
  const [uploadInputVersion, setUploadInputVersion] = useState(0);
  const generation = useRef(0);
  const mounted = useRef(true);
  const currentProject = useRef(projectId);
  currentProject.current = projectId;
  const scopeEpoch = useRef(0);
  const uploadEpoch = useRef(0);
  const pendingConfirmation = useRef<{ close: () => void } | null>(null);

  // 读请求、预检、确认、写后读回都属于发起时的项目；新上传还会取代旧上传。
  const scopeGuard = () => {
    const epoch = scopeEpoch.current;
    return () => mounted.current && currentProject.current === projectId
      && scopeEpoch.current === epoch;
  };

  const load = async (operationCurrent = scopeGuard()) => {
    if (!operationCurrent()) return false;
    const seq = ++generation.current;
    const inScope = scopeGuard();
    const isCurrentRead = () => inScope() && seq === generation.current;
    const isCurrent = () => operationCurrent() && isCurrentRead();
    // 成功写后的读回可能被新操作取代；旧清单先失效，不能继续冒充当前事实。
    setData(null);
    setLoading(true);
    setFeedback(null);
    setIssues([]);
    try {
      const resp = await getMaintenanceAcceptanceChecklist(projectId);
      if (isCurrent()) setData(resp.data);
      return isCurrent();
    } catch (err) {
      if (isCurrent()) {
        setData(null);
        setFeedback({ type: "error", text: readError(err, "验收清单加载失败") });
      }
      return false;
    } finally {
      if (isCurrentRead()) setLoading(false);
    }
  };

  useEffect(() => {
    mounted.current = true;
    setData(null);
    setImporting(false);
    void load();
    return () => {
      mounted.current = false;
      scopeEpoch.current += 1;
      uploadEpoch.current += 1;
      generation.current += 1;
      pendingConfirmation.current?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const downloadTemplate = async () => {
    const isCurrent = scopeGuard();
    setFeedback(null);
    setIssues([]);
    try {
      const resp = await downloadAcceptanceChecklistTemplate(projectId);
      if (isCurrent()) saveBlob(resp.data, "验收需求清单模板.xlsx");
    } catch (err) {
      if (!isCurrent()) return;
      setFeedback({ type: "error", text: readError(err, "模板下载失败") });
    }
  };

  const confirmApply = (preview: MaintenanceAcceptanceChecklistPreview, isCurrent: () => boolean) =>
    new Promise<void>((resolve) => {
      if (!isCurrent()) { resolve(); return; }
      let handle: ReturnType<typeof Modal.confirm>;
      const confirmation = { close: () => { handle.destroy(); finish(); } };
      const finish = () => {
        if (pendingConfirmation.current === confirmation) pendingConfirmation.current = null;
        resolve();
      };
      handle = Modal.confirm({
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
      onCancel: finish,
      onOk: async () => {
        if (!isCurrent()) { finish(); return; }
        setImporting(true);
        let committed = false;
        try {
          await applyMaintenanceAcceptanceChecklist(preview.batch_id);
          committed = true;
          if (!isCurrent()) return;
          const localReady = await load(isCurrent);
          if (!isCurrent()) return;
          let parentReady = true;
          try {
            parentReady = onChanged ? (await onChanged()) !== false : true;
          } catch {
            parentReady = false;
          }
          if (!isCurrent()) return;
          if (localReady && parentReady) {
            message.success(`验收清单已生效并刷新（${preview.item_rows} 条）`);
          } else {
            setFeedback({ type: "warning", text: "验收清单已生效，但页面刷新失败；旧数据已失效，请重试。" });
          }
        } catch (err) {
          if (!isCurrent()) return;
          if (committed) {
            setFeedback({ type: "warning", text: "验收清单已生效，但页面刷新失败；旧数据已失效，请重试。" });
            return;
          }
          setFeedback({ type: "error", text: readError(err, "应用失败，系统未写入") });
        } finally {
          if (isCurrent()) setImporting(false);
          finish();
        }
      },
      });
      pendingConfirmation.current = confirmation;
    });

  const beforeUpload = async (file: File) => {
    const inScope = scopeGuard();
    const epoch = ++uploadEpoch.current;
    const isCurrent = () => inScope() && epoch === uploadEpoch.current;
    pendingConfirmation.current?.close();
    setImporting(true);
    setFeedback(null);
    setIssues([]);
    try {
      const resp = await previewMaintenanceAcceptanceChecklist(projectId, {
        file,
        idempotencyKey: idempotencyKey(),
      });
      if (!isCurrent()) return false;
      const preview = resp.data;
      if (preview.issue_rows > 0) {
        setFeedback({ type: "error", text: `清单有 ${preview.issue_rows} 个问题行，未导入` });
        setIssues(preview.issues);
      } else {
        await confirmApply(preview, isCurrent);
      }
    } catch (err) {
      if (isCurrent()) setFeedback({ type: "error", text: readError(err, "清单解析失败") });
    } finally {
      if (isCurrent()) {
        setImporting(false);
        // 浏览器不会为同一个 file input 连续触发同文件的 change；预检失败或
        // 用户取消确认后重建 Upload，避免“再点同一文件没有反应”。
        setUploadInputVersion((current) => current + 1);
      }
    }
    return false; // 阻止 antd 自动上传
  };

  const current = data?.current ?? null;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {feedback ? <Alert type={feedback.type} showIcon message={feedback.text}
        description={issues.length ? <ul style={{ paddingLeft: 18, margin: 0 }}>{issues.map((issue, index) => <li key={index}>{issue}</li>)}</ul> : undefined} /> : null}
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
                key={`acceptance-checklist-upload-${uploadInputVersion}`}
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
        {/* 2026-08-25 客户口径：清单与下方交付区同屏但不联动，避免误解。 */}
        <Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 8 }}>
          清单仅作验收需求记录，与下方验收报告提交状态无关。
        </Text>
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

      {/* 验收交付（附件+提交即生效）2026-08-24 起随维保页面开放：
          销售/项目经理/维保负责人都能查看，按钮层再按提交权限收敛。 */}
      <MaintenanceAcceptancePanel projectId={projectId} onChanged={onChanged} />
    </Space>
  );
}

export default AcceptanceTab;
