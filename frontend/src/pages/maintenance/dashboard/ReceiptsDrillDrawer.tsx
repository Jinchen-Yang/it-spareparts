/** M-AC09 返件明细下钻 Drawer（R33 已有 GET /receipts 的轻量页面入口）。
 *
 * 从总览/项目/PN 汇总点击进入：参数由调用方按「原筛选 ∩ 点击维度」求交后
 * 传入（dashboardShared.drillBy*，DL-022）；本组件不写 URL、不改页面筛选，
 * 关闭即回到原视图（保留返回状态）。金额列随 amountsVisible（服务端事实），
 * 数量列人人可见；导出走完整筛选集（CSV/XLSX，含本下钻维度）。
 * 只读：本组件不产生任何写操作。请求落地前做序号 + 会话双重守卫（DL-010）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Drawer, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType, TableProps } from "antd/es/table";
import { TaxMoney } from "../../../context/TaxBasis";
import {
  fetchDashboardReceipts,
  type DashboardFilters,
  type ReceiptLineRow,
} from "../../../api/maintenanceDashboard";
import {
  DashboardExportButtons,
  apiErrorMessage,
  qtyText,
  sessionToken,
  sessionUnchanged,
} from "./dashboardShared";

const { Text } = Typography;

const PAGE_SIZE = 20;

/** 件况 → 标签色（只描述事实，不发布返还结论）。 */
const TEST_RESULT_COLORS: Record<string, string> = {
  坏品: "error",
  废品: "warning",
  好品: "success",
};

export interface ReceiptsDrill {
  title: string;
  params: DashboardFilters;
}

export default function ReceiptsDrillDrawer({ drill, onClose, amountsVisible, maxExportRows }: {
  drill: ReceiptsDrill | null;
  onClose: () => void;
  amountsVisible: boolean;
  maxExportRows: number;
}) {
  const [page, setPage] = useState(1);
  const [rows, setRows] = useState<ReceiptLineRow[] | null>(null);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seqRef = useRef(0);

  // 每次打开回到第一页（关闭后状态随 drill=null 清空）。
  useEffect(() => {
    setPage(1);
    setRows(null);
    setError(null);
  }, [drill]);

  const load = useCallback(async () => {
    if (!drill) return;
    const seq = seqRef.current + 1;
    seqRef.current = seq;
    const token = sessionToken();
    setLoading(true);
    setError(null);
    try {
      const resp = await fetchDashboardReceipts({
        ...drill.params, sort: "occurred_desc", page, page_size: PAGE_SIZE,
      });
      if (seqRef.current !== seq || !sessionUnchanged(token)) return;
      setRows(resp.rows ?? []);
      setTotal(resp.pagination.total);
    } catch (err) {
      if (seqRef.current !== seq || !sessionUnchanged(token)) return;
      setRows(null);
      setTotal(0);
      setError(apiErrorMessage(err));
    } finally {
      if (seqRef.current === seq && sessionUnchanged(token)) setLoading(false);
    }
  }, [drill, page]);

  useEffect(() => {
    if (drill) void load();
    return () => { seqRef.current += 1; };
  }, [drill, load]);

  const onTableChange: TableProps<ReceiptLineRow>["onChange"] = (pg) => {
    if (pg.current) setPage(pg.current);
  };

  const columns: ColumnsType<ReceiptLineRow> = [
    { title: "业务时间", dataIndex: "occurred_at", width: 150,
      render: (v: string | null) => v || "—" },
    { title: "项目", dataIndex: "project_code", width: 120,
      render: (v: string | null) => v || "—" },
    { title: "来源需求单", dataIndex: "source_order_no", width: 130,
      render: (v: string | null) => v || "未关联" },
    {
      title: "PN / 描述", dataIndex: "pn", width: 200, ellipsis: true,
      render: (v: string, row) => (
        <Space direction="vertical" size={0}>
          <Text strong>{v}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {row.description || "—"}{row.unit ? ` · ${row.unit}` : ""}
          </Text>
        </Space>
      ),
    },
    { title: "类型", dataIndex: "receipt_kind", width: 90,
      render: (v: string) => v || "—" },
    { title: "件况", dataIndex: "test_result", width: 90,
      render: (v: string | null) => (v
        ? <Tag color={TEST_RESULT_COLORS[v] ?? "default"}>{v}</Tag>
        : <Tag>未填</Tag>) },
    { title: "数量", dataIndex: "qty", width: 90, align: "right",
      render: (v: string) => qtyText(v) },
    { title: "来源", dataIndex: "source", width: 100,
      render: (v: string) => v || "—" },
    { title: "需复核", dataIndex: "review_required", width: 80,
      render: (v: boolean) => (v ? <Tag color="warning">复核</Tag> : "—") },
  ];
  if (amountsVisible) {
    columns.push({
      title: "试算价值（估算/试算）", key: "valuation", width: 190, align: "right",
      render: (_: unknown, row) => {
        const v = row.valuation;
        if (!v) return "—";
        if (v.state === "ready" && v.value_inc_tax !== undefined) {
          return (
            <Space direction="vertical" size={0} align="end">
              <TaxMoney scope="maintenance" stack exact
                inc={Number(v.value_inc_tax)} ex={Number(v.value_ex_tax ?? v.value_inc_tax)} />
              <Text type="secondary" style={{ fontSize: 12 }}>
                {v.classification === "finished" ? "成品" : v.classification === "bad" ? "坏件" : "未知件况"}
                {v.factor !== undefined ? ` × ${v.factor}` : ""}
              </Text>
            </Space>
          );
        }
        if (v.state === "awaiting_ratio") return <Tag>待试算</Tag>;
        return <Tag>待估{v.reason ? `（${v.reason}）` : ""}</Tag>;
      },
    });
  }

  return (
    <Drawer
      title={drill ? `返件明细：${drill.title}` : "返件明细"}
      width={Math.min(900, typeof window === "undefined" ? 900 : window.innerWidth - 32)}
      open={drill !== null}
      destroyOnClose
      onClose={onClose}
    >
      {error ? (
        <Alert
          type="error" showIcon message="返件明细加载失败" description={error}
          action={<Button size="small" onClick={() => void load()}>重试</Button>}
          style={{ marginBottom: 12 }}
        />
      ) : null}
      <Text type="secondary" style={{ fontSize: 12 }}>
        按当前筛选与点击维度求交的行级返件事实（只读）；导出为该交集的完整集合，不受分页影响。
      </Text>
      <div style={{ marginTop: 12, marginBottom: 12 }}>
        <DashboardExportButtons
          params={drill?.params ?? {}}
          kind="receipts"
          maxExportRows={maxExportRows}
        />
      </div>
      <Table<ReceiptLineRow>
        rowKey="rkd_line_id"
        size="small"
        loading={loading}
        dataSource={rows ?? []}
        columns={columns}
        onChange={onTableChange}
        scroll={{ x: amountsVisible ? 1160 : 960 }}
        pagination={{
          current: page,
          pageSize: PAGE_SIZE,
          total,
          showTotal: (t) => `共 ${t} 行`,
        }}
        locale={{ emptyText: "该筛选交集内没有返件行" }}
      />
    </Drawer>
  );
}
