import { useEffect, useRef, useState } from "react";
import { Alert, Button, Input, Modal, Space, Table, Tag, Typography } from "antd";
import { correctSiteIssueLine, type SiteIssueLineCorrectionInput } from "../../../api/maintenanceOperations";
import { readError } from "./panelUtils";

export interface SiteIssueReturnSelection {
  issueId: string;
  issueNo: string;
  issueVersion: number;
  lineId: string;
  lineNo: number;
  lineVersion: number;
  pn: string;
  noReturn: boolean | null;
}

type Status = "pending" | "success" | "unchanged" | "conflict" | "failed" | "unknown" | "unsubmitted";
interface Result extends SiteIssueReturnSelection {
  status: Status;
  detail: string;
  /** 首次发送即冻结，网络未知仅重放此载荷及幂等键。 */
  payload?: SiteIssueLineCorrectionInput;
}
const STATUS: Record<Status, { label: string; color: string }> = {
  pending: { label: "等待提交", color: "default" },
  success: { label: "成功", color: "green" },
  unchanged: { label: "未变化", color: "default" },
  conflict: { label: "冲突", color: "orange" },
  failed: { label: "失败", color: "red" },
  unknown: { label: "结果未知", color: "gold" },
  unsubmitted: { label: "未提交", color: "default" },
};
const ACTIONS = [
  { value: false, label: "批量设为应返", intent: "是否应返还：是（应返）" },
  { value: true, label: "批量设为免返", intent: "是否应返还：否（免返）" },
  { value: null, label: "恢复按规则判断", intent: "是否应返还：按规则判断" },
] as const;
const newKey = () => `site-line-correct-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;

function describeFailure(error: unknown): Pick<Result, "status" | "detail"> {
  const status = (error as { response?: { status?: number } })?.response?.status;
  const detail = readError(error, "请求未完成");
  if (status === 409) return { status: "conflict", detail: `请刷新后重新核对：${detail}` };
  if (status && status >= 400 && status < 500) return { status: "failed", detail };
  return { status: "unknown", detail: `可能已保存，请原样核对回执或刷新核实：${detail}` };
}

/** 逐行独立事务；只从上一成功回执接续同单版本，不自动接受冲突的新版本。 */
export default function SiteIssueReturnBatch({ selection, disabled, onChanged }: {
  selection: SiteIssueReturnSelection[];
  disabled: boolean;
  onChanged: () => Promise<boolean>;
}) {
  const [action, setAction] = useState<(typeof ACTIONS)[number] | null>(null);
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<SiteIssueReturnSelection[]>([]);
  const [reason, setReason] = useState("");
  const [results, setResults] = useState<Result[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [refreshFailed, setRefreshFailed] = useState(false);
  const [operator, setOperator] = useState("");
  const busyRef = useRef(false);
  const generation = useRef(0);
  const sessionToken = useRef<string | null>(null);
  const reasonTooLong = Array.from(reason.trim()).length > 1000;
  const hasUnknown = results?.some((row) => row.status === "unknown") ?? false;
  useEffect(() => () => { generation.current += 1; }, []);

  const refresh = async (run: number) => {
    let success = false;
    try { success = await onChanged(); } catch { /* 事实回执保留，刷新失败不重发。 */ }
    if (generation.current === run) setRefreshFailed(!success);
  };

  const submit = async () => {
    if (busyRef.current || !action || !reason.trim() || reasonTooLong) return;
    busyRef.current = true;
    setBusy(true);
    const run = ++generation.current;
    const versions = new Map<string, number>();
    const blockedIssues = new Set<string>();
    const final: Result[] = rows.map((row) => ({ ...row,
      status: row.noReturn === action.value ? "unchanged" : "pending",
      detail: row.noReturn === action.value ? "原始设置已相同，未提交" : "等待提交",
    }));
    setResults([...final]);
    for (const row of final) {
      if (generation.current !== run) return;
      if (row.status === "unchanged") continue;
      if (localStorage.getItem("token") !== sessionToken.current) {
        row.status = "unsubmitted";
        row.detail = "登录会话已变化，请重新加载后核对";
      } else if (blockedIssues.has(row.issueId)) {
        row.status = "unsubmitted";
        row.detail = "同单前行冲突或结果未知，请刷新后重新核对";
      } else {
        row.payload = {
          expected_issue_version: versions.get(row.issueId) ?? row.issueVersion,
          expected_line_version: row.lineVersion,
          changes: { no_return: action.value },
          reason: reason.trim(), idempotency_key: newKey(),
        };
        try {
          const { data } = await correctSiteIssueLine(row.issueId, row.lineId, row.payload);
          if (generation.current !== run) return;
          versions.set(row.issueId, data.issue_version);
          row.status = data.changed ? "success" : "unchanged";
          row.detail = data.changed ? "返还要求已保存" : "服务器确认未变化";
        } catch (error) {
          if (generation.current !== run) return;
          Object.assign(row, describeFailure(error));
          if (row.status === "conflict" || row.status === "unknown") blockedIssues.add(row.issueId);
        }
      }
      setResults([...final]);
    }
    await refresh(run);
    if (generation.current !== run) return;
    busyRef.current = false;
    setBusy(false);
  };

  const retryUnknown = async (row: Result) => {
    if (busyRef.current || !row.payload || localStorage.getItem("token") !== sessionToken.current) return;
    busyRef.current = true;
    setBusy(true);
    const run = generation.current;
    let patch: Pick<Result, "status" | "detail">;
    try {
      const { data } = await correctSiteIssueLine(row.issueId, row.lineId, row.payload);
      patch = { status: data.changed ? "success" : "unchanged", detail: data.idempotent_replay ? "已核对原始成功回执" : "返还要求已保存" };
    } catch (error) {
      // 后续 403/422 等只证明本次被拒，不能据此断言首次没有落库。
      patch = { status: "unknown", detail: `尚未核对首次结果，请刷新核实：${readError(error, "请求未完成")}` };
    }
    if (generation.current !== run) return;
    setResults((current) => current?.map((item) => item.lineId === row.lineId ? { ...item, ...patch } : item) ?? null);
    await refresh(run);
    if (generation.current !== run) return;
    busyRef.current = false;
    setBusy(false);
  };

  const counts = (status: Status) => results?.filter((row) => row.status === status).length ?? 0;
  return <>
    <Space wrap>
      <Typography.Text>已选择 {selection.length} 行</Typography.Text>
      {ACTIONS.map((item) => <Button key={item.label} disabled={disabled || !selection.length || busy || hasUnknown}
        onClick={() => {
          setRows(selection.map((row) => ({ ...row })));
          setAction(item); setReason(""); setResults(null); setRefreshFailed(false);
          setOpen(true);
          setOperator(localStorage.getItem("name") || "当前登录账号");
          sessionToken.current = localStorage.getItem("token");
        }}>{item.label}</Button>)}
      {results ? <Button disabled={busy} onClick={() => setOpen(true)}>查看上次结果</Button> : null}
    </Space>
    {hasUnknown ? <Typography.Paragraph type="warning">上次有结果未知的行，请先查看上次结果并核对回执；本页保留原请求，重新加载或离开项目后请刷新核实事实。</Typography.Paragraph> : null}
    <Modal open={open} title={results ? "领用返还要求批量结果" : action?.label}
      width={860} closable={!busy} maskClosable={!busy} keyboard={!busy}
      onCancel={() => { if (!busy) setOpen(false); }}
      footer={results ? <Button disabled={busy} onClick={() => setOpen(false)}>关闭</Button> : undefined}
      onOk={() => { void submit(); }} okText="确认修改" cancelText="取消"
      okButtonProps={{ disabled: !reason.trim() || reasonTooLong }} confirmLoading={busy}>
      <Space direction="vertical" style={{ width: "100%" }}>
        <Typography.Text strong>{action?.intent}</Typography.Text>
        <Typography.Text>操作人：{operator}（系统按登录实名记录）</Typography.Text>
        <Typography.Text type="secondary">逐行保存并留痕，失败不撤销已成功的行；项目免返标注仅作提醒。</Typography.Text>
        {results ? <>
          <Typography.Text role="status">成功 {counts("success")}，未变化 {counts("unchanged")}，冲突 {counts("conflict")}，失败 {counts("failed")}，结果未知 {counts("unknown")}，未提交 {counts("unsubmitted")}</Typography.Text>
          {refreshFailed ? <Alert type="warning" showIcon message="修改回执已保留，但页面刷新失败；请重新加载，成功行无需再次提交。" /> : null}
          <Table<Result> rowKey="lineId" size="small" dataSource={results} pagination={false} scroll={{ x: 680, y: 360 }} columns={[
            { title: "领用单 / 行", render: (_, row) => `${row.issueNo} / ${row.lineNo}` },
            { title: "PN", dataIndex: "pn" },
            { title: "结果", render: (_, row) => <Tag color={STATUS[row.status].color}>{STATUS[row.status].label}</Tag> },
            { title: "说明", dataIndex: "detail" },
            { title: "操作", render: (_, row) => row.status === "unknown"
              ? <Button disabled={busy} onClick={() => { void retryUnknown(row); }}>原样核对回执</Button> : null },
          ]} />
        </> : <>
          <Typography.Text>本次选择 {rows.length} 行；设置相同的行会显示“未变化”。</Typography.Text>
          <Input.TextArea aria-label="更正原因" value={reason} rows={3} autoFocus
            placeholder="更正原因（必填），如：已逐行核对合同返还要求"
            onChange={(event) => setReason(event.target.value)} />
          {reasonTooLong ? <Alert type="error" showIcon message="更正原因最多 1000 个字符，请删减后提交" /> : null}
        </>}
      </Space>
    </Modal>
  </>;
}
