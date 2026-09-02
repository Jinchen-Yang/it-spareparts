import { useCallback, useEffect, useRef, useState } from "react";
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
  const [rows, setRows] = useState<ProjectExpenseRow[]>([]);
  const [loading, setLoading] = useState(false);
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++requestSeq.current;
    setLoading(true);
    try {
      const nextRows = (await listProjectExpenseRows(projectId)).rows;
      if (seq !== requestSeq.current) return false;
      setRows(nextRows);
      return true;
    } catch (err) {
      if (seq === requestSeq.current) {
        setRows([]);
        message.error(readError(err, "报销明细加载失败"));
      }
      return false;
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    registerRefresh("expense", load);
    void load();
    return () => {
      requestSeq.current += 1;
      registerRefresh("expense", null);
    };
  }, [load, registerRefresh]);

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
        onApply={(file, opts) => applyProjectMaster(projectId, file, opts)}
        onAfterApply={onChanged}
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
