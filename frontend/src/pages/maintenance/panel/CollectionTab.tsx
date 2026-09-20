import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Form, Input, InputNumber, Modal, Select, Space, Table, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { MaintenanceCollectionSnapshotRow } from "../../../api/maintenanceOperations";
import {
  createProjectCollection,
  patchProjectCollection,
} from "../../../api/maintenanceOperations";
import {
  SHEETS,
  applyProjectMaster,
  downloadProjectMaster,
  getCollectionPlan,
  validateProjectMaster,
} from "../../../api/maintenanceWorkbooks";
import type { CollectionPlanRow } from "../../../api/maintenanceWorkbooks";
import WorkbookRoundTrip from "../../../components/maintenance/WorkbookRoundTrip";
import PanelActionBar from "./PanelActionBar";
import { readPermissionMap } from "../../../nav";
import {
  COLLECTION_STATUS,
  type RegisterPanelRefresh,
  raw,
  readError,
} from "./panelUtils";

const { Text } = Typography;

interface CollectionFormValues {
  project_contract_id?: string;
  report_month: string;
  cumulative_amount?: number;
  status?: "confirmed" | "unconfirmed";
  receipt_reference?: string | null;
  remark?: string | null;
  reason?: string;
}

/** 报告月份输入 YYYY-MM → 当月首日 ISO（后端要求 day==1）。 */
function normalizeMonth(value: string): string | null {
  const match = /^(\d{4})-(\d{2})$/.exec(value.trim());
  if (!match) return null;
  const month = Number(match[2]);
  if (month < 1 || month > 12) return null;
  return `${match[1]}-${match[2]}-01`;
}

/** 到款状态：应回未回一眼可见（用户 2026-08-20：计划填了但页面不显示状态）。 */
const ARRIVAL_STATUS: Record<string, { label: string; color: string }> = {
  paid: { label: "已到款", color: "green" },
  partial: { label: "部分到款", color: "orange" },
  pending: { label: "待回款", color: "blue" },
  overdue: { label: "逾期未回款", color: "red" },
};

/**
 * 回款 tab：每条快照的确认状态表。累计回款/进度已上页面健康带（2026-08-19 重设计），
 * 数据由面板页统一取 workspace 后传入，本 tab 不再重复请求。
 */
export function CollectionTab({
  projectId,
  exportBase,
  canUpload,
  rows,
  loading,
  onRefresh,
  registerRefresh,
}: {
  projectId: string;
  exportBase: string;
  canUpload: boolean;
  rows: MaintenanceCollectionSnapshotRow[];
  loading: boolean;
  /** 上传覆盖后回读（含健康带指标 + 计划状态）。 */
  onRefresh: () => Promise<boolean>;
  registerRefresh: RegisterPanelRefresh;
}) {
  const [planRows, setPlanRows] = useState<CollectionPlanRow[]>([]);
  const requestSeq = useRef(0);

  // ---- v1.36：实收回款页面 CRUD（后端 snapshot POST/PATCH，OCC + reason 留痕） ----
  const perms = readPermissionMap();
  const canManageCollections = !!perms.action_maintenance_roundtrip_apply
    && !!perms.data_profit;
  const [form] = Form.useForm<CollectionFormValues>();
  const [editing, setEditing] = useState<MaintenanceCollectionSnapshotRow | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const loadPlan = useCallback(async () => {
    const seq = ++requestSeq.current;
    try {
      const resp = await getCollectionPlan(projectId);
      if (seq !== requestSeq.current) return false;
      setPlanRows(resp.rows);
      return true;
    } catch (err) {
      if (seq === requestSeq.current) {
        setPlanRows([]);
        message.error(readError(err, "回款计划加载失败"));
      }
      return false;
    }
  }, [projectId]);

  useEffect(() => {
    registerRefresh("collection", loadPlan);
    void loadPlan();
    return () => {
      requestSeq.current += 1;
      registerRefresh("collection", null);
    };
  }, [loadPlan, registerRefresh]);

  const openCreate = () => {
    setSubmitError(null);
    setEditing(null);
    form.resetFields();
    setCreateOpen(true);
  };

  const openEdit = (row: MaintenanceCollectionSnapshotRow) => {
    setSubmitError(null);
    setEditing(row);
    form.setFieldsValue({
      report_month: row.report_month.slice(0, 7),
      cumulative_amount: row.cumulative_amount == null ? undefined : Number(row.cumulative_amount),
      status: row.status === "void" ? "unconfirmed" : (row.status as "confirmed" | "unconfirmed"),
      receipt_reference: row.receipt_reference ?? "",
      remark: row.remark ?? "",
      reason: undefined,
    });
    setCreateOpen(true);
  };

  const submitCollection = async () => {
    const values = await form.validateFields().catch(() => null);
    if (!values) return;
    const month = normalizeMonth(values.report_month);
    if (!month) {
      setSubmitError("报告月份格式须为 YYYY-MM");
      return;
    }
    if (!values.reason?.trim()) {
      setSubmitError(editing ? "修改原因必填" : "登记原因必填");
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    try {
      if (editing) {
        await patchProjectCollection(editing.collection_id, {
          version: editing.version,
          reason: values.reason.trim(),
          report_month: month,
          cumulative_amount: values.cumulative_amount,
          status: values.status,
          receipt_reference: values.receipt_reference?.trim() || null,
          remark: values.remark?.trim() || null,
        });
        message.success("回款记录已修改");
      } else {
        if (!values.project_contract_id?.trim()) {
          setSubmitError("请填写合同身份（project_contract_id）");
          return;
        }
        await createProjectCollection(projectId, {
          project_contract_id: values.project_contract_id.trim(),
          report_month: month,
          cumulative_amount: values.cumulative_amount ?? 0,
          status: values.status ?? "unconfirmed",
          receipt_reference: values.receipt_reference?.trim() || null,
          remark: values.remark?.trim() || null,
          reason: values.reason.trim(),
        });
        message.success("回款记录已登记");
      }
      setCreateOpen(false);
      await loadPlan();
      await onRefresh();
    } catch (err) {
      setSubmitError(readError(err, editing
        ? "修改失败。版本冲突时页面刷新后重试；确认月份金额是否违反单调校验。"
        : "登记失败：请核对合同身份、月份（须当月首日）与金额。"));
    } finally {
      setSubmitting(false);
    }
  };

  const voidCollection = async (row: MaintenanceCollectionSnapshotRow) => {
    Modal.confirm({
      title: `作废回款记录（${row.report_month.slice(0, 7)}）`,
      content: "软作废：退出累计与进度统计，历史与审计保留。",
      okText: "确认作废",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          await patchProjectCollection(row.collection_id, {
            version: row.version,
            reason: "页面作废",
            status: "void",
          });
          message.success("回款记录已作废");
          await loadPlan();
          await onRefresh();
        } catch (err) {
          message.error(readError(err, "作废失败：版本冲突时请刷新后重试"));
        }
      },
    });
  };

  const planColumns: ColumnsType<CollectionPlanRow> = [
    { title: "合同编号", dataIndex: "contract_no", render: raw },
    { title: "期次", dataIndex: "sequence", width: 70 },
    { title: "计划回款日期", dataIndex: "planned_date", render: raw },
    {
      title: "计划金额（含税）",
      dataIndex: "planned_amount",
      render: (value) => value == null ? "—" : `¥${Number(value).toFixed(2)}`,
    },
    {
      title: "累计计划",
      dataIndex: "cumulative_planned",
      render: (value) => `¥${Number(value || 0).toFixed(2)}`,
    },
    {
      title: "累计实收",
      dataIndex: "cumulative_actual",
      render: (value) => `¥${Number(value || 0).toFixed(2)}`,
    },
    {
      title: "到款状态",
      dataIndex: "arrival_state",
      width: 120,
      render: (value: string) => {
        const status = ARRIVAL_STATUS[value];
        return <Tag color={status?.color}>{status?.label ?? raw(value)}</Tag>;
      },
    },
    { title: "备注", dataIndex: "note", render: raw },
  ];

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <PanelActionBar
        workbook={(
          <WorkbookRoundTrip
            size="small"
            title="回款"
            filename={`${exportBase}-${SHEETS.collection}.xlsx`}
            canUpload={canUpload}
            onDownload={() => downloadProjectMaster(projectId, [SHEETS.collection])}
            onValidate={(file) => validateProjectMaster(projectId, file)}
            onApply={(file, opts) => applyProjectMaster(projectId, file, opts)}
            onAfterApply={onRefresh}
          />
        )}
        actions={canManageCollections ? (
          <Button type="primary" size="small" onClick={openCreate}>登记回款</Button>
        ) : undefined}
        hint="Excel 在左（在哪下载就在哪上传，可回填累计实收/状态/凭证号/备注）；单条登记/修改在下方记录表操作列"
      />
      <Card
        size="small"
        title="回款计划（应回未回在这里看）"
        extra={<Text type="secondary" style={{ fontSize: 12 }}>在总表 02_回款计划 里填写/维护；实收在 05 填写后状态自动更新</Text>}
      >
        <Table<CollectionPlanRow>
          rowKey="milestone_id"
          size="small"
          dataSource={planRows}
          columns={planColumns}
          pagination={false}
          locale={{ emptyText: "暂无回款计划——在总表 02_回款计划 填写后会显示在这里" }}
        />
      </Card>
      <Card size="small" title="实收回款记录（05）">
        <Table<MaintenanceCollectionSnapshotRow>
          rowKey="collection_id"
        size="small"
        loading={loading}
        dataSource={rows}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        locale={{ emptyText: "本项目暂无回款记录" }}
          columns={[
            { title: "合同编号", dataIndex: "contract_no", render: raw },
            {
              title: "报告月份",
              dataIndex: "report_month",
              render: (value: string) => {
                const text = raw(value);
                // 未来月份照常显示但打标（2026-08-21：此前静默隐藏导致页面空白）
                const month = value ? value.slice(0, 7) : "";
                const now = new Date();
                const isFuture = month > `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
                return isFuture ? <Tag color="gold">{text}（未来月份）</Tag> : text;
              },
            },
            {
              title: "累计实收金额（含税）",
              dataIndex: "cumulative_amount",
              render: (value) => value == null ? "—" : `¥${Number(value).toFixed(2)}`,
            },
            {
              title: "回款状态",
              dataIndex: "status",
              render: (value: string) => {
                const status = COLLECTION_STATUS[value];
                return <Tag color={status?.color}>{status?.label ?? raw(value)}</Tag>;
              },
            },
            { title: "回款凭证号", dataIndex: "receipt_reference", render: raw },
            { title: "备注", dataIndex: "remark", render: raw },
            ...(canManageCollections ? [{
              title: "操作",
              key: "ops",
              width: 130,
              render: (_v: unknown, row: MaintenanceCollectionSnapshotRow) => (
                row.status === "void" ? <Tag>已作废</Tag> : (
                  <Space size={4}>
                    <Button size="small" onClick={() => openEdit(row)}>修改</Button>
                    <Button size="small" danger onClick={() => { void voidCollection(row); }}>作废</Button>
                  </Space>
                )
              ),
            }] : []),
          ]}
        />
      </Card>

      <Modal
        open={createOpen}
        title={editing ? `修改回款记录（${editing.report_month.slice(0, 7)}，版本 ${editing.version}）` : "登记回款"}
        confirmLoading={submitting}
        okText={editing ? "保存修改" : "登记"}
        cancelText="取消"
        onCancel={() => { if (!submitting) setCreateOpen(false); }}
        onOk={() => { void submitCollection(); }}
        maskClosable={!submitting}
      >
        {submitError ? <Alert type="error" showIcon message={submitError} style={{ marginBottom: 12 }} /> : null}
        <Form form={form} layout="vertical" disabled={submitting}>
          {!editing ? (
            <Form.Item
              name="project_contract_id"
              label="项目合同身份（project_contract_id）"
              rules={[{ required: true, message: "必填——可从回款计划表的期次信息中获取" }]}
            >
              <Input placeholder="36 位合同身份" maxLength={36} />
            </Form.Item>
          ) : null}
          <Form.Item
            name="report_month"
            label="报告月份（YYYY-MM）"
            rules={[{ required: true, message: "格式 YYYY-MM" }]}
          >
            <Input placeholder="2026-09" style={{ width: 160 }} />
          </Form.Item>
          <Space size={12}>
            <Form.Item
              name="cumulative_amount"
              label="累计实收金额（含税）"
              rules={[{ required: true, message: "金额必填" }]}
            >
              <InputNumber min={0} precision={2} style={{ width: 160 }} placeholder="0.00" />
            </Form.Item>
            <Form.Item name="status" label="回款状态" initialValue="unconfirmed">
              <Select
                style={{ width: 140 }}
                options={[
                  { value: "confirmed", label: "已确认" },
                  { value: "unconfirmed", label: "未确认" },
                ]}
              />
            </Form.Item>
          </Space>
          <Form.Item name="receipt_reference" label="回款凭证号（可选）">
            <Input maxLength={128} placeholder="银行回单号 / 收款凭证" />
          </Form.Item>
          <Form.Item name="remark" label="备注（可选）">
            <Input.TextArea rows={2} maxLength={32767} />
          </Form.Item>
          <Form.Item
            name="reason"
            label={editing ? "修改原因（必填，留痕审计）" : "登记原因（必填）"}
            rules={[{ required: true, message: "必须填写原因" }]}
          >
            <Input.TextArea rows={2} maxLength={1000} placeholder="如：补录 9 月银行回款" />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  );
}

export default CollectionTab;
