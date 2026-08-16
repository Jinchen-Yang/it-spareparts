import { Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import type {
  BoardLineRow,
  BoardOrderRow,
} from "../../../api/maintenanceBossBoard";
import KnownCostCell from "./KnownCostCell";
import StatCell from "./StatCell";

const { Text } = Typography;

/** 数量原样展示：null 显示「—」，绝不补 0（铁律 3/5）。 */
function raw(value: string | number | null | undefined) {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

/**
 * 单据证据表（plan v1.3 §5.1）。自报四列与三源事实**纯并排**，
 * 无任何差异高亮/徽标——服务端亦不产出 mismatch（铁律 3 / M4-4 / F5 未豁免）。
 */
export function OrderEvidenceTable({
  rows,
  total,
  page,
  pageSize,
  loading,
  onChange,
  onSelect,
  selectedId,
}: {
  rows: BoardOrderRow[];
  total: number;
  page: number;
  pageSize: number;
  loading?: boolean;
  onChange: (page: number, pageSize: number) => void;
  onSelect: (sourceOrderId: string) => void;
  selectedId?: string | null;
}) {
  const columns: ColumnsType<BoardOrderRow> = [
    {
      title: "需求单号",
      dataIndex: "order_no",
      width: 190,
      render: (value: string, row) => (
        <Space direction="vertical" size={0}>
          <a onClick={() => onSelect(row.source_order_id)}>{value}</a>
          <Space size={4}>
            {row.is_pre_delivery ? <Tag>预交付</Tag> : null}
            {row.data_status ? <Tag color="default">{row.data_status}</Tag> : null}
          </Space>
        </Space>
      ),
    },
    { title: "制单日期", dataIndex: "order_date", width: 110, render: raw },
    { title: "明细行", dataIndex: "line_count", width: 80 },
    {
      title: "已知申请估算成本(含税)",
      dataIndex: "known_apply_cost_inc_tax",
      width: 200,
      render: (_: unknown, row) => (
        <KnownCostCell stat={row.known_apply_cost_inc_tax} compact />
      ),
    },
    {
      title: "自报·已发货",
      dataIndex: ["self_report", "head_shipped_qty"],
      width: 110,
      render: (_: unknown, row) => raw(row.self_report.head_shipped_qty),
    },
    {
      title: "事实·实发",
      dataIndex: ["facts", "shipped_qty"],
      width: 120,
      render: (_: unknown, row) => <StatCell stat={row.facts.shipped_qty} />,
    },
    {
      title: "自报·已返货",
      dataIndex: ["self_report", "head_returned_qty"],
      width: 110,
      render: (_: unknown, row) => raw(row.self_report.head_returned_qty),
    },
    {
      title: "事实·未用件收回",
      dataIndex: ["facts", "returned_good_qty"],
      width: 140,
      render: (_: unknown, row) => <StatCell stat={row.facts.returned_good_qty} />,
    },
    {
      title: "事实·坏件回收",
      dataIndex: ["facts", "returned_bad_qty"],
      width: 130,
      render: (_: unknown, row) => <StatCell stat={row.facts.returned_bad_qty} />,
    },
  ];
  return (
    <Table<BoardOrderRow>
      rowKey="source_order_id"
      size="small"
      loading={loading}
      dataSource={rows}
      columns={columns}
      scroll={{ x: 1300 }}
      rowClassName={(row) =>
        row.source_order_id === selectedId ? "ant-table-row-selected" : ""
      }
      pagination={{
        current: page,
        pageSize,
        total,
        showSizeChanger: false,
        showTotal: (value) => `共 ${value} 张单`,
        onChange,
      }}
    />
  );
}

/**
 * PN 证据行（plan v1.3 §4.5）：14 个流转状态列**原样展示**，
 * 不参与任何计算、不标注可信度（铁律 3）。
 */
export function LineEvidenceTable({
  rows,
  total,
  page,
  pageSize,
  loading,
  onChange,
}: {
  rows: BoardLineRow[];
  total: number;
  page: number;
  pageSize: number;
  loading?: boolean;
  onChange: (page: number, pageSize: number) => void;
}) {
  const columns: ColumnsType<BoardLineRow> = [
    {
      title: "PN",
      dataIndex: "pn_std",
      width: 150,
      render: (value: string | null, row) => raw(value || row.pn_raw),
    },
    { title: "描述", dataIndex: "description", width: 200, render: raw },
    { title: "需求", dataIndex: "qty", width: 80, render: raw },
    { title: "需采", dataIndex: "purchase_qty", width: 80, render: raw },
    { title: "已采", dataIndex: "purchased_qty", width: 80, render: raw },
    { title: "待采", dataIndex: "pending_purchase_qty", width: 80, render: raw },
    { title: "直采直发", dataIndex: "direct_ship_qty", width: 90, render: raw },
    { title: "库房需发", dataIndex: "warehouse_need_qty", width: 90, render: raw },
    { title: "库房发货", dataIndex: "warehouse_shipped_qty", width: 90, render: raw },
    { title: "已供", dataIndex: "supplied_qty", width: 80, render: raw },
    { title: "待供", dataIndex: "pending_supply_qty", width: 80, render: raw },
    { title: "退货", dataIndex: "return_qty", width: 80, render: raw },
    { title: "已返", dataIndex: "returned_qty", width: 80, render: raw },
    { title: "待返", dataIndex: "pending_return_qty", width: 80, render: raw },
    { title: "领用", dataIndex: "consumed_qty", width: 80, render: raw },
    { title: "需求待返", dataIndex: "demand_pending_return_qty", width: 90, render: raw },
    {
      title: "已知申请估算成本(含税)",
      dataIndex: "known_apply_cost_inc_tax",
      width: 170,
      render: (_: unknown, row) => <StatCell stat={row.known_apply_cost_inc_tax} />,
    },
    {
      title: "取价来源",
      dataIndex: "cost_source",
      width: 130,
      render: (_: unknown, row) => <StatCell stat={row.cost_source} />,
    },
    { title: "发货SN", dataIndex: "serial_numbers", width: 140, render: raw },
  ];
  return (
    <div>
      <Text type="secondary" style={{ fontSize: 11.5 }}>
        流转状态列（已采/待供/待返/领用等）为氚云原样数据，系统只展示、不参与任何计算。
      </Text>
      <Table<BoardLineRow>
        rowKey="raw_line_id"
        size="small"
        loading={loading}
        dataSource={rows}
        columns={columns}
        scroll={{ x: 2100 }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: false,
          showTotal: (value) => `共 ${value} 行`,
          onChange,
        }}
      />
    </div>
  );
}
