/**
 * 订单内容弹窗（池分析详情页点单号打开）：
 * 复用订单列表端点的 order_no 全等召回，取回该单完整 parts 明细渲染。
 */
import { Alert, Descriptions, Modal, Spin, Tag } from "antd";
import {
  dashboardPurchaseOrders, dashboardSales,
  type OrdersResp, type PurchaseOrderRow, type SalesOrderRow,
} from "../../api";
import { EMPTY, moneyExact, qty } from "../../utils/format";
import PartsTable, { type OrderSide } from "./PartsTable";
import { MUTED, useGuardedFetch, fmtMoneyR, type DateRange } from "./shared";

interface OrderDetailModalProps {
  side: OrderSide;
  orderNo: string | null;
  onClose: () => void;
  localCostRestricted: boolean;
  dateRange?: DateRange;
}

export default function OrderDetailModal({
  side, orderNo, onClose, localCostRestricted, dateRange,
}: OrderDetailModalProps) {
  const isPurchase = side === "purchase";
  const { data, loading, error } = useGuardedFetch<OrdersResp<SalesOrderRow | PurchaseOrderRow>>(
    () => {
      if (!orderNo) return Promise.resolve({ data: null as never });
      const params = { order_no: orderNo, status: "全部", page: 1, page_size: 20 };
      return isPurchase
        ? dashboardPurchaseOrders(params) as Promise<{ data: OrdersResp<SalesOrderRow | PurchaseOrderRow> }>
        : dashboardSales(params) as Promise<{ data: OrdersResp<SalesOrderRow | PurchaseOrderRow> }>;
    },
    [orderNo, isPurchase]);

  const row = orderNo ? data?.items?.find((i) => i.order_no === orderNo) ?? null : null;
  const contractVersion = Number(String(data?.contract_version ?? "").replace(/^v/i, ""));
  const legacyContract = !!data && !(Number.isFinite(contractVersion) && contractVersion >= 2);
  const costRestricted = localCostRestricted || (data?.cost_restricted ?? false);
  const partsRestricted = data?.parts_restricted ?? false;
  const manualRestricted = data?.manual_reference_restricted ?? false;

  return (
    <Modal open={orderNo != null} onCancel={onClose} footer={null} width={1100}
      title={`${isPurchase ? "采购" : "销售"}订单 ${orderNo ?? ""}`}>
      {loading ? (
        <div style={{ textAlign: "center", padding: 32 }}><Spin /></div>
      ) : error ? (
        <Alert type="error" showIcon message={`订单加载失败：${error}`} />
      ) : legacyContract ? (
        <Alert type="warning" showIcon
          message="服务升级中，精确订单详情暂不可用；为避免打开错误订单，本弹窗已暂停显示。" />
      ) : !row ? (
        <Alert type="warning" showIcon message="未找到该订单（可能超出可见范围）。" />
      ) : (
        <>
          <Descriptions size="small" column={{ xs: 1, sm: 2, md: 4 }} style={{ marginBottom: 12 }}>
            <Descriptions.Item label="日期">
              {row.order_date || EMPTY}{row.is_future && <Tag color="red" style={{ marginLeft: 4 }}>未来</Tag>}
            </Descriptions.Item>
            <Descriptions.Item label="状态">{row.data_status || EMPTY}</Descriptions.Item>
            <Descriptions.Item label="型号数">{row.pn_count ?? row.part_count}</Descriptions.Item>
            <Descriptions.Item label="数量">{qty(row.total_quantity)}</Descriptions.Item>
            {isPurchase ? (
              <>
                <Descriptions.Item label="采购员">{(row as PurchaseOrderRow).purchaser || EMPTY}</Descriptions.Item>
                <Descriptions.Item label="类型">{(row as PurchaseOrderRow).source_type || EMPTY}</Descriptions.Item>
                <Descriptions.Item label="金额(未税)">
                  {fmtMoneyR((row as PurchaseOrderRow).total_amount, costRestricted, "无成本权限")}
                </Descriptions.Item>
                <Descriptions.Item label="关联销售单">
                  {(row as PurchaseOrderRow).linked_sales_order || EMPTY}
                </Descriptions.Item>
              </>
            ) : (
              <>
                <Descriptions.Item label="销售员">
                  {partsRestricted ? <span style={MUTED}>无权限</span>
                    : ((row as SalesOrderRow).salesperson || EMPTY)}
                </Descriptions.Item>
                <Descriptions.Item label="客户">
                  {partsRestricted ? <span style={MUTED}>无权限</span>
                    : ((row as SalesOrderRow).customer || EMPTY)}
                </Descriptions.Item>
                <Descriptions.Item label="营收(未税)">
                  {(row as SalesOrderRow).total_revenue == null
                    ? EMPTY : moneyExact((row as SalesOrderRow).total_revenue)}
                </Descriptions.Item>
                <Descriptions.Item label="采购拉通">
                  {(row as SalesOrderRow).linked_purchase ? <Tag color="blue">已生效</Tag> : EMPTY}
                </Descriptions.Item>
              </>
            )}
          </Descriptions>
          {partsRestricted ? (
            <Alert type="info" showIcon message="当前账号无逐单明细查看权限（仅聚合可见）。" />
          ) : !Array.isArray(row.parts) ? (
            <Alert type="warning" showIcon
              message="当前服务版本尚未返回 PN 明细；订单概要仍可查看，请待后端升级完成后重试。" />
          ) : (
            <PartsTable side={side} parts={row.parts} dateRange={dateRange}
              costRestricted={costRestricted} manualRestricted={manualRestricted} />
          )}
        </>
      )}
    </Modal>
  );
}
