import { useCallback, useEffect, useState } from "react";
import { Card, Select, Space, Table, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { BoardOrderRow } from "../../../api/maintenanceBossBoard";
import { getBoardProjectOrders } from "../../../api/maintenanceBossBoard";
import { listProjectPartsRows } from "../../../api/maintenanceWorkbooks";
import type { ProjectPartsRow } from "../../../api/maintenanceWorkbooks";
import {
  SHEETS,
  applyProjectMaster,
  downloadProjectMaster,
  validateProjectMaster,
} from "../../../api/maintenanceWorkbooks";
import WorkbookRoundTrip from "../../../components/maintenance/WorkbookRoundTrip";
import {
  COST_CATEGORY_LEGEND,
  CostSourceTag,
  raw,
  readError,
  statText,
} from "./panelUtils";

const { Text } = Typography;

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
  /** 点选的需求单号＝行过滤（再点一次取消）；默认展示项目全部备件行。 */
  const [selectedOrderNo, setSelectedOrderNo] = useState<string | null>(null);
  const [lines, setLines] = useState<ProjectPartsRow[]>([]);
  const [loading, setLoading] = useState(false);

  const loadOrders = useCallback(async () => {
    setLoading(true);
    try {
      const [ordersResp, partsResp] = await Promise.all([
        getBoardProjectOrders(projectId, { page_size: 200 }),
        listProjectPartsRows(projectId),
      ]);
      setOrders(ordersResp.data.rows);
      setLines(partsResp.rows);
    } catch (err) {
      message.error(readError(err, "需求单/备件明细加载失败"));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadOrders();
  }, [loadOrders]);

  const shownOrders = contractFilter
    ? orders.filter((order) => order.order_no.includes(contractFilter)
        || (order.project_raw ?? "").includes(contractFilter))
    : orders;

  const orderColumns: ColumnsType<BoardOrderRow> = [
    {
      title: "需求单号",
      dataIndex: "order_no",
      render: (value: string, order) => (
        <a onClick={() => setSelectedOrderNo(selectedOrderNo === value ? null : value)}>
          {value}
        </a>
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

  // PN 为主的行级明细（2026-08-20 用户拍板）：PN+描述合并主列、单价两档、
  // 成本来源四分类彩标（绿=系统关联 / 橙=估算 / 紫=人工回填 / 红=缺失）。
  const shownLines = selectedOrderNo
    ? lines.filter((line) => line.order_no === selectedOrderNo)
    : lines;

  const lineColumns: ColumnsType<ProjectPartsRow> = [
    {
      title: "PN / 描述",
      dataIndex: "pn_std",
      width: 320,
      render: (v: string | null, r: ProjectPartsRow) => (
        <Space direction="vertical" size={0}>
          <Text strong copyable={Boolean(v)}>{raw(v)}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>{raw(r.description)}</Text>
        </Space>
      ),
    },
    { title: "维保单号", dataIndex: "order_no", width: 170, render: raw },
    { title: "需求数量", dataIndex: "qty", width: 90, render: raw },
    { title: "退货数量", dataIndex: "return_qty", width: 90, render: raw },
    {
      title: "未税单价",
      dataIndex: "unit_cost_ex_tax",
      width: 110,
      render: raw,
    },
    {
      title: "含税单价",
      dataIndex: "unit_cost_inc_tax",
      width: 110,
      render: raw,
    },
    {
      title: "已知成本(含税)",
      dataIndex: "cost_amount_inc_tax",
      width: 120,
      render: raw,
    },
    {
      title: "成本来源",
      width: 150,
      render: (_: unknown, line) => <CostSourceTag row={line} />,
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
        <Space size={12} wrap>
          {COST_CATEGORY_LEGEND.map((item) => (
            <Tag key={item.text} color={item.color}>{item.text}</Tag>
          ))}
          <Text type="secondary" style={{ fontSize: 11.5 }}>
            成本来源：绿=系统关联（采购单挂接）｜橙=估算（窗口/历史/池/月均/销售参考）｜紫=人工回填｜红=缺失
            {selectedOrderNo ? `｜当前过滤：${selectedOrderNo}（再点单号取消）` : ""}
          </Text>
        </Space>
        <Table<ProjectPartsRow>
          rowKey="line_id"
          size="small"
          loading={loading}
          dataSource={shownLines}
          columns={lineColumns}
          scroll={{ x: 1200 }}
          pagination={{ pageSize: 20, showSizeChanger: false }}
        />
      </Card>
    </Space>
  );
}

export default PartsOrdersTab;
