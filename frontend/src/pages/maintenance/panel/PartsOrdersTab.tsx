import { useCallback, useEffect, useState } from "react";
import { Card, Select, Space, Table, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { BoardLineRow, BoardOrderRow } from "../../../api/maintenanceBossBoard";
import {
  getBoardOrderLines,
  getBoardProjectOrders,
} from "../../../api/maintenanceBossBoard";
import {
  SHEETS,
  applyProjectMaster,
  downloadProjectMaster,
  validateProjectMaster,
} from "../../../api/maintenanceWorkbooks";
import WorkbookRoundTrip from "../../../components/maintenance/WorkbookRoundTrip";
import { CostSourceTag, raw, readError, statText } from "./panelUtils";
import type { CostSourceLike } from "./panelUtils";

const { Text } = Typography;

/** board 行级明细的 cost_source/confidence 是 Stat 信封，取出 ready 值喂给 CostSourceTag。 */
function lineCostSource(line: BoardLineRow): CostSourceLike {
  return {
    cost_source: line.cost_source?.state === "ready" ? line.cost_source.value : null,
    confidence: (line.confidence?.state === "ready"
      ? line.confidence.value
      : null) as CostSourceLike["confidence"],
  };
}

/**
 * 备件与需求单 tab（2026-08-19 重设计）：原顶部「出库明细」卡与原「备件成本」tab
 * 同源重叠，合并为一屏——上半需求单列表（合同筛选 + 点击单号钻取），下半选中单的
 * 行级明细（流转状态列原样展示 + 成本列）。03 sheet 的下载/上传留在本 tab（两阶段回传）。
 */
export function PartsOrdersTab({
  projectId,
  exportBase,
  canUpload,
  contractNos,
}: {
  projectId: string;
  exportBase: string;
  canUpload: boolean;
  /** 项目全部 XSDD 合同号（聚合行供给）；多于一个时给出合同筛选（#39）。 */
  contractNos: string[];
}) {
  const [orders, setOrders] = useState<BoardOrderRow[]>([]);
  const [contractFilter, setContractFilter] = useState<string | undefined>();
  const [selectedOrder, setSelectedOrder] = useState<string | null>(null);
  const [lines, setLines] = useState<BoardLineRow[]>([]);
  const [loading, setLoading] = useState(false);

  const loadOrders = useCallback(async () => {
    setLoading(true);
    try {
      const ordersResp = await getBoardProjectOrders(projectId, { page_size: 200 });
      setOrders(ordersResp.data.rows);
    } catch (err) {
      message.error(readError(err, "需求单加载失败"));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadOrders();
  }, [loadOrders]);

  useEffect(() => {
    if (!selectedOrder) {
      setLines([]);
      return;
    }
    void getBoardOrderLines(selectedOrder, { page_size: 200 })
      .then((resp) => setLines(resp.data.rows))
      .catch((err) => message.error(readError(err, "明细加载失败")));
  }, [selectedOrder]);

  const shownOrders = contractFilter
    ? orders.filter((order) => order.order_no.includes(contractFilter)
        || (order.project_raw ?? "").includes(contractFilter))
    : orders;

  const orderColumns: ColumnsType<BoardOrderRow> = [
    {
      title: "需求单号",
      dataIndex: "order_no",
      render: (value: string, order) => (
        <a onClick={() => setSelectedOrder(order.source_order_id)}>{value}</a>
      ),
    },
    { title: "制单日期", dataIndex: "order_date", render: raw },
    { title: "数据状态", dataIndex: "data_status", render: raw },
    { title: "明细行", dataIndex: "line_count" },
    {
      title: "已知申请估算成本(含税)",
      render: (_: unknown, order) =>
        order.known_apply_cost_inc_tax.state === "ready"
          ? String(order.known_apply_cost_inc_tax.value?.known_amount ?? "—")
          : statText(order.known_apply_cost_inc_tax),
    },
    {
      title: "实发（项目口径）",
      render: (_: unknown, order) => statText(order.facts.shipped_qty),
    },
  ];

  const lineColumns: ColumnsType<BoardLineRow> = [
    { title: "PN", dataIndex: "pn_std", render: (v, r) => raw(v || r.pn_raw) },
    { title: "描述", dataIndex: "description", render: raw },
    { title: "需求", dataIndex: "qty", render: raw },
    // 以下为流转状态列：原样展示，不参与任何计算（铁律 3）
    { title: "已采", dataIndex: "purchased_qty", render: raw },
    { title: "待供", dataIndex: "pending_supply_qty", render: raw },
    { title: "待返", dataIndex: "pending_return_qty", render: raw },
    { title: "领用", dataIndex: "consumed_qty", render: raw },
    {
      title: "已知申请估算成本(含税)",
      render: (_: unknown, line) => statText(line.known_apply_cost_inc_tax),
    },
    {
      title: "成本来源",
      render: (_: unknown, line) => <CostSourceTag row={lineCostSource(line)} />,
    },
  ];

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <WorkbookRoundTrip
        size="small"
        title="备件成本"
        filename={`${exportBase}-${SHEETS.parts}.xlsx`}
        canUpload={canUpload}
        hint="成本只读展示；缺成本请使用下载→修改黄色覆盖列→上传"
        onDownload={() => downloadProjectMaster(projectId, [SHEETS.parts])}
        onValidate={(file) => validateProjectMaster(projectId, file)}
        onApply={async (file) => {
          const result = await applyProjectMaster(projectId, file);
          await loadOrders();      // 上传覆盖后立刻回读，页面不留旧值
          return result;
        }}
      />
      <Card
        size="small"
        title="需求单"
        extra={contractNos.length > 1 ? (
          <Select
            allowClear
            size="small"
            style={{ width: 220 }}
            placeholder="全部合同"
            value={contractFilter}
            onChange={setContractFilter}
            options={contractNos.map((no) => ({ label: no, value: no }))}
          />
        ) : null}
      >
        <Table<BoardOrderRow>
          rowKey="source_order_id"
          size="small"
          loading={loading}
          dataSource={shownOrders}
          columns={orderColumns}
          pagination={{ pageSize: 10, showSizeChanger: false }}
        />
        {selectedOrder ? (
          <>
            <Text type="secondary" style={{ fontSize: 11.5 }}>
              流转状态列（已采/待供/待返/领用）为氚云原样数据，系统只展示、不参与任何计算。
            </Text>
            <Table<BoardLineRow>
              rowKey="raw_line_id"
              size="small"
              dataSource={lines}
              columns={lineColumns}
              scroll={{ x: 1100 }}
              pagination={{ pageSize: 10, showSizeChanger: false }}
            />
          </>
        ) : null}
      </Card>
    </Space>
  );
}

export default PartsOrdersTab;
