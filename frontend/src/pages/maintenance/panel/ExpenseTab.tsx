import { useContextQuery } from "../../../authz/contextQuery";
import { surfaceRule } from "../../../authz/rules";
import { useModuleAccess } from "../../../authz/access";
import { useEffect } from "react";
import { Alert, Button, Space, Table } from "antd";
import type { ProjectExpenseRow } from "../../../api/maintenanceWorkbooks";
import {
  SHEETS,
  listProjectExpenseRows,
} from "../../../api/maintenanceWorkbooks";
import ProjectWorkbookRoundTrip from "../../../components/maintenance/ProjectWorkbookRoundTrip";
import PanelActionBar from "./PanelActionBar";
import { type RegisterPanelRefresh, raw, readError } from "./panelUtils";

/** 报销 tab：04 表的 web 呈现（含备注，#47）+ 下载上传（两阶段回传）。只展示，不散改。 */
export function ExpenseTab({
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
  const query = useContextQuery({ entryId: "GET /api/maintenance/projects/stable/{project_id}/expense-rows",
    rule: surfaceRule("maintenance_expense.records"), queryKey: [projectId], legacyAllowed: true,
    query: capture => listProjectExpenseRows(projectId, capture) });
  const fieldAccess = useModuleAccess("maintenance_expense", query.context);
  const amounts = fieldAccess.field("data_profit", true);
  const rows = query.data?.rows ?? [], loading = query.loading;
  useEffect(() => {
    registerRefresh("expense", query.retry);
    return () => registerRefresh("expense", null);
  }, [query.retry, registerRefresh]);

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <PanelActionBar
        workbook={(
          <ProjectWorkbookRoundTrip projectId={projectId} surface="expense_roundtrip" sheets={[SHEETS.expense]}
            title="报销"
            filename={`${exportBase}-${SHEETS.expense}.xlsx`}
            canUpload={canUpload}
            onAfterApply={onChanged}
          />
        )}
        hint="在哪下载就在哪上传：黄底的「未税金额」「备注」两列可改"
      />
      {query.error ? <Alert type="error" showIcon message={readError(query.error, "报销加载失败")} action={<Button onClick={() => void query.retry()}>重试报销</Button>} /> : null}
      {query.entryState !== "deny" && <Table<ProjectExpenseRow>
        rowKey="raw_line_id"
        size="small"
        loading={loading}
        dataSource={rows}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        locale={{ emptyText: "本项目暂无报销行" }}
        columns={[
          { title: "报销单号", dataIndex: "bxd_no", render: raw },
          { title: "报销日期", dataIndex: "expense_date", render: raw },
          { title: "报销人员", dataIndex: "person", render: raw },
          { title: "费用分类", dataIndex: "fee_category", render: raw },
          { title: "合同编号", dataIndex: "contract_no", render: raw },
          { title: "未税金额", dataIndex: "amount_ex_tax", render: raw },
          { title: "含税金额(系统计算)", dataIndex: "amount_inc_tax", render: raw },
          { title: "流程状态", dataIndex: "data_status", render: raw },
          { title: "备注", dataIndex: "remark", render: raw },
        ].filter(column => amounts || !["amount_ex_tax", "amount_inc_tax"].includes(String(column.dataIndex)))}
      />}
    </Space>
  );
}

export default ExpenseTab;
