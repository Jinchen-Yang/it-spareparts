import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Descriptions, Input, Modal, Space, Table, Tag, Typography, message } from "antd";
import type {
  SiteIssueDocument,
  SiteIssueLine,
} from "../../../api/maintenanceOperations";
import {
  searchSiteIssues,
  voidSiteIssue,
} from "../../../api/maintenanceOperations";
import {
  SHEETS,
  applyProjectMaster,
  downloadProjectMaster,
  validateProjectMaster,
} from "../../../api/maintenanceWorkbooks";
import WorkbookRoundTrip from "../../../components/maintenance/WorkbookRoundTrip";
import ReturnReceiptsSection from "./ReturnReceiptsSection";
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

function formatVoidedAt(value: string | null | undefined): string | null {
  if (!value) return null;
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return value;
  return at.toLocaleString("zh-CN", { hour12: false });
}

async function fetchAllRows<T>(
  loadPage: (page: number) => Promise<{ data: { rows: T[]; total: number } }>,
): Promise<T[]> {
  const rows: T[] = [];
  let page = 1;
  let total = 0;
  do {
    const response = await loadPage(page);
    total = response.data.total;
    const nextRows = response.data.rows;
    if (!nextRows.length) break;
    rows.push(...nextRows);
    page += 1;
  } while (rows.length < total);
  return rows;
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
  const [rows, setRows] = useState<SiteReturnRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    try {
      setLoadError(null);
      const issues = await fetchAllRows<SiteIssueDocument>((page) => searchSiteIssues({
        project_id: projectId, page, page_size: 100,
      }));
      if (seq !== requestSeq.current) return false;
      setRows(issues.flatMap((issue): SiteReturnRow[] => {
        if (issue.workflow_status === "void" && !issue.lines.length) {
          // 整单作废后没有活行：给一行摘要（单号/日期/作废时间），不参与任何合计
          return [{
            issueLineId: `void:${issue.issue_id}`,
            issue,
            line: null,
          }];
        }
        return issue.lines.map((line) => {
          return {
            issueLineId: line.issue_line_id,
            issue,
            line,
          };
        });
      }));
      return true;
    } catch (err) {
      if (seq === requestSeq.current) {
        setRows([]);
        setLoadError(readError(err, "领用记录加载失败，不代表没有领用记录"));
      }
      return false;
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    registerRefresh("site", load);
    void load();
    return () => {
      requestSeq.current += 1;
      registerRefresh("site", null);
    };
  }, [load, registerRefresh]);

  // 2026-08-23：做错的领用单可整单作废（软作废，历史与审计保留）。
  // 门禁与后端一致：site_issue_manage 动作 + 成本可见（页面权限天然具备）。
  const perms = readPermissionMap();
  const canVoidIssues = !!perms.action_maintenance_site_issue_manage
    && !!perms.data_purchase_cost;
  const [voidTarget, setVoidTarget] = useState<SiteIssueDocument | null>(null);
  const [voidReason, setVoidReason] = useState("");
  const [voiding, setVoiding] = useState(false);
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
    if (!voidTarget || !voidReason.trim()) return;
    setVoiding(true);
    const content = JSON.stringify([voidTarget.issue_id, voidTarget.version, voidReason.trim()]);
    if (voidAttempt.current?.content !== content) voidAttempt.current = { content, key: idempotencyKey() };
    try {
      await voidSiteIssue(voidTarget.issue_id, {
        project_id: voidTarget.project_id,
        version: voidTarget.version,
        idempotency_key: voidAttempt.current.key,
        reason: voidReason.trim(),
      });
      const issueNo = voidTarget.issue_no;
      setVoidTarget(null);
      setVoidReason("");
      if (await onChanged()) {
        message.success(`领用单 ${issueNo} 已作废并刷新`);
      } else {
        message.warning(`领用单 ${issueNo} 已作废，但页面刷新失败；旧数据已失效，请重试。`);
      }
    } catch (err) {
      message.error(readError(err, "作废失败，请刷新后重试"));
    } finally {
      setVoiding(false);
    }
  };

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <WorkbookRoundTrip
        size="small"
        title="维保领用与返还"
        filename={`${exportBase}-${SHEETS.site}.xlsx`}
        canUpload={canUpload}
        hint="可回填领用事实和是否应返还；上传后页面立即刷新"
        onDownload={() => downloadProjectMaster(projectId, [SHEETS.site])}
        onValidate={(file) => validateProjectMaster(projectId, file)}
        onApply={(file, opts) => applyProjectMaster(projectId, file, opts)}
        onAfterApply={onChanged}
      />
      <ReturnReceiptsSection key={projectId} projectId={projectId} onChanged={onChanged} />
      <Typography.Title level={5} style={{ margin: 0 }}>领用记录</Typography.Title>
      {loadError ? <Alert type="error" showIcon message={loadError} action={<Button onClick={() => { void load(); }}>重新加载领用</Button>} /> : null}
      <Table<SiteReturnRow>
        rowKey="issueLineId"
        size="small"
        loading={loading}
        dataSource={rows}
        scroll={{ x: 1100 }}
        expandable={{
          rowExpandable: (row) => !!row.line,
          expandedRowRender: (row) => <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3 }}>
            <Descriptions.Item label="描述来源">当前型号主数据</Descriptions.Item>
            <Descriptions.Item label="品牌">{raw(row.line?.brand)}</Descriptions.Item>
            <Descriptions.Item label="品类">{[row.line?.category_major, row.line?.category_minor].filter(Boolean).join(" / ") || "—"}</Descriptions.Item>
            <Descriptions.Item label="单位">{raw(row.line?.unit)}</Descriptions.Item>
            <Descriptions.Item label="领用人">{raw(row.issue.receiver)}</Descriptions.Item>
            <Descriptions.Item label="发出人">{raw(row.issue.issued_by)}</Descriptions.Item>
            <Descriptions.Item label="现场位置">{raw(row.issue.site_location)}</Descriptions.Item>
            <Descriptions.Item label="关联需求单">{raw(row.line?.demand_order_no ?? row.line?.source_order_id)}</Descriptions.Item>
            <Descriptions.Item label="来源明细">{raw(row.line?.source_line_id)}</Descriptions.Item>
          </Descriptions>,
        }}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        locale={{ emptyText: loadError ? "领用记录读取失败，请重新加载" : "本项目暂无维保领用记录" }}
        columns={[
          { title: "领用单号", render: (_value, item) => raw(item.issue.issue_no) },
          { title: "领用日期", render: (_value, item) => raw(item.issue.issue_date) },
          {
            title: "领用状态",
            render: (_value, item) => {
              const status = ISSUE_STATUS[item.issue.workflow_status];
              const voidedAt = item.line ? null : formatVoidedAt(item.issue.voided_at);
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
          ...(canVoidIssues
            ? [{
                title: "操作",
                key: "ops",
                width: 90,
                render: (_value: unknown, item: SiteReturnRow) =>
                  item.issue.workflow_status === "void" ? (
                    <Tag>已作废</Tag>
                  ) : (
                    <Button
                      size="small"
                      danger
                      onClick={() => { setVoidTarget(item.issue); setVoidReason(""); }}
                    >
                      作废
                    </Button>
                  ),
              }]
            : []),
        ]}
      />
      <Modal
        open={voidTarget !== null}
        title={voidTarget
          ? `作废领用单 ${voidTarget.issue_no}（共 ${linesPerIssue.get(voidTarget.issue_id) ?? voidTarget.lines.length} 行）`
          : ""}
        confirmLoading={voiding}
        okText="确认作废"
        okButtonProps={{ danger: true, disabled: !voidReason.trim() }}
        cancelText="取消"
        onCancel={() => { setVoidTarget(null); setVoidReason(""); }}
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
