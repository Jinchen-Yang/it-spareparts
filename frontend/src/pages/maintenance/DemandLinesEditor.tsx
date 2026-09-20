import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert, Button, Input, InputNumber, Modal, Select, Space, Table, Tag, Typography, message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  type DemandLineRow,
  clearDemandLineOverride,
  listDemandLines,
  patchDemandLine,
} from "../../api/maintenanceDemands";
import DemandLineBatchEdit from "./DemandLineBatchEdit";

const { Text } = Typography;

function readError(error: unknown, fallback: string): string {
  if ( typeof error === "object" && error !== null) {
    const detail = (error as { response?: { data?: { detail?: unknown } } })
      .response?.data?.detail;
    if (typeof detail === "string" && detail) return detail;
  }
  return fallback;
}

function isConflict(error: unknown): boolean {
  return (error as { response?: { status?: number } })?.response?.status === 409;
}

/** 撤销入口实际覆盖的字段（v1.36 编辑器白名单）；PN 是一组身份，后端成对恢复。 */
const OVERRIDABLE_FIELDS: { field: string; label: string }[] = [
  { field: "pn_std", label: "PN" },
  { field: "description", label: "描述" },
  { field: "qty", label: "数量" },
  { field: "return_qty", label: "退货数量" },
  { field: "serial_numbers", label: "SN" },
];

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

/** 该行第一个实际被 override 的白名单字段（撤销弹窗的默认对象）。 */
function firstOverrideField(row: DemandLineRow): string {
  for (const { field } of OVERRIDABLE_FIELDS) {
    if (row.manual_override?.[field]) return field;
  }
  return "qty";
}

/**
 * 需求单行编辑器（v1.36 Phase E）：列出单头下的明细行，
 * 白名单字段（数量/退货数量/SN/描述/PN）逐行 Modal 编辑，
 * override 可撤销（恢复氚云原值）。成本列/头字段后端永不接受。
 *
 * OCC：编辑/撤销都带读取时的 digest；行被他人改过 → 409，表单保留、
 * 给「重新加载最新数据」按钮，重载更新 digest 与表单原值，用户确认重填再提交
 * ——绝不静默重试覆盖。
 *
 * 并发：编辑与撤销共用一个同步 ref 锁（任何 await 之前）+ 对话框代次
 * （opEpoch）：关闭/换 sourceOrderId/卸载推进代次，旧请求的成功/错误/finally
 * 一律不碰新对话框状态、不解锁；行数据读取用独立 seq，旧 GET 不回写。
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
  /** 编辑 409 冲突：表单保留，弹「重新加载最新数据」。 */
  const [conflict, setConflict] = useState(false);
  /** 撤销弹窗（受控）：行/字段/原因/冲突态与错误都在弹窗内消化；gone＝行/override 已不存在。 */
  const [clearAsk, setClearAsk] = useState<{
    row: DemandLineRow; field: string; reason: string; conflict: boolean; error: string | null;
    gone?: boolean;
  } | null>(null);
  /** 撤销在途（防双击）。 */
  const [clearing, setClearing] = useState(false);
  /** 表格多选只保存 raw id；打开批量弹窗时再冻结完整行快照。 */
  const [selectedRowKeys, setSelectedRowKeys] = useState<string[]>([]);
  const [batchEditingRows, setBatchEditingRows] = useState<DemandLineRow[] | null>(null);
  const seq = useRef(0);
  /** 操作代次：关闭表单/撤销弹窗、换 sourceOrderId、卸载都推进。 */
  const opEpoch = useRef(0);
  /** 编辑/撤销共用的同步锁：任何 await 之前生效，防快速双击双写。 */
  const writeLock = useRef(false);

  /** 行数据读取：返回刚拿到的 rows（供 409 重载直接用，不依赖异步 render）。 */
  const load = useCallback(async (): Promise<DemandLineRow[] | null> => {
    const current = ++seq.current;
    setLoading(true);
    setError(null);
    try {
      const resp = await listDemandLines(sourceOrderId);
      if (seq.current !== current) return null;
      setRows(resp.data.items);
      setSelectedRowKeys((current) => current.filter((key) =>
        resp.data.items.some((row) => row.raw_line_id === key)));
      return resp.data.items;
    } catch (err) {
      if (seq.current === current) {
        setError(readError(err, "明细行加载失败，请重试"));
      }
      return null;
    } finally {
      if (seq.current === current) setLoading(false);
    }
  }, [sourceOrderId]);

  useEffect(() => {
    void load();
    // cleanup＝换 source / 卸载：seq 递增让旧 GET 不回写。
    return () => { seq.current += 1; };
  }, [load]);

  // 换 sourceOrderId / 卸载：整代作废（旧写请求/旧 GET 都不再回写），锁复位。
  // 旧代的 finally 已被 epoch 挡住不能复位 loading，所以新代自己 reset。
  useEffect(() => {
    opEpoch.current += 1;
    writeLock.current = false;
    setEditing(null);
    setClearAsk(null);
    setSaving(false);
    setClearing(false);
    setSaveError(null);
    setConflict(false);
    setSelectedRowKeys([]);
    setBatchEditingRows(null);
    setError(null);
    return () => {
      opEpoch.current += 1;
      writeLock.current = false;
    };
  }, [sourceOrderId]);

  const openEdit = (row: DemandLineRow) => {
    // 开新编辑＝新代：旧弹窗/旧重载请求的回写不能再覆盖这个新对象。
    opEpoch.current += 1;
    writeLock.current = false;
    setSaving(false);
    setConflict(false);
    setEditing(row);
    setEditQty(row.qty != null ? Number(row.qty) : null);
    setEditReturnQty(row.return_qty != null ? Number(row.return_qty) : null);
    setEditSn(row.serial_numbers ?? "");
    setEditDesc(row.description ?? "");
    setEditPn(row.pn_std ?? "");
    setReason("");
    setSaveError(null);
    setConflict(false);
  };

  /** 把服务器最新行同步进编辑表单（基准值 + digest 一起换）。 */
  const syncEditForm = (fresh: DemandLineRow) => {
    setEditing(fresh);
    setEditQty(fresh.qty != null ? Number(fresh.qty) : null);
    setEditReturnQty(fresh.return_qty != null ? Number(fresh.return_qty) : null);
    setEditSn(fresh.serial_numbers ?? "");
    setEditDesc(fresh.description ?? "");
    setEditPn(fresh.pn_std ?? "");
  };

  /**
   * 编辑 409 后重载：刷新行数据并把表单基准值/digest 换成服务器最新，
   * 用户重填差异后可再次提交。绝不静默重试覆盖。重载期间持锁——表单里的
   * digest 已知是旧的，不能在重载完成前再发一次注定 409 的提交。
   */
  const reloadAfterConflict = async () => {
    if (!editing || writeLock.current) return;
    writeLock.current = true;
    const epoch = opEpoch.current;
    try {
      const freshRows = await load();
      if (opEpoch.current !== epoch) return;
      if (!freshRows) {
        setSaveError("重新加载失败，请重试或关闭后重新打开");
        return;
      }
      const fresh = freshRows.find((r) => r.raw_line_id === editing.raw_line_id);
      if (!fresh) {
        // 行已不存在/已作废：绝不能再拿旧 snapshot 发提交，只给关闭指引。
        setSaveError("该行已不存在或已作废，请关闭后重新打开");
        setConflict(false);
        setEditing((prev) => prev && { ...prev, digest: "" });
        return;
      }
      syncEditForm(fresh);
      setConflict(false);
      setSaveError("已重新加载最新数据；当前值是服务器最新值，请核对后重新修改提交");
    } finally {
      if (opEpoch.current === epoch) writeLock.current = false;
    }
  };

  const submit = async () => {
    if (!editing || writeLock.current) return;
    if (editing.digest === "") {
      // 行已不存在/已作废（409 重载发现）：不能再发旧 snapshot。
      setSaveError("该行已不存在或已作废，请关闭后重新打开");
      return;
    }
    writeLock.current = true;
    const epoch = opEpoch.current;
    if (!reason.trim()) {
      setSaveError("修改原因必填（审计留痕）");
      writeLock.current = false;
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
      writeLock.current = false;
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      await patchDemandLine(editing.raw_line_id, updates, reason.trim(), editing.digest);
      if (opEpoch.current !== epoch) return;
      message.success("明细行已修改");
      setEditing(null);
      await load();
    } catch (err) {
      if (opEpoch.current !== epoch) return;
      if (isConflict(err)) {
        setConflict(true);
        setSaveError(readError(err, "该明细行已被他人修改，请重新加载最新数据后重试"));
      } else {
        setSaveError(readError(err, "修改失败；该行可能未归属项目或字段被拒"));
      }
    } finally {
      // 解锁必须与 loading 复位同代：旧请求的 finally 不能解锁新代的提交。
      if (opEpoch.current === epoch) {
        setSaving(false);
        writeLock.current = false;
      }
    }
  };

  /** 撤销弹窗内 409 后重载：用最新 row/digest 更新弹窗持有者，用户重新确认。 */
  const reloadClearAfterConflict = async () => {
    if (!clearAsk || writeLock.current) return;
    writeLock.current = true;
    const epoch = opEpoch.current;
    try {
      const freshRows = await load();
      if (opEpoch.current !== epoch) return;
      if (!freshRows) {
        setClearAsk({
          ...clearAsk, conflict: false,
          error: "重新加载失败，请重试或关闭弹窗。",
          gone: false,
        });
        return;
      }
      const fresh = freshRows.find((r) => r.raw_line_id === clearAsk.row.raw_line_id);
      if (!fresh || !fresh.manual_override?.[clearAsk.field]) {
        // 行不在/字段已无 override：绝不能拿旧 row/digest 再发撤销，标记 gone
        // 禁确认，只给关闭指引。
        setClearAsk({
          ...clearAsk, conflict: false, gone: true,
          error: "该行最新数据里这个字段已没有待撤销的修改（可能已被他人撤销）；请关闭弹窗查看最新行。",
        });
        return;
      }
      setClearAsk({
        ...clearAsk, row: fresh, conflict: false, gone: false,
        error: "已重新加载最新数据；请核对后重新点「确认撤销」。",
      });
    } finally {
      if (opEpoch.current === epoch) writeLock.current = false;
    }
  };

  const confirmClear = async () => {
    if (!clearAsk || writeLock.current || clearAsk.gone) return;
    writeLock.current = true;
    const epoch = opEpoch.current;
    setClearing(true);
    try {
      await clearDemandLineOverride(
        clearAsk.row.raw_line_id, clearAsk.field, clearAsk.reason.trim(), clearAsk.row.digest,
      );
      if (opEpoch.current !== epoch) return;
      message.success(`${OVERRIDABLE_FIELDS.find((f) => f.field === clearAsk.field)?.label ?? clearAsk.field} 已恢复原值`);
      setClearAsk(null);
      await load();
    } catch (err) {
      if (opEpoch.current !== epoch) return;
      if (isConflict(err)) {
        // 保留弹窗与已填原因：用户点「重新加载最新数据」换新 digest 后重新确认，
        // 禁止自动重试覆盖。
        setClearAsk({
          ...clearAsk, conflict: true,
          error: readError(err, "该明细行已被他人修改，请重新加载最新数据后重试"),
        });
      } else {
        setClearAsk({ ...clearAsk, conflict: false, error: readError(err, "撤销失败，可重试") });
      }
    } finally {
      // 同 submit：旧代的 finally 不得触碰新代的 loading 与锁。
      if (opEpoch.current === epoch) {
        setClearing(false);
        writeLock.current = false;
      }
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
      render: (v: string | null) => v ?? "—",
    },
    {
      title: "退货数量", dataIndex: "return_qty", width: 90, align: "right",
      render: (v: string | null) => v ?? "—",
    },
    {
      title: "SN", dataIndex: "serial_numbers", width: 180,
      render: (v: string | null, row) => <Space size={0} wrap>
        <Text style={{ fontSize: 11, wordBreak: "break-all", whiteSpace: "pre-wrap" }}>{v ?? "—"}</Text>
        <OverrideBadge field="serial_numbers" row={row} />
      </Space>,
    },
    { title: "来源", dataIndex: "edited_source", width: 100,
      render: (v: string) => v === "page_manual" ? <Tag color="purple">页面</Tag>
        : v === "workbook_manual" ? <Tag color="blue">总表</Tag>
        : <Tag>氚云</Tag> },
    {
      title: "操作", key: "ops", width: 120,
      render: (_v, row) => (
        <Space size={8}>
          <Button size="small" onClick={() => openEdit(row)}>编辑</Button>
          {OVERRIDABLE_FIELDS.some((f) => row.manual_override?.[f.field]) ? (
            <Button
              size="small"
              onClick={() => {
                // 开新撤销弹窗＝新代：旧弹窗/旧重载请求的回写不能再覆盖新对象。
                opEpoch.current += 1;
                writeLock.current = false;
                setClearing(false);
                setClearAsk({
                  row, field: firstOverrideField(row), reason: "",
                  conflict: false, error: null, gone: false,
                });
              }}
            >
              撤销
            </Button>
          ) : null}
        </Space>
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
          可修改数量、退货数量、SN、描述和型号；导入遇到已修改字段会按冲突规则处理；
          撤销可恢复该字段首次手工修改前的值。成本列与单头信息请走既有通道。
        </Text>
        {error ? (
          <Alert type="error" showIcon message={error} action={
            <Button size="small" onClick={() => { void load(); }}>重试</Button>
          } />
        ) : null}
        <div>
          <Button
            type="primary"
            disabled={!selectedRowKeys.length || loading}
            onClick={() => setBatchEditingRows(rows.filter((row) =>
              selectedRowKeys.includes(row.raw_line_id)))}
          >
            批量修改选中行{selectedRowKeys.length ? `（${selectedRowKeys.length}）` : ""}
          </Button>
        </div>
        <Table<DemandLineRow>
          rowKey="raw_line_id"
          size="small"
          loading={loading}
          dataSource={rows}
          columns={columns}
          rowSelection={{
            selectedRowKeys,
            onChange: (keys) => setSelectedRowKeys(keys as string[]),
          }}
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
        onCancel={() => {
          if (saving) return;
          // 关闭弹窗＝新代：重载期间在途的旧请求回来不得重开/覆盖（epoch 推进）。
          opEpoch.current += 1;
          writeLock.current = false;
          setEditing(null);
        }}
        onOk={() => { void submit(); }}
        maskClosable={!saving}
      >
        {saveError ? (
          <Alert
            type={conflict ? "warning" : "error"}
            showIcon
            message={saveError}
            style={{ marginBottom: 12 }}
            action={conflict ? (
              <Button size="small" onClick={() => { void reloadAfterConflict(); }}>
                重新加载最新数据
              </Button>
            ) : undefined}
          />
        ) : null}
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
              placeholder="如：数量录入错误，按实物更正" />
          </div>
        </Space>
      </Modal>

      <Modal
        open={clearAsk !== null}
        title={clearAsk ? `撤销「${OVERRIDABLE_FIELDS.find((f) => f.field === clearAsk.field)?.label ?? clearAsk.field}」的页面直改` : ""}
        okText="确认撤销"
        cancelText="取消"
        confirmLoading={clearing}
        okButtonProps={{ disabled: !clearAsk?.reason.trim() || clearAsk?.gone === true }}
        onCancel={() => {
          if (clearing) return;
          // 同编辑弹窗：关闭＝新代，重载期间在途的旧请求不得回写。
          opEpoch.current += 1;
          writeLock.current = false;
          setClearAsk(null);
        }}
        onOk={() => { void confirmClear(); }}
        maskClosable={false}
      >
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Text>
            撤销后会恢复该字段首次手工修改前的值；撤销型号时会一起恢复原型号及其关联。
            之后再导入数据时，会按正常冲突规则处理该字段。
          </Text>
          <div>
            <Text type="secondary" style={{ fontSize: 12, display: "block" }}>选择要撤销的字段</Text>
            <Select
              aria-label="选择要撤销的字段"
              style={{ width: 260 }}
              value={clearAsk?.field}
              disabled={clearing || clearAsk?.gone}
              options={clearAsk ? OVERRIDABLE_FIELDS
                .filter(({ field }) => Boolean(clearAsk.row.manual_override?.[field]))
                .map(({ field, label }) => ({
                  value: field,
                  label: field === "pn_std" ? `${label}（型号及其关联一起恢复）` : label,
                })) : []}
              onChange={(field) => setClearAsk((previous) => previous ? {
                ...previous,
                field,
                conflict: false,
                error: null,
                gone: false,
              } : previous)}
            />
          </div>
          {clearAsk?.error ? (
            <Alert
              type={clearAsk.conflict ? "warning" : "error"}
              showIcon
              message={clearAsk.error}
              style={{ marginBottom: 4 }}
              action={clearAsk.conflict ? (
                <Button size="small" onClick={() => { void reloadClearAfterConflict(); }}>
                  重新加载最新数据
                </Button>
              ) : undefined}
            />
          ) : null}
          <div>
            <Text type="secondary" style={{ fontSize: 12, display: "block" }}>撤销原因（必填，审计留痕）</Text>
            <Input.TextArea
              value={clearAsk?.reason ?? ""}
              onChange={(e) => setClearAsk((prev) =>
                prev ? { ...prev, reason: e.target.value } : prev)}
              rows={2}
              placeholder="如：页面改错了，恢复修改前的值"
            />
          </div>
        </Space>
      </Modal>

      {batchEditingRows ? (
        <DemandLineBatchEdit
          sourceOrderId={sourceOrderId}
          rows={batchEditingRows}
          onClose={() => setBatchEditingRows(null)}
          onCommitted={load}
        />
      ) : null}
    </Modal>
  );
}
