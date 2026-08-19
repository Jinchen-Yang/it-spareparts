import { useCallback, useEffect, useState } from "react";
import { Space, Table, Tag, message } from "antd";
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
} from "../../../api/maintenanceOperations";
import {
  SHEETS,
  applyProjectMaster,
  downloadProjectMaster,
} from "../../../api/maintenanceWorkbooks";
import WorkbookRoundTrip from "../../../components/maintenance/WorkbookRoundTrip";
import {
  ISSUE_STATUS,
  RETURN_DOCUMENT_STATUS,
  raw,
  readError,
} from "./panelUtils";

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
        ]}
      />
    </Space>
  );
}

export default SiteReturnTab;
