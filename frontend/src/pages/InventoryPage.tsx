import { useEffect, useState } from "react";
import {
  Card, Input, Space, Button, Modal, InputNumber, Form, message, Tag, Alert, Table, Tooltip,
} from "antd";
import { InfoCircleOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import ResizableTable from "../components/ResizableTable";
import PageHeader from "../components/PageHeader";
import api from "../api";
import { money, splitFixed } from "../utils/format";
import { useTaxBasis } from "../context/TaxBasis";

// 分仓参考行（来自最近库存快照，仅参考；人工修正仍在此层做——修正即改期初）
interface WhRow {
  id: number;
  warehouse: string;
  qty: number | null;
  source_qty: number | null;
  manual_qty: number | null;
  is_qty_overridden: boolean;
  safety_stock: number | null;
  unit_cost: number | null;
  inventory_value: number | null;
  snapshot_date: string | null;
}

// 型号级动态库存行（主口径：期初=最近快照/盘点，之后跟单据流水）
interface DynRow {
  part_id: number;
  pn_std: string;
  description: string | null;
  brand: string | null;
  dynamic_qty: number | null;
  anchor_qty: number | null;
  anchor_date: string | null;
  in_qty: number | null;
  out_sales: number | null;
  out_maint: number | null;
  warehouses: WhRow[];
}

export default function InventoryPage() {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<DynRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<(WhRow & { pn_std: string }) | null>(null);
  const [form] = Form.useForm();
  const { basis } = useTaxBasis();

  const load = async (p = page) => {
    setLoading(true);
    setError(null);
    try {
      const { data } = await api.get("/inventory/dynamic", {
        params: { q: q || undefined, page: p, page_size: 20 },
      });
      setRows(data.items);
      setTotal(data.total);
      setPage(p);
    } catch (e: any) {
      const msg = !e?.response ? "无法连接服务器，请检查网络后重试"
        : e?.response?.data?.detail || "库存加载失败，请稍后重试";
      setRows([]);
      setError(msg);
      message.error(msg);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openEdit = (r: WhRow, pn: string) => {
    setEditing({ ...r, pn_std: pn });
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
      message.success("已修正并记录审计（期初随之更新）");
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

  const whCols = (pn: string): ColumnsType<WhRow> => [
    { title: "仓库", dataIndex: "warehouse", width: 130 },
    { title: "数量(快照)", dataIndex: "qty", width: 100, align: "right",
      render: (v, r) => <span>{v}{r.is_qty_overridden && <Tag color="gold" style={{ marginLeft: 4 }}>人工</Tag>}</span> },
    { title: "安全库存", dataIndex: "safety_stock", width: 90, align: "right",
      render: (v) => v ?? "-" },
    ...(basis !== "ex" ? [{ title: "单位成本(含税)", key: "uc_inc", width: 110, align: "right" as const,
      render: (_: unknown, r: WhRow) => money(splitFixed(r.unit_cost, "ex").inc) }] as ColumnsType<WhRow> : []),
    ...(basis !== "inc" ? [{ title: "单位成本(不含税)", key: "uc_ex", width: 110, align: "right" as const,
      render: (_: unknown, r: WhRow) => money(splitFixed(r.unit_cost, "ex").ex) }] as ColumnsType<WhRow> : []),
    ...(basis !== "ex" ? [{ title: "库存金额(含税)", key: "iv_inc", width: 120, align: "right" as const,
      render: (_: unknown, r: WhRow) => money(splitFixed(r.inventory_value, "ex").inc) }] as ColumnsType<WhRow> : []),
    ...(basis !== "inc" ? [{ title: "库存金额(不含税)", key: "iv_ex", width: 120, align: "right" as const,
      render: (_: unknown, r: WhRow) => money(splitFixed(r.inventory_value, "ex").ex) }] as ColumnsType<WhRow> : []),
    { title: "快照日期", dataIndex: "snapshot_date", width: 100,
      render: (v) => v || "-" },
    { title: "操作", width: 70, render: (_, r) => <a onClick={() => openEdit(r, pn)}>修正</a> },
  ];

  const cols: ColumnsType<DynRow> = [
    { title: "型号 (PN)", dataIndex: "pn_std", width: 190, fixed: "left",
      render: (v) => <span style={{ fontFamily: "monospace", fontSize: 12.5 }}>{v}</span> },
    { title: "描述", dataIndex: "description", ellipsis: true },
    { title: "品牌", dataIndex: "brand", width: 100, ellipsis: true },
    { title: (
        <Tooltip title="动态可用 = 期初（最近盘点/库存快照）+ 之后采购入库 − 销售出库 − 维保出库（退货冲抵）。单据导入即时生效，不依赖重导库存。">
          动态可用 <InfoCircleOutlined style={{ color: "var(--mb-text-3)" }} />
        </Tooltip>
      ), dataIndex: "dynamic_qty", width: 110, align: "right",
      render: (v: number | null) => (
        <b style={{ color: (v ?? 0) < 0 ? "var(--mb-danger)" : undefined }}>{v ?? 0}</b>
      ) },
    { title: "期初", dataIndex: "anchor_qty", width: 90, align: "right",
      render: (v, r) => (
        <Tooltip title={r.anchor_date ? `期初 = ${r.anchor_date} 快照/盘点` : "无快照记录：期初按 0，纯流水推算"}>
          <span style={{ color: "var(--mb-text-3)" }}>{v ?? 0}</span>
        </Tooltip>
      ) },
    { title: "期初后入库", dataIndex: "in_qty", width: 100, align: "right",
      render: (v) => <span style={{ color: "#3f7a45" }}>+{v ?? 0}</span> },
    { title: "期初后出库", key: "out", width: 120, align: "right",
      render: (_, r) => (
        <Tooltip title={`销售 ${r.out_sales ?? 0} · 维保 ${r.out_maint ?? 0}`}>
          <span style={{ color: "#c0524a" }}>-{(r.out_sales ?? 0) + (r.out_maint ?? 0)}</span>
        </Tooltip>
      ) },
    { title: "分仓(参考)", key: "wh", width: 110,
      render: (_, r) => r.warehouses.length
        ? <span style={{ color: "var(--mb-text-3)" }}>{r.warehouses.length} 仓 · 点击展开</span>
        : <span style={{ color: "var(--mb-text-3)" }}>无快照</span> },
  ];

  return (
    <>
      <PageHeader
        title="库存查询"
        subtitle="动态口径：期初（最近盘点/快照）+ 单据流水实时推算（型号级）；分仓分布为快照参考。8 月盘点导入后期初自动更新"
      />
      <Card>
      <Space style={{ marginBottom: 16 }} wrap>
        <Input.Search
          placeholder="型号 / 描述 / 品牌" style={{ width: 300 }}
          value={q} onChange={(e) => setQ(e.target.value)} onSearch={() => load(1)} allowClear
        />
      </Space>
      {error && (
        <Alert
          type="error" showIcon message="加载失败" description={error} style={{ marginBottom: 12 }}
          action={<Button size="small" onClick={() => load(page)}>重试</Button>}
        />
      )}
      <ResizableTable<DynRow>
        storageKey="inventory-dyn"
        rowKey="part_id" size="small" loading={loading} columns={cols} dataSource={rows}
        locale={{ emptyText: error ? "加载失败，请点上方重试" : "暂无数据" }}
        scroll={{ x: 1100 }}
        expandable={{
          rowExpandable: (r) => r.warehouses.length > 0,
          expandedRowRender: (r) => (
            <Table<WhRow>
              size="small" rowKey="id" pagination={false}
              columns={whCols(r.pn_std)} dataSource={r.warehouses}
            />
          ),
        }}
        pagination={{
          current: page, pageSize: 20, total, showSizeChanger: false,
          showTotal: (t) => `共 ${t} 个型号`,
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
        <Alert type="info" showIcon style={{ marginBottom: 12 }}
               message="修正的是快照（期初）数量——动态可用会随之变化" />
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
