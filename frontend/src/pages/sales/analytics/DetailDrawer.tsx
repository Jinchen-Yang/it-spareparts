// 订单详情抽屉：三金额 + 最新对账块 + 对账历史（每个快照一条，D-40 §3 只追加、
// 旧→新排序，末条与最新块一致）。include_returns 随层透传，reconciled_display 与
// 列表同口径；对账权限受限或无快照时按「无对账数据」展示，不显示 0。
// 404（不存在 / 非已生效）关抽屉；其余失败保留抽屉与所查单号，就地处显示错误与
// 重试（失败 / 未知分开，不清掉用户的位置）。会话令牌在请求前捕获、落地前比对
// （DL-010）：换账号后迟到的响应不写状态、不弹提示。
import { useEffect, useRef, useState } from "react";
import { App, Alert, Button, Descriptions, Drawer, Spin, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { fmtDateTime, fmtMoney, isReconBlockEmpty } from "../shared";
import {
  fetchSalesAnalyticsOrderDetail,
  type SalesAnalyticsOrderDetail,
  type SalesOrderLine,
} from "../../../api/salesAnalytics";
import { pct, qty } from "../../../utils/format";
import { readSessionToken, SESSION_EXPIRED_MESSAGE, sessionStale } from "../../../utils/session";
import type { AmountKey } from "./urlState";

/** 错误文案（与页面同口径：后端 detail 优先，403/422 固定文案）。 */
function apiErrorMessage(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  const status = (err as { response?: { status?: number } })?.response?.status;
  if (status === 403) return "没有访问权限（403）";
  if (status === 422) return "请求参数非法（422）";
  return "加载失败，请稍后重试";
}

export interface DetailDrawerProps {
  orderId: number | null;
  /** 与列表同一开关（D-40 §5）：影响 reconciled_display。 */
  includeReturns: boolean;
  amounts: AmountKey[];
  /** data_reconciliation=false：对账块与历史按「无对账数据」/「—」展示。 */
  reconRestricted: boolean;
  /** data_purchase_cost=false：明细行数量/单价/行金额被后端置空（A-AC01 防重建
   * 成交价），行级表上方给出说明而非裸「—」。 */
  costRestricted: boolean;
  /** 所属会话 owner（页级下传，DL-010 返工）：失效时请求前拒发并清旧详情。 */
  ownerToken: string | null;
  onClose: () => void;
}

const SOURCE_LABEL: Record<string, string> = {
  import: "导入",
  backfill: "回填",
};

export default function DetailDrawer({
  orderId,
  includeReturns,
  amounts,
  reconRestricted,
  costRestricted,
  ownerToken,
  onClose,
}: DetailDrawerProps) {
  const { message } = App.useApp();
  const [detail, setDetail] = useState<SalesAnalyticsOrderDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  // 重试只针对非 404 失败：自增触发 effect 重发，不动 orderId
  const [retryTick, setRetryTick] = useState(0);
  const seqRef = useRef(0);
  // onClose 存 ref：父页每次渲染会传新的内联函数，进 effect 依赖会造成重复请求
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (orderId == null) {
      // 关闭抽屉同样作废在途请求（seq 自增）：迟到的成功/失败不再写状态、不再弹
      // 提示，也不会触发多余的 onClose——与换单/切含退货时的防竞态同一套语义
      seqRef.current++;
      setLoading(false);
      setDetail(null);
      setLoadError(null);
      return;
    }
    const seq = ++seqRef.current;
    // 所属会话失效（DL-010 返工）：拒发并清旧详情——旧会话的抽屉不以当前账号取数
    if (sessionStale(ownerToken)) {
      setLoading(false);
      setDetail(null);
      setLoadError(SESSION_EXPIRED_MESSAGE);
      return;
    }
    const token = readSessionToken();
    setLoading(true);
    setDetail(null);
    setLoadError(null);
    fetchSalesAnalyticsOrderDetail(orderId, includeReturns)
      .then(({ data }) => {
        if (seq !== seqRef.current || sessionStale(token)) return;
        setDetail(data);
      })
      .catch((err) => {
        if (seq !== seqRef.current || sessionStale(token)) return;
        const status = (err as { response?: { status?: number } })?.response?.status;
        if (status === 404) {
          // 404 = 不存在或非已生效（下钻口径）：关抽屉回列表
          message.error("订单不存在（或非已生效单）");
          onCloseRef.current();
          return;
        }
        // 其余失败（网络 / 5xx / 未知）：抽屉保留，就地重试（不丢所查单号）
        setLoadError(apiErrorMessage(err));
      })
      .finally(() => {
        // finally 也守卫（DL-010 返工）：失效会话的收尾不触碰当前 UI 状态
        if (seq === seqRef.current && !sessionStale(token)) setLoading(false);
      });
  }, [orderId, includeReturns, retryTick, ownerToken]);

  const recon = detail?.reconciliation;
  const reconNoData = !recon || reconRestricted || isReconBlockEmpty(recon);

  // 明细行（D-44）：单价 / 行金额随成本权限、毛利随利润权限由后端置空，前端统一「—」
  const lineColumns: ColumnsType<SalesOrderLine> = [
    { title: "行号", dataIndex: "line_no", width: 56, align: "right", render: (v: number | null) => v ?? "—" },
    { title: "PN", dataIndex: "pn", width: 140, ellipsis: true, render: (v: string | null) => v || "—" },
    { title: "描述", dataIndex: "description", ellipsis: true, render: (v: string | null) => v || "—" },
    { title: "大类", dataIndex: "category_major", width: 90, render: (v: string | null) => v || "未分类" },
    { title: "品牌", dataIndex: "brand", width: 90, render: (v: string | null) => v || "—" },
    { title: "数量", dataIndex: "qty", width: 70, align: "right", render: (v: number | null) => qty(v) },
    { title: "含税单价", dataIndex: "unit_price", width: 100, align: "right", render: (v: number | null) => fmtMoney(v) },
    { title: "行金额", dataIndex: "line_amount", width: 100, align: "right", render: (v: number | null) => fmtMoney(v) },
    {
      title: "营收", dataIndex: "revenue", width: 100, align: "right",
      render: (v: number | null, r) => (r.counts_revenue ? fmtMoney(v) : <span title="该行不计营收">不计营收</span>),
    },
    {
      title: "毛利", dataIndex: "gross_profit", width: 120, align: "right",
      render: (v: number | null, r) => (
        <span>{fmtMoney(v)}{r.gross_margin != null && <span style={{ marginLeft: 4, fontSize: 12 }}>{pct(r.gross_margin)}</span>}</span>
      ),
    },
  ];

  const historyColumns: ColumnsType<SalesAnalyticsOrderDetail["reconciliation_history"][number]> = [
    { title: "导入批次", dataIndex: "import_batch_id", width: 84, align: "right" },
    { title: "来源", dataIndex: "source", width: 70, render: (v: string) => SOURCE_LABEL[v] ?? v },
    { title: "数据截至时间", dataIndex: "reconciled_as_of", width: 128, render: (v: string | null) => fmtDateTime(v) },
    { title: "快照订单金额", dataIndex: "order_amount_at_snapshot", width: 110, align: "right", render: (v: number | null) => fmtMoney(v) },
    { title: "已对账金额", dataIndex: "reconciled_amount", width: 104, align: "right", render: (v: number | null) => fmtMoney(v) },
    { title: "待对账金额", dataIndex: "pending_amount", width: 104, align: "right", render: (v: number | null) => fmtMoney(v) },
    { title: "退货已对账金额", dataIndex: "return_reconciled_amount", width: 118, align: "right", render: (v: number | null) => fmtMoney(v) },
  ];

  return (
    <Drawer
      open={orderId != null}
      width={900}
      title={detail ? `销售单 ${detail.order_no}` : "销售单详情"}
      onClose={onClose}
      destroyOnClose
    >
      {loading && <div style={{ textAlign: "center", padding: 40 }}><Spin /></div>}
      {!loading && loadError != null && (
        <Alert
          type="error"
          showIcon
          message={`销售单 #${orderId} 详情加载失败`}
          description={loadError}
          action={<Button type="link" size="small" onClick={() => setRetryTick((t) => t + 1)}>重试</Button>}
        />
      )}
      {!loading && detail && (
        <div style={{ display: "grid", gap: 14 }}>
          <Descriptions
            size="small"
            column={2}
            items={[
              {
                key: "date", label: "订单日期", children: (
                  <span>
                    {detail.order_date || "—"}
                    {detail.is_future && <Tag color="orange" style={{ marginLeft: 6 }}>未来</Tag>}
                  </span>
                ),
              },
              { key: "type", label: "业务类型", children: detail.business_type || "—" },
              { key: "sales", label: "销售人员", children: detail.salesperson || "—" },
              { key: "customer", label: "客户", children: detail.customer || "—" },
              { key: "status", label: "数据状态", children: detail.data_status || "—" },
            ]}
          />
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
            {amounts.includes("ex") && (
              <span>不含税金额 <b>{fmtMoney(detail.amount_ex_tax)}</b></span>
            )}
            {amounts.includes("inc") && (
              <span>含税订单金额 <b>{fmtMoney(detail.amount_inc_tax)}</b></span>
            )}
            {amounts.includes("revenue") && (
              <span>营收 <b>{fmtMoney(detail.revenue)}</b></span>
            )}
            {detail.gross_profit !== undefined && (
              <span>
                毛利 <b>{fmtMoney(detail.gross_profit)}</b>
                {detail.gross_margin != null && <span style={{ marginLeft: 4 }}>（{pct(detail.gross_margin)}）</span>}
              </span>
            )}
          </div>

          {detail.lines && (
            <>
              {costRestricted && (
                <Alert
                  type="info"
                  showIcon
                  message="当前账号无成本权限（data_purchase_cost）：明细行的数量 / 单价 / 行金额不可见（防「营收 ÷ 数量」重建成交价），仅保留品目信息。"
                />
              )}
              <Table<SalesOrderLine>
                size="small"
                rowKey="line_id"
                title={() => `明细行（${detail.lines?.length ?? 0}）`}
                dataSource={detail.lines}
                columns={lineColumns}
                pagination={false}
                scroll={{ x: 900 }}
                locale={{ emptyText: "该单没有明细行" }}
              />
            </>
          )}

          {reconRestricted ? (
            <Alert type="info" showIcon message="无对账数据（当前账号无销售单对账数据权限）" />
          ) : reconNoData ? (
            <Alert
              type="info"
              showIcon
              message={recon?.has_data
                ? "无对账数据（对账金额与时间均为空或不可见）"
                : "无对账数据（该单没有任何对账快照）"}
            />
          ) : (
            <Descriptions
              size="small"
              column={2}
              title="对账快照（最新）"
              items={[
                {
                  key: "display", label: includeReturns ? "已对账金额（含退货）" : "已对账金额",
                  children: (
                    <b>{fmtMoney(recon.reconciled_display)}</b>
                  ),
                },
                { key: "pending", label: "待对账金额", children: fmtMoney(recon.pending_amount) },
                { key: "returns", label: "退货已对账金额", children: fmtMoney(recon.return_reconciled_amount) },
                { key: "snap", label: "快照订单金额", children: fmtMoney(recon.order_amount_at_snapshot) },
                { key: "asof", label: "数据截至时间", children: fmtDateTime(recon.reconciled_as_of) },
                {
                  key: "note", label: "口径",
                  children: `已对账金额 = 实际回款；${includeReturns ? "含退货 = 已对账 + 退货已对账（相加，不扣减）" : "不含退货 = 只看已对账"}`,
                },
              ]}
            />
          )}

          {reconRestricted ? null : (
            <Table
              size="small"
              rowKey={(r) => `${r.import_batch_id}-${r.reconciled_as_of ?? ""}-${r.created_at ?? ""}`}
              dataSource={detail.reconciliation_history}
              columns={historyColumns}
              pagination={false}
              scroll={{ x: 760 }}
              locale={{ emptyText: "无对账数据（该单没有任何对账快照）" }}
            />
          )}
          <div style={{ fontSize: 12, color: "var(--mb-text-3)" }}>
            对账历史按旧→新排列，每个导入/回填批次一条；金额为空的单元格按「—」展示，不补 0。
          </div>
        </div>
      )}
    </Drawer>
  );
}
