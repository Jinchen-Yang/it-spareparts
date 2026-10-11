import { useEffect, useRef, useState } from "react";
import { Alert, Button, Input, Modal, Space, Table, Typography } from "antd";
import { reconcileWbdd, type WbddMissing, type WbddMissingLine, type WbddMissingOrder, type WbddReconcileRequest } from "../../api/maintenanceWbddImport";

interface Props {
  data: WbddMissing | null;
  loading: boolean;
  canReconcile: boolean;
  onRefresh: () => Promise<boolean>;
  onCommitted: () => Promise<boolean>;
}

interface Attempt { batchId: number; payload: WbddReconcileRequest; orders: number; lines: number; linkedLines: number }
const newKey = () => `wbdd-reconcile-${typeof globalThis.crypto?.randomUUID === "function" ? globalThis.crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;

/** D-27: a candidate is applied only with its preview digest and an explicit reason. */
export default function WbddReconciliation({ data, loading, canReconcile, onRefresh, onCommitted }: Props) {
  const scope = `${data?.batch_id ?? ""}:${data?.candidate_digest ?? ""}`;
  const [selection, setSelection] = useState<{ scope: string; orders: string[]; lines: number[] }>({ scope, orders: [], lines: [] });
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(false);
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const attempt = useRef<Attempt | null>(null);
  const inFlight = useRef(false);
  useEffect(() => {
    if (!attempt.current) {
      setSelection({ scope, orders: [], lines: [] });
      setOpen(false);
    }
  }, [scope]);
  const selected = selection.scope === scope ? selection : { scope, orders: [], lines: [] };
  const ready = data?.readiness === "ready" && !!data.candidate_digest && data.batch_id != null;
  const locked = loading || busy || retry || !canReconcile || !ready || needsRefresh;
  const total = selected.orders.length + selected.lines.length;
  const linkedLines = (data?.missing_orders ?? []).filter(row => selected.orders.includes(row.source_order_id)).reduce((sum, row) => sum + (row.linked_site_line_count ?? 0), 0)
    + (data?.missing_lines ?? []).filter(row => selected.lines.includes(row.line_id)).reduce((sum, row) => sum + row.linked_site_line_count, 0);

  const refresh = async () => {
    if (inFlight.current || attempt.current) return;
    try {
      if (await onRefresh()) { setNeedsRefresh(false); setError(null); }
      else setError("差异清单刷新失败，请重试。");
    } catch { setError("差异清单刷新失败，请重试。"); }
  };
  const submit = async () => {
    if (inFlight.current || !canReconcile) return;
    if (!attempt.current) {
      if (!ready || locked || !total || !reason.trim() || reason.trim().length > 1000) return;
      attempt.current = {
        batchId: data!.batch_id!, orders: selected.orders.length, lines: selected.lines.length, linkedLines,
        payload: { expected_digest: data!.candidate_digest!, source_order_ids: [...selected.orders],
          line_ids: [...selected.lines], reason: reason.trim(), idempotency_key: newKey() },
      };
    }
    const current = attempt.current;
    inFlight.current = true;
    setBusy(true); setError(null); setSuccess(null);
    let result;
    try { result = (await reconcileWbdd(current.batchId, current.payload)).data; }
    catch (failure) {
      const status = (failure as { response?: { status?: number } })?.response?.status;
      if (status != null && status >= 400 && status < 500) {
        attempt.current = null;
        setRetry(false); setNeedsRefresh(true); setOpen(false);
        setSelection({ scope, orders: [], lines: [] });
        setError("同步请求未通过校验，请刷新差异清单后重新核对。");
      } else {
        setRetry(true);
        setError("本次同步结果尚未确认。请重试同一次同步，系统会沿用原请求，避免重复操作。");
      }
      inFlight.current = false; setBusy(false);
      return;
    }
    attempt.current = null;
    setRetry(false); setOpen(false); setReason("");
    setSelection({ scope, orders: [], lines: [] });
    const summary = `同步${result.replayed ? "结果已确认" : "已完成"}：作废 ${result.voided_orders} 张需求单、${result.voided_lines} 条需求明细、${result.voided_site_lines} 条关联领用明细、${result.voided_site_issues} 张领用单`;
    try {
      setSuccess(await onCommitted() ? `${summary}，页面已刷新。` : `${summary}；页面刷新失败，请刷新清单。`);
    } catch { setSuccess(`${summary}；页面刷新失败，请刷新清单。`); }
    finally { inFlight.current = false; setBusy(false); }
  };

  const unavailable = data?.readiness === "snapshot_required" || (data !== null && (!data.readiness || (data.readiness === "ready" && !data.candidate_digest)));
  return <section aria-label="上传缺项核对">
    <Space direction="vertical" style={{ width: "100%" }}>
      <Typography.Title level={5}>上传缺项核对</Typography.Title>
      <Typography.Paragraph type="secondary">
        每天分别按本文件出现的单号区间核对，不跨日期连接。单内按原始明细身份核对；局部导出也可能产生缺项，请先核对再确认同步。
      </Typography.Paragraph>
      {data?.coverage && <Typography.Text type="secondary">本次包含 {data.coverage.source_order_count} 张单据；{data.coverage.blocked_order_count} 张暂不判断缺行。{data.coverage.daily_windows.map(window => `${window.date}：${window.from_sequence}–${window.to_sequence}`).join("；")}</Typography.Text>}
      {unavailable ? <Alert type="info" showIcon message="历史上传没有可靠的原件快照，请重新上传原件后核对。" /> : data?.readiness === "blocked" ? <Alert type="warning" showIcon message="本文件存在身份或解析问题，暂不提供缺项同步。请修正后重新上传。" /> : data?.readiness === "not_imported" || !data ? <Typography.Text type="secondary">上传需求单原件后，在这里核对缺项。</Typography.Text> : <>
        {(data.warnings ?? []).map((warning, index) => <Alert key={index} type="warning" showIcon message={warning} />)}
        {data.truncated && <Alert type="warning" showIcon message="候选较多，本次仅显示部分；处理后请刷新继续核对。" />}
        <Table<WbddMissingOrder> aria-label="缺失需求单" rowKey="source_order_id" size="small" loading={loading} pagination={false} scroll={{ x: 600 }} dataSource={data.missing_orders}
          locale={{ emptyText: "当前比较范围内没有缺失需求单" }}
          rowSelection={canReconcile ? { selectedRowKeys: selected.orders, onChange: keys => setSelection(previous => ({ ...previous, scope, orders: keys.map(String) })), getCheckboxProps: () => ({ disabled: locked }) } : undefined}
          columns={[{ title: "缺失需求单", dataIndex: "order_no" }, { title: "项目", dataIndex: "project_name", render: value => value || "未归属" }, { title: "需求明细", dataIndex: "line_count" }, { title: "关联领用明细", dataIndex: "linked_site_line_count", render: value => value ?? 0 }]} />
        <Table<WbddMissingLine> aria-label="缺失需求明细" rowKey="line_id" size="small" loading={loading} pagination={false} scroll={{ x: 650 }} dataSource={data.missing_lines ?? []}
          locale={{ emptyText: "当前比较范围内没有缺失需求明细" }}
          rowSelection={canReconcile ? { selectedRowKeys: selected.lines, onChange: keys => setSelection(previous => ({ ...previous, scope, lines: keys.map(Number) })), getCheckboxProps: () => ({ disabled: locked }) } : undefined}
          columns={[{ title: "需求单号", dataIndex: "order_no" }, { title: "PN", dataIndex: "pn", render: value => value || "未填写" }, { title: "数量", dataIndex: "qty", render: value => value ?? "未填写" }, { title: "项目", dataIndex: "project_name", render: value => value || "未归属" }, { title: "关联领用明细", dataIndex: "linked_site_line_count" }]} />
      </>}
      {error && <Alert type="error" showIcon message={error} />}
      {success && <Alert type="success" showIcon message={success} />}
      <Space wrap>
        {canReconcile && <Button type="primary" danger disabled={retry ? busy : locked || !total} onClick={() => { if (!retry) setReason(""); setOpen(true); }}>{retry ? "重试同一次同步" : `确认同步${total ? `（${selected.orders.length} 张单、${selected.lines.length} 条明细）` : ""}`}</Button>}
        <Button disabled={busy || retry || loading} onClick={() => void refresh()}>刷新差异清单</Button>
      </Space>
      <Modal title="确认缺项同步" open={open} maskClosable={false} okText={retry ? "重试同一次同步" : "确认同步"} cancelText="返回核对"
        onOk={() => void submit()} onCancel={() => { if (!inFlight.current) setOpen(false); }}
        cancelButtonProps={{ disabled: busy }} okButtonProps={{ danger: true, loading: busy, disabled: !canReconcile || busy || (!retry && (!reason.trim() || reason.trim().length > 1000 || locked || !total)) }}>
        <Typography.Paragraph>已选 {attempt.current?.orders ?? selected.orders.length} 张需求单、{attempt.current?.lines ?? selected.lines.length} 条明细，涉及 {attempt.current?.linkedLines ?? linkedLines} 条关联领用明细。确认后将作废所选缺项及其关联领用明细；请确认这些内容确实已从原件删除，而非仅被筛选或裁剪。</Typography.Paragraph>
        <Input.TextArea aria-label="同步原因" placeholder="填写同步原因（必填）" maxLength={1000} showCount rows={3} value={reason} disabled={busy || retry} onChange={event => setReason(event.target.value)} />
        {retry && <Alert type="warning" message="结果尚未确认，将继续使用原选择、原因和同一次请求。" />}
      </Modal>
    </Space>
  </section>;
}
