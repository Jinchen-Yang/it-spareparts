/**
 * 订单行明细表（v2 嵌套 parts）：PN 深链、池归属、池均价、人工约束价、差额、价格参考状态。
 * 订单展开区与订单详情弹窗共用。权限三态：无权限 ≠ 暂无数据。
 */
import { Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Link, useNavigate } from "react-router-dom";
import type { PurchaseOrderPart, SalesOrderPart } from "../../api";
import { EMPTY, moneyExact, pctSigned, qty } from "../../utils/format";
import { MUTED, ReferenceStatusTag, fmtMoneyR } from "./shared";

export type OrderSide = "purchase" | "sales";
type AnyPart = SalesOrderPart | PurchaseOrderPart;

export function PartLink({ partId, pn }: { partId: number | null; pn: string | null }) {
  if (!pn) return <span style={MUTED}>{EMPTY}</span>;
  if (!partId) return <span style={{ fontFamily: "monospace", fontSize: 12.5 }}>{pn}</span>;
  return (
    <Link to={`/parts?part_id=${partId}`} style={{ fontFamily: "monospace", fontSize: 12.5 }}
      aria-label={`查看型号 ${pn} 全景`}>{pn}</Link>
  );
}

export function PoolLink({ groupId, name }: { groupId: number | null; name: string | null }) {
  const navigate = useNavigate();
  if (!groupId) return <span style={MUTED}>未入池</span>;
  return (
    <a onClick={() => navigate(`/pool-analysis/${groupId}`)} aria-label={`进入池「${name || groupId}」分析详情`}>
      {name || `池 #${groupId}`}
    </a>
  );
}

/** 差额：优先 vs 人工约束价，无约束时 vs 池均价（明确标注口径，不混单位） */
function DeltaCell({ p, restricted }: { p: AnyPart; restricted: boolean }) {
  if (p.manual_limit_delta != null) {
    const worse = p.manual_limit_delta > 0
      ? ("max_purchase_price" in p)      // 采购：高于上限为差
      : !("max_purchase_price" in p);    // 销售：低于下限为差
    return (
      <Tooltip title={`vs 人工约束价（${pctSigned(p.manual_limit_delta_pct)}）`}>
        <span style={{ color: worse && p.manual_limit_delta !== 0 ? "#c0524a" : undefined }}>
          {p.manual_limit_delta > 0 ? "+" : ""}{moneyExact(p.manual_limit_delta)}
          <span style={{ ...MUTED, marginLeft: 4 }}>vs约束</span>
        </span>
      </Tooltip>
    );
  }
  if (p.pool_avg_delta != null) {
    return (
      <Tooltip title={`vs 池均价（${pctSigned(p.pool_avg_delta_pct)}）`}>
        <span>
          {p.pool_avg_delta > 0 ? "+" : ""}{moneyExact(p.pool_avg_delta)}
          <span style={{ ...MUTED, marginLeft: 4 }}>vs池均</span>
        </span>
      </Tooltip>
    );
  }
  if (restricted) return <span style={MUTED}>无权限</span>;
  return <span style={MUTED}>{EMPTY}</span>;
}

export interface PartsTableProps {
  side: OrderSide;
  parts: AnyPart[];
  /** 采购金额权限（data_purchase_cost=False）——采购单价/金额显示「无成本权限」 */
  costRestricted: boolean;
  /** 治理权限（data_pool_price_governance=False）——约束价/差额显示「无权限」 */
  manualRestricted: boolean;
}

export default function PartsTable({ side, parts, costRestricted, manualRestricted }: PartsTableProps) {
  const isPurchase = side === "purchase";
  // 销售侧 unit_price_ex_tax/amount 与采购成本键同名，对 cost-blind 账号会被后端有意过遮
  // （契约既定取舍）：null 且账号受限 → 显示无权限而非「-」
  const priceRestricted = costRestricted;

  const columns: ColumnsType<AnyPart> = [
    { title: "PN", key: "pn", width: 170, render: (_, p) => (
      <span>
        <PartLink partId={p.part_id} pn={p.pn_std} />
        {p.brand && <Tag style={{ marginLeft: 6 }}>{p.brand}</Tag>}
      </span>) },
    { title: "描述", dataIndex: "description", width: 200, ellipsis: true,
      render: (v) => v || <span style={MUTED}>{EMPTY}</span> },
    { title: "数量", dataIndex: "quantity", width: 72, align: "right", render: qty },
    { title: "未税单价", dataIndex: "unit_price_ex_tax", width: 100, align: "right",
      render: (v) => fmtMoneyR(v, priceRestricted && v == null, "无成本权限") },
    { title: "金额(未税)", dataIndex: "amount", width: 110, align: "right",
      render: (v) => fmtMoneyR(v, priceRestricted && v == null, "无成本权限") },
    { title: "所属池", key: "pool", width: 130, ellipsis: true,
      render: (_, p) => <PoolLink groupId={p.pool_group_id} name={p.pool_name} /> },
    { title: "池均价", key: "pool_avg", width: 100, align: "right",
      render: (_, p) => {
        const v = isPurchase
          ? (p as PurchaseOrderPart).pool_avg_purchase_price
          : (p as SalesOrderPart).pool_avg_sale_price;
        if (p.pool_group_id == null) return <span style={MUTED}>{EMPTY}</span>;
        return fmtMoneyR(v, isPurchase && costRestricted && v == null, "无成本权限");
      } },
    { title: isPurchase ? "人工最高采购价" : "人工最低销售价", key: "limit", width: 128, align: "right",
      render: (_, p) => {
        if (p.pool_group_id == null) return <span style={MUTED}>{EMPTY}</span>;
        const v = isPurchase
          ? (p as PurchaseOrderPart).max_purchase_price
          : (p as SalesOrderPart).min_sale_price;
        if (manualRestricted) return <span style={MUTED}>无权限</span>;
        if (v == null) return <span style={MUTED}>未设置</span>;
        return moneyExact(v);
      } },
    { title: "差额", key: "delta", width: 140, align: "right",
      render: (_, p) => p.pool_group_id == null
        ? <span style={MUTED}>{EMPTY}</span>
        : <DeltaCell p={p} restricted={manualRestricted} /> },
    { title: "分析状态", key: "ref", width: 116, render: (_, p) => (
      <span>
        <ReferenceStatusTag status={p.reference_status} />
        {!p.in_stats_scope && (
          <Tooltip title="不在池统计口径内（非已生效/未来/不计营收或成本/无价格），不计入池指标与越线计数">
            <Tag style={{ marginLeft: 2 }}>不计统计</Tag>
          </Tooltip>
        )}
      </span>) },
  ];

  return (
    <Table<AnyPart>
      size="small"
      rowKey="line_id"
      columns={columns}
      dataSource={parts}
      pagination={false}
      scroll={{ x: 1150 }}
    />
  );
}
