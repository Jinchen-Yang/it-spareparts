import { Alert, Descriptions, Modal, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useRef, useState } from "react";
import {
  fetchPoolAnalysisOrderDetail,
  type PoolAnalysisOrderDetail,
  type PoolAnalysisRange,
  type PoolAnalysisSide,
} from "../../api/poolAnalysis";
import { taxSidesForBasis, useTaxBasis } from "../../context/TaxBasis";
import { completeTaxPair, moneyExact, qty, splitFixed } from "../../utils/format";
import PoolIdentityLink from "./PoolIdentityLink";

type Item = PoolAnalysisOrderDetail["items"][number];
const muted = { color: "var(--mb-text-3)" };

export type PoolOrderDetailLoader = (
  side: PoolAnalysisSide,
  orderId: number,
) => Promise<PoolAnalysisOrderDetail>;

export default function PoolOrderDetailModal({ side, orderId, range = "90d", dateFrom, dateTo,
  forcePriceRestricted = false, loadDetail = fetchPoolAnalysisOrderDetail, onClose }: {
  side: PoolAnalysisSide;
  orderId: number | null;
  range?: PoolAnalysisRange;
  dateFrom?: string;
  dateTo?: string;
  forcePriceRestricted?: boolean;
  loadDetail?: PoolOrderDetailLoader;
  onClose: () => void;
}) {
  const basis = useTaxBasis(side);
  const [detail, setDetail] = useState<PoolAnalysisOrderDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const seq = useRef(0);

  useEffect(() => {
    if (orderId == null) { setDetail(null); return; }
    const request = ++seq.current;
    setDetail(null);
    setFailed(false);
    setLoading(true);
    loadDetail(side, orderId)
      .then((data) => { if (request === seq.current) setDetail(data); })
      .catch(() => { if (request === seq.current) setFailed(true); })
      .finally(() => { if (request === seq.current) setLoading(false); });
    return () => { seq.current += 1; };
  }, [side, orderId, loadDetail]);

  const price = (value: number | null | undefined, taxSide: "inc" | "ex") =>
    forcePriceRestricted || detail?.price_restricted
      ? <span style={muted}>无池价格权限</span>
      : moneyExact(splitFixed(value, "ex")[taxSide]);
  const columns: ColumnsType<Item> = [
    { title: "PN", key: "pn", width: 190, render: (_, row) => (
      <span>
        <span style={{ fontFamily: "monospace" }}>{row.pn_std || `#${row.part_id}`}</span>
        <PoolIdentityLink groupId={row.pool_group_id} name={row.pool_name} pn={row.pn_std}
          side={side} range={range} dateFrom={dateFrom} dateTo={dateTo} />
      </span>
    ) },
    { title: "描述", dataIndex: "description", width: 180, ellipsis: true },
    { title: "数量", dataIndex: "quantity", width: 72, align: "right", render: qty },
    ...taxSidesForBasis(basis).map((taxSide) => ({
      title: `单价(${taxSide === "inc" ? "含税" : "不含税"})`,
      key: `price_${taxSide}`,
      width: 112,
      align: "right" as const,
      render: (_: unknown, row: Item) => price(
        side === "purchase" ? row.purchase_unit_price_ex_tax : row.sale_unit_price_ex_tax,
        taxSide,
      ),
    })),
    ...taxSidesForBasis(basis).map((taxSide) => ({
      title: `金额(${taxSide === "inc" ? "含税" : "不含税"})`,
      key: `amount_${taxSide}`,
      width: 120,
      align: "right" as const,
      render: (_: unknown, row: Item) => price(
        side === "purchase" ? row.purchase_line_value_ex_tax : row.sale_line_value_ex_tax,
        taxSide,
      ),
    })),
    { title: "数据提示", dataIndex: "anomaly_flags", width: 130,
      render: (flags: string[]) => flags?.length ? flags.map((flag) => <Tag key={flag} color="orange">{flag}</Tag>) : "—" },
  ];

  const order = detail?.order;
  const counterparty = side === "purchase"
    ? detail?.supplier_restricted ? "无供应商权限" : order?.supplier || "—"
    : detail?.customer_restricted ? "无客户权限" : order?.customer || "—";
  const employee = side === "purchase" ? order?.purchaser : order?.salesperson;
  const orderAmount = side === "purchase"
    ? completeTaxPair(
      order?.purchase_order_amount_inc_tax,
      order?.purchase_order_amount_ex_tax,
    )
    : splitFixed(order?.sale_order_amount_ex_tax, "ex");

  return (
    <Modal
      open={orderId != null}
      onCancel={onClose}
      footer={null}
      width="min(920px, calc(100vw - 16px))"
      title={`${side === "purchase" ? "采购" : "销售"}订单 ${order?.order_no || ""}`}
      destroyOnHidden
    >
      {failed ? <Alert type="error" showIcon message="订单详情加载失败，请稍后重试" /> : (
        <>
          <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3 }} style={{ marginBottom: 14 }}>
            <Descriptions.Item label="日期">{order?.order_date || "—"}</Descriptions.Item>
            <Descriptions.Item label={side === "purchase" ? "采购员" : "销售员"}>{employee || "—"}</Descriptions.Item>
            <Descriptions.Item label={side === "purchase" ? "供应商" : "客户"}>{counterparty}</Descriptions.Item>
            <Descriptions.Item label="业务类型">{side === "purchase" ? order?.source_type || "—" : order?.business_type || "—"}</Descriptions.Item>
            <Descriptions.Item label="状态">{order?.data_status || "—"}</Descriptions.Item>
            {taxSidesForBasis(basis).map((taxSide) => (
              <Descriptions.Item
                key={`order-amount-${taxSide}`}
                label={`订单金额(${taxSide === "inc" ? "含税" : "不含税"})`}
              >
                {forcePriceRestricted || detail?.price_restricted
                  ? <span style={muted}>无池价格权限</span>
                  : moneyExact(orderAmount[taxSide])}
              </Descriptions.Item>
            ))}
          </Descriptions>
          <Table<Item>
            size="small"
            rowKey="line_id"
            loading={loading}
            columns={columns}
            dataSource={detail?.items ?? []}
            pagination={false}
            scroll={{ x: basis === "both" ? 980 : 780 }}
            locale={{ emptyText: loading ? "加载中" : "订单没有可显示的明细" }}
          />
        </>
      )}
    </Modal>
  );
}
