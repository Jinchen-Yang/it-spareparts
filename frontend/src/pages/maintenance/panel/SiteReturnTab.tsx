import { useContextQuery } from "../../../authz/contextQuery";
import { useContextTransport } from "../../../authz/domainTransport";
import { useContextAdmission } from "../../../authz/access";
import { surfaceRule, actionRule, allRule } from "../../../authz/rules";
import { ContextChanged } from "../../../authz/contextProtocol";
import type { AuthorizationContext } from "../../../authz/contextTypes";
import { formatBusinessDateTime } from "../../../utils/date";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { App, Alert, Button, Descriptions, Input, Modal, Space, Table, Tag, Typography } from "antd";
import type {
  SiteIssueDocument,
  SiteIssueLine,
  MaintenanceCategoryFilterInput,
} from "../../../api/maintenanceOperations";
import {
  searchSiteIssues,
  voidSiteIssue,
} from "../../../api/maintenanceOperations";
import {
  SHEETS,
} from "../../../api/maintenanceWorkbooks";
import SiteIssueWorkflowPanel from "../../../components/maintenance/SiteIssueWorkflowPanel";
import SiteIssueAddress from "../../../components/maintenance/SiteIssueAddress";
import MaintenanceCategoryFilter from "../../../components/maintenance/MaintenanceCategoryFilter";
import ProjectWorkbookRoundTrip from "../../../components/maintenance/ProjectWorkbookRoundTrip";
import PanelActionBar from "./PanelActionBar";
import ReturnReceiptsSection from "./ReturnReceiptsSection";
import SiteIssueReturnBatch from "./SiteIssueReturnBatch";
import SiteIssueEditDrawer from "./SiteIssueEditDrawer";
import { readPermissionMap } from "../../../nav";
import {
  ISSUE_STATUS,
  type RegisterPanelRefresh,
  raw,
  readError,
} from "./panelUtils";

function idempotencyKey(): string {
  const suffix = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `site-void-${suffix}`;
}

interface SiteReturnRow {
  issueLineId: string;
  issue: SiteIssueDocument;
  /**
   * null = 整单作废后的摘要行：面板作废把明细一并软作废、search 只回活行，
   * 没有这一行用户就无从核对「作废成功了没有」。
   */
  line: SiteIssueLine | null;
}

function returnRequirement(row: SiteReturnRow): { label: string; color: string } {
  if (row.issue.workflow_status === "void" || !row.line) return { label: "领用已作废", color: "default" };
  const line = row.line;
  const state = line.return_requirement?.requirement_status;
  if (state) {
    // Native issue requirements may preserve a server-owned category snapshot.
    // Current editable flags describe input intent and cannot override that result.
    const label = state === "required" ? "应返" : state === "exempt" ? "免返" : "待确认品类";
    return {
      label: line.no_return == null ? `按规则判断（${label}）` : label,
      color: state === "required" ? "orange" : state === "exempt" ? "blue" : "gold",
    };
  }
  if (line.no_return === true) return { label: "免返", color: "blue" };
  if (line.no_return === false) return { label: "应返", color: "orange" };
  return { label: "按规则判断（待确认品类）", color: "gold" };
}

/** 领用事实与收到返件分别展示；返件总数仅由项目/需求单台账汇总。 */
export function SiteReturnTab({
  projectId,
  exportBase,
  canUpload,
  onChanged,
  registerRefresh,
}: {
  projectId: string;
  exportBase: string;
  canUpload: boolean;
  onChanged: () => Promise<boolean>;
  registerRefresh: RegisterPanelRefresh;
}) {
  const { message } = App.useApp();
  const [linesPage, setLinesPage] = useState(1);
  const [selectedLineIds, setSelectedLineIds] = useState<React.Key[]>([]);
  const [editTarget, setEditTarget] = useState<SiteReturnRow | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [editingPending, setEditingPending] = useState(false);
  const [categoryFilter, setCategoryFilter] = useState<MaintenanceCategoryFilterInput>({});
  const query = useContextQuery({ entryId: "POST /api/maintenance/site-issues/search",
    rule: surfaceRule("maintenance_issue.facts"), queryKey: [projectId, categoryFilter], legacyAllowed: true,
    query: async capture => {
      const issues: SiteIssueDocument[] = [];
      let page = 1, total = 0, authorization_context: AuthorizationContext | undefined;
      do {
        if (!capture.isCurrent()) throw new Error("领用查询已失效");
        const response = await searchSiteIssues({ project_id: projectId, page, page_size: 100, ...categoryFilter }, capture);
        if (!capture.isCurrent()) throw new Error("领用查询已失效");
        if (authorization_context && JSON.stringify(authorization_context) !== JSON.stringify(response.data.authorization_context)) throw new ContextChanged();
        authorization_context = response.data.authorization_context;
        total = response.data.total;
        issues.push(...response.data.rows);
        if (!response.data.rows.length) break;
        page += 1;
      } while (issues.length < total);
      return { issues, authorization_context };
    },
  });
  const rows = useMemo(() => (query.data?.issues ?? []).flatMap((issue): SiteReturnRow[] => {
    if (issue.workflow_status === "void" && !issue.lines.length) return [{ issueLineId: `void:${issue.issue_id}`, issue, line: null }];
    return issue.lines.map(line => ({ issueLineId: line.issue_line_id, issue, line }));
  }), [query.data]);
  const loading = query.loading, loadError = query.error ? readError(query.error, "领用记录加载失败，不代表没有领用记录") : null;
  const load = query.retry;
  useEffect(() => {
    registerRefresh("site", query.entryState === "deny" ? null : load);
    return () => registerRefresh("site", null);
  }, [load, query.entryState, registerRefresh]);
  useEffect(() => { setSelectedLineIds([]); }, [query.data]);

  // 2026-08-23：做错的领用单可整单作废（软作废，历史与审计保留）。
  // 门禁与后端一致：site_issue_manage 动作 + 成本可见（页面权限天然具备）。
  const perms = readPermissionMap();
  const legacyManage = !!perms.action_maintenance_site_issue_manage && !!perms.data_purchase_cost;
  const admission = useContextAdmission();
  const canManageIssues = admission.surface("maintenance_issue.correction", legacyManage) !== "deny";
  const canVoidIssues = admission.state(allRule(surfaceRule("maintenance_issue.facts"), actionRule("maintenance_issue", "void")), legacyManage) !== "deny";
  const canCorrectLine = (row: SiteReturnRow) => !!row.line
    && row.issue.project_id === projectId
    && ["legacy", "workbook", "page_manual"].includes(row.issue.source ?? "")
    && ["confirmed", "corrected"].includes(row.issue.workflow_status)
    && !row.line.delivery_line_id;
  const selectedLines = rows.filter((row) => selectedLineIds.includes(row.issueLineId) && canCorrectLine(row))
    .map((row) => ({
      issueId: row.issue.issue_id, issueNo: row.issue.display_no || row.issue.issue_no,
      issueVersion: row.issue.version, lineId: row.line!.issue_line_id,
      lineNo: row.line!.line_no, lineVersion: row.line!.version,
      pn: row.line!.pn, noReturn: row.line!.no_return,
    }));
  const [voidTarget, setVoidTarget] = useState<SiteIssueDocument | null>(null);
  const [voidReason, setVoidReason] = useState("");
  const [voiding, setVoiding] = useState(false);
  const commandCapture = useContextTransport(JSON.stringify([projectId, voidTarget?.issue_id, voidTarget?.version, voidReason]), legacyManage);
  const voidAttempt = useRef<{ content: string; key: string } | null>(null);
  // 2026-08-23 体验修正：作废是整单语义，同单每一行都给按钮（用户看到
  // 空白列会以为该单不能作废）；弹窗里写清该单共几行一起作废
  const linesPerIssue = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of rows) {
      if (!row.line) continue; // 已作废摘要行不是明细，不计入
      counts.set(row.issue.issue_id, (counts.get(row.issue.issue_id) ?? 0) + 1);
    }
    return counts;
  }, [rows]);

  const confirmVoid = async () => {
    if (!canVoidIssues || !commandCapture.isCurrent() || !voidTarget || !voidReason.trim()) return;
    setVoiding(true);
    const content = JSON.stringify([voidTarget.issue_id, voidTarget.version, voidReason.trim()]);
    if (voidAttempt.current?.content !== content) voidAttempt.current = { content, key: idempotencyKey() };
    try {
      await voidSiteIssue(voidTarget.issue_id, {
        project_id: voidTarget.project_id,
        version: voidTarget.version,
        idempotency_key: voidAttempt.current.key,
        reason: voidReason.trim(),
      }, commandCapture);
      if (!commandCapture.isCurrent()) return;
      const issueNo = voidTarget.display_no || voidTarget.issue_no;
      setVoidTarget(null);
      setVoidReason("");
      if (await onChanged()) {
        message.success(`领用单 ${issueNo} 已作废并刷新`);
      } else {
        message.warning(`领用单 ${issueNo} 已作废，但页面刷新失败；旧数据已失效，请重试。`);
      }
    } catch (err) {
      if (commandCapture.isCurrent()) message.error(readError(err, "作废失败，请刷新后重试"));
    } finally {
      if (commandCapture.isCurrent()) setVoiding(false);
    }
  };

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <section aria-labelledby="return-receipts-heading">
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Typography.Title id="return-receipts-heading" level={4} style={{ margin: 0 }}>
            返还登记与台账
          </Typography.Title>
          <ReturnReceiptsSection
            key={`receipts-${projectId}`}
            projectId={projectId}
            onChanged={onChanged}
            registerRefresh={registerRefresh}
          />
        </Space>
      </section>

      <section aria-labelledby="site-issue-heading">
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Typography.Title id="site-issue-heading" level={4} style={{ margin: "12px 0 0" }}>
            领用登记与单据
          </Typography.Title>
          {/* v1.36：复活 v1.21 的领用工作台（新增/编辑/确认，candidate 适配器门禁），
              入口与作废同门禁：site_issue_manage + 成本可见。 */}
          <SiteIssueWorkflowPanel
            key={`workflow-${projectId}`}
            projectId={projectId}
            canManage={legacyManage}
            onChanged={onChanged}
            registerRefresh={registerRefresh}
          />
        </Space>
      </section>

      <section aria-labelledby="site-issue-details-heading">
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Typography.Title id="site-issue-details-heading" level={5} style={{ margin: "12px 0 0" }}>
            领用明细与批量维护
          </Typography.Title>
          <PanelActionBar
            workbook={(
              <ProjectWorkbookRoundTrip projectId={projectId} surface="site_roundtrip" sheets={[SHEETS.site]}
                title="维保领用与返还"
                filename={`${exportBase}-${SHEETS.site}.xlsx`}
                canUpload={canUpload}
                onAfterApply={onChanged}
              />
            )}
            hint="Excel 批量维护：下载后回填领用事实、领用地址和是否应返还，再在此上传；地址留空保留原值，清空请在地址操作列填写“清空”"
          />
          {query.entryState !== "deny" && <><MaintenanceCategoryFilter labelPrefix="领用" value={categoryFilter} onChange={(next) => {
            setSelectedLineIds([]);
            setCategoryFilter(next);
            setLinesPage(1);
          }} />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            按当前主数据库品类筛选领用明细；作废操作仍影响整张领用单。
          </Typography.Text>
          {canManageIssues ? <SiteIssueReturnBatch key={`return-requirement-${projectId}`}
            selection={selectedLines} disabled={loading || !!loadError || editingPending}
            onChanged={async () => { setSelectedLineIds([]); return onChanged(); }} /> : null}
          {canManageIssues && editTarget && !editOpen ? <Button onClick={() => setEditOpen(true)}>继续编辑或核对领用</Button> : null}
          {loadError ? <Alert type="error" showIcon message={loadError} action={<Button onClick={() => { void load(); }}>重新加载领用</Button>} /> : null}
          <Table<SiteReturnRow>
        rowKey="issueLineId"
        size="small"
        loading={loading}
        dataSource={rows}
        rowSelection={canManageIssues ? {
          selectedRowKeys: selectedLineIds,
          onChange: setSelectedLineIds,
          getCheckboxProps: (row) => ({
            disabled: loading || !canCorrectLine(row),
            title: canCorrectLine(row) ? "选择领用行" : "此来源或状态不支持页面更正返还要求",
          }),
        } : undefined}
        scroll={{ x: 1320 }}
        expandable={{
          rowExpandable: (row) => !!row.line,
          expandedRowRender: (row) => <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3 }}>
            <Descriptions.Item label="描述来源">当前型号主数据</Descriptions.Item>
            <Descriptions.Item label="品牌">{raw(row.line?.brand)}</Descriptions.Item>
            <Descriptions.Item label="品类">{[row.line?.category_major, row.line?.category_minor].filter(Boolean).join(" / ") || "—"}</Descriptions.Item>
            <Descriptions.Item label="单位">{raw(row.line?.unit)}</Descriptions.Item>
            <Descriptions.Item label="领用人">{raw(row.issue.receiver)}</Descriptions.Item>
            <Descriptions.Item label="发出人">{raw(row.issue.issued_by)}</Descriptions.Item>
            <Descriptions.Item label="领用地址（现场位置）"><SiteIssueAddress value={row.issue.site_location} showLabel={false} /></Descriptions.Item>
            <Descriptions.Item label="关联需求单">{raw(row.line?.demand_order_no ?? row.line?.source_order_id)}</Descriptions.Item>
            <Descriptions.Item label="来源明细">{raw(row.line?.source_line_id)}</Descriptions.Item>
          </Descriptions>,
        }}
        pagination={{ current: linesPage, onChange: setLinesPage, pageSize: 10, showSizeChanger: false }}
        locale={{ emptyText: loadError ? "领用记录读取失败，请重新加载" : "本项目暂无维保领用记录" }}
        columns={[
          { title: "领用单号", render: (_value, item) => raw(item.issue.display_no || item.issue.issue_no) },
          { title: "领用日期", render: (_value, item) => raw(item.issue.issue_date) },
          { title: "领用地址", width: 220, render: (_value, item) => <SiteIssueAddress value={item.issue.site_location} showLabel={false} /> },
          {
            title: "领用状态",
            render: (_value, item) => {
              const status = ISSUE_STATUS[item.issue.workflow_status];
              const voidedAt = item.line || !item.issue.voided_at ? null : formatBusinessDateTime(item.issue.voided_at);
              return (
                <Space direction="vertical" size={0}>
                  <Tag color={status?.color}>{status?.label ?? item.issue.workflow_status}</Tag>
                  {voidedAt ? (
                    <span style={{ fontSize: 12, color: "rgba(0,0,0,.45)" }}>
                      作废于 {voidedAt}
                    </span>
                  ) : null}
                </Space>
              );
            },
          },
          { title: "PN / 当前型号描述", width: 220, render: (_value, item) => <Space direction="vertical" size={0}>
            <span style={{ fontFamily: "monospace" }}>{raw(item.line?.pn)}</span>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>{raw(item.line?.description)}</Typography.Text>
          </Space> },
          { title: "SN", render: (_value, item) => raw(item.line?.serial_number) },
          { title: "领用数量", render: (_value, item) => raw(item.line?.quantity) },
          {
            title: "是否应返还",
            render: (_value, item) => {
              const status = returnRequirement(item);
              return <Tag color={status.color}>{status.label}</Tag>;
            },
          },
          { title: "现场备注", width: 200, render: (_value, item) => raw(item.line?.remark) },
          ...((canVoidIssues || canManageIssues)
            ? [{
                title: "操作",
                key: "ops",
                width: 150,
                render: (_value: unknown, item: SiteReturnRow) =>
                  item.issue.workflow_status === "void" ? (
                    <Tag>已作废</Tag>
                  ) : (
                    <Space>
                    {canManageIssues && canCorrectLine(item) ? <Button size="small" aria-label="编辑领用行"
                      disabled={loading || editingPending}
                      onClick={() => { setEditTarget(item); setEditOpen(true); }}>编辑</Button> : null}
                    {canVoidIssues && <Button
                      size="small"
                      danger
                      disabled={editingPending}
                      onClick={() => { setVoidTarget(item.issue); setVoidReason(""); setVoiding(false); }}
                    >
                      作废
                    </Button>}
                    </Space>
                  ),
              }]
            : []),
        ]}
          /></>}
        </Space>
      </section>
      {canManageIssues && editTarget?.line ? <SiteIssueEditDrawer key={`${projectId}-${editTarget.issueLineId}`}
        issue={editTarget.issue} line={editTarget.line} open={editOpen}
        onClose={() => setEditOpen(false)} onChanged={onChanged} onPendingChange={setEditingPending} /> : null}
      <Modal
        open={voidTarget !== null}
        title={voidTarget
          ? `作废领用单 ${voidTarget.display_no || voidTarget.issue_no}（共 ${voidTarget.total_line_count ?? linesPerIssue.get(voidTarget.issue_id) ?? voidTarget.lines.length} 行）`
          : ""}
        confirmLoading={voiding}
        okText="确认作废"
        okButtonProps={{ danger: true, disabled: !voidReason.trim() }}
        cancelText="取消"
        onCancel={() => { setVoidTarget(null); setVoidReason(""); setVoiding(false); }}
        onOk={() => { void confirmVoid(); }}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <span style={{ fontSize: 12, color: "rgba(0,0,0,.55)" }}>
            整单软作废：该单全部领用行退出成本与返还义务计算，历史与审计保留、可追溯。
          </span>
          <Input.TextArea
            value={voidReason}
            onChange={(event) => setVoidReason(event.target.value)}
            placeholder="作废原因（必填），如：录错项目 / 重复录入"
            rows={2}
            autoFocus
          />
        </Space>
      </Modal>
    </Space>
  );
}

export default SiteReturnTab;
