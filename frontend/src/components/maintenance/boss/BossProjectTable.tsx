import { Link } from "react-router-dom";
import { Pagination, Space, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import ResizableTable from "../../ResizableTable";
import type { BoardProjectRow } from "../../../api/maintenanceBossBoard";
import { UNASSIGNED_BUCKET } from "../../../api/maintenanceBossBoard";
import KnownCostCell from "./KnownCostCell";
import StatCell from "./StatCell";

const { Text } = Typography;

const LIFECYCLE_TEXT: Record<string, { text: string; color?: string }> = {
  ongoing: { text: "进行中", color: "blue" },
  ended: { text: "已结束" },
  missing: { text: "期限缺失", color: "default" },
};

/**
 * 全项目分页表（plan v1.3 §5.1）。服务端分页/筛选/排序；未归属桶恒在首行。
 * 数量列一律走 StatCell（六态唯一入口，未导入不渲染 0）。
 */
export interface BossProjectTableProps {
  rows: BoardProjectRow[];
  total: number;
  page: number;
  pageSize: number;
  loading?: boolean;
  onChange: (page: number, pageSize: number) => void;
}

export function BossProjectTable({
  rows,
  total,
  page,
  pageSize,
  loading,
  onChange,
}: BossProjectTableProps) {
  const columns: ColumnsType<BoardProjectRow> = [
    {
      title: "项目",
      dataIndex: "display_name",
      width: 260,
      render: (_: unknown, row) => {
        const isBucket = row.project_id === UNASSIGNED_BUCKET;
        const displayNames = Array.from(new Set([row.display_name, ...(row.peer_names ?? [])]));
        return (
          <Space direction="vertical" size={0}>
            {displayNames.map((name) => (
              <Link
                key={name}
                to={`/maintenance/boss/projects/${encodeURIComponent(row.project_id)}`}
              >
                {name}
              </Link>
            ))}
            <Space size={4}>
              {isBucket ? (
                <Tag color="purple">待人工确认</Tag>
              ) : (
                <Tag color={LIFECYCLE_TEXT[row.lifecycle]?.color}>
                  {LIFECYCLE_TEXT[row.lifecycle]?.text ?? row.lifecycle}
                </Tag>
              )}
              {row.is_archived ? <Tag color="default">已归档</Tag> : null}
            </Space>
          </Space>
        );
      },
    },
    {
      title: "本期需求单",
      dataIndex: "orders_ytd",
      width: 120,
      render: (_: unknown, row) => <StatCell stat={row.orders_ytd} />,
    },
    {
      title: "本期明细行",
      dataIndex: "lines_ytd",
      width: 120,
      render: (_: unknown, row) => <StatCell stat={row.lines_ytd} />,
    },
    {
      title: "已知申请估算成本(含税)",
      dataIndex: "known_apply_cost_inc_tax",
      width: 200,
      render: (_: unknown, row) => (
        <KnownCostCell stat={row.known_apply_cost_inc_tax} compact />
      ),
    },
    {
      title: "实发（发货单）",
      dataIndex: "shipped_qty",
      width: 140,
      render: (_: unknown, row) => <StatCell stat={row.shipped_qty} />,
    },
    {
      title: "未用件收回（返库单）",
      dataIndex: "returned_good_qty",
      width: 160,
      render: (_: unknown, row) => <StatCell stat={row.returned_good_qty} />,
    },
    {
      title: "坏件回收（入库单）",
      dataIndex: "returned_bad_qty",
      width: 150,
      render: (_: unknown, row) => <StatCell stat={row.returned_bad_qty} />,
    },
  ];
  // 未归属桶是后端额外置顶的一行，不计入 total，且只在「第 1 页 + 全范围 +
  // 无搜索 + lifecycle=all」时才注入——文案不能无条件承诺它在。
  const hasBucket = rows.some((row) => row.project_id === UNASSIGNED_BUCKET);
  return (
    <>
      <ResizableTable<BoardProjectRow>
        storageKey="boss-projects"
        rowKey="project_id"
        size="small"
        loading={loading}
        dataSource={rows}
        columns={columns}
        // dataSource 已是服务端切好的当页数据，分页器另置于表下：交给 antd 内置
        // 分页的话，为了躲开客户端切片得把 pageSize 谎报成 rows.length，而
        // rc-pagination 会把这个假值原样回吐给 onChange，页码一翻就带着
        // pageSize=21 去请求下一页，offset 算成 21，第 21 个真实项目哪一页都不出现。
        pagination={false}
      />
      <Pagination
        style={{ marginTop: 12, textAlign: "right" }}
        align="end"
        current={page}
        pageSize={pageSize}
        total={total}
        showSizeChanger
        pageSizeOptions={["20", "50", "100", "200"]}
        showTotal={() =>
          hasBucket
            ? `共 ${total} 个项目（未归属单另置顶一行）`
            : `共 ${total} 个项目`
        }
        onChange={(nextPage, nextSize) => onChange(nextPage, nextSize ?? pageSize)}
      />
    </>
  );
}

export default BossProjectTable;
