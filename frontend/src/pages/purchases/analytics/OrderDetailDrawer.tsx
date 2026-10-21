// 订单明细抽屉：采购模块自己的明细接口（不依赖老板看板权限）。
// 分类筛选活跃时把 category 随层传入：展示行按分类过滤并附「匹配分类行合计」。
// 行级口径 = 只计单头所属批次的明细行（D-39 补充决策）。
import { useEffect, useRef, useState } from "react";
import { Alert, Descriptions, Drawer, Spin, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "antd";
import { fmt, fmtMoney } from "../shared";
import {
  fetchPurchaseAnalyticsOrderDetail,
  type PurchaseAnalyticsOrderDetail,
} from "../../../api/purchaseAnalytics";
import { readSessionToken, SESSION_EXPIRED_MESSAGE, sessionStale } from "../../../utils/session";
import type { AmountBasis } from "./urlState";

export interface OrderDetailDrawerProps {
  orderId: number | null;
  category: string[];
  basis: AmountBasis[];
  /** 所属会话 owner（页级下传，DL-010 返工）：失效时请求前拒发；迟到响应落地 /
   * finally 前比对 localStorage token，换账号后不作废不作数。 */
  ownerToken: string | null;
  /** 页级过期通知（DL-010 落地清理）：检测到 owner 失效时通知页级清旧上下文。 */
  onOwnerStale: () => void;
  onClose: () => void;
}

function TaxTag({ detail }: { detail: PurchaseAnalyticsOrderDetail }) {
  const marked = detail.is_tax_inclusive == null ? "未标注" : detail.is_tax_inclusive ? "含税单" : "不含税单";
  // 未标/0 税率：按 13% 推算的那一侧是名义值（D-39 名义侧裁定）——含税单推算不含税侧、
  // 非含税单推算含税侧；标注过税率的单据两侧都按票面税率换算，不附加说明
  const nominal = detail.tax_rate == null || detail.tax_rate === 0;
  const rate = nominal
    ? "未标税率"
    : `税率 ${(detail.tax_rate! * 100).toFixed(1).replace(/\.0$/, "")}%`;
  const effective = `按 ${(detail.tax_rate_effective ?? 0.13) * 100}% 换算`;
  return (
    <span>
      <Tag color={detail.is_tax_inclusive == null ? "orange" : detail.is_tax_inclusive ? "default" : "blue"}>
        {marked}
      </Tag>
      <span style={{ color: "var(--mb-text-2)" }}>
        {rate}
        {nominal ? `（${effective}；未标或 0 税率按 13%）` : ""}
      </span>
    </span>
  );
}

/** 名义侧金额标注：只标在 nominal_side 指向的那一笔金额后面（D-39 名义侧裁定）。 */
function NominalAmountNote({ side, kind }: { side: string | null; kind: "inc" | "ex" }) {
  if (side !== kind) return null;
  const tip =
    kind === "inc"
      ? "未标税率（或税率为 0）：此含税金额按 13% 从不含税额推算，不是票面金额。"
      : "未标税率（或税率为 0）：此不含税金额按 13% 从含税额推算，不是票面金额。";
  return (
    <Tooltip title={tip}>
      <span
        aria-label={kind === "inc" ? "名义含税说明" : "名义不含税说明"}
        style={{ cursor: "help", borderBottom: "1px dotted", marginLeft: 4, fontSize: 12 }}
      >
        名义（按 13% 推算）
      </span>
    </Tooltip>
  );
}

export default function OrderDetailDrawer({ orderId, category, basis, ownerToken, onOwnerStale, onClose }: OrderDetailDrawerProps) {
  const [detail, setDetail] = useState<PurchaseAnalyticsOrderDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const seqRef = useRef(0);
  // onClose 存 ref：父页每次渲染会传新的内联函数，进 effect 依赖会造成重复请求
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const onOwnerStaleRef = useRef(onOwnerStale);
  onOwnerStaleRef.current = onOwnerStale;
  const categoryKey = category.join(",");

  useEffect(() => {
    if (orderId == null) {
      setDetail(null);
      return;
    }
    const seq = ++seqRef.current;
    // 所属会话失效（DL-010 返工）：拒发并清旧详情——旧会话的抽屉不以当前账号取数
    if (sessionStale(ownerToken)) {
      setLoading(false);
      setDetail(null);
      message.error(SESSION_EXPIRED_MESSAGE);
      onOwnerStaleRef.current?.();
      return;
    }
    const token = readSessionToken();
    setLoading(true);
    setDetail(null);
    fetchPurchaseAnalyticsOrderDetail(orderId, categoryKey ? categoryKey.split(",") : [])
      .then(({ data }) => {
        if (seq !== seqRef.current) return;
        if (sessionStale(token)) { onOwnerStaleRef.current?.(); return; }
        setDetail(data);
      })
      .catch(() => {
        if (seq !== seqRef.current) return;
        if (sessionStale(token)) { onOwnerStaleRef.current?.(); return; }
        message.error("订单明细加载失败");
        onCloseRef.current();
      })
      .finally(() => {
        // finally 也守卫：失效会话的收尾不触碰当前 UI 状态，并通知页级清旧上下文
        if (seq !== seqRef.current) return;
        if (sessionStale(token)) { onOwnerStaleRef.current?.(); return; }
        setLoading(false);
      });
  }, [orderId, categoryKey, ownerToken]);

  const lineColumns: ColumnsType<PurchaseAnalyticsOrderDetail["lines"][number]> = [
    { title: "#", dataIndex: "line_no", width: 46, render: (v: number | null) => fmt(v) },
    { title: "型号", dataIndex: "pn_std", width: 130, render: (v: string | null) => (
      <span style={{ fontFamily: "monospace", fontSize: 12.5 }}>{v || "—"}</span>
    ) },
    { title: "描述", dataIndex: "description", ellipsis: true, render: (v: string | null) => v || "—" },
    { title: "品牌", dataIndex: "brand", width: 90, render: (v: string | null) => v || "—" },
    { title: "分类", dataIndex: "category", width: 90, render: (v: string | null) => v || "—" },
    { title: "数量", dataIndex: "qty", width: 70, align: "right", render: (v: number | null) => fmt(v) },
    { title: "单价", dataIndex: "unit_price", width: 90, align: "right", render: (v: number | null) => fmtMoney(v) },
    { title: "行金额（实付）", dataIndex: "line_amount", width: 110, align: "right", render: (v: number | null) => fmtMoney(v) },
    ...(basis.includes("inc")
      ? [{ title: "行含税", key: "line_amount_inc", width: 100, align: "right" as const,
          render: (_: unknown, r: PurchaseAnalyticsOrderDetail["lines"][number]) => fmtMoney(r.line_amount_inc) }]
      : []),
    ...(basis.includes("ex")
      ? [{ title: "行不含税", key: "line_amount_ex", width: 100, align: "right" as const,
          render: (_: unknown, r: PurchaseAnalyticsOrderDetail["lines"][number]) => fmtMoney(r.line_amount_ex) }]
      : []),
  ];

  const matched = detail?.category_matched;

  return (
    <Drawer
      open={orderId != null}
      width={720}
      title={detail ? `采购单 ${detail.order_no}` : "采购单明细"}
      onClose={onClose}
      destroyOnClose
    >
      {loading && <div style={{ textAlign: "center", padding: 40 }}><Spin /></div>}
      {!loading && detail && (
        <div style={{ display: "grid", gap: 14 }}>
          <Descriptions
            size="small"
            column={2}
            items={[
              { key: "date", label: "采购日期", children: detail.order_date || "—" },
              { key: "type", label: "采购类型", children: detail.source_type || "—" },
              { key: "purchaser", label: "采购员", children: detail.purchaser || "—" },
              { key: "sales", label: "销售人员", children: detail.salesperson || "—" },
              { key: "customer", label: "客户", children: detail.customer || "—" },
              { key: "linked", label: "关联销售单号", children: detail.linked_sales_order_no || "—" },
              { key: "tax", label: "税率口径", children: <TaxTag detail={detail} /> },
              { key: "lines", label: "明细行数", children: `${detail.lines_count}（只计单头所属批次）` },
            ]}
          />
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
            {basis.includes("inc") && (
              <span>含税金额 <b>{fmtMoney(detail.amount_inc)}</b><NominalAmountNote side={detail.nominal_side} kind="inc" /></span>
            )}
            {basis.includes("ex") && (
              <span>不含税金额 <b>{fmtMoney(detail.amount_ex)}</b><NominalAmountNote side={detail.nominal_side} kind="ex" /></span>
            )}
            {basis.includes("paid") && (
              <span>实付金额 <b>{fmtMoney(detail.amount_paid)}</b></span>
            )}
          </div>
          {matched && (
            <Alert
              type="info"
              showIcon
              message={`匹配分类行合计（${category.join("、")}）：${matched.lines} 行 · 实付 ${fmtMoney(matched.amount_paid)} · 含税 ${fmtMoney(matched.amount_inc)} · 不含税 ${fmtMoney(matched.amount_ex)}`}
              description="分类口径下实付 = 匹配明细行行金额之和；明细只计单头所属批次的行。"
            />
          )}
          <Table
            size="small"
            rowKey="line_id"
            dataSource={detail.lines}
            columns={lineColumns}
            pagination={false}
            scroll={{ x: 760 }}
            locale={{ emptyText: "没有明细行（单头批次内无匹配行）" }}
          />
        </div>
      )}
    </Drawer>
  );
}
