import { useCallback, useEffect, useRef, useState } from "react";
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
import ProjectProcurementPanel from "../../../components/maintenance/ProjectProcurementPanel";
import WorkbookRoundTrip from "../../../components/maintenance/WorkbookRoundTrip";
import {
  COST_CATEGORY_LEGEND,
  CostSourceTag,
  type RegisterPanelRefresh,
  raw,
  readError,
  statText,
} from "./panelUtils";

const { Text } = Typography;

/** PN 明细服务端分页页长（后端上限 200）。 */
const LINES_PAGE_SIZE = 20;

/**
 * 备件与需求单 tab（2026-08-19 重设计）：原顶部「出库明细」卡与原「备件成本」tab
 * 同源重叠，合并为一屏——上半需求单列表（合同筛选 + 点击单号钻取），下半选中单的
 * 行级明细（流转状态列原样展示 + 成本列）。03 sheet 的下载/上传留在本 tab（两阶段回传）。
 *
 * #259：三段（需求单 / PN 明细 / 采购单）各自加载、各自出错，互不清空；合同筛选与
 * 需求单钻取都交给服务端（合同号按挂靠 XSDD 归一化相等，不再拿 WBDD 单号做包含匹配）；
 * PN 明细与采购单走服务端分页。
 */
export function PartsOrdersTab({
  projectId,
  exportBase,
  canUpload,
  contractNos,
  onChanged,
  registerRefresh,
}: {
  projectId: string;
  exportBase: string;
  canUpload: boolean;
  onChanged: () => Promise<boolean>;
  registerRefresh: RegisterPanelRefresh;
  /** 项目全部 XSDD 合同号（聚合行供给）；多于一个时给出合同筛选（#39）。 */
  contractNos: string[];
}) {
  const [orders, setOrders] = useState<BoardOrderRow[]>([]);
  const [ordersLoading, setOrdersLoading] = useState(false);
  const [contractFilter, setContractFilter] = useState<string | undefined>();
  /** 点选的需求单号＝行过滤（再点一次取消）；默认展示项目全部备件行。 */
  const [selectedOrderNo, setSelectedOrderNo] = useState<string | null>(null);
  const [lines, setLines] = useState<ProjectPartsRow[]>([]);
  const [linesTotal, setLinesTotal] = useState(0);
  const [linesPage, setLinesPage] = useState(1);
  const [linesLoading, setLinesLoading] = useState(false);
  const ordersSeq = useRef(0);
  const linesSeq = useRef(0);

  const loadOrders = useCallback(async () => {
    const seq = ++ordersSeq.current;
    setOrdersLoading(true);
    try {
      const pageSize = 200;
      const all: BoardOrderRow[] = [];
      let page = 1;
      let total = 0;
      do {
        const response = await getBoardProjectOrders(projectId, {
          page,
          page_size: pageSize,
          contract_no: contractFilter,
        });
        if (seq !== ordersSeq.current) return false;
        total = response.data.total;
        all.push(...response.data.rows);
        if (!response.data.rows.length) break;
        page += 1;
      } while (all.length < total);
      setOrders(all);
      return true;
    } catch (err) {
      if (seq === ordersSeq.current) {
        setOrders([]);
        message.error(readError(err, "需求单加载失败"));
      }
      return false;
    } finally {
      if (seq === ordersSeq.current) setOrdersLoading(false);
    }
  }, [projectId, contractFilter]);

  const loadLines = useCallback(async () => {
    const seq = ++linesSeq.current;
    setLinesLoading(true);
    try {
      const resp = await listProjectPartsRows(projectId, {
        page: linesPage,
        page_size: LINES_PAGE_SIZE,
        order_no: selectedOrderNo ?? undefined,
        contract_no: contractFilter,
      });
      if (seq !== linesSeq.current) return false;
      setLines(resp.rows);
      setLinesTotal(resp.total);
      return true;
    } catch (err) {
      if (seq === linesSeq.current) {
        setLines([]);
        setLinesTotal(0);
        message.error(readError(err, "备件明细加载失败"));
      }
      return false;
    } finally {
      if (seq === linesSeq.current) setLinesLoading(false);
    }
  }, [projectId, contractFilter, selectedOrderNo, linesPage]);

  useEffect(() => { void loadOrders(); }, [loadOrders]);
  useEffect(() => { void loadLines(); }, [loadLines]);

  // 落库后的读回屏障：两段各自读回，任一没完成都算没完成（采购段与 03 表无关，不进屏障）。
  const refreshAll = useCallback(async () => {
    const [ordersOk, linesOk] = await Promise.all([loadOrders(), loadLines()]);
    return ordersOk && linesOk;
  }, [loadOrders, loadLines]);

  useEffect(() => {
    registerRefresh("parts-orders", refreshAll);
    return () => { registerRefresh("parts-orders", null); };
  }, [refreshAll, registerRefresh]);

  useEffect(() => () => {
    ordersSeq.current += 1;
    linesSeq.current += 1;
  }, []);

  /** 合同一换，原选中的需求单多半已不在列表里：清选中、回第一页，三段一起换范围。 */
  const onContractChange = (value: string | undefined) => {
    setContractFilter(value);
    setSelectedOrderNo(null);
    setLinesPage(1);
  };

  const toggleOrder = (orderNo: string) => {
    setSelectedOrderNo((prev) => (prev === orderNo ? null : orderNo));
    setLinesPage(1);
  };

  const selectedOrder = selectedOrderNo
    ? orders.find((order) => order.order_no === selectedOrderNo) ?? null
    : null;

  const orderColumns: ColumnsType<BoardOrderRow> = [
    {
      title: "需求单号",
      dataIndex: "order_no",
      render: (value: string) => (
        <a onClick={() => toggleOrder(value)}>
          {value}
        </a>
      ),
    },
    { title: "销售订单", dataIndex: "linked_sales_order_no", render: raw },
    { title: "制单日期", dataIndex: "order_date", render: raw },
    { title: "数据状态", dataIndex: "data_status", render: raw },
    { title: "明细行", dataIndex: "line_count" },
    {
      title: "已知申请估算成本(含税)",
      render: (_: unknown, order) =>
        ["ready", "partial", "stale"].includes(order.known_apply_cost_inc_tax.state)
          ? (order.known_apply_cost_inc_tax.value?.quality === "incomplete"
              && Number(order.known_apply_cost_inc_tax.value.coverage_pct ?? 0) === 0
            ? (order.known_apply_cost_inc_tax.value.known_amount == null
                ? "暂无可计算成本（无有效需求明细）"
                : `暂无可计算成本（缺价 ${order.known_apply_cost_inc_tax.value.missing_lines} 行）`)
            : `${String(order.known_apply_cost_inc_tax.value?.known_amount ?? "—")}${order.known_apply_cost_inc_tax.value?.quality === "incomplete" ? "（已知下限）" : ""}`)
          : statText(order.known_apply_cost_inc_tax),
    },
    {
      title: "实发（项目口径）",
      render: (_: unknown, order) => statText(order.facts.shipped_qty),
    },
  ];

  // PN 为主的行级明细（2026-08-20 用户拍板）：PN+描述合并主列、单价两档、
  // 成本来源四分类彩标（绿=系统关联 / 橙=估算 / 紫=人工回填 / 红=缺失）。
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
    { title: "已返数量", dataIndex: "returned_qty", width: 90, render: raw },
    { title: "待返数量", dataIndex: "pending_return_qty", width: 90, render: raw },
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
        onApply={(file, opts) => applyProjectMaster(projectId, file, opts)}
        onAfterApply={onChanged}
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
            onChange={onContractChange}
            options={contractNos.map((no) => ({ label: no, value: no }))}
          />
        ) : null}
      >
        <Table<BoardOrderRow>
          rowKey="source_order_id"
          size="small"
          loading={ordersLoading}
          dataSource={orders}
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
        <Text type="secondary" style={{ display: "block", fontSize: 11.5 }}>
          已返数量、待返数量是 WBDD 源表流转状态，仅作展示；净量和成本仍只按需求数量减退货数量。
        </Text>
        <Table<ProjectPartsRow>
          rowKey="line_id"
          size="small"
          loading={linesLoading}
          dataSource={lines}
          columns={lineColumns}
          scroll={{ x: 1380 }}
          pagination={{
            current: linesPage,
            pageSize: LINES_PAGE_SIZE,
            total: linesTotal,
            showSizeChanger: false,
            onChange: (page) => setLinesPage(page),
          }}
        />
      </Card>
      <ProjectProcurementPanel
        projectId={projectId}
        sourceOrderId={selectedOrder?.source_order_id ?? null}
        sourceOrderNo={selectedOrder?.order_no ?? null}
      />
    </Space>
  );
}

export default PartsOrdersTab;
