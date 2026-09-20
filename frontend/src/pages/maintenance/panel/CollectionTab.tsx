import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Form, Input, InputNumber, Modal, Select, Space, Table, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import type {
  MaintenanceCollectionSnapshotRow,
  MaintenanceContractSummary,
} from "../../../api/maintenanceOperations";
import {
  createProjectCollection,
  getMaintenanceProjectWorkspace,
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

/** 对话框用途：登记 / 修改 / 作废 / 恢复共用一个 Modal，字段按模式裁剪。 */
type DialogMode = "create" | "edit" | "void" | "restore";

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

/** 合同下拉标签：只展示编号与有效性，金额等敏感字段一律不取用。 */
function contractOptionLabel(contract: MaintenanceContractSummary): string {
  const no = contract.contract_no?.trim() || "（无合同编号）";
  return contract.is_effective ? no : `${no} · 已失效`;
}

/**
 * 回款 tab：每条快照的确认状态表。累计回款/进度已上页面健康带（2026-08-19 重设计），
 * 数据由面板页统一取 workspace 后传入，本 tab 不再重复请求。
 *
 * 页面 CRUD（v1.36）：登记时合同从当前项目 workspace 的合同关系里选择（不再手输
 * 内部 UUID）；作废/恢复走同一 PATCH + version(OCC) + reason 审计协议，恢复的安全
 * 默认是「待确认」——不直接回「已确认」，绕过单调性需要人工再走一次修改。
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
  const [mode, setMode] = useState<DialogMode>("create");
  const [createOpen, setCreateOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  /** 对话框打开时锁定的项目：提交前核对，防止快速切项目把旧表单写给新项目。 */
  const modalProjectRef = useRef(projectId);
  /**
   * 对话框世代：打开新对话框、取消、切换项目都推进。提交在第一个 await 前捕获
   * 世代，此后任何 await 点（validate/写请求/两次读回）返回后都先比较再动状态——
   * 旧提交的迟到响应既不能关掉新项目的表单、不能用旧项目的 loadPlan/onRefresh
   * 污染新视图，其 finally 也不许复位新表单的 submitting。
   */
  const dialogEpoch = useRef(0);

  // ---- 登记时的合同候选：按需读 workspace.project.contracts（页面同源权限） ----
  const [contracts, setContracts] = useState<{
    projectId: string;
    rows: MaintenanceContractSummary[];
  } | null>(null);
  const [contractsState, setContractsState] = useState<"idle" | "loading" | "done" | "error">("idle");
  const contractsEpoch = useRef(0);

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

  const loadContracts = useCallback(async () => {
    const epoch = ++contractsEpoch.current;
    setContractsState("loading");
    try {
      const resp = await getMaintenanceProjectWorkspace(projectId, {
        collection_page_size: 1,
        requisition_page_size: 1,
        expense_page_size: 1,
      });
      // 过期响应（期间切了项目/重试了）直接丢弃，不写回状态。
      if (epoch !== contractsEpoch.current) return;
      setContracts({
        projectId,
        rows: Array.isArray(resp.data?.project?.contracts)
          ? resp.data.project.contracts
          : [],
      });
      setContractsState("done");
    } catch {
      if (epoch === contractsEpoch.current) setContractsState("error");
    }
  }, [projectId]);

  useEffect(() => {
    // 快速切项目：作废旧世代（对话框 + 提交副作用 + 合同加载全部失效）。
    // 旧表单绝不提交给新项目，旧提交的迟到响应也不许碰新项目的 UI 状态。
    contractsEpoch.current += 1;
    dialogEpoch.current += 1;
    modalProjectRef.current = projectId;
    setContracts(null);
    setContractsState("idle");
    setCreateOpen(false);
    setEditing(null);
    setMode("create");
    setSubmitError(null);
    setSubmitting(false);
    form.resetFields();
  }, [projectId, form]);

  const contractRows = contracts?.projectId === projectId ? contracts.rows : [];

  /** 每次打开新对话框都开新世代并复位提交态：旧提交的 finally 被 epoch 挡住，不复位新表单。 */
  const beginDialog = () => {
    dialogEpoch.current += 1;
    modalProjectRef.current = projectId;
    setSubmitting(false);
  };

  const openCreate = () => {
    beginDialog();
    setSubmitError(null);
    setEditing(null);
    setMode("create");
    form.resetFields();
    setCreateOpen(true);
    if (contracts?.projectId !== projectId || contractsState === "error") {
      void loadContracts();
    }
  };

  const openEdit = (row: MaintenanceCollectionSnapshotRow) => {
    beginDialog();
    setSubmitError(null);
    setEditing(row);
    setMode("edit");
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

  const openVoid = (row: MaintenanceCollectionSnapshotRow) => {
    beginDialog();
    setSubmitError(null);
    setEditing(row);
    setMode("void");
    form.resetFields();
    setCreateOpen(true);
  };

  const openRestore = (row: MaintenanceCollectionSnapshotRow) => {
    beginDialog();
    setSubmitError(null);
    setEditing(row);
    setMode("restore");
    form.resetFields();
    setCreateOpen(true);
  };

  const submitCollection = async () => {
    // epoch 必须先于第一个 await 捕获：validateFields 期间切了项目，这里拿到的
    // 仍是旧世代，validate 返回后第一件事就是比较，过期直接退出——原因/月份
    // 校验、setSubmitting 和写请求全部不会发生，更不会关掉新项目的对话框。
    const epoch = dialogEpoch.current;
    const submitMode = mode;
    const submitProjectId = projectId;
    const submitModalProject = modalProjectRef.current;
    const values = await form.validateFields().catch(() => null);
    if (epoch !== dialogEpoch.current || !values) return;
    if (!values.reason?.trim()) {
      setSubmitError("必须填写原因");
      return;
    }
    // 月份在发请求前显式校验（Form 只管必填管不了 2026-13）：不合法直接上屏，
    // 绝不把 null 月份发给后端。create 与 edit 都要走这条闸。
    let month: string | null = null;
    if (submitMode === "create" || submitMode === "edit") {
      month = normalizeMonth(values.report_month ?? "");
      if (!month) {
        setSubmitError("报告月份格式须为 YYYY-MM（且为真实月份）");
        return;
      }
    }
    const reason = values.reason.trim();
    if (submitMode === "create" && submitModalProject !== submitProjectId) {
      // 快速切项目守卫：对话框期间项目已切换，宁可不提交也不给新项目落脏数据。
      setSubmitError("项目已切换，本次登记已中止；请在当前项目重新打开登记");
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    // 本函数闭包捕获的 loadPlan/onRefresh 即提交时项目的版本，await 后不漂移。
    const refreshPlan = loadPlan;
    const refreshProject = onRefresh;
    const target = editing;
    try {
      if (submitMode === "create") {
        await createProjectCollection(submitProjectId, {
          project_contract_id: values.project_contract_id!,
          report_month: month!,
          cumulative_amount: values.cumulative_amount ?? 0,
          status: values.status ?? "unconfirmed",
          receipt_reference: values.receipt_reference?.trim() || null,
          remark: values.remark?.trim() || null,
          reason,
        });
      } else {
        if (!target) return;
        if (submitMode === "edit") {
          await patchProjectCollection(target.collection_id, {
            version: target.version,
            reason,
            report_month: month!,
            cumulative_amount: values.cumulative_amount,
            status: values.status,
            receipt_reference: values.receipt_reference?.trim() || null,
            remark: values.remark?.trim() || null,
          });
        } else if (submitMode === "void") {
          await patchProjectCollection(target.collection_id, {
            version: target.version,
            reason,
            status: "void",
          });
        } else {
          // 恢复的安全默认：待确认。不直接回「已确认」，需要人工再改一次，
          // 避免恢复瞬间绕过 confirmed 的累计单调性语义。
          await patchProjectCollection(target.collection_id, {
            version: target.version,
            reason,
            status: "unconfirmed",
          });
        }
      }
      // 只对仍然当前的世代做 UI 副作用；两次读回之间再比较一次，防切项目
      // 恰好落在 loadPlan 与 onRefresh 之间。
      if (epoch !== dialogEpoch.current) return;
      if (submitMode === "create") message.success("回款记录已登记");
      else if (submitMode === "edit") message.success("回款记录已修改");
      else if (submitMode === "void") message.success("回款记录已作废");
      else message.success("回款记录已恢复为待确认");
      setCreateOpen(false);
      await refreshPlan();
      if (epoch !== dialogEpoch.current) return;
      await refreshProject();
    } catch (err) {
      if (epoch !== dialogEpoch.current) return;
      setSubmitError(readError(err, submitMode === "create"
        ? "登记失败：请核对合同、月份（YYYY-MM）与金额；也可改用 Excel 批量入口。"
        : submitMode === "edit"
          ? "修改失败。版本冲突时页面刷新后重试；确认月份金额是否违反单调校验。"
          : submitMode === "void"
            ? "作废失败：版本冲突时请刷新后重试。"
            : "恢复失败：版本冲突时请刷新后重试。"));
    } finally {
      if (epoch === dialogEpoch.current) setSubmitting(false);
    }
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

  const modalTitle = mode === "create" || !editing ? "登记回款"
    : mode === "edit" ? `修改回款记录（${editing.report_month.slice(0, 7)}，版本 ${editing.version}）`
    : mode === "void" ? `作废回款记录（${editing.report_month.slice(0, 7)}，版本 ${editing.version}）`
    : `恢复回款记录（${editing.report_month.slice(0, 7)}，版本 ${editing.version}）`;
  const okText = mode === "create" ? "登记"
    : mode === "edit" ? "保存修改"
    : mode === "void" ? "确认作废"
    : "确认恢复";
  const reasonLabel = mode === "create" ? "登记原因（必填）"
    : mode === "edit" ? "修改原因（必填，留痕审计）"
    : mode === "void" ? "作废原因（必填，留痕审计）"
    : "恢复原因（必填，留痕审计）";
  const reasonPlaceholder = mode === "create" ? "如：补录 9 月银行回款"
    : mode === "void" ? "如：银行冲正，该月金额作废"
    : mode === "restore" ? "如：误作废，恢复该月回款"
    : "如：核对银行回单后修正金额";

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
        hint="Excel 在左（在哪下载就在哪上传，可回填累计实收/状态/凭证号/备注）；单条登记/修改/作废/恢复在下方记录表操作列，登记时合同从当前项目下拉选择"
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
              width: 170,
              render: (_v: unknown, row: MaintenanceCollectionSnapshotRow) => (
                row.status === "void" ? (
                  <Space size={4}>
                    <Tag>已作废</Tag>
                    <Button size="small" onClick={() => openRestore(row)}>恢复</Button>
                  </Space>
                ) : (
                  <Space size={4}>
                    <Button size="small" onClick={() => openEdit(row)}>修改</Button>
                    <Button size="small" danger onClick={() => openVoid(row)}>作废</Button>
                  </Space>
                )
              ),
            }] : []),
          ]}
        />
      </Card>

      <Modal
        open={createOpen}
        title={modalTitle}
        confirmLoading={submitting}
        okText={okText}
        okButtonProps={mode === "void" ? { danger: true } : undefined}
        cancelText="取消"
        onCancel={() => {
          if (submitting) return;
          // 取消也推进世代：同项目内验证未完就取消重开另一条记录，旧提交全部作废。
          dialogEpoch.current += 1;
          setCreateOpen(false);
        }}
        onOk={() => { void submitCollection(); }}
        maskClosable={!submitting}
      >
        {submitError ? <Alert type="error" showIcon message={submitError} style={{ marginBottom: 12 }} /> : null}
        {mode === "create" && contractsState === "error" ? (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 12 }}
            message="合同列表加载失败"
            description="读取当前项目的合同关系失败（网络或权限问题）。页面登记需要选择合同；也可改用左侧 Excel 批量入口。"
            action={<Button size="small" onClick={() => { void loadContracts(); }}>重试</Button>}
          />
        ) : null}
        {mode === "create" && contractsState === "done" && contractRows.length === 0 ? (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="当前项目暂无合同关系"
            description="页面登记按合同落账，需要当前项目先有合同关系；请先维护项目合同关系后重试。Excel 批量入口仍保留。"
          />
        ) : null}
        {mode === "void" && editing ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={`软作废 ${editing.report_month.slice(0, 7)} 记录：退出累计与进度统计，历史与审计保留；之后可在记录表「恢复」找回。`}
          />
        ) : null}
        {mode === "restore" && editing ? (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="恢复后状态回到「待确认」（安全默认，不计入已确认累计）；如需改为已确认，请在恢复后再「修改」。"
          />
        ) : null}
        <Form form={form} layout="vertical" disabled={submitting}>
          {mode === "create" ? (
            <Form.Item
              name="project_contract_id"
              label="关联合同（当前项目内选择）"
              rules={[{ required: true, message: "请选择合同" }]}
            >
              <Select
                showSearch
                style={{ width: "100%" }}
                placeholder="输入合同编号搜索"
                optionFilterProp="label"
                loading={contractsState === "loading"}
                notFoundContent={contractsState === "loading" ? "合同加载中…" : "未找到匹配合同"}
                options={contractRows.map((contract) => ({
                  value: contract.project_contract_id,
                  label: contractOptionLabel(contract),
                }))}
              />
            </Form.Item>
          ) : null}
          {mode === "create" || mode === "edit" ? (
            <Form.Item
              name="report_month"
              label="报告月份（YYYY-MM）"
              rules={[{ required: true, message: "格式 YYYY-MM" }]}
            >
              <Input placeholder="2026-09" style={{ width: 160 }} />
            </Form.Item>
          ) : null}
          {mode === "create" || mode === "edit" ? (
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
          ) : null}
          {mode === "create" || mode === "edit" ? (
            <Form.Item name="receipt_reference" label="回款凭证号（可选）">
              <Input maxLength={128} placeholder="银行回单号 / 收款凭证" />
            </Form.Item>
          ) : null}
          {mode === "create" || mode === "edit" ? (
            <Form.Item name="remark" label="备注（可选）">
              <Input.TextArea rows={2} maxLength={32767} />
            </Form.Item>
          ) : null}
          <Form.Item
            name="reason"
            label={reasonLabel}
            rules={[{ required: true, message: "必须填写原因" }]}
          >
            <Input.TextArea rows={2} maxLength={1000} placeholder={reasonPlaceholder} />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  );
}

export default CollectionTab;
