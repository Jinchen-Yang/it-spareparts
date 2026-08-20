import { useEffect, useState } from "react";
import { Card, Space, Table, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { MaintenanceCollectionSnapshotRow } from "../../../api/maintenanceOperations";
import {
  SHEETS,
  applyProjectMaster,
  downloadProjectMaster,
  getCollectionPlan,
} from "../../../api/maintenanceWorkbooks";
import type { CollectionPlanRow } from "../../../api/maintenanceWorkbooks";
import WorkbookRoundTrip from "../../../components/maintenance/WorkbookRoundTrip";
import { COLLECTION_STATUS, raw, readError } from "./panelUtils";

const { Text } = Typography;

/** 到款状态：应回未回一眼可见（用户 2026-08-20：计划填了但页面不显示状态）。 */
const ARRIVAL_STATUS: Record<string, { label: string; color: string }> = {
  paid: { label: "已到款", color: "green" },
  partial: { label: "部分到款", color: "orange" },
  pending: { label: "待回款", color: "blue" },
  overdue: { label: "逾期未回款", color: "red" },
};

/**
 * 回款 tab：每条快照的确认状态表。累计回款/进度已上页面健康带（2026-08-19 重设计），
 * 数据由面板页统一取 workspace 后传入，本 tab 不再重复请求。
 */
export function CollectionTab({
  projectId,
  exportBase,
  canUpload,
  rows,
  loading,
  onRefresh,
}: {
  projectId: string;
  exportBase: string;
  canUpload: boolean;
  rows: MaintenanceCollectionSnapshotRow[];
  loading: boolean;
  /** 上传覆盖后回读（含健康带指标 + 计划状态）。 */
  onRefresh: () => Promise<void>;
}) {
  const [planRows, setPlanRows] = useState<CollectionPlanRow[]>([]);

  const loadPlan = async () => {
    try {
      const resp = await getCollectionPlan(projectId);
      setPlanRows(resp.rows);
    } catch (err) {
      message.error(readError(err, "回款计划加载失败"));
    }
  };

  useEffect(() => {
    void loadPlan();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const planColumns: ColumnsType<CollectionPlanRow> = [
    { title: "合同编号", dataIndex: "contract_no", render: raw },
    { title: "期次", dataIndex: "sequence", width: 70 },
    { title: "计划回款日期", dataIndex: "planned_date", render: raw },
    {
      title: "计划金额（含税）",
      dataIndex: "planned_amount",
      render: (value) => value == null ? "—" : `¥${Number(value).toFixed(2)}`,
    },
    {
      title: "累计计划",
      dataIndex: "cumulative_planned",
      render: (value) => `¥${Number(value || 0).toFixed(2)}`,
    },
    {
      title: "累计实收",
      dataIndex: "cumulative_actual",
      render: (value) => `¥${Number(value || 0).toFixed(2)}`,
    },
    {
      title: "到款状态",
      dataIndex: "arrival_state",
      width: 120,
      render: (value: string) => {
        const status = ARRIVAL_STATUS[value];
        return <Tag color={status?.color}>{status?.label ?? raw(value)}</Tag>;
      },
    },
    { title: "备注", dataIndex: "note", render: raw },
  ];

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <WorkbookRoundTrip
        size="small"
        title="回款"
        filename={`${exportBase}-${SHEETS.collection}.xlsx`}
        canUpload={canUpload}
        hint="下载后可回填累计实收、状态、凭证号和备注"
        onDownload={() => downloadProjectMaster(projectId, [SHEETS.collection])}
        onApply={async (file) => {
          const result = await applyProjectMaster(projectId, file);
          await onRefresh();
          await loadPlan();
          return result;
        }}
      />
      <Card
        size="small"
        title="回款计划（应回未回在这里看）"
        extra={<Text type="secondary" style={{ fontSize: 12 }}>在总表 02_回款计划 里填写/维护；实收在 05 填写后状态自动更新</Text>}
      >
        <Table<CollectionPlanRow>
          rowKey="milestone_id"
          size="small"
          dataSource={planRows}
          columns={planColumns}
          pagination={false}
          locale={{ emptyText: "暂无回款计划——在总表 02_回款计划 填写后会显示在这里" }}
        />
      </Card>
      <Card size="small" title="实收回款记录（05）">
        <Table<MaintenanceCollectionSnapshotRow>
          rowKey="collection_id"
        size="small"
        loading={loading}
        dataSource={rows}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        locale={{ emptyText: "本项目暂无回款记录" }}
          columns={[
            { title: "合同编号", dataIndex: "contract_no", render: raw },
            { title: "报告月份", dataIndex: "report_month", render: raw },
            {
              title: "累计实收金额（含税）",
              dataIndex: "cumulative_amount",
              render: (value) => value == null ? "—" : `¥${Number(value).toFixed(2)}`,
            },
            {
              title: "回款状态",
              dataIndex: "status",
              render: (value: string) => {
                const status = COLLECTION_STATUS[value];
                return <Tag color={status?.color}>{status?.label ?? raw(value)}</Tag>;
              },
            },
            { title: "回款凭证号", dataIndex: "receipt_reference", render: raw },
            { title: "备注", dataIndex: "remark", render: raw },
          ]}
        />
      </Card>
    </Space>
  );
}

export default CollectionTab;
