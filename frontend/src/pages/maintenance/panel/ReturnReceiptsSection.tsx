import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert, Button, Card, Col, Descriptions, Form, Input, InputNumber, Modal,
  Row, Select, Space, Table, Tag, Timeline, Tooltip, Typography, message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import PartPicker from "../../../components/PartPicker";
import { listMaintenanceProjects } from "../../../api/maintenanceProjects";
import {
  type ReturnReceipt, type ReturnReceiptAuditEntry, type ReturnReceiptSummary,
  createReturnReceipt, getReturnReceiptAudit, getReturnReceiptSummary, getReturnReceiptDemands,
  searchReturnReceipts, updateReturnReceipt, voidReturnReceipt,
} from "../../../api/maintenanceOperations";
import { readPermissionMap } from "../../../nav";
import { raw, readError } from "./panelUtils";
import ReturnReceiptImport from "./ReturnReceiptImport";

const { Text } = Typography;

const CONDITIONS = ["成品", "坏品", "废品"] as const;
type Condition = (typeof CONDITIONS)[number];

const CONDITION_COLOR: Record<string, string> = {
  成品: "green",
  坏品: "red",
  废品: "default",
};

const AUDIT_ACTION_LABEL: Record<string, string> = {
  create: "登记",
  update: "修改",
  void: "作废",
  transfer_out: "转出项目",
  import: "入库导入",
  import_update: "导入更正",
  import_create: "入库导入",
  import_correct: "导入更正",
};

interface DemandOption {
  value: string; // order_no（后端按 order_no 解析归属）
  label: string;
}

interface ReceiptFormValues {
  project_id: string;
  wbdd_no?: string | null;
  qty?: number;
  condition?: Condition | null;
  note?: string;
  evidence_ref?: string;
  reason?: string;
}

interface PickedPart {
  part_id: number | null;
  pn: string;
  description: string | null;
}

function fmtQty(value: string | number | null | undefined): string {
  if (value == null) return "—";
  const text = String(value);
  return /^-?\d+(?:\.\d+)?$/.test(text) ? text.replace(/(\.\d*?[1-9])0+$|\.0+$/, "$1") : text;
}

function fmtDate(value: string | null | undefined): string {
  if (!value) return "—";
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return value;
  return at.toLocaleString("zh-CN", { hour12: false });
}

const AUDIT_FIELD_LABEL: Record<string, string> = {
  pn: "PN", description: "描述", qty: "数量", condition: "件况", project_id: "项目",
  source_order_id: "需求单身份", order_no: "需求单号", note: "备注", evidence_ref: "凭据",
  source: "来源", batch_id: "批次", head_no: "来源单号", head_row_id: "原始单据身份",
  source_ref: "原始明细身份", line_status: "有效状态", version: "版本", created_by: "登记人",
  created_at: "登记时间", updated_by: "修改人", updated_at: "修改时间", voided_by: "作废人",
  voided_at: "作废时间", void_reason: "作废原因", receipt_id: "记录身份", occurred_at: "收到时间",
  source_evidence: "导入来源凭证",
  part_id: "型号身份", receipt_kind: "返件层级", review_required: "数量待审",
};
function auditValue(value: unknown, item?: { field?: string }): string {
  if (item?.field === "source_evidence" && value && typeof value === "object") {
    const evidence = value as Record<string, unknown>;
    const metadata = evidence.source_metadata as Record<string, unknown> | undefined;
    return [evidence.batch_id ? `导入批次：${evidence.batch_id}` : null,
      evidence.file_hash ? `原件校验：${evidence.file_hash}` : null,
      evidence.category ? `入库类别：${evidence.category}` : null,
      metadata?.sn ? `SN：${metadata.sn}` : null,
      metadata?.machine_source_qty != null ? `整机原始数量：${metadata.machine_source_qty}` : null,
    ].filter(Boolean).join("；") || "来源凭证已保留";
  }
  if (value === null) return "已清空 / 未填写";
  if (value === undefined) return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

/**
 * 返还收货台账（2026-09-11 拍板口径）：
 * 项目必选、需求单可选、PN 不限原领用、按数量统计；登记即视为已收到返件；
 * 修改/作废带版本 CAS 与审计。汇总满足恒等式 Σ(需求单) + 未关联 = 项目总量。
 */
export function ReturnReceiptsSection({ projectId, canImport = true, onChanged }: {
  projectId: string; canImport?: boolean; onChanged?: () => Promise<boolean>;
}) {
  const [summary, setSummary] = useState<ReturnReceiptSummary | null>(null);
  const [receipts, setReceipts] = useState<ReturnReceipt[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [includeVoided, setIncludeVoided] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [demands, setDemands] = useState<DemandOption[]>([]);
  const requestSeq = useRef(0);
  const contextSeq = useRef(0);
  const demandSeq = useRef(0);
  const auditSeq = useRef(0);
  const projectSeq = useRef(0);
  const [demandError, setDemandError] = useState<string | null>(null);
  const [demandLoading, setDemandLoading] = useState(false);
  const [targetProject, setTargetProject] = useState(projectId);
  const [projects, setProjects] = useState<{ value: string; label: string }[]>([]);
  const [projectError, setProjectError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [demandFilter, setDemandFilter] = useState<string | undefined>();
  const filters = useRef<{ q?: string; source_order_id?: string; unassigned?: boolean }>({});
  const createAttempt = useRef<{ content: string; key: string } | null>(null);

  const perms = readPermissionMap();
  const canManage = !!perms.action_maintenance_bad_return_manage;

  const loadDemands = useCallback(async (id: string) => {
    const seq = ++demandSeq.current;
    setDemandLoading(true);
    setDemandError(null);
    setDemands([]);
    const options: DemandOption[] = [];
    try {
      for (let p = 1; ; p += 1) {
        const response = await getReturnReceiptDemands(id, { page: p, page_size: 100 });
        if (seq !== demandSeq.current) return;
        const rows = response.data.rows ?? [];
        options.push(...rows.map((row) => ({ value: row.order_no,
          label: row.order_date ? `${row.order_no}（${row.order_date}）` : row.order_no })));
        if (options.length >= (response.data.total ?? 0)) break;
        if (!rows.length) throw new Error("需求单分页不完整，请重新加载");
      }
      setDemands(options);
    } catch (err) {
      if (seq === demandSeq.current) setDemandError(readError(err, "需求单候选加载失败，请重试后选择；也可明确不关联需求单"));
    } finally {
      if (seq === demandSeq.current) setDemandLoading(false);
    }
  }, []);

  const loadProjects = async () => {
    const seq = ++projectSeq.current;
    setProjectError(null);
    try {
      const options: { value: string; label: string }[] = [];
      for (let p = 1; ; p += 1) {
        const response = await listMaintenanceProjects({ page: p, page_size: 100, include_inactive: false });
        if (seq !== projectSeq.current) return;
        const rows = response.data.rows ?? [];
        options.push(...rows.map((row) => ({ value: row.project_id, label: `${row.display_name} · ${row.project_code}` })));
        if (options.length >= response.data.total) break;
        if (!rows.length) throw new Error("项目候选分页不完整");
      }
      setProjects(options);
    } catch (err) {
      if (seq === projectSeq.current) setProjectError(readError(err, "项目候选加载失败，请重试"));
    }
  };

  const load = useCallback(async (targetPage: number, withVoided: boolean) => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setLoadError(null);
    try {
      const [summaryResponse, listResponse] = await Promise.all([
        getReturnReceiptSummary(projectId),
        searchReturnReceipts(projectId, {
          page: targetPage,
          page_size: 20,
          line_status: withVoided ? "all" : "active",
          ...filters.current,
        }),
      ]);
      if (seq !== requestSeq.current) return;
      setSummary(summaryResponse.data);
      setReceipts(listResponse.data.items ?? []);
      setTotal(listResponse.data.total ?? 0);
    } catch (err) {
      if (seq === requestSeq.current) {
        setSummary(null);
        setReceipts([]);
        setTotal(0);
        setLoadError(readError(err, "返还记录加载失败；若刚刚已提交成功，请重试刷新，不要重复登记。"));
      }
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    setPage(1);
    setIncludeVoided(false);
    setTargetProject(projectId);
    setModalOpen(false);
    setEditing(null);
    setAuditTarget(null);
    setVoidTarget(null);
    setSubmitting(false);
    setVoiding(false);
    setQuery("");
    setDemandFilter(undefined);
    filters.current = {};
    void loadDemands(projectId);
    void load(1, false);
    return () => { contextSeq.current += 1; requestSeq.current += 1; demandSeq.current += 1; auditSeq.current += 1; projectSeq.current += 1; };
  }, [load, loadDemands]);

  // ---- 登记与修改 ----
  const [form] = Form.useForm<ReceiptFormValues>();
  const [editing, setEditing] = useState<ReturnReceipt | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  // PartPicker 只回 part_id；pn/description 在选中时一并暂存，登记时随 part_id 提交
  const [pickedPart, setPickedPart] = useState<PickedPart | null>(null);

  const openCreate = () => {
    setSubmitError(null);
    setEditing(null);
    setPickedPart(null);
    form.resetFields();
    form.setFieldsValue({ project_id: projectId });
    setTargetProject(projectId);
    createAttempt.current = null;
    void loadDemands(projectId);
    setModalOpen(true);
  };

  const openEdit = (receipt: ReturnReceipt) => {
    setSubmitError(null);
    setEditing(receipt);
    setPickedPart({
      part_id: receipt.part_id ?? null,
      pn: receipt.pn,
      description: receipt.description,
    });
    setTargetProject(receipt.project_id);
    void loadDemands(receipt.project_id);
    void loadProjects();
    form.setFieldsValue({
      project_id: receipt.project_id,
      wbdd_no: receipt.order_no ?? undefined,
      qty: Number(receipt.qty),
      condition: (receipt.condition as Condition | null) ?? undefined,
      note: receipt.note ?? undefined,
      evidence_ref: receipt.evidence_ref ?? undefined,
      reason: undefined,
    });
    setModalOpen(true);
  };

  const submit = async () => {
    const seq = contextSeq.current;
    const values = await form.validateFields().catch(() => null);
    if (!values || seq !== contextSeq.current) return;
    if (!pickedPart?.pn) {
      message.error("请先搜索并选择返件 PN");
      return;
    }
    if (editing && !values.reason?.trim()) {
      message.error("修改必须填写原因");
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    try {
      if (editing) {
        await updateReturnReceipt(editing.receipt_id, {
          project_id: values.project_id,
          version: editing.version,
          reason: values.reason!.trim(),
          wbdd_no: values.wbdd_no ?? null,
          pn: pickedPart.pn,
          part_id: pickedPart.part_id,
          description: pickedPart.description,
          ...(values.qty !== Number(editing.qty) ? { qty: values.qty! } : {}),
          ...((values.condition ?? null) !== editing.condition ? { condition: values.condition ?? null } : {}),
          note: values.note?.trim() || null,
          evidence_ref: values.evidence_ref?.trim() || null,
        });
        if (seq !== contextSeq.current) return;
        message.success("返还记录已修改");
      } else {
        const payload = {
          pn: pickedPart.pn,
          qty: values.qty!,
          wbdd_no: values.wbdd_no ?? null,
          part_id: pickedPart.part_id,
          description: pickedPart.description,
          condition: values.condition ?? null,
          note: values.note?.trim() || null,
          evidence_ref: values.evidence_ref?.trim() || null,
        };
        const content = JSON.stringify(payload);
        if (createAttempt.current?.content !== content) {
          createAttempt.current = { content, key: `return-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`}` };
        }
        const response = await createReturnReceipt(projectId, { ...payload, idempotency_key: createAttempt.current.key });
        if (seq !== contextSeq.current) return;
        message.success(response.data.replayed ? "重复提交：返回已有登记，未重复计数" : "返还已登记");
      }
      setModalOpen(false);
      await load(page, includeVoided);
      if (onChanged) await onChanged();
    } catch (err) {
      if (seq === contextSeq.current) setSubmitError(readError(err, editing ? "修改失败。版本冲突时请加载最新记录后重新修改；网络失败可原样重试。" : "登记失败，可原样重试；相同内容不会重复登记。"));
    } finally {
      if (seq === contextSeq.current) setSubmitting(false);
    }
  };

  // ---- 作废 ----
  const [voidTarget, setVoidTarget] = useState<ReturnReceipt | null>(null);
  const [voidReason, setVoidReason] = useState("");
  const [voiding, setVoiding] = useState(false);

  const confirmVoid = async () => {
    if (!voidTarget || !voidReason.trim()) return;
    const seq = contextSeq.current;
    setVoiding(true);
    try {
      await voidReturnReceipt(voidTarget.receipt_id, {
        version: voidTarget.version,
        reason: voidReason.trim(),
      });
      if (seq !== contextSeq.current) return;
      message.success("返还记录已作废，不再计入统计");
      setVoidTarget(null);
      setVoidReason("");
      await load(page, includeVoided);
      if (onChanged) await onChanged();
    } catch (err) {
      if (seq === contextSeq.current) message.error(readError(err, "作废失败，请刷新后重试"));
    } finally {
      if (seq === contextSeq.current) setVoiding(false);
    }
  };

  // ---- 审计历史 ----
  const [auditTarget, setAuditTarget] = useState<ReturnReceipt | null>(null);
  const [auditItems, setAuditItems] = useState<ReturnReceiptAuditEntry[] | null>(null);
  const [auditError, setAuditError] = useState<string | null>(null);

  const openAudit = async (receipt: ReturnReceipt) => {
    const seq = ++auditSeq.current;
    setAuditTarget(receipt);
    setAuditItems(null);
    setAuditError(null);
    try {
      const response = await getReturnReceiptAudit(receipt.receipt_id);
      if (seq === auditSeq.current) setAuditItems(response.data.items ?? []);
    } catch (err) {
      if (seq === auditSeq.current) setAuditError(readError(err, "历史加载失败，请重试"));
    }
  };

  const demandTotal = useMemo(
    () => {
      if (!summary) return "0";
      // Source quantities retain three decimal places; sum integer thousandths.
      const total = summary.by_demand.reduce((acc, item) => {
        const [whole, fraction = ""] = item.qty.split(".");
        return acc + BigInt(whole) * 1000n + BigInt(fraction.padEnd(3, "0"));
      }, 0n);
      return fmtQty(`${total / 1000n}.${String(total % 1000n).padStart(3, "0")}`);
    },
    [summary],
  );

  const columns: ColumnsType<ReturnReceipt> = [
    {
      title: "来源",
      dataIndex: "source",
      width: 96,
      render: (value: string) => value === "manual"
        ? <Tag color="blue">手工登记</Tag>
        : <Tag color="purple">入库导入</Tag>,
    },
    {
      title: "返件 PN",
      width: 180,
      render: (_v, item) => (
        <Space direction="vertical" size={0}>
          <Text copyable style={{ fontFamily: "monospace" }}>{item.pn}</Text>
          {item.description
            ? <span style={{ fontSize: 12, color: "rgba(0,0,0,.45)" }}>{item.description}</span>
            : null}
        </Space>
      ),
    },
    { title: "数量", dataIndex: "qty", width: 120, render: (v: string, item) => <>{fmtQty(v)} {item.review_required ? <Tag color="gold">待审</Tag> : null}{item.receipt_kind === "machine" ? <Tag>整机</Tag> : null}</> },
    {
      title: "件况",
      dataIndex: "condition",
      width: 84,
      render: (value: string | null) => value
        ? <Tag color={CONDITION_COLOR[value] ?? "default"}>{value}</Tag>
        : <span style={{ color: "rgba(0,0,0,.35)" }}>未填写</span>,
    },
    {
      title: "关联需求单",
      dataIndex: "order_no",
      width: 160,
      render: (value: string | null) => value
        ? <Text style={{ fontFamily: "monospace" }}>{value}</Text>
        : <span style={{ color: "rgba(0,0,0,.35)" }}>未关联</span>,
    },
    { title: "备注", dataIndex: "note", width: 160, render: (v: string | null) => raw(v) },
    { title: "凭据/单号", dataIndex: "evidence_ref", width: 130, render: (v: string | null) => raw(v) },
    { title: "收到时间", dataIndex: "occurred_at", width: 150, render: (v: string | null) => fmtDate(v) },
    {
      title: "登记人",
      dataIndex: "created_by",
      width: 120,
      render: (value: string, item) => (
        <Space direction="vertical" size={0}>
          <span>{value}</span>
          <span style={{ fontSize: 12, color: "rgba(0,0,0,.45)" }}>{fmtDate(item.created_at)}</span>
        </Space>
      ),
    },
    {
      title: "状态",
      dataIndex: "line_status",
      width: 110,
      render: (value: string, item) => value === "active"
        ? <Tag color="green">有效</Tag>
        : (
          <Tooltip title={item.void_reason}>
            <Tag>已作废</Tag>
          </Tooltip>
        ),
    },
    {
      title: "操作",
      key: "ops",
      width: canManage ? 200 : 90,
      render: (_v, item) => (
        <Space size={4}>
          {canManage && item.line_status === "active" ? (
            <>
              <Button size="small" type="link" onClick={() => openEdit(item)}>修改</Button>
              <Button size="small" type="link" danger onClick={() => { setVoidTarget(item); setVoidReason(""); }}>作废</Button>
            </>
          ) : null}
          <Button size="small" type="link" onClick={() => { void openAudit(item); }}>历史</Button>
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {loadError ? (
        <Alert
          type="error"
          showIcon
          message="返还记录加载失败，不代表没有记录"
          description={loadError}
          action={<Button size="small" onClick={() => { void load(page, includeVoided); }}>重新加载</Button>}
        />
      ) : null}
      <Row gutter={12}>
        <Col xs={24} sm={8}>
          <Card size="small">
            <div style={{ fontSize: 12, color: "rgba(0,0,0,.45)" }}>项目已返还（有效）</div>
            <div style={{ fontSize: 26, fontWeight: 600 }}>
              {summary ? fmtQty(summary.project_total_qty) : "—"}
            </div>
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card size="small">
            <div style={{ fontSize: 12, color: "rgba(0,0,0,.45)" }}>已关联需求单</div>
            <div style={{ fontSize: 26, fontWeight: 600 }}>
              {summary ? fmtQty(String(demandTotal)) : "—"}
            </div>
            {summary?.by_demand.length ? (
              <div style={{ fontSize: 12, color: "rgba(0,0,0,.45)" }}>
                {summary.by_demand
                  .slice(0, 3)
                  .map((item) => `${item.order_no ?? item.source_order_id}: ${fmtQty(item.qty)}`)
                  .join("；")}
                {summary.by_demand.length > 3 ? ` 等 ${summary.by_demand.length} 张需求单` : ""}
              </div>
            ) : null}
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card size="small">
            <div style={{ fontSize: 12, color: "rgba(0,0,0,.45)" }}>未关联需求单</div>
            <div style={{ fontSize: 26, fontWeight: 600 }}>
              {summary ? fmtQty(summary.unassigned_qty) : "—"}
            </div>
          </Card>
        </Col>
      </Row>

      <Space wrap style={{ justifyContent: "space-between", width: "100%" }}>
        <Space>
          {canManage ? (
            <Button type="primary" size="small" onClick={openCreate}>登记返还</Button>
          ) : null}
          <Text type="secondary" style={{ fontSize: 12 }}>
            登记即视为已收到返件；数量按项目统计，需求单为可选归属，PN 不要求与领用一致
          </Text>
        </Space>
        {canImport && canManage ? <ReturnReceiptImport onApplied={async () => {
          await load(page, includeVoided);
          if (onChanged) await onChanged();
        }} /> : null}
        <Button
          size="small"
          type={includeVoided ? "primary" : "default"}
          onClick={() => {
            const next = !includeVoided;
            setIncludeVoided(next);
            setPage(1);
            void load(1, next);
          }}
        >
          {includeVoided ? "含已作废" : "只看有效"}
        </Button>
      </Space>

      {summary ? <Table
        size="small"
        rowKey="key"
        pagination={false}
        dataSource={[
          ...summary.by_demand.map((item) => ({ key: item.source_order_id, label: item.order_no ?? item.source_order_id, qty: item.qty })),
          { key: "unassigned", label: "未关联需求单", qty: summary.unassigned_qty },
        ]}
        columns={[
          { title: "需求单返还汇总", dataIndex: "label" },
          { title: "已返还数量", dataIndex: "qty", render: fmtQty },
          { title: "操作", render: (_v, item) => <Button size="small" type="link" onClick={() => {
            setDemandFilter(item.key); setPage(1);
            filters.current = { ...filters.current, source_order_id: item.key === "unassigned" ? undefined : item.key, unassigned: item.key === "unassigned" };
            void load(1, includeVoided);
          }}>查看明细</Button> },
        ]}
      /> : null}
      <Space wrap>
        <Input.Search value={query} allowClear placeholder="搜索返件 PN、凭据或备注" onChange={(event) => setQuery(event.target.value)} onSearch={(value) => {
          filters.current = { ...filters.current, q: value }; setPage(1); void load(1, includeVoided);
        }} style={{ width: 280 }} />
        {demandFilter ? <Button size="small" onClick={() => {
          setDemandFilter(undefined); filters.current = { q: query }; setPage(1); void load(1, includeVoided);
        }}>清除需求单筛选</Button> : null}
      </Space>

      <Table<ReturnReceipt>
        rowKey="receipt_id"
        size="small"
        loading={loading}
        dataSource={receipts}
        columns={columns}
        scroll={{ x: 1320 }}
        expandable={{ expandedRowRender: (item) => <Space direction="vertical" style={{ width: "100%" }}><Descriptions size="small" column={{ xs: 1, sm: 2 }}>
          <Descriptions.Item label="备注">{raw(item.note)}</Descriptions.Item>
          <Descriptions.Item label="来源单号">{raw(item.head_no)}</Descriptions.Item>
          <Descriptions.Item label="导入批次">{raw(item.batch_id)}</Descriptions.Item>
          <Descriptions.Item label="原始单据身份">{raw(item.head_row_id)}</Descriptions.Item>
          <Descriptions.Item label="原始明细身份">{raw(item.source_ref)}</Descriptions.Item>
          <Descriptions.Item label="最后修改">{raw(item.updated_by)} · {fmtDate(item.updated_at)}</Descriptions.Item>
          {item.line_status === "voided" ? <Descriptions.Item label="作废">{raw(item.voided_by)} · {fmtDate(item.voided_at)} · {raw(item.void_reason)}</Descriptions.Item> : null}
        </Descriptions>
          {item.components?.length ? <>
            <Text type="secondary">整机附属明细（仅展示，不计入已返还数量）</Text>
            <Table size="small" rowKey="row_id" pagination={false} dataSource={item.components} scroll={{ x: 600 }} columns={[
              { title: "附属备件 PN", dataIndex: "pn" },
              { title: "描述", dataIndex: "description", render: raw },
              { title: "原始数量", dataIndex: "qty", render: fmtQty },
              { title: "件况", dataIndex: "condition", render: raw },
              { title: "来源明细身份", dataIndex: "row_id" },
            ]} />
          </> : null}
        </Space> }}
        pagination={{
          current: page,
          pageSize: 20,
          total,
          showSizeChanger: false,
          onChange: (next) => { setPage(next); void load(next, includeVoided); },
        }}
        locale={{ emptyText: loadError ? "记录读取失败，请重新加载" : includeVoided ? "暂无返还记录" : "暂无有效返还记录" }}
      />

      <Modal
        open={modalOpen}
        title={editing ? `修改返还记录（${editing.pn} × ${fmtQty(editing.qty)}）` : "登记返还"}
        confirmLoading={submitting}
        okText={editing ? "保存修改" : "登记"}
        cancelText="取消"
        onCancel={() => { if (!submitting) setModalOpen(false); }}
        cancelButtonProps={{ disabled: submitting }}
        maskClosable={!submitting}
        onOk={() => { void submit(); }}
      >
        {submitError ? <Alert type="error" showIcon message={submitError} action={editing ? <Button size="small" disabled={submitting} onClick={() => {
          setModalOpen(false); void load(page, includeVoided);
        }}>加载最新记录</Button> : undefined} /> : null}
        <Form form={form} layout="vertical" disabled={submitting}>
          <Form.Item name="project_id" label={editing ? "项目（转移需同时具备原项目和目标项目修改权限）" : "项目（当前项目）"} rules={[{ required: true, message: "请选择项目" }]}>
            <Select disabled={!editing} showSearch optionFilterProp="label" options={projects.some((item) => item.value === projectId) ? projects : [{ value: projectId, label: "当前项目" }, ...projects]} onChange={(value: string) => {
              form.setFieldValue("wbdd_no", null); setTargetProject(value); void loadDemands(value);
            }} />
          </Form.Item>
          {editing && projectError ? <Alert type="error" message={projectError} action={<Button size="small" onClick={() => { void loadProjects(); }}>重试加载项目</Button>} /> : null}
          {demandError ? <Alert type="warning" message={demandError} action={<Button size="small" onClick={() => { void loadDemands(targetProject); }}>重试加载需求单</Button>} /> : null}
          <Form.Item
            name="wbdd_no"
            label="维保需求单（可选——不选则计入项目「未关联需求单」）"
          >
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder={demands.length ? "选择需求单（可搜索单号）" : "本项目暂无可选需求单"}
              options={demands}
              loading={demandLoading}
            />
          </Form.Item>
          {editing && pickedPart?.part_id === null ? <Alert type="info" message={`保留原返件 PN：${pickedPart.pn}`} description="此返件尚未关联型号主数据。可保留原 PN 修改其他字段，或搜索选择新型号。" /> : null}
          <Form.Item label="返件 PN（必选——搜型号/别名/描述，不要求与领用一致）" required>
            <PartPicker
              value={pickedPart?.part_id ?? null}
              onChange={(partId, item) => {
                setPickedPart(item
                  ? { part_id: partId, pn: item.pn_std, description: item.description ?? null }
                  : null);
              }}
              key={editing?.receipt_id ?? "create"}
              disabled={submitting}
              initialItem={editing?.part_id != null
                ? { part_id: editing.part_id, pn_std: editing.pn, description: editing.description }
                : null}
            />
          </Form.Item>
          <Space size={12}>
            <Form.Item
              name="qty"
              label="数量"
              extra={editing?.receipt_kind === "machine" ? "整机固定计 1 台，附属明细不累计" : undefined}
              rules={[{ required: true, message: "数量必填" }, { validator: (_rule, value: unknown) =>
                typeof value === "number" && ((!!editing && value === Number(editing.qty)) || (Number.isInteger(value) && value > 0 && value <= 99999999999))
                  ? Promise.resolve() : Promise.reject(new Error("数量须为正整数，且不超过 99999999999")) }]}
            >
              <InputNumber disabled={editing?.receipt_kind === "machine" || submitting} min={1} max={99999999999} style={{ width: 120 }} placeholder="正整数" />
            </Form.Item>
            <Form.Item name="condition" label="件况（可选）" initialValue={null}>
              <Select
                allowClear
                style={{ width: 140 }}
                placeholder="未填写"
                options={CONDITIONS.map((value) => ({ value, label: value }))}
              />
            </Form.Item>
          </Space>
          <Form.Item name="evidence_ref" label="来源单号/凭据（可选）">
            <Input placeholder="如原始入库单号、快递单号" maxLength={128} />
          </Form.Item>
          <Form.Item name="note" label="备注（可选）">
            <Input.TextArea rows={2} maxLength={512} placeholder="现场说明" />
          </Form.Item>
          {editing ? (
            <Form.Item
              name="reason"
              label="修改原因（必填，留痕审计）"
              rules={[{ required: true, message: "必须填写修改原因" }]}
            >
              <Input.TextArea rows={2} maxLength={256} placeholder="如：实物清点修正 / 补选需求单" />
            </Form.Item>
          ) : null}
        </Form>
      </Modal>

      <Modal
        open={voidTarget !== null}
        title={voidTarget ? `作废返还记录（${voidTarget.pn} × ${fmtQty(voidTarget.qty)}）` : ""}
        confirmLoading={voiding}
        okText="确认作废"
        okButtonProps={{ danger: true, disabled: !voidReason.trim() }}
        cancelText="取消"
        onCancel={() => { setVoidTarget(null); setVoidReason(""); }}
        onOk={() => { void confirmVoid(); }}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <span style={{ fontSize: 12, color: "rgba(0,0,0,.55)" }}>
            作废后该记录退出项目/需求单的全部有效统计；历史与审计保留，可追溯。
          </span>
          <Input.TextArea
            value={voidReason}
            onChange={(event) => setVoidReason(event.target.value)}
            placeholder="作废原因（必填），如：重复登记 / 数量录入错误"
            rows={2}
            autoFocus
          />
        </Space>
      </Modal>

      <Modal
        open={auditTarget !== null}
        title={auditTarget ? `操作历史（${auditTarget.pn} × ${fmtQty(auditTarget.qty)}）` : ""}
        footer={null}
        onCancel={() => { auditSeq.current += 1; setAuditTarget(null); }}
        width={640}
      >
        {auditError ? <Alert type="error" message={auditError} action={<Button onClick={() => { if (auditTarget) void openAudit(auditTarget); }}>重新加载历史</Button>} /> : auditItems === null ? (
          <Text type="secondary">加载中…</Text>
        ) : auditItems.length === 0 ? (
          <Text type="secondary">暂无历史记录</Text>
        ) : (
          <Timeline
            items={auditItems.map((entry) => ({
              color: entry.action === "void" ? "red" : entry.action === "create" ? "green" : "blue",
              children: (
                <Descriptions
                  size="small"
                  column={1}
                  title={`${AUDIT_ACTION_LABEL[entry.action] ?? entry.action} · ${entry.operated_by} · ${fmtDate(entry.operated_at)}`}
                >
                  <Descriptions.Item label="原因">{entry.reason}</Descriptions.Item>
                  <Descriptions.Item label="项目">{entry.project_id}</Descriptions.Item>
                  <Descriptions.Item label="变更详情">
                    <Table size="small" pagination={false} rowKey="field" scroll={{ x: 480 }}
                      dataSource={Array.from(new Set([...Object.keys(entry.before_json ?? {}), ...Object.keys(entry.after_json ?? {})])).map((field) => ({
                        field, before: entry.before_json?.[field], after: entry.after_json?.[field],
                      }))}
                      columns={[
                        { title: "字段", dataIndex: "field", render: (field: string) => AUDIT_FIELD_LABEL[field] ?? field },
                        { title: "之前", dataIndex: "before", render: auditValue },
                        { title: "之后", dataIndex: "after", render: auditValue },
                      ]}
                    />
                  </Descriptions.Item>
                </Descriptions>
              ),
            }))}
          />
        )}
      </Modal>
    </Space>
  );
}

export default ReturnReceiptsSection;
