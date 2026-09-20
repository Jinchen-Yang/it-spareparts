import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert, Button, Input, InputNumber, Modal, Space, Table, Tag, Typography, message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  type DemandLineRow,
  clearDemandLineOverride,
  listDemandLines,
  patchDemandLine,
} from "../../api/maintenanceDemands";

const { Text } = Typography;

function readError(error: unknown, fallback: string): string {
  if (typeof error === "object" && error !== null) {
    const detail = (error as { response?: { data?: { detail?: unknown } } })
      .response?.data?.detail;
    if (typeof detail === "string" && detail) return detail;
  }
  return fallback;
}

/** 覆盖标记徽标：该字段被页面直改保护中（重导不覆盖） */
function OverrideBadge({ field, row }: { field: string; row: DemandLineRow }) {
  const entry = row.manual_override?.[field];
  if (!entry) return null;
  return (
    <Tag color="purple" style={{ marginLeft: 4, fontSize: 11 }}>
      已改（{String(entry.updated_by ?? "?")}）
    </Tag>
  );
}

/**
 * 需求单行编辑器（v1.36 Phase E）：列出单头下的明细行，
 * 白名单字段（数量/退货数量/SN/描述/PN）逐行 Modal 编辑，
 * override 可撤销（恢复氚云原值）。成本列/头字段后端永不接受。
 */
export default function DemandLinesEditor({
  sourceOrderId,
  orderNo,
  onClose,
}: {
  sourceOrderId: string;
  orderNo: string;
  onClose: () => void;
}) {
  const [rows, setRows] = useState<DemandLineRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<DemandLineRow | null>(null);
  const [editQty, setEditQty] = useState<number | null>(null);
  const [editReturnQty, setEditReturnQty] = useState<number | null>(null);
  const [editSn, setEditSn] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [editPn, setEditPn] = useState("");
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const seq = useRef(0);

  const load = useCallback(async () => {
    const current = ++seq.current;
    setLoading(true);
    setError(null);
    try {
      const resp = await listDemandLines(sourceOrderId);
      if (seq.current !== current) return;
      setRows(resp.data.items);
    } catch (err) {
      if (seq.current === current) {
        setError(readError(err, "明细行加载失败，请重试"));
      }
    } finally {
      if (seq.current === current) setLoading(false);
    }
  }, [sourceOrderId]);

  useEffect(() => {
    void load();
  }, [load]);

  const openEdit = (row: DemandLineRow) => {
    setEditing(row);
    setEditQty(row.qty != null ? Number(row.qty) : null);
    setEditReturnQty(row.return_qty != null ? Number(row.return_qty) : null);
    setEditSn(row.serial_numbers ?? "");
    setEditDesc(row.description ?? "");
    setEditPn(row.pn_std ?? "");
    setReason("");
    setSaveError(null);
  };

  const submit = async () => {
    if (!editing) return;
    if (!reason.trim()) {
      setSaveError("修改原因必填（审计留痕）");
      return;
    }
    const updates: Record<string, unknown> = {};
    const qtyNum = editQty ?? 0;
    if (editing.qty == null || Math.abs(Number(editing.qty) - qtyNum) > 1e-9) updates.qty = qtyNum;
    const rqNum = editReturnQty ?? 0;
    if (editing.return_qty == null || Math.abs(Number(editing.return_qty) - rqNum) > 1e-9) updates.return_qty = rqNum;
    if (editSn.trim() !== (editing.serial_numbers ?? "")) updates.serial_numbers = editSn.trim();
    if (editDesc.trim() !== (editing.description ?? "")) updates.description = editDesc.trim();
    if (editPn.trim() !== (editing.pn_std ?? "")) {
      updates.pn_std = editPn.trim();
      updates.pn_raw = editPn.trim();
    }
    if (!Object.keys(updates).length) {
      setSaveError("没有检测到变更");
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      await patchDemandLine(editing.raw_line_id, updates, reason.trim());
      message.success("明细行已修改（override 保护中，Excel 重导不会覆盖）");
      setEditing(null);
      await load();
    } catch (err) {
      setSaveError(readError(err, "修改失败；该行可能未归属项目或字段被拒"));
    } finally {
      setSaving(false);
    }
  };

  const clearField = async (row: DemandLineRow, field: string) => {
    try {
      await clearDemandLineOverride(row.raw_line_id, field, "撤销页面直改，恢复原值");
      message.success(`${field} 已恢复原值`);
      await load();
    } catch (err) {
      message.error(readError(err, "撤销失败"));
    }
  };

  const columns: ColumnsType<DemandLineRow> = [
    { title: "#", dataIndex: "line_no", width: 44 },
    {
      title: "PN", dataIndex: "pn_std", width: 160,
      render: (v: string | null, row) => <Space size={0}>
        <Text style={{ fontFamily: "monospace", fontSize: 12 }}>{v ?? "—"}</Text>
        <OverrideBadge field="pn_std" row={row} />
      </Space>,
    },
    {
      title: "描述", dataIndex: "description",
      render: (v: string | null, row) => <Space size={0}>
        <Text style={{ fontSize: 12 }}>{v ?? "—"}</Text>
        <OverrideBadge field="description" row={row} />
      </Space>,
    },
    {
      title: "数量", dataIndex: "qty", width: 90, align: "right",
      render: (v: string | null, row) => <Space size={0}>
        <span>{v ?? "—"}</span>
        {row.manual_override?.qty
          ? <a style={{ fontSize: 11, marginLeft: 4 }} onClick={() => { void clearField(row, "qty"); }}>撤销</a>
          : null}
      </Space>,
    },
    {
      title: "退货数量", dataIndex: "return_qty", width: 90, align: "right",
      render: (v: string | null) => v ?? "—",
    },
    {
      title: "SN", dataIndex: "serial_numbers", width: 180,
      render: (v: string | null) => (
        <Text style={{ fontSize: 11, wordBreak: "break-all" }}>{v ?? "—"}</Text>
      ),
    },
    { title: "来源", dataIndex: "edited_source", width: 100,
      render: (v: string) => v === "page_manual" ? <Tag color="purple">页面</Tag>
        : v === "workbook_manual" ? <Tag color="blue">总表</Tag>
        : <Tag>氚云</Tag> },
    {
      title: "操作", key: "ops", width: 80,
      render: (_v, row) => (
        <Button size="small" onClick={() => openEdit(row)}>编辑</Button>
      ),
    },
  ];

  return (
    <Modal
      open
      width={960}
      title={`需求单明细行 —— ${orderNo}`}
      onCancel={onClose}
      footer={null}
      maskClosable={false}
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          白名单字段可编辑：数量 / 退货数量 / SN / 描述 / PN。修改后字段进入 override
          保护（Excel 重导不覆盖），可逐字段撤销恢复氚云原值。成本列与单头信息请走既有通道。
        </Text>
        {error ? (
          <Alert type="error" showIcon message={error} action={
            <Button size="small" onClick={() => { void load(); }}>重试</Button>
          } />
        ) : null}
        <Table<DemandLineRow>
          rowKey="raw_line_id"
          size="small"
          loading={loading}
          dataSource={rows}
          columns={columns}
          pagination={false}
          scroll={{ x: 880 }}
          locale={{ emptyText: "该单没有有效明细行" }}
        />
      </Space>

      <Modal
        open={editing !== null}
        title={editing ? `编辑第 ${editing.line_no ?? "?"} 行（${editing.pn_std ?? ""}）` : ""}
        confirmLoading={saving}
        okText="保存修改"
        cancelText="取消"
        onCancel={() => { if (!saving) setEditing(null); }}
        onOk={() => { void submit(); }}
        maskClosable={!saving}
      >
        {saveError ? <Alert type="error" showIcon message={saveError} style={{ marginBottom: 12 }} /> : null}
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Space size={12}>
            <div>
              <Text type="secondary" style={{ fontSize: 12, display: "block" }}>数量</Text>
              <InputNumber value={editQty} onChange={setEditQty} min={0} precision={3} style={{ width: 120 }} />
            </div>
            <div>
              <Text type="secondary" style={{ fontSize: 12, display: "block" }}>退货数量</Text>
              <InputNumber value={editReturnQty} onChange={setEditReturnQty} min={0} precision={3} style={{ width: 120 }} />
            </div>
          </Space>
          <div>
            <Text type="secondary" style={{ fontSize: 12, display: "block" }}>PN（改后重新匹配型号主数据）</Text>
            <Input value={editPn} onChange={(e) => setEditPn(e.target.value)} style={{ width: 260 }} />
          </div>
          <div>
            <Text type="secondary" style={{ fontSize: 12, display: "block" }}>SN</Text>
            <Input.TextArea value={editSn} onChange={(e) => setEditSn(e.target.value)} rows={2} />
          </div>
          <div>
            <Text type="secondary" style={{ fontSize: 12, display: "block" }}>描述</Text>
            <Input.TextArea value={editDesc} onChange={(e) => setEditDesc(e.target.value)} rows={2} />
          </div>
          <div>
            <Text type="secondary" style={{ fontSize: 12, display: "block" }}>修改原因（必填，审计留痕）</Text>
            <Input.TextArea value={reason} onChange={(e) => setReason(e.target.value)} rows={2}
              placeholder="如：氚云数量录入错误，按实物更正" />
          </div>
        </Space>
      </Modal>
    </Modal>
  );
}
