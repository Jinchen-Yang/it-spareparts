import { useEffect, useState } from "react";
import {
  Card, Input, Select, Space, Button, Modal, InputNumber, Form, message, Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import ResizableTable from "../components/ResizableTable";
import PageHeader from "../components/PageHeader";
import api from "../api";

interface InvRow {
  id: number;
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
}

const money = (v: number | null) => (v == null ? "-" : `¥${v.toLocaleString()}`);

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
    { title: "型号 (PN)", dataIndex: "pn_std", width: 180, fixed: "left" },
    { title: "仓库", dataIndex: "warehouse", width: 120 },
    { title: "描述", dataIndex: "description", ellipsis: true },
    {
      title: "可用数量", dataIndex: "display_qty", width: 110, align: "right",
      render: (v, r) => (
        <span>
          {v} {r.is_qty_overridden && <Tag color="gold">人工</Tag>}
        </span>
      ),
    },
    { title: "源系统数量", dataIndex: "source_qty", width: 100, align: "right" },
    { title: "安全库存", dataIndex: "safety_stock", width: 90, align: "right" },
    { title: "单位成本", dataIndex: "unit_cost", width: 100, align: "right", render: money },
    { title: "库存金额", dataIndex: "inventory_value", width: 120, align: "right", render: money },
    {
      title: "操作", width: 80, fixed: "right",
      render: (_, r) => <a onClick={() => openEdit(r)}>修正</a>,
    },
  ];

  return (
    <>
      <PageHeader
        title="库存查询"
        subtitle="动态结存（源系统数量 + 人工修正）· 含单位成本与库存金额，可改安全库存"
      />
      <Card>
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
      <ResizableTable
        storageKey="inventory"
        rowKey="id" size="small" loading={loading} columns={cols} dataSource={rows}
        scroll={{ x: 1100 }}
        pagination={{
          current: page, pageSize: 20, total, showSizeChanger: false,
          onChange: (p) => load(p),
        }}
      />

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
    </>
  );
}
