import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { Alert, Button, Input, List, Modal, Space, Spin, Table, Tag, Typography } from "antd";
import {
  getSourceRestoration,
  listSourceRestorations,
  restoreSourceCancellation,
  type SourceRestorationInfo,
  type SourceRestorationResult,
  type SourceRestorationSiteIssue,
  type SourceRestorationSiteLine,
  type SourceRestorationDemand,
} from "../../api/maintenanceSourceCancelCascade";
import { ISSUE_STATUS, readError } from "../../pages/maintenance/panel/panelUtils";
import { TAG_STYLES } from "../../theme";

const wrapText = { overflowWrap: "anywhere" as const };

interface Props {
  projectId: string;
  /** 与确认作废同权限（action_maintenance_demand_delete）；无权限整个入口隐藏。 */
  canConfirm: boolean;
  sessionToken: string;
  onChanged: () => Promise<boolean>;
}

/** 状态标签只用主题 token（TAG_STYLES，对比度 ≥ 4.5:1）或 antd default 预设，
 * 不得回退 orange/green/red 等预设（实测对比度不足 4.5:1）。 */
const STATE_TAGS: Record<string, { label: string; color?: string; style?: CSSProperties }> = {
  restorable: { label: "可恢复", style: TAG_STYLES.warning },
  conflict: { label: "冲突", style: TAG_STYLES.danger },
  restored: { label: "已恢复", style: TAG_STYLES.success },
  unavailable: { label: "不可自动恢复", color: "default" },
};

const statusLabel = (value: string | null) =>
  ISSUE_STATUS[value ?? ""]?.label ?? value ?? "—";

function newKey() {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `cancel-restore-${suffix}`;
}

/** D-0022：误取消确认的完整恢复入口——预览→二次确认→回执；冲突逐项展示，零写不误报。 */
export default function SourceCancelRestorationSection({ projectId, canConfirm, sessionToken, onChanged }: Props) {
  const [items, setItems] = useState<SourceRestorationInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<SourceRestorationResult | null>(null);
  const [selected, setSelected] = useState<SourceRestorationInfo | null>(null);
  const [detail, setDetail] = useState<SourceRestorationInfo | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailConflicts, setDetailConflicts] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const [doubleConfirm, setDoubleConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [retryUnknown, setRetryUnknown] = useState(false);
  const mounted = useRef(true);
  const attempt = useRef<{ sourceOrderId: string; body: {
    expected_digest: string; reason: string; idempotency_key: string } } | null>(null);
  const inFlight = useRef(false);
  const detailSeq = useRef(0);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; detailSeq.current += 1; };
  }, []);

  // 弹窗打开期间账号可能被切换：与确认作废入口同口径，按 localStorage token
  // 比对打开时的会话标识；不一致就关弹窗、清旧预览与 attempt，不向新账号提交。
  const sessionIsCurrent = useCallback(
    () => mounted.current && (localStorage.getItem("token") ?? "") === sessionToken,
    [sessionToken]);

  const abandonForSessionSwitch = useCallback(() => {
    detailSeq.current += 1;
    attempt.current = null;
    setSelected(null); setDetail(null); setDetailError(null); setDetailConflicts([]);
    setDoubleConfirm(false); setRetryUnknown(false); setReason("");
    setListError("账号已切换，本次恢复已取消；请用当前账号重新打开核对。");
  }, []);

  const loadList = useCallback(async () => {
    if (!canConfirm || !mounted.current) return;
    setLoading(true);
    try {
      const response = await listSourceRestorations(projectId);
      if (!mounted.current) return;
      setItems(response.data.items);
      setListError(null);
    } catch (failure) {
      if (!mounted.current) return;
      setItems([]);
      setListError(readError(failure, "恢复清单加载失败，请重试。"));
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, [projectId, canConfirm]);

  useEffect(() => { void loadList(); }, [loadList]);

  if (!canConfirm) return null;

  const openPreview = async (info: SourceRestorationInfo) => {
    if (inFlight.current || !mounted.current || !sessionIsCurrent()) return;
    const seq = ++detailSeq.current;
    setSelected(info); setDetail(null); setDetailError(null); setDetailConflicts([]);
    setReason(""); setRetryUnknown(false); setReceipt(null);
    setDetailLoading(true);
    try {
      const response = await getSourceRestoration(projectId, info.source_order_id);
      if (!mounted.current || seq !== detailSeq.current) return;
      if (!sessionIsCurrent()) { abandonForSessionSwitch(); return; }
      setDetail(response.data);
    } catch (failure) {
      if (!mounted.current || seq !== detailSeq.current) return;
      if (!sessionIsCurrent()) { abandonForSessionSwitch(); return; }
      setDetailError(readError(failure, "恢复预览加载失败，请重试。"));
    } finally {
      if (mounted.current && seq === detailSeq.current) setDetailLoading(false);
    }
  };

  const closePreview = () => {
    if (inFlight.current) return;
    detailSeq.current += 1;
    attempt.current = null;
    setSelected(null); setDetail(null); setDetailError(null); setDetailConflicts([]);
    setDoubleConfirm(false); setRetryUnknown(false); setReason("");
  };

  const counts = detail
    ? `${detail.restorable_demands?.length ?? 0} 条需求明细、`
      + `${detail.restorable_site_lines?.length ?? 0} 条领用行、`
      + `${detail.restorable_site_issues?.length ?? 0} 张领用单头`
    : "";

  const performRestore = async () => {
    const current = detail;
    if (inFlight.current || !current || !mounted.current) return;
    // 首次提交与未知结果重试都先比对会话：换过账号的旧弹窗不得再提交。
    if (!sessionIsCurrent()) { abandonForSessionSwitch(); return; }
    if (!attempt.current) {
      if (current.state !== "restorable" || !current.expected_digest
          || !reason.trim() || reason.trim().length > 1000) return;
      attempt.current = {
        sourceOrderId: current.source_order_id,
        body: { expected_digest: current.expected_digest, reason: reason.trim(), idempotency_key: newKey() },
      };
    }
    inFlight.current = true;
    setBusy(true); setDetailError(null); setDetailConflicts([]); setDoubleConfirm(false);
    try {
      const response = await restoreSourceCancellation(
        projectId, attempt.current.sourceOrderId, attempt.current.body, sessionToken);
      if (!mounted.current) return;
      if (!sessionIsCurrent()) { abandonForSessionSwitch(); return; }
      attempt.current = null;
      setRetryUnknown(false);
      detailSeq.current += 1;
      setSelected(null); setDetail(null); setReason(""); setDoubleConfirm(false);
      const result = response.data;
      setReceipt(result);
      setListError(null);
      // 回执立即可见；项目刷新与清单刷新失败不掩盖已恢复的事实。
      void (async () => {
        try { await onChanged(); } catch { /* 已有回执，只报清单刷新。 */ }
        await loadList();
      })().catch(() => undefined);
    } catch (failure) {
      if (!mounted.current) return;
      if (!sessionIsCurrent()) { abandonForSessionSwitch(); return; }
      const status = (failure as { response?: { status?: number } })?.response?.status;
      if (status != null && status >= 400 && status < 500) {
        attempt.current = null;
        setRetryUnknown(false);
        const payload = (failure as { response?: { data?: { detail?: {
          message?: string; conflicts?: { reason: string }[] } } } })?.response?.data?.detail;
        setDetailError(payload?.message ?? readError(failure, status === 403
          ? "无权恢复此项目，请核对账号及项目权限。" : "恢复未执行，请刷新预览后重新核对。"));
        setDetailConflicts((payload?.conflicts ?? []).map(row => row.reason));
      } else {
        setRetryUnknown(true);
        setDetailError("本次恢复结果尚未确认，请重试同一次恢复；系统将沿用原原因和请求，避免重复操作。");
      }
    } finally {
      inFlight.current = false;
      if (mounted.current) setBusy(false);
    }
  };

  return <Space direction="vertical" size={8} style={{ width: "100%", minWidth: 0 }}>
    <Typography.Text strong>已确认作废恢复</Typography.Text>
    <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
      误确认「来源单已取消」后，按确认回执精确恢复本次连带作废的需求与领用；恢复前逐项核对，数据有变即整次拒绝。
    </Typography.Paragraph>
    {receipt ? <Alert type="success" showIcon closable onClose={() => setReceipt(null)}
      message={receipt.status === "already_restored"
        ? `该确认此前已恢复（${receipt.restored_by ?? "未知操作人"} 于 ${receipt.restored_at ?? "未知时间"}），本次未重复写入。`
        : `已恢复：${receipt.restored_lines} 条需求明细、${receipt.restored_site_lines} 条领用行、`
          + `${receipt.restored_site_issues} 张领用单头（${receipt.restored_at}）。`} /> : null}
    {(receipt?.warnings ?? []).map((warning, index) =>
      <Alert key={index} type="warning" showIcon message={warning} />)}
    {listError ? <Alert type="error" showIcon message={listError} /> : null}
    <Button loading={loading} disabled={busy} onClick={() => void loadList()}>刷新恢复清单</Button>
    <List loading={loading} dataSource={items}
      locale={{ emptyText: listError ? "恢复清单暂不可用" : "当前没有可恢复的取消确认" }}
      renderItem={info => <List.Item key={`${info.batch_id}:${info.source_order_id}`}>
        <Space direction="vertical" size={4} style={{ width: "100%", minWidth: 0 }}>
          <Space wrap>
            <Typography.Text strong style={wrapText}>{info.order_no ?? info.source_order_id}</Typography.Text>
            <Tag color={STATE_TAGS[info.state]?.color ?? "default"} style={STATE_TAGS[info.state]?.style}>
              {STATE_TAGS[info.state]?.label ?? info.state}
            </Tag>
            <Button disabled={busy} onClick={() => void openPreview(info)}>查看恢复预览</Button>
          </Space>
          <Typography.Text type="secondary" style={wrapText}>
            {info.confirmed_by ?? "未知"} 于 {info.confirmed_at ?? "未知时间"} 确认作废：
            {info.voided_lines} 条需求明细、{info.voided_site_lines} 条领用行、{info.voided_site_issues} 张领用单头
          </Typography.Text>
          {info.conflicts.map((conflict, index) => <Alert key={index} type="warning" showIcon
            message={conflict.reason} />)}
        </Space>
      </List.Item>} />
    <Modal title={<span style={wrapText}>恢复预览 · {selected?.order_no ?? selected?.source_order_id}</span>}
      open={selected !== null} width={760} style={{ maxWidth: "calc(100vw - 24px)" }}
      maskClosable={false} onCancel={closePreview} closable={!busy} keyboard={!busy}
      footer={<Space wrap>
        <Button disabled={busy} onClick={closePreview}>返回核对</Button>
        {detail?.state === "restorable" ? <Button type="primary" danger loading={busy}
          disabled={busy || !detail?.expected_digest || !reason.trim() || reason.trim().length > 1000}
          onClick={() => setDoubleConfirm(true)}>确认恢复</Button> : null}
      </Space>}>
      <Space direction="vertical" size={12} style={{ width: "100%", minWidth: 0 }}>
        {detailLoading ? <Spin aria-label="加载恢复预览" /> : null}
        {detailError ? <Alert type={retryUnknown ? "warning" : "error"} showIcon message={detailError}
          action={retryUnknown
            ? <Button danger loading={busy} onClick={() => void performRestore()}>重试同一次恢复</Button>
            : (!detail && selected ? <Button onClick={() => void openPreview(selected)}>重试预览</Button> : undefined)} /> : null}
        {detailConflicts.map((reasonText, index) => <Alert key={index} type="error" showIcon message={reasonText} />)}
        {detail ? <>
          <Typography.Text>
            {detail.confirmed_by ?? "未知"} 于 {detail.confirmed_at ?? "未知时间"} 确认作废
            （原因：{detail.confirm_reason ?? "未填写"}）。
          </Typography.Text>
          {detail.state === "conflict" ? <Alert type="error" showIcon
            message="相关数据已变化，本次不可恢复；请逐项核对以下冲突原因并人工处理。" /> : null}
          {detail.conflicts.map((conflict, index) => <Alert key={index} type="error" showIcon
            message={conflict.reason} />)}
          {detail.state === "restored" ? <Alert type="success" showIcon message={
            detail.restore_receipt
              ? `该确认已于 ${detail.restore_receipt.restored_at ?? "未知时间"} 由 ${detail.restore_receipt.operated_by ?? "未知操作人"} 恢复；如需再次核对请联系管理员。`
              : "该确认已通过实名恢复流程恢复。"
          } /> : null}
          {detail.state === "unavailable" ? <Alert type="warning" showIcon
            message="历史确认回执缺少恢复前置快照，不可自动恢复；需人工按实名更正流程处理。" /> : null}
          <Typography.Text strong>可恢复需求明细（{detail.restorable_demands?.length ?? 0} 条）</Typography.Text>
          <Table<SourceRestorationDemand> aria-label="可恢复需求明细" rowKey="line_id" size="small"
            pagination={false} scroll={{ x: 420 }} dataSource={detail.restorable_demands ?? []}
            locale={{ emptyText: "无可恢复需求明细" }} columns={[
              { title: "PN", dataIndex: "pn", render: value => <span style={wrapText}>{value || "未填写"}</span> },
              { title: "需求数量", dataIndex: "qty", width: 110, render: value => value ?? "未填写" },
            ]} />
          <Typography.Text strong>可恢复领用行（{detail.restorable_site_lines?.length ?? 0} 条）</Typography.Text>
          <Table<SourceRestorationSiteLine> aria-label="可恢复领用行" rowKey="issue_line_id" size="small"
            pagination={false} scroll={{ x: 520 }} dataSource={detail.restorable_site_lines ?? []}
            locale={{ emptyText: "无可恢复领用行" }} columns={[
              { title: "领用单号", dataIndex: "issue_no", width: 160, render: value => <span style={wrapText}>{value || "未填写"}</span> },
              { title: "PN", dataIndex: "pn", render: value => <span style={wrapText}>{value || "未填写"}</span> },
              { title: "实际领用量", dataIndex: "quantity", width: 110 },
            ]} />
          <Typography.Text strong>可恢复领用单头（{detail.restorable_site_issues?.length ?? 0} 张）</Typography.Text>
          <Table<SourceRestorationSiteIssue> aria-label="可恢复领用单头" rowKey="issue_id" size="small"
            pagination={false} scroll={{ x: 420 }} dataSource={detail.restorable_site_issues ?? []}
            locale={{ emptyText: "无可恢复领用单头" }} columns={[
              { title: "领用单号", dataIndex: "issue_no", width: 160, render: value => <span style={wrapText}>{value || "未填写"}</span> },
              { title: "状态变化", key: "status", render: (_, row) =>
                `${statusLabel(row.voided_status)} → ${statusLabel(row.original_status)}` },
            ]} />
          {detail.state === "restorable" ? <>
            <Alert type="info" showIcon message="只恢复本次确认连带作废的对象；确认前已因其他原因作废的行不复活。已登记的返还、库存等证据与 requirements 更正只写反向记录，不删除。" />
            <Input.TextArea aria-label="恢复原因" placeholder="填写恢复原因（必填）" value={reason}
              disabled={busy || retryUnknown} maxLength={1000} showCount rows={3}
              onChange={event => setReason(event.target.value)} />
          </> : null}
        </> : null}
      </Space>
    </Modal>
    <Modal title="二次确认恢复" open={doubleConfirm} maskClosable={false}
      okText="确认执行恢复" cancelText="再核对"
      okButtonProps={{ danger: true, loading: busy }}
      onOk={() => void performRestore()} onCancel={() => { if (!busy) setDoubleConfirm(false); }}>
      <Space direction="vertical" size={8}>
        <Typography.Text>{`将恢复 ${counts}。`}</Typography.Text>
        <Typography.Text type="secondary">该操作写入反向记录并升版项目工作簿；执行前会在锁内复验，数据有变即整次拒绝。</Typography.Text>
      </Space>
    </Modal>
  </Space>;
}
