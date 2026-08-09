import {
  Alert,
  Button,
  Card,
  Checkbox,
  Empty,
  Input,
  InputNumber,
  List,
  Modal,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useRef, useState } from "react";

import {
  confirmSiteIssue,
  createSiteIssueDraft,
  patchSiteIssue,
  previewSiteIssue,
  searchSiteIssueCandidates,
  searchSiteIssues,
  voidSiteIssue,
  type SiteIssueAdapterState,
  type SiteIssueCandidate,
  type SiteIssueDocument,
  type SiteIssueLineInput,
  type SiteIssuePreview,
} from "../../api/maintenanceOperations";

const { Text, Title } = Typography;

const workflowLabel: Record<string, { label: string; color?: string }> = {
  draft: { label: "待确认草稿", color: "blue" },
  confirmed: { label: "已确认", color: "green" },
  corrected: { label: "已更正", color: "gold" },
  void: { label: "已作废" },
};

const commandKey = (prefix: string) => {
  const nativeUuid = globalThis.crypto?.randomUUID?.();
  return `${prefix}-${nativeUuid ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
};

const today = () => {
  const current = new Date();
  const offset = current.getTimezoneOffset() * 60_000;
  return new Date(current.getTime() - offset).toISOString().slice(0, 10);
};

const amount = (value: string | null | undefined) => value == null
  ? "—"
  : `¥${Number(value).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;

interface EditorValues {
  issueDate: string;
  receiver: string;
  issuedBy: string;
  siteLocation: string;
  reason: string;
}

const emptyEditor = (): EditorValues => ({
  issueDate: today(),
  receiver: "",
  issuedBy: "",
  siteLocation: "",
  reason: "",
});

export default function SiteIssueWorkflowPanel({
  projectId,
  canManage,
  onChanged,
}: {
  projectId: string;
  canManage: boolean;
  onChanged: () => void;
}) {
  const [issues, setIssues] = useState<SiteIssueDocument[]>([]);
  const [candidates, setCandidates] = useState<SiteIssueCandidate[]>([]);
  const [issueTotal, setIssueTotal] = useState(0);
  const [issuePage, setIssuePage] = useState(1);
  const [candidateTotal, setCandidateTotal] = useState(0);
  const [candidatePage, setCandidatePage] = useState(1);
  const [adapter, setAdapter] = useState<SiteIssueAdapterState | null>(null);
  const [issueLoading, setIssueLoading] = useState(false);
  const [candidateLoading, setCandidateLoading] = useState(false);
  const [issueError, setIssueError] = useState(false);
  const [candidateError, setCandidateError] = useState(false);
  const [issueQuery, setIssueQuery] = useState("");
  const [candidateQuery, setCandidateQuery] = useState("");
  const issueGeneration = useRef(0);
  const candidateGeneration = useRef(0);
  const activeProject = useRef(projectId);

  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<SiteIssueDocument | null>(null);
  const [editor, setEditor] = useState<EditorValues>(emptyEditor);
  const [selected, setSelected] = useState<Record<string, number>>({});
  const [editorError, setEditorError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const [preview, setPreview] = useState<SiteIssuePreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [confirmReason, setConfirmReason] = useState("确认现场实际领用");
  const [voidTarget, setVoidTarget] = useState<SiteIssueDocument | null>(null);
  const [voidReason, setVoidReason] = useState("");
  const [voiding, setVoiding] = useState(false);
  const [voidError, setVoidError] = useState<string | null>(null);

  activeProject.current = projectId;

  const loadIssues = async (q = issueQuery, page = 1, append = false) => {
    const request = ++issueGeneration.current;
    const requestedProject = projectId;
    setIssueLoading(true);
    setIssueError(false);
    try {
      const { data } = await searchSiteIssues({
        project_id: requestedProject,
        q,
        page,
        page_size: 20,
      });
      if (request !== issueGeneration.current || activeProject.current !== requestedProject) return;
      setIssues((current) => append
        ? [...current, ...data.rows.filter((row) => !current.some((item) => item.issue_id === row.issue_id))]
        : data.rows);
      setIssueTotal(data.total);
      setIssuePage(data.page);
    } catch {
      if (request !== issueGeneration.current || activeProject.current !== requestedProject) return;
      if (!append) {
        setIssues([]);
        setIssueTotal(0);
        setIssuePage(1);
      }
      setIssueError(true);
    } finally {
      if (request === issueGeneration.current && activeProject.current === requestedProject) {
        setIssueLoading(false);
      }
    }
  };

  const loadCandidates = async (q = candidateQuery, page = 1, append = false) => {
    const request = ++candidateGeneration.current;
    const requestedProject = projectId;
    setCandidateLoading(true);
    setCandidateError(false);
    try {
      const { data } = await searchSiteIssueCandidates(requestedProject, {
        q,
        page,
        page_size: 50,
      });
      if (
        request !== candidateGeneration.current
        || activeProject.current !== requestedProject
      ) return;
      setCandidates((current) => append
        ? [...current, ...data.rows.filter((row) => !current.some((item) => item.delivery_line_id === row.delivery_line_id))]
        : data.rows);
      setCandidateTotal(data.total);
      setCandidatePage(data.page);
      setAdapter(data.adapter);
    } catch {
      if (
        request !== candidateGeneration.current
        || activeProject.current !== requestedProject
      ) return;
      if (!append) {
        setCandidates([]);
        setCandidateTotal(0);
        setCandidatePage(1);
      }
      setCandidateError(true);
    } finally {
      if (
        request === candidateGeneration.current
        && activeProject.current === requestedProject
      ) setCandidateLoading(false);
    }
  };

  useEffect(() => {
    issueGeneration.current += 1;
    candidateGeneration.current += 1;
    setIssues([]);
    setCandidates([]);
    setIssueTotal(0);
    setCandidateTotal(0);
    setIssuePage(1);
    setCandidatePage(1);
    setAdapter(null);
    setIssueQuery("");
    setCandidateQuery("");
    setPreview(null);
    setEditorOpen(false);
    setVoidTarget(null);
    if (!canManage || !projectId) return undefined;
    void loadIssues("");
    void loadCandidates("");
    return () => {
      issueGeneration.current += 1;
      candidateGeneration.current += 1;
    };
    // The project and permission are the complete loading identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, canManage]);

  if (!canManage) return null;

  const openEditor = (issue: SiteIssueDocument | null) => {
    setEditing(issue);
    setEditor(issue ? {
      issueDate: issue.issue_date,
      receiver: issue.receiver,
      issuedBy: issue.issued_by,
      siteLocation: issue.site_location,
      reason: issue.workflow_status === "draft" ? "修改现场领用草稿" : "更正已确认现场领用",
    } : emptyEditor());
    setSelected(Object.fromEntries(
      (issue?.lines ?? []).flatMap((line) => line.delivery_line_id
        ? [[line.delivery_line_id, Number(line.quantity)]]
        : []),
    ));
    setEditorError(null);
    setEditorOpen(true);
  };

  const saveEditor = async () => {
    const clean = {
      issueDate: editor.issueDate.trim(),
      receiver: editor.receiver.trim(),
      issuedBy: editor.issuedBy.trim(),
      siteLocation: editor.siteLocation.trim(),
      reason: editor.reason.trim(),
    };
    const lines: SiteIssueLineInput[] = Object.entries(selected)
      .filter(([, quantity]) => Number.isFinite(quantity) && quantity > 0)
      .map(([delivery_line_id, quantity]) => ({ delivery_line_id, quantity }));
    if (
      !clean.issueDate
      || !clean.receiver
      || !clean.issuedBy
      || !clean.siteLocation
      || !clean.reason
      || lines.length === 0
    ) {
      setEditorError("请填写日期、领用/接收人、现场位置、原因，并至少选择一条正数量发货明细");
      return;
    }
    setSaving(true);
    setEditorError(null);
    const requestedProject = projectId;
    try {
      if (editing) {
        await patchSiteIssue(editing.issue_id, {
          project_id: requestedProject,
          version: editing.version,
          idempotency_key: commandKey("site-issue-patch"),
          issue_date: clean.issueDate,
          receiver: clean.receiver,
          issued_by: clean.issuedBy,
          site_location: clean.siteLocation,
          lines,
          reason: clean.reason,
        });
      } else {
        await createSiteIssueDraft(requestedProject, {
          idempotency_key: commandKey("site-issue-draft"),
          issue_date: clean.issueDate,
          receiver: clean.receiver,
          issued_by: clean.issuedBy,
          site_location: clean.siteLocation,
          lines,
          reason: clean.reason,
        });
      }
      if (activeProject.current !== requestedProject) return;
      setEditorOpen(false);
      message.success(editing?.workflow_status === "draft" ? "草稿已更新" : editing ? "领用单已更正" : "草稿已保存");
      setIssueQuery("");
      setCandidateQuery("");
      await Promise.all([loadIssues("", 1), loadCandidates("", 1)]);
      onChanged();
    } catch {
      if (activeProject.current === requestedProject) {
        setEditorError(editing ? "现场领用单保存失败，请刷新后重试" : "现场领用草稿保存失败，请重试");
      }
    } finally {
      if (activeProject.current === requestedProject) setSaving(false);
    }
  };

  const openPreview = async (issue: SiteIssueDocument) => {
    const requestedProject = projectId;
    setPreviewLoading(true);
    try {
      const { data } = await previewSiteIssue(issue.issue_id, {
        project_id: requestedProject,
        version: issue.version,
      });
      if (activeProject.current !== requestedProject) return;
      setPreview(data);
      setConfirmReason("确认现场实际领用");
    } catch {
      if (activeProject.current === requestedProject) message.error("确认影响预览失败，请刷新后重试");
    } finally {
      if (activeProject.current === requestedProject) setPreviewLoading(false);
    }
  };

  const confirmPreview = async () => {
    if (!preview || !confirmReason.trim()) return;
    const requestedProject = projectId;
    setConfirming(true);
    try {
      await confirmSiteIssue(preview.issue_id, {
        project_id: requestedProject,
        version: preview.version,
        idempotency_key: commandKey("site-issue-confirm"),
        reason: confirmReason.trim(),
      });
      if (activeProject.current !== requestedProject) return;
      setPreview(null);
      message.success("现场领用已确认，成本证据与返还义务来源已冻结");
      setIssueQuery("");
      setCandidateQuery("");
      await Promise.all([loadIssues("", 1), loadCandidates("", 1)]);
      onChanged();
    } catch {
      if (activeProject.current === requestedProject) message.error("确认失败，发货余额或版本可能已变化");
    } finally {
      if (activeProject.current === requestedProject) setConfirming(false);
    }
  };

  const requestVoid = (issue: SiteIssueDocument) => {
    setVoidTarget(issue);
    setVoidReason("");
    setVoidError(null);
  };

  const confirmVoid = async () => {
    if (!voidTarget || !voidReason.trim()) return;
    const requestedProject = projectId;
    setVoiding(true);
    setVoidError(null);
    try {
      await voidSiteIssue(voidTarget.issue_id, {
        project_id: requestedProject,
        version: voidTarget.version,
        idempotency_key: commandKey("site-issue-void"),
        reason: voidReason.trim(),
      });
      if (activeProject.current !== requestedProject) return;
      setVoidTarget(null);
      setVoidReason("");
      message.success("现场领用单已作废");
      setIssueQuery("");
      setCandidateQuery("");
      await Promise.all([loadIssues("", 1), loadCandidates("", 1)]);
      onChanged();
    } catch {
      if (activeProject.current === requestedProject) {
        setVoidError("作废失败；若返还模块已生成下游事实，请改走更正并刷新后重试");
      }
    } finally {
      if (activeProject.current === requestedProject) setVoiding(false);
    }
  };

  const previewColumns: ColumnsType<SiteIssuePreview["lines"][number]> = [
    { title: "PN / SN", render: (_, row) => <>{row.pn}<br /><Text type="secondary">{row.serial_number || "无 SN"}</Text></> },
    { title: "领用 / 可用", render: (_, row) => `${row.quantity} / ${row.available_quantity ?? "—"}` },
    { title: "未税成本", dataIndex: "cost_amount_ex_tax", render: amount },
    { title: "含税成本", dataIndex: "cost_amount_inc_tax", render: amount },
    {
      title: "取价依据",
      render: (_, row) => row.cost_source == null
        ? <Tag color="orange">待补价格，金额留空</Tag>
        : <Tag color={row.cost_is_estimate ? "gold" : "blue"}>{row.cost_source_label}</Tag>,
    },
  ];

  const isDevelopmentBuild = Boolean(
    (import.meta as ImportMeta & { env?: { DEV?: boolean } }).env?.DEV,
  );
  const productionConfirmationReady = adapter?.production_ready === true || isDevelopmentBuild;
  const candidateIds = new Set(candidates.map((row) => row.delivery_line_id));
  const pinnedSelectedLines = (editing?.lines ?? []).filter((line) => (
    line.delivery_line_id != null
    && Object.prototype.hasOwnProperty.call(selected, line.delivery_line_id)
    && !candidateIds.has(line.delivery_line_id)
  ));

  return (
    <Card
      data-testid="site-issue-workflow"
      title="现场备件领用单"
      extra={<Button type="primary" onClick={() => openEditor(null)}>新建领用单</Button>}
    >
      <Space direction="vertical" size={14} style={{ width: "100%" }}>
        <Alert
          type="info"
          showIcon
          message="这里记录现场实际消耗"
          description="草稿不计成本；确认后才冻结成本证据并生成返还义务来源。全过程不修改公司库、地区库或前置库库存。"
        />
        {adapter && !adapter.production_ready && (
          <Alert
            type={adapter.state === "unavailable" ? "error" : "warning"}
            showIcon
            message="发货来源适配器尚未达到生产就绪"
            description={adapter.detail || "系统不会按项目名猜测发货来源"}
          />
        )}
        {candidateError && (
          <Alert
            type="error"
            showIcon
            message="发货候选加载失败"
            action={<Button aria-label="重试发货候选" size="small" onClick={() => void loadCandidates(candidateQuery, 1)}>重试</Button>}
          />
        )}
        <Input.Search
          aria-label="搜索现场领用单"
          value={issueQuery}
          onChange={(event) => setIssueQuery(event.target.value)}
          onSearch={(value) => void loadIssues(value, 1)}
          placeholder="搜索系统单号、领用/接收人或现场位置"
          allowClear
          enterButton="搜索"
          maxLength={256}
        />
        {issueError && (
          <Alert
            type="error"
            showIcon
            message="现场领用单加载失败"
            action={<Button aria-label="重试" size="small" onClick={() => void loadIssues(issueQuery, 1)}>重试</Button>}
          />
        )}
        {issueLoading && issues.length === 0 ? (
          <div style={{ textAlign: "center", padding: 24 }}><Spin /></div>
        ) : issues.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有现场领用单，可先保存一张草稿" />
        ) : (
          <>
            <Text type="secondary">共 {issueTotal} 张，已显示 {issues.length} 张</Text>
            <List
              grid={{ gutter: 12, xs: 1, sm: 1, md: 2, lg: 2, xl: 3, xxl: 3 }}
              dataSource={issues}
              renderItem={(issue) => {
                const state = workflowLabel[issue.workflow_status] ?? { label: issue.workflow_status };
                const missing = issue.lines.filter((line) => line.cost_source == null).length;
                return (
                  <List.Item>
                    <Card size="small" className="site-issue-document-card">
                      <Space direction="vertical" size={8} style={{ width: "100%" }}>
                        <Space wrap>
                          <Text strong>{issue.issue_no}</Text>
                          <Tag color={state.color}>{state.label}</Tag>
                        </Space>
                        <Text type="secondary">{issue.issue_date} · {issue.site_location}</Text>
                        <Text>{issue.receiver} 接收 · {issue.issued_by} 发出 · {issue.lines.length} 行</Text>
                        {missing > 0
                          ? <Tag color="orange">{missing} 行暂无价格成本，数量仍完整展示</Tag>
                          : <Tag color="green">价格成本已完整</Tag>}
                        <Space wrap>
                          {issue.workflow_status === "draft" && (
                            <Button aria-label="编辑草稿" size="small" onClick={() => openEditor(issue)}>编辑草稿</Button>
                          )}
                          {issue.workflow_status === "draft" && (
                            <Button
                              size="small"
                              type="primary"
                              aria-label="预览并确认"
                              loading={previewLoading}
                              onClick={() => void openPreview(issue)}
                            >
                              预览并确认
                            </Button>
                          )}
                          {["confirmed", "corrected"].includes(issue.workflow_status) && (
                            <Button aria-label="更正" size="small" onClick={() => openEditor(issue)}>更正</Button>
                          )}
                          {issue.workflow_status !== "void" && (
                            <Button aria-label="作废" size="small" danger onClick={() => requestVoid(issue)}>作废</Button>
                          )}
                        </Space>
                      </Space>
                    </Card>
                  </List.Item>
                );
              }}
            />
            {issues.length < issueTotal && (
              <Button
                block
                loading={issueLoading}
                onClick={() => void loadIssues(issueQuery, issuePage + 1, true)}
              >
                加载更多领用单
              </Button>
            )}
          </>
        )}
      </Space>

      <Modal
        title={editing
          ? editing.workflow_status === "draft" ? "编辑现场领用草稿" : "更正现场领用单"
          : "新建现场领用草稿"}
        open={editorOpen}
        width={900}
        okText={editing?.workflow_status === "draft" ? "保存修改" : editing ? "提交更正" : "保存草稿"}
        cancelText="取消"
        confirmLoading={saving}
        onOk={() => void saveEditor()}
        onCancel={() => setEditorOpen(false)}
        destroyOnHidden
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Alert type="info" showIcon message="系统自动生成领用单号、表头 ID 和每一行 ID；这里只填写业务内容。" />
          {editorError && <Alert type="error" showIcon message={editorError} />}
          <div className="site-issue-editor-grid">
            <label>领用日期<Input type="date" aria-label="领用日期" value={editor.issueDate} onChange={(event) => setEditor({ ...editor, issueDate: event.target.value })} /></label>
            <label>接收人<Input aria-label="接收人" value={editor.receiver} onChange={(event) => setEditor({ ...editor, receiver: event.target.value })} /></label>
            <label>发出人<Input aria-label="发出人" value={editor.issuedBy} onChange={(event) => setEditor({ ...editor, issuedBy: event.target.value })} /></label>
            <label>现场位置<Input aria-label="现场位置" value={editor.siteLocation} onChange={(event) => setEditor({ ...editor, siteLocation: event.target.value })} /></label>
          </div>
          <label>操作原因<Input aria-label="操作原因" value={editor.reason} onChange={(event) => setEditor({ ...editor, reason: event.target.value })} /></label>
          <Input.Search
            aria-label="搜索发货候选"
            value={candidateQuery}
            onChange={(event) => setCandidateQuery(event.target.value)}
            onSearch={(value) => void loadCandidates(value, 1)}
            placeholder="按发货单、PN 或 SN 搜索"
            enterButton="查找发货明细"
            maxLength={256}
          />
          {pinnedSelectedLines.length > 0 && (
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              <Text strong>本单已选明细（固定展示）</Text>
              <Alert
                type="info"
                showIcon
                message="以下明细不在当前候选页或搜索结果中，仍完整保留；提交时由服务端重新校验来源与余额。"
              />
              <div className="site-issue-candidate-grid">
                {pinnedSelectedLines.map((line) => (
                  <Card key={line.issue_line_id} size="small">
                    <Space direction="vertical" size={6} style={{ width: "100%" }}>
                      <Checkbox
                        checked
                        onChange={() => setSelected((current) => {
                          const next = { ...current };
                          if (line.delivery_line_id) delete next[line.delivery_line_id];
                          return next;
                        })}
                      >
                        <Text strong>{line.pn}</Text>
                      </Checkbox>
                      <Text type="secondary">
                        稳定来源（WBDD/source_order_id）：{line.source_order_id || "—"} · 行 {line.source_line_id || "—"}
                      </Text>
                      <Text>SN：{line.serial_number || "无"}</Text>
                      {line.delivery_line_id && (
                        <label>
                          领用数量
                          <InputNumber
                            aria-label={`${line.pn} 领用数量`}
                            min={0.001}
                            precision={3}
                            value={selected[line.delivery_line_id]}
                            onChange={(value) => setSelected((current) => ({
                              ...current,
                              [line.delivery_line_id as string]: Number(value ?? 0),
                            }))}
                            style={{ width: "100%" }}
                          />
                        </label>
                      )}
                    </Space>
                  </Card>
                ))}
              </div>
            </Space>
          )}
          {candidateLoading && candidates.length === 0 ? <Spin /> : candidates.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有可关联的稳定发货明细" />
          ) : (
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              <Text type="secondary">共 {candidateTotal} 条稳定发货明细，已显示 {candidates.length} 条</Text>
              <div className="site-issue-candidate-grid">
                {candidates.map((row) => {
                  const checked = Object.prototype.hasOwnProperty.call(selected, row.delivery_line_id);
                  const existingLine = editing?.lines.find((line) => line.delivery_line_id === row.delivery_line_id);
                  const ownConfirmedQuantity = editing && ["confirmed", "corrected"].includes(editing.workflow_status)
                    ? Number(existingLine?.quantity ?? 0)
                    : 0;
                  const selectableMax = Number(row.available_quantity) + ownConfirmedQuantity;
                  const depleted = selectableMax <= 0;
                  return (
                    <Card key={row.delivery_line_id} size="small">
                      <Space direction="vertical" size={6} style={{ width: "100%" }}>
                        <Checkbox
                          checked={checked}
                          disabled={!checked && depleted}
                          onChange={(event) => setSelected((current) => {
                            const next = { ...current };
                            if (event.target.checked) next[row.delivery_line_id] = Math.min(1, selectableMax);
                            else delete next[row.delivery_line_id];
                            return next;
                          })}
                        >
                          <Text strong>{row.pn}</Text>
                        </Checkbox>
                        <Text type="secondary">发货单：{row.delivery_no} · {row.delivery_date}</Text>
                        <Text type="secondary">
                          稳定来源（WBDD/source_order_id）：{row.source_order_id} · 行 {row.source_line_id}
                        </Text>
                        <Text>SN：{row.serial_number || "无"}</Text>
                        <Tag color={depleted ? "default" : "blue"}>
                          已发 {row.delivered_quantity} · 已确认 {row.confirmed_quantity} · 可领 {row.available_quantity}
                          {depleted ? " · 已领完" : ""}
                        </Tag>
                        {checked && (
                          <label>
                            领用数量
                            <InputNumber
                              aria-label={`${row.pn} 领用数量`}
                              min={0.001}
                              max={Math.max(selectableMax, 0)}
                              disabled={depleted}
                              precision={3}
                              value={selected[row.delivery_line_id]}
                              onChange={(value) => setSelected((current) => ({
                                ...current,
                                [row.delivery_line_id]: Number(value ?? 0),
                              }))}
                              style={{ width: "100%" }}
                            />
                          </label>
                        )}
                      </Space>
                    </Card>
                  );
                })}
              </div>
              {candidates.length < candidateTotal && (
                <Button
                  block
                  loading={candidateLoading}
                  onClick={() => void loadCandidates(candidateQuery, candidatePage + 1, true)}
                >
                  加载更多发货明细
                </Button>
              )}
            </Space>
          )}
        </Space>
      </Modal>

      <Modal
        title="确认影响预览"
        open={preview != null}
        width={980}
        onCancel={() => setPreview(null)}
        footer={preview ? [
          <Button key="cancel" onClick={() => setPreview(null)}>返回草稿</Button>,
          <Button
            key="confirm"
            type="primary"
            disabled={!preview.can_confirm || !productionConfirmationReady || !confirmReason.trim()}
            loading={confirming}
            onClick={() => void confirmPreview()}
          >
            确认现场领用
          </Button>,
        ] : null}
      >
        {preview && (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Alert type="info" showIcon message="库存影响：无" description="确认只形成现场实际消耗、成本证据快照和返还义务接口事件，不写任何库存。" />
            {preview.blockers.length > 0 && (
              <Alert type="error" showIcon message="当前不能确认" description={preview.blockers.join("；")} />
            )}
            {!adapter?.production_ready && !isDevelopmentBuild && (
              <Alert type="error" showIcon message="真实发货适配器未就绪，生产确认已禁用" />
            )}
            <Title level={5}>{preview.issue_no}</Title>
            <Table
              rowKey="issue_line_id"
              size="small"
              pagination={false}
              columns={previewColumns}
              dataSource={preview.lines}
              scroll={{ x: 850 }}
            />
            <label>确认原因<Input aria-label="确认原因" value={confirmReason} onChange={(event) => setConfirmReason(event.target.value)} /></label>
          </Space>
        )}
      </Modal>

      <Modal
        title={voidTarget ? `作废 ${voidTarget.issue_no}` : "作废现场领用单"}
        open={voidTarget != null}
        okText="确认作废"
        cancelText="取消"
        confirmLoading={voiding}
        okButtonProps={{ danger: true, disabled: !voidReason.trim() }}
        onOk={() => void confirmVoid()}
        onCancel={() => {
          if (voiding) return;
          setVoidTarget(null);
          setVoidReason("");
          setVoidError(null);
        }}
        destroyOnHidden
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Alert
            type="warning"
            showIcon
            message="作废不会冲减历史成本，也不会修改任何库存"
            description="若返还模块已生成下游事实，系统会拒绝作废并要求改走更正。"
          />
          {voidError && <Alert type="error" showIcon message={voidError} />}
          <label>
            作废原因（必填）
            <Input.TextArea
              aria-label="作废原因"
              value={voidReason}
              maxLength={1000}
              showCount
              rows={3}
              placeholder="请填写可审计的真实业务原因"
              onChange={(event) => setVoidReason(event.target.value)}
            />
          </label>
        </Space>
      </Modal>
    </Card>
  );
}
