import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Input, Modal, Space, Table, Tag, message } from "antd";
import type {
  MaintenanceBadReturn,
  MaintenanceReturnObligation,
  SiteIssueDocument,
  SiteIssueLine,
} from "../../../api/maintenanceOperations";
import {
  searchMaintenanceBadReturns,
  searchMaintenanceReturnObligations,
  searchSiteIssues,
  voidSiteIssue,
} from "../../../api/maintenanceOperations";
import {
  SHEETS,
  applyProjectMaster,
  downloadProjectMaster,
} from "../../../api/maintenanceWorkbooks";
import WorkbookRoundTrip from "../../../components/maintenance/WorkbookRoundTrip";
import { readPermissionMap } from "../../../nav";
import {
  ISSUE_STATUS,
  RETURN_DOCUMENT_STATUS,
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
  line: SiteIssueLine;
  obligation: MaintenanceReturnObligation | null;
  returns: MaintenanceBadReturn[];
}

function returnStatus(row: SiteReturnRow): { label: string; color: string } {
  if (row.issue.workflow_status === "void") return { label: "领用已作废", color: "default" };
  if (row.line.no_return === true || row.obligation?.classification === "exempt") {
    return { label: "免返", color: "blue" };
  }
  if (row.obligation?.classification === "pending_category") {
    return { label: "待确认品类", color: "gold" };
  }
  if (!row.obligation) return { label: "待生成返还义务", color: "gold" };
  const remaining = Number(row.obligation.remaining_quantity);
  const confirmed = Number(row.obligation.warehouse_confirmed_quantity);
  const registered = Number(row.obligation.registered_quantity);
  if (remaining <= 0 && confirmed > 0) return { label: "仓库已确认返还", color: "green" };
  if (registered > 0 && remaining > 0) return { label: "部分返还", color: "orange" };
  const activeReturn = row.returns.find((item) => item.status !== "void");
  if (activeReturn) {
    return {
      label: RETURN_DOCUMENT_STATUS[activeReturn.status] ?? activeReturn.status,
      color: activeReturn.status === "in_transit" ? "cyan" : "blue",
    };
  }
  return { label: "待返还", color: "red" };
}

/** 领用与返还 tab：以领用行 part_id 为主轴，合并返还义务和返还单状态。 */
export function SiteReturnTab({
  projectId,
  exportBase,
  canUpload,
}: {
  projectId: string;
  exportBase: string;
  canUpload: boolean;
}) {
  const [rows, setRows] = useState<SiteReturnRow[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [issuesResponse, obligationsResponse, returnsResponse] = await Promise.all([
        searchSiteIssues({ project_id: projectId, page: 1, page_size: 100 }),
        searchMaintenanceReturnObligations({
          project_id: projectId,
          active_only: false,
          page: 1,
          page_size: 200,
        }),
        searchMaintenanceBadReturns({ project_id: projectId, page: 1, page_size: 100 }),
      ]);
      const obligations = new Map(
        obligationsResponse.data.rows.map((item) => [item.issue_line_id, item]),
      );
      const returns = new Map<string, MaintenanceBadReturn[]>();
      for (const document of returnsResponse.data.rows) {
        for (const line of document.lines) {
          const current = returns.get(line.obligation_id) ?? [];
          current.push(document);
          returns.set(line.obligation_id, current);
        }
      }
      setRows(issuesResponse.data.rows.flatMap((issue) =>
        issue.lines.map((line) => {
          const obligation = obligations.get(line.issue_line_id) ?? null;
          return {
            issueLineId: line.issue_line_id,
            issue,
            line,
            obligation,
            returns: obligation ? returns.get(obligation.obligation_id) ?? [] : [],
          };
        }),
      ));
    } catch (err) {
      message.error(readError(err, "维保领用与返还状态加载失败"));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);

  // 2026-08-23：做错的领用单可整单作废（软作废，历史与审计保留）。
  // 门禁与后端一致：site_issue_manage 动作 + 成本可见（页面权限天然具备）。
  const perms = readPermissionMap();
  const canVoidIssues = !!perms.action_maintenance_site_issue_manage
    && !!perms.data_purchase_cost;
  const [voidTarget, setVoidTarget] = useState<SiteIssueDocument | null>(null);
  const [voidReason, setVoidReason] = useState("");
  const [voiding, setVoiding] = useState(false);
  // 2026-08-23 体验修正：作废是整单语义，同单每一行都给按钮（用户看到
  // 空白列会以为该单不能作废）；弹窗里写清该单共几行一起作废
  const linesPerIssue = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of rows) {
      counts.set(row.issue.issue_id, (counts.get(row.issue.issue_id) ?? 0) + 1);
    }
    return counts;
  }, [rows]);

  const confirmVoid = async () => {
    if (!voidTarget || !voidReason.trim()) return;
    setVoiding(true);
    try {
      await voidSiteIssue(voidTarget.issue_id, {
        project_id: voidTarget.project_id,
        version: voidTarget.version,
        idempotency_key: idempotencyKey(),
        reason: voidReason.trim(),
      });
      message.success(`领用单 ${voidTarget.issue_no} 已作废`);
      setVoidTarget(null);
      setVoidReason("");
      await load();
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
        onApply={async (file) => {
          const result = await applyProjectMaster(projectId, file);
          await load();
          return result;
        }}
      />
      <Table<SiteReturnRow>
        rowKey="issueLineId"
        size="small"
        loading={loading}
        dataSource={rows}
        scroll={{ x: 1300 }}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        locale={{ emptyText: "本项目暂无维保领用记录" }}
        columns={[
          { title: "领用单号", render: (_value, item) => raw(item.issue.issue_no) },
          { title: "领用日期", render: (_value, item) => raw(item.issue.issue_date) },
          {
            title: "领用状态",
            render: (_value, item) => {
              const status = ISSUE_STATUS[item.issue.workflow_status];
              return <Tag color={status?.color}>{status?.label ?? item.issue.workflow_status}</Tag>;
            },
          },
          { title: "PN", render: (_value, item) => raw(item.line.pn) },
          { title: "SN", render: (_value, item) => raw(item.line.serial_number) },
          { title: "领用数量", render: (_value, item) => raw(item.line.quantity) },
          {
            title: "应返数量",
            render: (_value, item) => raw(item.obligation?.required_quantity),
          },
          {
            title: "已登记返还",
            render: (_value, item) => raw(item.obligation?.registered_quantity),
          },
          {
            title: "仓库确认返还",
            render: (_value, item) => raw(item.obligation?.warehouse_confirmed_quantity),
          },
          {
            title: "返还状态",
            render: (_value, item) => {
              const status = returnStatus(item);
              return <Tag color={status.color}>{status.label}</Tag>;
            },
          },
          {
            title: "返还单号",
            render: (_value, item) => {
              const numbers = item.returns
                .filter((document) => document.status !== "void")
                .map((document) => document.return_no);
              return numbers.length ? numbers.join("、") : "—";
            },
          },
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
