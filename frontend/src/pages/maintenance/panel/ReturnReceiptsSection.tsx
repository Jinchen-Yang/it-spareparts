import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Button, Card, Col, Descriptions, Form, Input, InputNumber, Modal,
  Row, Select, Space, Table, Tag, Timeline, Tooltip, Typography, message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import PartPicker from "../../../components/PartPicker";
import { getBoardProjectOrders } from "../../../api/maintenanceBossBoard";
import {
  type ReturnReceipt, type ReturnReceiptAuditEntry, type ReturnReceiptSummary,
  createReturnReceipt, getReturnReceiptAudit, getReturnReceiptSummary,
  searchReturnReceipts, updateReturnReceipt, voidReturnReceipt,
} from "../../../api/maintenanceOperations";
import { readPermissionMap } from "../../../nav";
import { raw, readError } from "./panelUtils";

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
};

interface DemandOption {
  value: string; // order_no（后端按 order_no 解析归属）
  label: string;
}

interface ReceiptFormValues {
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

function fmtQty(value: string | null | undefined): string {
  if (value == null) return "—";
  const num = Number(value);
  return Number.isFinite(num) ? String(num) : value;
}

function fmtDate(value: string | null | undefined): string {
  if (!value) return "—";
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return value;
  return at.toLocaleString("zh-CN", { hour12: false });
}

/**
 * 返还收货台账（2026-09-11 拍板口径）：
 * 项目必选、需求单可选、PN 不限原领用、按数量统计；登记即视为已收到返件；
 * 修改/作废带版本 CAS 与审计。汇总满足恒等式 Σ(需求单) + 未关联 = 项目总量。
 */
export function ReturnReceiptsSection({ projectId }: { projectId: string }) {
  const [summary, setSummary] = useState<ReturnReceiptSummary | null>(null);
  const [receipts, setReceipts] = useState<ReturnReceipt[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [includeVoided, setIncludeVoided] = useState(false);
  const [loading, setLoading] = useState(false);
  const [demands, setDemands] = useState<DemandOption[]>([]);
  const requestSeq = useRef(0);

  const perms = readPermissionMap();
  const canManage = !!perms.action_maintenance_bad_return_manage;

  const loadDemands = useCallback(async () => {
    const options: DemandOption[] = [];
    let p = 1;
    for (;;) {
      try {
        const response = await getBoardProjectOrders(projectId, {
          page: p, page_size: 100,
        });
        const rows = response.data.rows ?? [];
        options.push(...rows.map((row) => ({
          value: row.order_no,
          label: row.order_date ? `${row.order_no}（${row.order_date}）` : row.order_no,
        })));
        if (options.length >= (response.data.total ?? 0) || rows.length === 0 || p >= 10) {
          break;
        }
        p += 1;
      } catch {
        break; // 需求单候选加载失败不阻塞台账本身
      }
    }
    setDemands(options);
  }, [projectId]);

  const load = useCallback(async (targetPage: number, withVoided: boolean) => {
    const seq = ++requestSeq.current;
    setLoading(true);
    try {
      const [summaryResponse, listResponse] = await Promise.all([
        getReturnReceiptSummary(projectId),
        searchReturnReceipts(projectId, {
          page: targetPage,
          page_size: 20,
          line_status: withVoided ? "all" : "active",
        }),
      ]);
      if (seq !== requestSeq.current) return;
      setSummary(summaryResponse.data);
      setReceipts(listResponse.data.items ?? []);
      setTotal(listResponse.data.total ?? 0);
    } catch {
      if (seq === requestSeq.current) {
        setSummary(null);
        setReceipts([]);
        setTotal(0);
      }
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadDemands();
    void load(1, false);
    return () => { requestSeq.current += 1; };
  }, [load, loadDemands]);

  // ---- 登记与修改 ----
  const [form] = Form.useForm<ReceiptFormValues>();
  const [editing, setEditing] = useState<ReturnReceipt | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  // PartPicker 只回 part_id；pn/description 在选中时一并暂存，登记时随 part_id 提交
  const [pickedPart, setPickedPart] = useState<PickedPart | null>(null);

  const openCreate = () => {
    setEditing(null);
    setPickedPart(null);
    form.resetFields();
    setModalOpen(true);
  };

  const openEdit = (receipt: ReturnReceipt) => {
    setEditing(receipt);
    setPickedPart({
      part_id: receipt.part_id ?? null,
      pn: receipt.pn,
      description: receipt.description,
    });
    form.setFieldsValue({
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
    const values = await form.validateFields().catch(() => null);
    if (!values) return;
    if (!pickedPart?.pn) {
      message.error("请先搜索并选择返件 PN");
      return;
    }
    if (editing && !values.reason?.trim()) {
      message.error("修改必须填写原因");
      return;
    }
    setSubmitting(true);
    try {
      if (editing) {
        await updateReturnReceipt(editing.receipt_id, {
          version: editing.version,
          reason: values.reason!.trim(),
          wbdd_no: values.wbdd_no ?? null,
          pn: pickedPart.pn,
          part_id: pickedPart.part_id,
          qty: values.qty!,
          condition: values.condition ?? null,
          note: values.note?.trim() || null,
          evidence_ref: values.evidence_ref?.trim() || null,
        });
        message.success("返还记录已修改");
      } else {
        const response = await createReturnReceipt(projectId, {
          pn: pickedPart.pn,
          qty: values.qty!,
          wbdd_no: values.wbdd_no ?? null,
          part_id: pickedPart.part_id,
          condition: values.condition ?? null,
          note: values.note?.trim() || null,
          evidence_ref: values.evidence_ref?.trim() || null,
          idempotency_key: `web-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
        });
        message.success(response.data.replayed ? "重复提交：返回已有登记，未重复计数" : "返还已登记");
      }
      setModalOpen(false);
      await load(page, includeVoided);
    } catch (err) {
      message.error(readError(err, editing ? "修改失败，请刷新后重试" : "登记失败，请重试"));
    } finally {
      setSubmitting(false);
    }
  };

  // ---- 作废 ----
  const [voidTarget, setVoidTarget] = useState<ReturnReceipt | null>(null);
  const [voidReason, setVoidReason] = useState("");
  const [voiding, setVoiding] = useState(false);

  const confirmVoid = async () => {
    if (!voidTarget || !voidReason.trim()) return;
    setVoiding(true);
    try {
      await voidReturnReceipt(voidTarget.receipt_id, {
        version: voidTarget.version,
        reason: voidReason.trim(),
      });
      message.success("返还记录已作废，不再计入统计");
      setVoidTarget(null);
      setVoidReason("");
      await load(page, includeVoided);
    } catch (err) {
      message.error(readError(err, "作废失败，请刷新后重试"));
    } finally {
      setVoiding(false);
    }
  };

  // ---- 审计历史 ----
  const [auditTarget, setAuditTarget] = useState<ReturnReceipt | null>(null);
  const [auditItems, setAuditItems] = useState<ReturnReceiptAuditEntry[] | null>(null);

  const openAudit = async (receipt: ReturnReceipt) => {
    setAuditTarget(receipt);
    setAuditItems(null);
    try {
      const response = await getReturnReceiptAudit(receipt.receipt_id);
      setAuditItems(response.data.items ?? []);
    } catch {
      setAuditItems([]);
    }
  };

  const demandTotal = useMemo(
    () => summary?.by_demand.reduce((acc, item) => acc + Number(item.qty), 0) ?? 0,
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
    { title: "数量", dataIndex: "qty", width: 72, render: (v: string) => fmtQty(v) },
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

      <Space style={{ justifyContent: "space-between", width: "100%" }}>
        <Space>
          {canManage ? (
            <Button type="primary" size="small" onClick={openCreate}>登记返还</Button>
          ) : null}
          <Text type="secondary" style={{ fontSize: 12 }}>
            登记即视为已收到返件；数量按项目统计，需求单为可选归属，PN 不要求与领用一致
          </Text>
        </Space>
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

      <Table<ReturnReceipt>
        rowKey="receipt_id"
        size="small"
        loading={loading}
        dataSource={receipts}
        columns={columns}
        scroll={{ x: 1180 }}
        pagination={{
          current: page,
          pageSize: 20,
          total,
          showSizeChanger: false,
          onChange: (next) => { setPage(next); void load(next, includeVoided); },
        }}
        locale={{ emptyText: includeVoided ? "暂无返还记录" : "暂无有效返还记录" }}
      />

      <Modal
        open={modalOpen}
        title={editing ? `修改返还记录（${editing.pn} × ${fmtQty(editing.qty)}）` : "登记返还"}
        confirmLoading={submitting}
        okText={editing ? "保存修改" : "登记"}
        cancelText="取消"
        onCancel={() => setModalOpen(false)}
        onOk={() => { void submit(); }}
      >
        <Form form={form} layout="vertical">
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
            />
          </Form.Item>
          <Form.Item label="返件 PN（必选——搜型号/别名/描述，不要求与领用一致）" required>
            <PartPicker
              value={pickedPart?.part_id ?? null}
              onChange={(partId, item) => {
                setPickedPart(item
                  ? { part_id: partId, pn: item.pn_std, description: item.description ?? null }
                  : null);
              }}
              initialItem={editing
                ? { part_id: editing.part_id ?? -1, pn_std: editing.pn, description: editing.description }
                : null}
            />
          </Form.Item>
          <Space size={12}>
            <Form.Item
              name="qty"
              label="数量"
              rules={[{ required: true, message: "数量必填" }]}
            >
              <InputNumber min={1} precision={0} style={{ width: 120 }} placeholder="正整数" />
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
        onCancel={() => setAuditTarget(null)}
        width={640}
      >
        {auditItems === null ? (
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
                  {entry.action !== "create" && entry.before_json ? (
                    <Descriptions.Item label="之前">
                      {fmtQty(String(entry.before_json.qty ?? ""))} 件 ·{" "}
                      {String(entry.before_json.condition ?? "未填写")} ·{" "}
                      {String(entry.before_json.order_no ?? "未关联")}
                    </Descriptions.Item>
                  ) : null}
                  {entry.after_json ? (
                    <Descriptions.Item label="之后">
                      {fmtQty(String(entry.after_json.qty ?? ""))} 件 ·{" "}
                      {String(entry.after_json.condition ?? "未填写")} ·{" "}
                      {String(entry.after_json.order_no ?? "未关联")}
                    </Descriptions.Item>
                  ) : null}
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
