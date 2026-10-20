import { useEffect, useRef, useState } from "react";
import { App, Alert, AutoComplete, Button, Descriptions, Divider, Drawer, Form, Input, Select, Space, Typography } from "antd";
import {
  correctSiteIssueHeader,
  correctSiteIssueLine,
  getSiteIssueDemandCandidates,
  searchSiteIssues,
  type SiteIssueDocument,
  type SiteIssueHeaderCorrectionInput,
  type SiteIssueHeaderCorrectionReceipt,
  type SiteIssueLine,
  type SiteIssueLineCorrectionInput,
  type SiteIssueLineCorrectionReceipt,
} from "../../../api/maintenanceOperations";
import PartPicker from "../../../components/PartPicker";
import { readError } from "./panelUtils";

type LineDraft = {
  part_id: number;
  pn: string;
  quantity: string;
  serial_number: string;
  remark: string;
  demand_order_no: string;
  no_return: boolean | null;
};
type HeaderDraft = Required<SiteIssueHeaderCorrectionInput["changes"]>;
type Attempt = { kind: "line"; payload: SiteIssueLineCorrectionInput }
  | { kind: "header"; payload: SiteIssueHeaderCorrectionInput };
type Receipt = SiteIssueLineCorrectionReceipt | SiteIssueHeaderCorrectionReceipt;
type Status = "ready" | "conflict" | "unknown" | "success" | "unavailable";
const newKey = () => `site-edit-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
const reasonValid = (value: string) => !!value.trim() && Array.from(value.trim()).length <= 1000;
const normalizedQuantity = (value: string) => value.trim().replace(/^0+(?=\d)/, "").replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
const lineDraft = (line: SiteIssueLine): LineDraft => ({
  part_id: line.part_id, pn: line.pn, quantity: line.quantity,
  serial_number: line.serial_number ?? "", remark: line.remark ?? "",
  demand_order_no: line.demand_order_no ?? "", no_return: line.no_return ?? null,
});
const headerDraft = (issue: SiteIssueDocument): HeaderDraft => ({
  issue_no: issue.issue_no, issue_date: issue.issue_date,
  receiver: issue.receiver ?? "", issued_by: issue.issued_by ?? "", site_location: issue.site_location ?? "",
});

function lineChanges(draft: LineDraft, line: SiteIssueLine): SiteIssueLineCorrectionInput["changes"] {
  const before = lineDraft(line);
  const changes: SiteIssueLineCorrectionInput["changes"] = {};
  if (normalizedQuantity(draft.quantity) !== normalizedQuantity(before.quantity)) changes.quantity = draft.quantity.trim();
  if (draft.part_id !== before.part_id || draft.pn !== before.pn) {
    changes.part_id = draft.part_id; changes.pn = draft.pn;
  }
  for (const key of ["serial_number", "remark"] as const) {
    if (draft[key] !== before[key]) changes[key] = draft[key];
  }
  if (draft.demand_order_no.trim() !== before.demand_order_no.trim()) changes.demand_order_no = draft.demand_order_no.trim();
  if (draft.no_return !== before.no_return) changes.no_return = draft.no_return;
  return changes;
}

function headerChanges(draft: HeaderDraft, issue: SiteIssueDocument): SiteIssueHeaderCorrectionInput["changes"] {
  const before = headerDraft(issue);
  const changes: SiteIssueHeaderCorrectionInput["changes"] = {};
  for (const key of ["issue_no", "issue_date", "receiver", "issued_by", "site_location"] as const) {
    const value = draft[key].trim();
    if (value !== before[key].trim()) changes[key] = value;
  }
  return changes;
}

function editable(issue: SiteIssueDocument, line: SiteIssueLine) {
  return ["legacy", "workbook", "page_manual"].includes(issue.source ?? "")
    && ["confirmed", "corrected"].includes(issue.workflow_status) && !line.delivery_line_id;
}

/** 行与单头是独立命令。每次实际提交冻结载荷，任何恢复都不自动接受新版本。 */
export default function SiteIssueEditDrawer({ issue, line, open, onClose, onChanged, onPendingChange }: {
  issue: SiteIssueDocument;
  line: SiteIssueLine;
  open: boolean;
  onClose: () => void;
  onChanged: () => Promise<boolean>;
  onPendingChange: (pending: boolean) => void;
}) {
  const { message } = App.useApp();
  const [baseline, setBaseline] = useState({ issue, line });
  const [draft, setDraft] = useState(() => lineDraft(line));
  const [header, setHeader] = useState(() => headerDraft(issue));
  const [lineReason, setLineReason] = useState("");
  const [headerReason, setHeaderReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const busyRef = useRef(false);
  const generation = useRef(0);
  const token = useRef(localStorage.getItem("token"));
  const [status, setStatus] = useState<Status>("ready");
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [refreshFailed, setRefreshFailed] = useState(false);
  const [demandOptions, setDemandOptions] = useState<{ value: string }[]>([]);
  const changes = lineChanges(draft, baseline.line);
  const headChanges = headerChanges(header, baseline.issue);
  const canEditHeader = baseline.issue.lines.every((item) => !item.delivery_line_id)
    && (baseline.issue.total_line_count ?? baseline.issue.lines.length) === baseline.issue.lines.length;
  const frozen = busy || !loaded || status !== "ready";
  const hasChanges = Object.keys(changes).length > 0;
  const hasHeadChanges = Object.keys(headChanges).length > 0;
  const quantityError = changes.quantity !== undefined && (
    !/^\d+(\.\d{1,3})?$/.test(changes.quantity) || Number(changes.quantity) <= 0 || Number(changes.quantity) >= 1e12
  );
  const sessionCurrent = () => {
    if (localStorage.getItem("token") === token.current) return true;
    setError("登录会话已变化，请重新进入项目后核对"); setStatus("unavailable");
    return false;
  };
  useEffect(() => {
    onPendingChange(busy || status === "unknown");
  }, [busy, status, onPendingChange]);
  useEffect(() => () => { generation.current += 1; busyRef.current = false; }, []);

  const reload = async (preserve = true) => {
    if (busyRef.current || status === "unknown" || !sessionCurrent()) return;
    busyRef.current = true;
    setBusy(true);
    const run = generation.current;
    try {
      const { data } = await searchSiteIssues({ project_id: issue.project_id, issue_id: issue.issue_id });
      if (generation.current !== run || !sessionCurrent()) return;
      const freshIssue = data.rows.find((row) => row.issue_id === issue.issue_id);
      const freshLine = freshIssue?.lines.find((row) => row.issue_line_id === line.issue_line_id);
      if (!freshIssue || freshIssue.project_id !== issue.project_id || !freshLine || !editable(freshIssue, freshLine)) {
        setStatus("unavailable");
        throw new Error("领用单或明细已不可编辑，请重新进入项目核对");
      }
      setDraft({ ...lineDraft(freshLine), ...(preserve ? changes : {}) });
      setHeader({ ...headerDraft(freshIssue), ...(preserve ? headChanges : {}) });
      setBaseline({ issue: freshIssue, line: freshLine });
      setStatus("ready"); setError(""); setLoaded(true);
      if (preserve) message.info("已读取最新事实并保留草稿，请核对后再次确认保存");
    } catch (error) {
      if (generation.current === run) setError(readError(error, "重新读取失败，草稿已保留"));
    } finally {
      if (generation.current === run) { busyRef.current = false; setBusy(false); }
    }
  };
  // 以稳定 ID 读取整单，不能用当前品类筛选后的行集合判断单头可编辑性。
  useEffect(() => { void reload(false); }, []); // target 改变由父组件 key 建立新编辑会话。
  useEffect(() => {
    let current = true;
    const timer = setTimeout(() => {
      getSiteIssueDemandCandidates(issue.project_id, { partId: draft.part_id, q: draft.demand_order_no, pageSize: 50 })
        .then(({ data }) => { if (current) setDemandOptions(data.rows.map((item) => ({ value: item.order_no }))); })
        .catch(() => { if (current) setDemandOptions([]); });
    }, 300);
    return () => { current = false; clearTimeout(timer); };
  }, [issue.project_id, draft.part_id, draft.demand_order_no]);

  const send = async (command: Attempt, recheck = false) => {
    if (busyRef.current || !sessionCurrent()) return;
    busyRef.current = true;
    setBusy(true); setError("");
    const run = generation.current;
    try {
      const { data } = command.kind === "line"
        ? await correctSiteIssueLine(issue.issue_id, line.issue_line_id, command.payload)
        : await correctSiteIssueHeader(issue.issue_id, command.payload);
      if (generation.current !== run || !sessionCurrent()) return;
      setReceipt(data); setStatus("success"); setAttempt(null);
      // 已获成功回执的字段不再是待提交草稿。仍不升 CAS 基线；下一命令须显式重读。
      // 只更新已保存分区，另一分区尚未提交的修改继续保留。
      setBaseline((current) => command.kind === "line"
        ? { ...current, line: { ...current.line, ...command.payload.changes } }
        : { ...current, issue: { ...current.issue, ...command.payload.changes } });
      let refreshed = false;
      try { refreshed = await onChanged(); } catch { /* 成功回执保留，刷新失败不能重发写命令。 */ }
      if (generation.current === run) setRefreshFailed(!refreshed);
    } catch (error) {
      if (generation.current !== run) return;
      const code = (error as { response?: { status?: number } })?.response?.status;
      const detail = readError(error, "请求未完成");
      if (recheck || !code || code >= 500) {
        setStatus("unknown");
        setError(recheck ? `尚未核对首次结果：${detail}` : `结果未知，可能已保存，请原样核对回执：${detail}`);
      } else if (code === 409) {
        setStatus("conflict"); setError(`草稿已保留，请显式重读后重新确认：${detail}`);
      } else {
        setStatus("ready"); setError(detail);
      }
    } finally {
      if (generation.current === run) { busyRef.current = false; setBusy(false); }
    }
  };
  const submit = (kind: "line" | "header") => {
    if (frozen || busyRef.current || !sessionCurrent()) return;
    const reason = kind === "line" ? lineReason : headerReason;
    if (!reasonValid(reason) || (kind === "line" ? (!hasChanges || quantityError) : (!hasHeadChanges || !canEditHeader))) return;
    const command: Attempt = kind === "line" ? {
      kind, payload: { expected_issue_version: baseline.issue.version, expected_line_version: baseline.line.version,
        changes, reason: reason.trim(), idempotency_key: newKey() },
    } : {
      kind, payload: { expected_issue_version: baseline.issue.version,
        changes: headChanges, reason: reason.trim(), idempotency_key: newKey() },
    };
    setAttempt(command); setReceipt(null); setRefreshFailed(false);
    void send(command);
  };
  const textField = (label: string, value: string, onChange: (value: string) => void, multiline = false) => (
    <Form.Item label={label} style={{ marginBottom: 12 }}>
      {multiline
        ? <Input.TextArea aria-label={label} value={value} disabled={frozen} rows={2} onChange={(event) => onChange(event.target.value)} />
        : <Input aria-label={label} value={value} disabled={frozen} onChange={(event) => onChange(event.target.value)} />}
    </Form.Item>
  );
  return <Drawer open={open} title="编辑领用事实" onClose={onClose} width={640}
    closable={!busy} maskClosable={!busy} keyboard={!busy}
    footer={<Button disabled={busy} onClick={onClose}>关闭编辑</Button>}>
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Typography.Text>{baseline.issue.display_no || baseline.issue.issue_no} · 第 {baseline.line.line_no} 行</Typography.Text>
      {error ? <Alert type={status === "unknown" ? "warning" : "error"} showIcon message={error} /> : null}
      {receipt ? <Alert type="success" showIcon message={receipt.changed ? "上次命令保存成功，回执已保留" : "上次命令服务器确认未变化"}
        description={<>
          {"scope" in receipt ? <div>{receipt.message}</div> : null}
          <div>单据版本 {receipt.issue_version} · 工作簿版本 {receipt.workbook_revision}</div>
          <div>回执仅对应上次提交，未保存草稿不包含在内。</div>
          <div>继续编辑前请重新读取，核对最新事实与成本；其他草稿会保留。</div>
        </>} /> : null}
      {refreshFailed ? <Alert type="warning" message="修改回执已保留，但页面刷新失败；成功命令无需再次提交。" /> : null}
      <Space wrap>
        <Button disabled={busy || status === "unknown"} onClick={() => { void reload(); }}>重新读取并保留草稿</Button>
        {status === "unknown" && attempt ? <Button disabled={busy} onClick={() => { void send(attempt, true); }}>原样核对回执</Button> : null}
      </Space>
      <Form layout="vertical" component="div" style={{ width: "100%" }}>
        <Typography.Title level={5}>当前领用行</Typography.Title>
        <Form.Item label="PN / 备件">
          <div role="group" aria-label="PN / 备件">
            <PartPicker value={draft.part_id} allowClear={false} disabled={frozen} style={{ minWidth: 0, width: "100%" }}
              initialItem={{ part_id: draft.part_id, pn_std: draft.pn, description: baseline.line.description ?? null }}
              onChange={(partId, item) => {
                if (partId != null && item) setDraft((value) => ({ ...value, part_id: partId, pn: item.pn_std }));
              }} />
          </div>
        </Form.Item>
        {textField("领用数量", draft.quantity, (quantity) => setDraft((value) => ({ ...value, quantity })))}
        {quantityError ? <Typography.Paragraph type="danger">领用数量须为最多三位小数的正数，且小于 1000000000000。</Typography.Paragraph> : null}
        {textField("SN", draft.serial_number, (serial_number) => setDraft((value) => ({ ...value, serial_number })))}
        <Form.Item label="是否应返还">
          <Select aria-label="是否应返还" style={{ width: "100%" }} disabled={frozen}
            value={draft.no_return === null ? "rule" : draft.no_return ? "exempt" : "required"}
            options={[{ value: "rule", label: "按规则判断" }, { value: "required", label: "是（应返）" }, { value: "exempt", label: "否（免返）" }]}
            onChange={(value) => setDraft((current) => ({ ...current, no_return: value === "rule" ? null : value === "exempt" }))} />
        </Form.Item>
        <Form.Item label="关联需求单" extra="可选同项目需求单或输入完整单号；清空表示解除关联，不会修改返还收货关联。">
          <AutoComplete aria-label="关联需求单" value={draft.demand_order_no} disabled={frozen} options={demandOptions}
            style={{ width: "100%" }} onChange={(demand_order_no) => setDraft((value) => ({ ...value, demand_order_no }))} />
        </Form.Item>
        {textField("现场备注", draft.remark, (remark) => setDraft((value) => ({ ...value, remark })), true)}
        <Descriptions size="small" column={1} title="成本结果（只读）">
          <Descriptions.Item label="未税成本">{baseline.line.cost_amount_ex_tax ?? "待补价格"}</Descriptions.Item>
          <Descriptions.Item label="含税成本">{baseline.line.cost_amount_inc_tax ?? "待补价格"}</Descriptions.Item>
          <Descriptions.Item label="取价来源">{baseline.line.cost_source_label ?? "待补价格"}</Descriptions.Item>
        </Descriptions>
        <Typography.Paragraph type="secondary">数量、PN 或需求关联变更按现有规则重新取价；换 PN 会清除旧人工价格证据。成本在重读后显示。</Typography.Paragraph>
        {textField("行更正原因", lineReason, setLineReason, true)}
        {Array.from(lineReason.trim()).length > 1000 ? <Typography.Paragraph type="danger">更正原因最多 1000 个字符</Typography.Paragraph> : null}
        <Button type="primary" disabled={frozen || !hasChanges || !reasonValid(lineReason) || quantityError}
          onClick={() => submit("line")}>保存行修改</Button>
        <Divider />
        <Typography.Title level={5}>整单单头</Typography.Title>
        <Alert type="warning" showIcon message="影响整张领用单全部行"
          description="单头独立保存。修改日期或内部单号会按既有规则重算全单成本；地址修改不触发重价。展示号保持不变，不在此合单。" />
        {canEditHeader ? <>
          {textField("内部单号", header.issue_no, (issue_no) => setHeader((value) => ({ ...value, issue_no })))}
          {textField("领用日期（YYYY-MM-DD）", header.issue_date, (issue_date) => setHeader((value) => ({ ...value, issue_date })))}
          {textField("领用人", header.receiver, (receiver) => setHeader((value) => ({ ...value, receiver })))}
          {textField("发出人", header.issued_by, (issued_by) => setHeader((value) => ({ ...value, issued_by })))}
          {textField("领用地址", header.site_location, (site_location) => setHeader((value) => ({ ...value, site_location })), true)}
          <Typography.Paragraph type="secondary">地址改为空白并保存会清空整单地址；历史空字段可保持。</Typography.Paragraph>
          {textField("单头更正原因", headerReason, setHeaderReason, true)}
          {Array.from(headerReason.trim()).length > 1000 ? <Typography.Paragraph type="danger">更正原因最多 1000 个字符</Typography.Paragraph> : null}
          <Button type="primary" disabled={frozen || !hasHeadChanges || !reasonValid(headerReason)}
            onClick={() => submit("header")}>保存单头修改</Button>
        </> : <Typography.Paragraph type="secondary">本单包含发货关联行或尚未读取完整明细，单头须走原有受控流程。</Typography.Paragraph>}
      </Form>
    </Space>
  </Drawer>;
}
