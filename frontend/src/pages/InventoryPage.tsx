import { useEffect, useState } from "react";
import {
  Card, Table, Input, Select, Space, Button, Modal, Drawer, InputNumber, Form,
  message, Tag, Tooltip, Switch, Alert,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import api from "../api";

interface InvRow {
  id: number;
  part_id: number;
  pn_std: string;
  warehouse: string;
  display_qty: number | null;
  source_qty: number | null;
  manual_qty: number | null;
  is_qty_overridden: boolean;
  safety_stock: number | null;
  description: string | null;
  unit: string | null;
  unit_cost: number | null;
  inventory_value: number | null;
  computed_qty: number | null;     // 流水回放在库（§7.6）
  ledger_diff: number | null;      // computed - source（对账差异）
}

interface Movement {
  id: number;
  movement_date: string | null;
  doc_type: string;
  doc_type_raw: string | null;
  doc_no: string | null;
  warehouse: string;
  direction: number;
  qty: number;
  signed_qty: number;
  is_absolute: boolean;
  unit_price: number | null;
  balance: number | null;
}

interface ReconRow {
  part_id: number;
  pn_std: string | null;
  warehouse: string;
  computed_qty: number | null;
  snapshot_qty: number | null;
  diff: number;
  status: string;
}

const money = (v: number | null) => (v == null ? "-" : `¥${v.toLocaleString()}`);

const DOC_TYPE: Record<string, string> = {
  receipt: "收货入库", issue: "出库发货", transfer_in: "调拨入", transfer_out: "调拨出",
  return_in: "退货返库", assembly_in: "组装入库", assembly_out: "组装领料",
  stocktake: "盘点", stocktake_gain: "盘盈", stocktake_loss: "盘亏",
  direct_ship: "直发", other: "其它",
};

const RECON_STATUS: Record<string, { label: string; color: string }> = {
  match: { label: "一致", color: "green" },
  diff: { label: "有差异", color: "red" },
  ledger_only: { label: "仅流水（无快照）", color: "orange" },
  snapshot_only: { label: "仅快照（无流水）", color: "gold" },
};

function DiffTag({ v }: { v: number | null }) {
  if (v == null) return <span style={{ color: "var(--mb-text-3)" }}>-</span>;
  if (v === 0) return <Tag color="green">0</Tag>;
  return <Tag color="red">{v > 0 ? `+${v}` : v}</Tag>;
}

export default function InventoryPage() {
  const [warehouses, setWarehouses] = useState<string[]>([]);
  const [warehouse, setWarehouse] = useState<string | undefined>();
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<InvRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState<InvRow | null>(null);
  const [form] = Form.useForm();

  // 流水抽屉
  const [ledgerFor, setLedgerFor] = useState<InvRow | null>(null);
  const [ledger, setLedger] = useState<Movement[]>([]);
  const [ledgerLoading, setLedgerLoading] = useState(false);

  // 对账弹窗
  const [reconOpen, setReconOpen] = useState(false);
  const [recon, setRecon] = useState<ReconRow[]>([]);
  const [reconSummary, setReconSummary] = useState<{ match: number; diff: number; total: number } | null>(null);
  const [reconLoading, setReconLoading] = useState(false);
  const [onlyDiff, setOnlyDiff] = useState(true);

  useEffect(() => {
    api.get("/inventory/warehouses").then((r) => setWarehouses(r.data));
  }, []);

  const load = async (p = page) => {
    setLoading(true);
    try {
      const { data } = await api.get("/inventory", {
        params: { warehouse, q: q || undefined, page: p, page_size: 20 },
      });
      setRows(data.items);
      setTotal(data.total);
      setPage(p);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [warehouse]);

  const openLedger = async (r: InvRow) => {
    setLedgerFor(r);
    setLedger([]);
    setLedgerLoading(true);
    try {
      const { data } = await api.get(`/inventory/parts/${r.part_id}/ledger`, {
        params: { warehouse: r.warehouse },
      });
      setLedger(data.items);
    } finally {
      setLedgerLoading(false);
    }
  };

  const openRecon = async (od = onlyDiff) => {
    setReconOpen(true);
    setReconLoading(true);
    try {
      const { data } = await api.get("/inventory/reconciliation", {
        params: { warehouse, only_diff: od },
      });
      setRecon(data.rows);
      setReconSummary(data.summary);
    } finally {
      setReconLoading(false);
    }
  };

  const openEdit = (r: InvRow) => {
    setEditing(r);
    form.setFieldsValue({ manual_qty: r.manual_qty, safety_stock: r.safety_stock, reason: "" });
  };

  const submitEdit = async () => {
    const v = await form.validateFields();
    try {
      await api.put(`/inventory/${editing!.id}`, {
        manual_qty: v.manual_qty,
        safety_stock: v.safety_stock,
        reason: v.reason,
      });
      message.success("已修正并记录审计");
      setEditing(null);
      load();
    } catch {
      message.error("修正失败（需要管理员权限）");
    }
  };

  const clearOverride = async () => {
    await api.put(`/inventory/${editing!.id}`, { clear_override: true, reason: "撤销人工修正" });
    message.success("已撤销人工修正，恢复源系统数量");
    setEditing(null);
    load();
  };

  const cols: ColumnsType<InvRow> = [
    { title: "型号 (PN)", dataIndex: "pn_std", width: 170, fixed: "left" },
    { title: "仓库", dataIndex: "warehouse", width: 100 },
    { title: "描述", dataIndex: "description", ellipsis: true },
    {
      title: "可用数量", dataIndex: "display_qty", width: 100, align: "right",
      render: (v, r) => (
        <span>{v} {r.is_qty_overridden && <Tag color="gold">人工</Tag>}</span>
      ),
    },
    {
      title: (
        <Tooltip title="源系统库存快照（导入时覆盖），作对账基准">源系统数量</Tooltip>
      ),
      dataIndex: "source_qty", width: 100, align: "right",
    },
    {
      title: (
        <Tooltip title="由出入流水按时间回放求得的永续在库（§7.6）">动态结存</Tooltip>
      ),
      dataIndex: "computed_qty", width: 100, align: "right",
      render: (v) => (v == null ? <span style={{ color: "var(--mb-text-3)" }}>无流水</span> : v),
    },
    {
      title: (
        <Tooltip title="动态结存 − 源系统数量。非 0 说明流水与快照对不上（可能流水不全）">对账差异</Tooltip>
      ),
      dataIndex: "ledger_diff", width: 100, align: "right",
      render: (v) => <DiffTag v={v} />,
    },
    { title: "安全库存", dataIndex: "safety_stock", width: 90, align: "right" },
    { title: "单位成本", dataIndex: "unit_cost", width: 100, align: "right", render: money },
    { title: "库存金额", dataIndex: "inventory_value", width: 110, align: "right", render: money },
    {
      title: "操作", width: 110, fixed: "right",
      render: (_, r) => (
        <Space size="small">
          <a onClick={() => openLedger(r)}>流水</a>
          <a onClick={() => openEdit(r)}>修正</a>
        </Space>
      ),
    },
  ];

  const ledgerCols: ColumnsType<Movement> = [
    { title: "日期", dataIndex: "movement_date", width: 110,
      render: (t) => (t ? new Date(t).toLocaleDateString("zh-CN") : "-") },
    { title: "单据类型", dataIndex: "doc_type", width: 110,
      render: (t, r) => <Tooltip title={r.doc_type_raw || ""}>{DOC_TYPE[t] || t}</Tooltip> },
    { title: "单据号", dataIndex: "doc_no", width: 130, ellipsis: true },
    { title: "仓库", dataIndex: "warehouse", width: 90 },
    {
      title: "出入数量", dataIndex: "signed_qty", width: 100, align: "right",
      render: (v, r) =>
        r.is_absolute
          ? <Tag color="blue">盘点={r.qty}</Tag>
          : <span style={{ color: v > 0 ? "var(--mb-success)" : v < 0 ? "var(--mb-danger)" : undefined }}>
              {v > 0 ? `+${v}` : v}
            </span>,
    },
    { title: "结存", dataIndex: "balance", width: 90, align: "right" },
    { title: "单价", dataIndex: "unit_price", width: 90, align: "right", render: money },
  ];

  const reconCols: ColumnsType<ReconRow> = [
    { title: "型号 (PN)", dataIndex: "pn_std", width: 180 },
    { title: "仓库", dataIndex: "warehouse", width: 110 },
    { title: "动态结存（流水）", dataIndex: "computed_qty", width: 130, align: "right",
      render: (v) => (v == null ? "-" : v) },
    { title: "源系统数量（快照）", dataIndex: "snapshot_qty", width: 140, align: "right",
      render: (v) => (v == null ? "-" : v) },
    { title: "差异", dataIndex: "diff", width: 90, align: "right", render: (v) => <DiffTag v={v} /> },
    { title: "状态", dataIndex: "status", width: 150,
      render: (s) => {
        const m = RECON_STATUS[s] || { label: s, color: "default" };
        return <Tag color={m.color}>{m.label}</Tag>;
      } },
  ];

  return (
    <Card
      title="库存查询"
      extra={<Button onClick={() => openRecon()}>流水 / 快照对账</Button>}
    >
      <Space style={{ marginBottom: 16 }} wrap>
        <Select
          allowClear placeholder="全部仓库" style={{ width: 160 }}
          value={warehouse} onChange={setWarehouse}
          options={warehouses.map((w) => ({ label: w, value: w }))}
        />
        <Input.Search
          placeholder="型号 / 描述" style={{ width: 280 }}
          value={q} onChange={(e) => setQ(e.target.value)} onSearch={() => load(1)} allowClear
        />
      </Space>
      <Table
        rowKey="id" size="small" loading={loading} columns={cols} dataSource={rows}
        scroll={{ x: 1300 }}
        pagination={{
          current: page, pageSize: 20, total, showSizeChanger: false,
          onChange: (p) => load(p),
        }}
      />

      {/* 单型号出入流水明细 */}
      <Drawer
        title={ledgerFor ? `出入流水 · ${ledgerFor.pn_std} @ ${ledgerFor.warehouse}` : "出入流水"}
        open={!!ledgerFor} width={760} onClose={() => setLedgerFor(null)}
      >
        <Alert
          type="info" showIcon style={{ marginBottom: 12 }}
          message="结存按时间逐笔回放：入库 + / 出库 − / 盘点重置 / 直发不计。这就是动态在库的来源。"
        />
        <Table
          rowKey="id" size="small" loading={ledgerLoading} columns={ledgerCols} dataSource={ledger}
          pagination={false} scroll={{ y: "60vh" }}
        />
      </Drawer>

      {/* 对账 */}
      <Modal
        title="流水 / 快照对账" open={reconOpen} onCancel={() => setReconOpen(false)}
        width={920} footer={null}
      >
        <Space style={{ marginBottom: 12 }}>
          <span>仅看有差异</span>
          <Switch checked={onlyDiff} onChange={(v) => { setOnlyDiff(v); openRecon(v); }} />
          {reconSummary && (
            <span style={{ color: "var(--mb-text-3)" }}>
              一致 {reconSummary.match} · 异常 {reconSummary.diff}
            </span>
          )}
        </Space>
        <Table
          rowKey={(r) => `${r.part_id}-${r.warehouse}`} size="small" loading={reconLoading}
          columns={reconCols} dataSource={recon}
          pagination={{ pageSize: 15, showSizeChanger: false }} scroll={{ y: "55vh" }}
        />
      </Modal>

      <Modal
        open={!!editing} title={`修正库存 · ${editing?.pn_std} @ ${editing?.warehouse}`}
        onCancel={() => setEditing(null)} onOk={submitEdit} okText="保存修正"
        footer={[
          editing?.is_qty_overridden && (
            <Button key="clear" danger onClick={clearOverride}>撤销人工修正</Button>
          ),
          <Button key="cancel" onClick={() => setEditing(null)}>取消</Button>,
          <Button key="ok" type="primary" onClick={submitEdit}>保存修正</Button>,
        ]}
      >
        <Form form={form} layout="vertical">
          <Form.Item label={`人工修正数量（源系统数量 ${editing?.source_qty}，留空不改）`} name="manual_qty">
            <InputNumber style={{ width: "100%" }} min={0} />
          </Form.Item>
          <Form.Item label="安全库存" name="safety_stock">
            <InputNumber style={{ width: "100%" }} min={0} />
          </Form.Item>
          <Form.Item label="修改原因" name="reason" rules={[{ required: true, message: "请填写修改原因（写入审计）" }]}>
            <Input.TextArea rows={2} placeholder="如：盘点差异修正" />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}
