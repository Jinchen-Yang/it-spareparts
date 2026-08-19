import { Space, Table, Tag } from "antd";
import type { MaintenanceCollectionSnapshotRow } from "../../../api/maintenanceOperations";
import {
  SHEETS,
  applyProjectMaster,
  downloadProjectMaster,
} from "../../../api/maintenanceWorkbooks";
import WorkbookRoundTrip from "../../../components/maintenance/WorkbookRoundTrip";
import { COLLECTION_STATUS, raw } from "./panelUtils";

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
  /** 上传覆盖后回读（含健康带指标）。 */
  onRefresh: () => Promise<void>;
}) {
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
          return result;
        }}
      />
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
    </Space>
  );
}

export default CollectionTab;
