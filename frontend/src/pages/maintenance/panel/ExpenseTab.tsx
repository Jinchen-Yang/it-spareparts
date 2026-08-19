import { useCallback, useEffect, useState } from "react";
import { Space, Table, message } from "antd";
import type { ProjectExpenseRow } from "../../../api/maintenanceWorkbooks";
import {
  SHEETS,
  applyProjectMaster,
  downloadProjectMaster,
  listProjectExpenseRows,
  validateProjectMaster,
} from "../../../api/maintenanceWorkbooks";
import WorkbookRoundTrip from "../../../components/maintenance/WorkbookRoundTrip";
import { raw, readError } from "./panelUtils";

/** 报销 tab：04 表的 web 呈现（含备注，#47）+ 下载上传（两阶段回传）。只展示，不散改。 */
export function ExpenseTab({
  projectId,
  exportBase,
  canUpload,
}: {
  projectId: string;
  exportBase: string;
  canUpload: boolean;
}) {
  const [rows, setRows] = useState<ProjectExpenseRow[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRows((await listProjectExpenseRows(projectId)).rows);
    } catch (err) {
      message.error(readError(err, "报销明细加载失败"));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <WorkbookRoundTrip
        size="small"
        title="报销"
        filename={`${exportBase}-${SHEETS.expense}.xlsx`}
        canUpload={canUpload}
        hint="在哪下载就在哪上传：黄底的「未税金额」「备注」两列可改"
        onDownload={() => downloadProjectMaster(projectId, [SHEETS.expense])}
        onValidate={(file) => validateProjectMaster(projectId, file)}
        onApply={async (file) => {
          const result = await applyProjectMaster(projectId, file);
          await load();          // 上传覆盖后立刻回读，页面不留旧值
          return result;
        }}
      />
      <Table<ProjectExpenseRow>
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
        ]}
      />
    </Space>
  );
}

export default ExpenseTab;
