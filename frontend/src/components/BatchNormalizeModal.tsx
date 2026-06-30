import { useEffect, useState } from "react";
import { Alert, Button, Checkbox, Modal, Space, Table, Tag, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { batchApply, batchPreview, type BatchPreviewItem } from "../api";

// 批量「应用」冻结：旧规范化会写错（已发生一次生产回滚），等确定性 standardize 系统接入再开。
// 预览保留（只读，可看会改什么），仅停写回。新系统上线时改回 false。
const BATCH_APPLY_FROZEN = true;

const FIELD_OPTS = [
  { label: "描述", value: "description" },
  { label: "品类", value: "category" },
  { label: "品牌", value: "brand" },
];

/** 批量规范化：按近期销售额降序列出会被规范化的备件，勾选后批量应用（锁定字段不动）。 */
export default function BatchNormalizeModal({ open, onClose, onApplied }: {
  open: boolean; onClose: () => void; onApplied: () => void;
}) {
  const [items, setItems] = useState<BatchPreviewItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<number[]>([]);
  const [fields, setFields] = useState<string[]>(["description", "category", "brand"]);
  const [applying, setApplying] = useState(false);

  const load = async (p: number) => {
    setLoading(true);
    try {
      const { data } = await batchPreview(p, 20, true);
      setItems(data.items);
      setSelected(data.items.map((i) => i.part_id));   // 默认全选
      setPage(p);
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "加载失败");
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { if (open) load(1); }, [open]);

  const apply = async () => {
    if (!selected.length || !fields.length) return;
    setApplying(true);
    try {
      const { data } = await batchApply(selected, fields);
      message.success(`已规范化 ${data.applied} 条（跳过 ${data.skipped}）`);
      onApplied();
      load(page);
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "应用失败");
    } finally {
      setApplying(false);
    }
  };

  const cols: ColumnsType<BatchPreviewItem> = [
    { title: "PN", dataIndex: "pn_std", width: 160, ellipsis: true },
    {
      title: "现描述 → 标准描述",
      render: (_, r) => (
        <div style={{ fontSize: 12.5 }}>
          <div style={{ color: "#9c968b", textDecoration: r.changes.includes("description") ? "line-through" : "none" }}>
            {r.description || "—"}
          </div>
          {r.changes.includes("description") && (
            <div style={{ fontWeight: 500, color: "#2a2722" }}>{r.suggestion.canonical_description}</div>
          )}
        </div>
      ),
    },
    {
      title: "建议分类", width: 150,
      render: (_, r) => r.changes.includes("category")
        ? <Tag color="blue">{r.suggestion.category_l1}{r.suggestion.category_l2 ? ` / ${r.suggestion.category_l2}` : ""}</Tag>
        : <span style={{ color: "#ccc" }}>—</span>,
    },
    {
      title: "建议品牌", width: 110,
      render: (_, r) => r.changes.includes("brand") ? <Tag>{r.suggestion.brand_norm}</Tag> : <span style={{ color: "#ccc" }}>—</span>,
    },
    {
      title: "近期销售额", dataIndex: "recent_sales_amount", width: 110,
      render: (v) => v ? `¥${Math.round(v).toLocaleString()}` : "—",
    },
  ];

  return (
    <Modal
      open={open} onCancel={onClose} width={1000} title="批量规范化（按销售额优先）"
      footer={
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <Space>
            <span style={{ fontSize: 12.5, color: "#6b665e" }}>应用字段</span>
            <Checkbox.Group options={FIELD_OPTS} value={fields} onChange={(v) => setFields(v as string[])} />
          </Space>
          <Space>
            <Button disabled={page <= 1 || loading} onClick={() => load(page - 1)}>上一批</Button>
            <Button disabled={items.length < 20 || loading} onClick={() => load(page + 1)}>下一批</Button>
            <Button onClick={onClose}>关闭</Button>
            <Button type="primary" loading={!BATCH_APPLY_FROZEN && applying}
              disabled={BATCH_APPLY_FROZEN || !selected.length || !fields.length} onClick={apply}>
              {BATCH_APPLY_FROZEN ? "应用已暂停" : `应用选中（${selected.length}）`}
            </Button>
          </Space>
        </div>
      }
    >
      {BATCH_APPLY_FROZEN && (
        <Alert type="warning" showIcon style={{ marginBottom: 8 }}
          message="批量「应用」已暂停"
          description="描述标准化引擎正在升级为确定性系统（先识别类型→按字段证据渲染→无证据不猜），避免批量写错。此期间预览仍可查看，但暂不写回；需要的话可单条手工编辑。" />
      )}
      <div style={{ marginBottom: 8, color: "#888", fontSize: 12.5 }}>
        只列出会被规范化的备件，按近期销售额降序（高价值先清）。勾选后应用；已人工锁定的字段不动，应用后字段锁定防氚云重导覆盖。
      </div>
      <Table
        rowKey="part_id" size="small" loading={loading} columns={cols} dataSource={items}
        rowSelection={{ selectedRowKeys: selected, onChange: (k) => setSelected(k as number[]) }}
        pagination={false} scroll={{ y: 440 }}
        locale={{ emptyText: loading ? "加载中…" : "本批没有需要规范化的备件（可点「下一批」）" }}
      />
    </Modal>
  );
}
