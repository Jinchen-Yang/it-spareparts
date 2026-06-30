import { useEffect, useState } from "react";
import { Alert, Button, Checkbox, Modal, Space, Table, Tag, Tooltip, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  batchApply, batchPreview, SPEC_SOURCE_LABEL,
  type BatchPreviewItem, type SpecField,
} from "../api";

// 批量「应用」冻结：等确定性 standardize 系统在生产稳定运行 + 甲方确认后再开。
// 预览已是新确定性引擎的结果（含字段证据/审核状态），仅停写回。新系统开放时改回 false。
const BATCH_APPLY_FROZEN = true;

const FIELD_OPTS = [
  { label: "描述", value: "description" },
  { label: "品类", value: "category" },
  { label: "品牌", value: "brand" },
];

const isAuto = (r: BatchPreviewItem) => r.review_status === "AUTO_OK";

/** 一行的解析字段 + 证据 + 校验（§16 展开行）。每字段标注来源，无证据的字段引擎根本不产出。 */
function EvidenceRows({ r }: { r: BatchPreviewItem }) {
  const specs = r.suggestion.structured_specs || {};
  const errs = r.suggestion.validation_errors || [];
  const keys = Object.keys(specs);
  return (
    <div style={{ padding: "4px 8px", fontSize: 12.5 }}>
      <div style={{ marginBottom: 6, color: "#6b665e" }}>
        识别类型 <Tag>{r.suggestion.object_type || "—"}</Tag>
        {isAuto(r)
          ? <Tag color="green">自动通过</Tag>
          : <Tag color="orange">需人工复核</Tag>}
        {!keys.length && <span style={{ color: "#bbb" }}>（无可抽取字段 → 转人工）</span>}
      </div>
      {keys.length > 0 && (
        <table style={{ borderCollapse: "collapse", width: "100%", maxWidth: 720 }}>
          <thead>
            <tr style={{ color: "#9c968b", textAlign: "left" }}>
              <th style={{ padding: "2px 10px 2px 0", fontWeight: 400 }}>字段</th>
              <th style={{ padding: "2px 10px 2px 0", fontWeight: 400 }}>值</th>
              <th style={{ padding: "2px 10px 2px 0", fontWeight: 400 }}>来源</th>
              <th style={{ padding: "2px 0", fontWeight: 400 }}>证据</th>
            </tr>
          </thead>
          <tbody>
            {keys.map((k) => {
              const f = specs[k] as SpecField;
              const safe = f.source === "DESCRIPTION_EXPLICIT" || f.source === "MODEL_DICTIONARY";
              return (
                <tr key={k}>
                  <td style={{ padding: "2px 10px 2px 0", color: "#6b665e" }}>{k}</td>
                  <td style={{ padding: "2px 10px 2px 0", fontWeight: 500 }}>{f.value}</td>
                  <td style={{ padding: "2px 10px 2px 0" }}>
                    <Tag color={safe ? "blue" : "gold"} style={{ marginInlineEnd: 0 }}>
                      {SPEC_SOURCE_LABEL[f.source] || f.source}
                    </Tag>
                  </td>
                  <td style={{ padding: "2px 0", color: "#9c968b" }}>{f.evidence}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {errs.length > 0 && (
        <div style={{ marginTop: 6, color: "#c0392b" }}>校验：{errs.join("；")}</div>
      )}
    </div>
  );
}

/** 批量规范化：按近期销售额降序列出会被规范化的备件；展开看字段证据，仅自动通过项默认勾选（§17）。 */
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
      setSelected(data.items.filter(isAuto).map((i) => i.part_id));   // §17：仅自动通过项默认勾选
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

  const autoCount = items.filter(isAuto).length;

  const cols: ColumnsType<BatchPreviewItem> = [
    { title: "PN", dataIndex: "pn_std", width: 150, ellipsis: true },
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
      title: "建议分类", width: 140,
      render: (_, r) => r.changes.includes("category")
        ? <Tag color="blue">{r.suggestion.category_l1}{r.suggestion.category_l2 ? ` / ${r.suggestion.category_l2}` : ""}</Tag>
        : <span style={{ color: "#ccc" }}>—</span>,
    },
    {
      title: "建议品牌", width: 100,
      render: (_, r) => r.changes.includes("brand") ? <Tag>{r.suggestion.brand_norm}</Tag> : <span style={{ color: "#ccc" }}>—</span>,
    },
    {
      title: "审核", width: 96,
      render: (_, r) => isAuto(r)
        ? <Tag color="green">自动通过</Tag>
        : (
          <Tooltip title={(r.suggestion.validation_errors || []).join("；") || "缺关键字段/证据不足，转人工"}>
            <Tag color="orange">需复核</Tag>
          </Tooltip>
        ),
    },
    {
      title: "近期销售额", dataIndex: "recent_sales_amount", width: 100,
      render: (v) => v ? `¥${Math.round(v).toLocaleString()}` : "—",
    },
  ];

  return (
    <Modal
      open={open} onCancel={onClose} width={1040} title="批量规范化（按销售额优先 · 确定性引擎）"
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
          message="批量「应用」暂未开放"
          description="下方预览已是新确定性引擎的结果（含字段证据与审核状态）。批量写回待引擎在生产稳定 + 确认后开放；此期间可展开核对，或单条手工编辑（已用新引擎）。" />
      )}
      <div style={{ marginBottom: 8, color: "#888", fontSize: 12.5 }}>
        按近期销售额降序（高价值先清）。点行首 ▸ 展开看每个字段的值/来源/证据。
        <b>仅「自动通过」项默认勾选</b>（类型与分类确定、无校验错、无猜测字段）；「需复核」项不自动写回，交单条人工。
        本批自动通过 {autoCount}/{items.length}。已人工锁定的字段不动。
      </div>
      <Table
        rowKey="part_id" size="small" loading={loading} columns={cols} dataSource={items}
        rowSelection={{
          selectedRowKeys: selected,
          onChange: (k) => setSelected(k as number[]),
          getCheckboxProps: (r) => ({ disabled: BATCH_APPLY_FROZEN && !isAuto(r) }),
        }}
        expandable={{ expandedRowRender: (r) => <EvidenceRows r={r} /> }}
        pagination={false} scroll={{ y: 430 }}
        locale={{ emptyText: loading ? "加载中…" : "本批没有需要规范化的备件（可点「下一批」）" }}
      />
    </Modal>
  );
}
