import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Input, List, Modal, Pagination, Space, Spin, Table, Tag, Typography } from "antd";
import {
  confirmSourceCancellation,
  getSourceCancellation,
  listSourceCancellations,
  type SourceCancellationCandidate,
  type SourceCancellationConfirmation,
  type SourceCancellationDetail,
  type SourceCancellationSiteLine,
} from "../../api/maintenanceSourceCancelCascade";
import { readError, useLatestRead, type RegisterPanelRefresh } from "../../pages/maintenance/panel/panelUtils";

const PAGE_SIZE = 10;
const wrapText = { overflowWrap: "anywhere" as const };
interface Props {
  projectId: string;
  canConfirm: boolean;
  onChanged: () => Promise<boolean>;
  registerRefresh: RegisterPanelRefresh;
}
interface Attempt {
  batchId: number;
  sourceOrderId: string;
  body: SourceCancellationConfirmation;
}

function newKey() {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `source-cancel-${suffix}`;
}

function SiteLines({ rows, label }: { rows: SourceCancellationSiteLine[]; label: string }) {
  return <Table<SourceCancellationSiteLine>
    aria-label={label} rowKey="issue_line_id" size="small" pagination={false} scroll={{ x: 760 }}
    dataSource={rows} locale={{ emptyText: "无此类领用明细" }}
    columns={[
      { title: "领用单号", dataIndex: "issue_no", width: 150, render: value => <span style={wrapText}>{value || "未填写"}</span> },
      { title: "领用日期", dataIndex: "issue_date", width: 105, render: value => value || "未填写" },
      { title: "PN", dataIndex: "pn", render: value => <span style={wrapText}>{value || "未填写"}</span> },
      { title: "实际领用量", dataIndex: "quantity", width: 105 },
      { title: "关联依据", dataIndex: "link_reason", width: 160 },
      { title: "当前状态", dataIndex: "is_active", width: 90, render: value => value ? "有效" : "已作废" },
    ]}
  />;
}

/** 项目和登录账号切换后，旧选择及在途响应不能进入新的确认上下文。 */
export default function SourceCancelImpactPanel(props: Props) {
  const sessionToken = localStorage.getItem("token") ?? "";
  return <SourceCancelImpactContent key={`${props.projectId}:${sessionToken}`} {...props} sessionToken={sessionToken} />;
}

function SourceCancelImpactContent({ projectId, canConfirm, onChanged, registerRefresh, sessionToken }: Props & { sessionToken: string }) {
  const [page, setPage] = useState(1);
  const [items, setItems] = useState<SourceCancellationCandidate[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [resultWarnings, setResultWarnings] = useState<string[]>([]);
  const [selected, setSelected] = useState<SourceCancellationCandidate | null>(null);
  const [detail, setDetail] = useState<SourceCancellationDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const [retry, setRetry] = useState(false);
  const currentPage = useRef(1);
  const listGeneration = useRef(0);
  const attempt = useRef<Attempt | null>(null);
  const inFlight = useRef(false);
  const detailSeq = useRef(0);
  const mounted = useRef(true);
  const readList = useLatestRead();
  const sessionIsCurrent = useCallback(() => mounted.current
    && (localStorage.getItem("token") ?? "") === sessionToken, [sessionToken]);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; detailSeq.current += 1; };
  }, []);

  const loadList = useCallback(() => readList(async (isCurrent) => {
    if (attempt.current || !sessionIsCurrent()) return false;
    const generation = listGeneration.current;
    const isValid = () => isCurrent() && sessionIsCurrent() && generation === listGeneration.current;
    setLoading(true);
    try {
      let requestedPage = currentPage.current;
      let response = await listSourceCancellations(projectId, { page: requestedPage, page_size: PAGE_SIZE });
      if (!isValid()) return false;
      const lastPage = Math.max(1, Math.ceil(response.data.total / PAGE_SIZE));
      if (requestedPage > lastPage) {
        requestedPage = lastPage;
        currentPage.current = lastPage;
        setPage(lastPage);
        if (response.data.total > 0) {
          response = await listSourceCancellations(projectId, { page: lastPage, page_size: PAGE_SIZE });
          if (!isValid()) return false;
        }
      }
      setItems(response.data.items);
      setTotal(response.data.total);
      setError(null);
      setNeedsRefresh(false);
      return true;
    } catch (failure) {
      if (isValid()) {
        setItems([]); setTotal(0);
        setError(readError(failure, "已取消影响加载失败，请重试。"));
      }
      return false;
    } finally { if (isValid()) setLoading(false); }
  }), [projectId, readList, sessionIsCurrent]);

  useEffect(() => { void loadList(); }, [loadList]);
  useEffect(() => {
    registerRefresh("source-cancel", loadList);
    return () => registerRefresh("source-cancel", null);
  }, [loadList, registerRefresh]);

  const loadDetail = async (candidate: SourceCancellationCandidate) => {
    const seq = ++detailSeq.current;
    setDetailLoading(true); setDetail(null); setDetailError(null);
    try {
      const response = await getSourceCancellation(projectId, candidate.batch_id, candidate.source_order_id);
      if (seq === detailSeq.current && sessionIsCurrent()) setDetail(response.data);
    } catch (failure) {
      if (seq === detailSeq.current && sessionIsCurrent()) setDetailError(readError(failure, "影响详情加载失败，请重试。"));
    } finally {
      if (seq === detailSeq.current && sessionIsCurrent()) setDetailLoading(false);
    }
  };

  const openDetail = (candidate: SourceCancellationCandidate) => {
    if (inFlight.current || attempt.current || needsRefresh || !sessionIsCurrent()) return;
    setSelected(candidate); setReason(""); setSuccess(null); setResultWarnings([]);
    void loadDetail(candidate);
  };

  const closeDetail = () => {
    if (inFlight.current || attempt.current) return;
    detailSeq.current += 1;
    setSelected(null); setDetail(null); setDetailError(null); setReason("");
  };

  const submit = async () => {
    if (inFlight.current || needsRefresh || !sessionIsCurrent() || !canConfirm) return;
    if (!attempt.current) {
      if (!detail?.executable || !detail.expected_digest || !reason.trim() || reason.trim().length > 1000) return;
      attempt.current = {
        batchId: detail.batch_id, sourceOrderId: detail.source_order_id,
        body: { expected_digest: detail.expected_digest, reason: reason.trim(), idempotency_key: newKey() },
      };
    }
    const current = attempt.current;
    inFlight.current = true;
    // 确认前发起的读不能在 409 之后迟到并解除“必须刷新”的门禁。
    listGeneration.current += 1;
    setLoading(false);
    setBusy(true); setDetailError(null);
    try {
      const response = await confirmSourceCancellation(projectId, current.batchId, current.sourceOrderId, current.body, sessionToken);
      if (!sessionIsCurrent()) return;
      const result = response.data;
      attempt.current = null;
      setRetry(false);
      setSelected(null); setDetail(null); setReason("");
      const counts = `${result.voided_orders} 张需求单、${result.voided_lines} 条需求明细、${result.voided_site_lines} 条精确关联领用`;
      const summary = result.replayed
        ? `已返回原确认结果（${result.confirmed_at}）：${counts}。该记录不代表当前状态`
        : `已确认作废：${counts}`;
      setResultWarnings(result.warnings);
      let refreshed = false;
      try { refreshed = await onChanged(); } catch { /* 写入已成功，只报告刷新失败。 */ }
      if (sessionIsCurrent()) setSuccess(`${summary}；${refreshed ? "项目已刷新。" : "页面刷新失败，请刷新清单。"}`);
    } catch (failure) {
      if (sessionIsCurrent()) {
        const status = (failure as { response?: { status?: number } })?.response?.status;
        if (status != null && status >= 400 && status < 500) {
          attempt.current = null; setRetry(false);
          setSelected(null); setDetail(null); setReason(""); setNeedsRefresh(true);
          setError(status === 409
            ? "候选状态、范围或影响内容已变化，本次未执行，请刷新影响清单后重新核对。"
            : readError(failure, status === 403 ? "无权确认此项目，请核对账号及项目权限后刷新。" : "确认未通过校验，请刷新影响清单后重新核对。"));
        } else {
          setRetry(true);
          setDetailError("本次结果尚未确认，请重试同一次确认。系统将沿用原选择、原因和请求，避免重复操作。");
        }
      }
    } finally {
      inFlight.current = false;
      if (sessionIsCurrent()) setBusy(false);
    }
  };

  return <Card size="small" title="已取消影响" style={{ minWidth: 0 }}>
    <Space direction="vertical" size={12} style={{ width: "100%", minWidth: 0 }}>
      <Typography.Paragraph style={{ marginBottom: 0 }}>
        来源单头已取消时，需求退出有效视图，现场实际领用保留。查看具体影响并填写原因后，才会连带作废精确关联领用。
      </Typography.Paragraph>
      {!canConfirm ? <Alert type="info" showIcon message="当前账号没有连带作废权限，可查看影响清单。" /> : null}
      {error ? <Alert type="error" showIcon message={error} /> : null}
      {success ? <Alert type="success" showIcon message={success} /> : null}
      {resultWarnings.map((warning, index) => <Alert key={index} type="warning" showIcon message={warning} />)}
      <Button loading={loading} disabled={busy || retry} onClick={() => void loadList()}>刷新影响清单</Button>
      <List loading={loading} dataSource={items}
        locale={{ emptyText: error ? "影响清单暂不可用" : "当前没有已取消候选或未知非生效状态告警" }}
        renderItem={candidate => <List.Item key={`${candidate.batch_id}:${candidate.source_order_id}`}>
          <Space direction="vertical" size={8} style={{ width: "100%", minWidth: 0 }}>
            <Typography.Text strong style={wrapText}>{candidate.order_no}</Typography.Text>
            <Space wrap>
              <Tag color={candidate.executable ? "orange" : "default"}>{candidate.data_status || "未生效"}</Tag>
              <Button disabled={busy || retry || needsRefresh} onClick={() => openDetail(candidate)}>查看影响</Button>
            </Space>
            {candidate.warnings.map((warning, index) => <Alert key={index} type="warning" showIcon message={warning} />)}
          </Space>
        </List.Item>}
      />
      <Pagination current={page} total={total} pageSize={PAGE_SIZE} simple hideOnSinglePage
        disabled={busy || retry || needsRefresh || loading} onChange={next => {
          currentPage.current = next; setPage(next); void loadList();
        }} />
    </Space>
    <Modal title={<span style={wrapText}>已取消影响 · {selected?.order_no}</span>} open={selected !== null}
      width={760} style={{ maxWidth: "calc(100vw - 24px)" }} maskClosable={false} onCancel={closeDetail}
      closable={!busy && !retry} keyboard={!busy && !retry}
      footer={<Space wrap>
        <Button disabled={busy || retry} onClick={closeDetail}>返回核对</Button>
        {canConfirm && detail?.executable ? <Button type="primary" danger loading={busy}
          disabled={busy || detailLoading || !detail.expected_digest || !reason.trim() || reason.trim().length > 1000}
          onClick={() => void submit()}>{retry ? "重试同一次确认" : "确认连带作废"}</Button> : null}
      </Space>}>
      <Space direction="vertical" size={12} style={{ width: "100%", minWidth: 0 }}>
        {detailLoading ? <Spin aria-label="加载影响详情" /> : null}
        {detailError ? <Alert type="error" showIcon message={detailError}
          action={!detail && selected ? <Button onClick={() => void loadDetail(selected)}>重试详情</Button> : undefined} /> : null}
        {detail ? <>
          {detail.warnings.map((warning, index) => <Alert key={index} type="warning" showIcon message={warning} />)}
          <Typography.Text strong>需求明细（{detail.demands.length} 条）</Typography.Text>
          <Table aria-label="受影响需求明细" rowKey="line_id" size="small" pagination={false} scroll={{ x: 330 }}
            dataSource={detail.demands} locale={{ emptyText: "无需求明细" }} columns={[
              { title: "PN", dataIndex: "pn", render: value => <span style={wrapText}>{value || "未填写"}</span> },
              { title: "需求数量", dataIndex: "qty", width: 105, render: value => value ?? "未填写" },
              { title: "当前状态", dataIndex: "is_active", width: 90, render: value => value ? "有效" : "已作废" },
            ]} />
          <Typography.Text strong>精确关联领用（{detail.exact_site_lines.length} 条）</Typography.Text>
          <SiteLines rows={detail.exact_site_lines} label="精确关联领用" />
          <Typography.Text strong>无法精确关联的领用保留（{detail.unlinked_site_lines.length} 条）</Typography.Text>
          <SiteLines rows={detail.unlinked_site_lines} label="无法精确关联领用" />
          <Alert type="info" showIcon message="已登记返还、库存、采购、报销、回款和历史成本证据保留，不删除或冲减。已系统作废的需求和领用不会随源单重新生效自动恢复，需走实名恢复或更正。" />
          {canConfirm && detail.executable ? <Input.TextArea aria-label="作废原因" placeholder="填写作废原因（必填）"
            value={reason} disabled={busy || retry} maxLength={1000} showCount rows={3}
            onChange={event => setReason(event.target.value)} /> : null}
        </> : null}
      </Space>
    </Modal>
  </Card>;
}
