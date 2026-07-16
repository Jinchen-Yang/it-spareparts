import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Card, Grid, Input, List, Segmented, Tag, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import ResizableTable from "../../components/ResizableTable";
import PageHeader from "../../components/PageHeader";
import MobileDetailDrawer from "../../components/MobileDetailDrawer";
import PoolIdentityLink from "../../components/pools/PoolIdentityLink";
import PoolReferencePanel from "../../components/pools/PoolReferencePanel";
import type { DetailField } from "../../components/MobileDetailDrawer";
import { listRecentPurchases } from "../../api";
import type { RecentPurchaseRow } from "../../api";
import { useTaxBasis } from "../../context/TaxBasis";
import {
  DAY_OPTIONS, STATUS_FILTER, STATUS_COLOR, fmtMoney, byTax, readNum,
  useUrlPatch, activatableProps, mobileUnitPrice,
} from "./shared";

// 逐笔采购明细页：按时间/型号/供应商/状态筛选、分页浏览。数据源 listRecentPurchases 原样复用。
export default function PurchaseRecordsPage() {
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false;
  const { basis } = useTaxBasis();

  // 筛选状态存进 URL query，复制链接可恢复
  const [sp, setSp] = useSearchParams();
  const patch = useUrlPatch(sp, setSp);
  const days = readNum(sp, "days", 30);
  const status = sp.get("status") || "已生效";
  const qParam = sp.get("q") || "";
  const supplierParam = sp.get("supplier") || "";
  const page = readNum(sp, "page", 1);
  const pageSize = readNum(sp, "pageSize", 50);

  // 搜索框本地态：输入时不立即改 URL，回车/清除才提交（避免每键一次历史记录）
  const [q, setQ] = useState(qParam);
  const [supplier, setSupplier] = useState(supplierParam);
  useEffect(() => { setQ(qParam); }, [qParam]);
  useEffect(() => { setSupplier(supplierParam); }, [supplierParam]);

  const [total, setTotal] = useState(0);
  const [rows, setRows] = useState<RecentPurchaseRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [detail, setDetail] = useState<RecentPurchaseRow | null>(null);
  const loadSeqRef = useRef(0);

  useEffect(() => {
    const seq = ++loadSeqRef.current;
    setLoading(true);
    listRecentPurchases({
      q: qParam.trim() || undefined,
      supplier: supplierParam.trim() || undefined,
      days, page, page_size: pageSize, status,
    })
      .then(({ data }) => {
        if (seq !== loadSeqRef.current) return;
        setRows(data.items);
        setTotal(data.total);
      })
      .catch(() => { if (seq === loadSeqRef.current) message.error("查询失败，请稍后重试"); })
      .finally(() => { if (seq === loadSeqRef.current) setLoading(false); });
  }, [days, status, qParam, supplierParam, page, pageSize]);

  // 单价/金额双列：按订单税口径归列，另一侧 "—"；跟随全局开关。零计算。
  const priceColumns: ColumnsType<RecentPurchaseRow> = [
    { title: "口径", key: "tax_basis", width: 74, align: "center",
      render: (_, r) => (r.is_tax_inclusive == null
        ? <span style={{ color: "var(--mb-text-3)" }}>—</span>
        : <Tag color={r.is_tax_inclusive ? "default" : "blue"}>{r.is_tax_inclusive ? "含税" : "不含税"}</Tag>) },
    ...(basis !== "ex" ? [{ title: "单价(含税)", key: "up_inc", width: 110, align: "right",
      render: (_, r) => fmtMoney(byTax(r.unit_price, r.is_tax_inclusive).inc) }] as ColumnsType<RecentPurchaseRow> : []),
    ...(basis !== "inc" ? [{ title: "单价(不含税)", key: "up_ex", width: 110, align: "right",
      render: (_, r) => fmtMoney(byTax(r.unit_price, r.is_tax_inclusive).ex) }] as ColumnsType<RecentPurchaseRow> : []),
    ...(basis !== "ex" ? [{ title: "金额(含税)", key: "amt_inc", width: 120, align: "right",
      render: (_, r) => fmtMoney(byTax(r.line_amount, r.is_tax_inclusive).inc) }] as ColumnsType<RecentPurchaseRow> : []),
    ...(basis !== "inc" ? [{ title: "金额(不含税)", key: "amt_ex", width: 120, align: "right",
      render: (_, r) => fmtMoney(byTax(r.line_amount, r.is_tax_inclusive).ex) }] as ColumnsType<RecentPurchaseRow> : []),
  ];

  const desktopColumns: ColumnsType<RecentPurchaseRow> = [
    { title: "采购日期", dataIndex: "order_date", width: 110, fixed: "left", render: (v) => v || "—" },
    { title: "型号", dataIndex: "pn_std", width: 190,
      render: (v, r) => (
        <span>{v}{r.needs_review && <Tag style={{ marginLeft: 6 }} color="orange">待复核</Tag>}
          <PoolIdentityLink groupId={r.pool_group_id} name={r.pool_name} pn={r.pn_std} />
        </span>
      ) },
    { title: "描述", dataIndex: "description", ellipsis: true },
    { title: "品牌", dataIndex: "brand", width: 110, ellipsis: true },
    { title: "数量", dataIndex: "qty", width: 80, align: "right", render: (v) => (v == null ? "—" : Number(v)) },
    ...priceColumns,
    { title: "供应商", dataIndex: "supplier", width: 160, ellipsis: true },
    { title: "采购类型", dataIndex: "source_type", width: 100, render: (v) => v ? <Tag>{v}</Tag> : "—" },
    { title: "流程状态", dataIndex: "data_status", width: 96,
      render: (v: string | null) => v ? <Tag color={STATUS_COLOR[v] || "default"}>{v}</Tag> : "—" },
    { title: "采购员", dataIndex: "purchaser", width: 90, render: (v) => v || "—" },
    { title: "单号", dataIndex: "order_no", width: 170,
      render: (v) => <span style={{ fontFamily: "monospace", fontSize: 12 }}>{v}</span> },
  ];

  // 移动端详情抽屉字段（次要字段），价格明确标含/不含
  const detailFields = (r: RecentPurchaseRow): DetailField[] => {
    const taxLabel = r.is_tax_inclusive == null ? "—" : (r.is_tax_inclusive ? "含税" : "不含税");
    return [
      { label: "描述", value: r.description },
      { label: "品牌", value: r.brand },
      { label: "数量", value: r.qty == null ? null : Number(r.qty) },
      { label: "价格口径", value: taxLabel },
      { label: "单价(含税)", value: fmtMoney(byTax(r.unit_price, r.is_tax_inclusive).inc) },
      { label: "单价(不含税)", value: fmtMoney(byTax(r.unit_price, r.is_tax_inclusive).ex) },
      { label: "金额(含税)", value: fmtMoney(byTax(r.line_amount, r.is_tax_inclusive).inc) },
      { label: "金额(不含税)", value: fmtMoney(byTax(r.line_amount, r.is_tax_inclusive).ex) },
      { label: "供应商", value: r.supplier },
      { label: "采购类型", value: r.source_type },
      { label: "采购员", value: r.purchaser },
      { label: "单号", value: r.order_no },
    ];
  };

  const pagination = {
    current: page, pageSize, total, showSizeChanger: true,
    showTotal: (t: number) => `共 ${t} 条`,
    onChange: (p: number, ps: number) => patch({ page: p, pageSize: ps }),
  };

  return (
    <>
      <PageHeader title="采购明细" subtitle="按时间 / 型号 / 供应商 / 状态查询逐笔采购记录" />
      <Card>
        <div style={{ display: "flex", gap: 12, marginBottom: 16, flexWrap: "wrap", alignItems: "center" }}>
          <Segmented options={DAY_OPTIONS} value={days} onChange={(v) => patch({ days: v as number, page: 1 })} />
          <Input.Search
            placeholder="型号 / 描述 / 品牌关键词"
            style={{ width: isMobile ? "100%" : 280 }}
            allowClear
            value={q}
            onChange={(e) => setQ(e.target.value)}
            // 用 onSearch 回调提供的新值（清除时为 ""）→ patch 把 q 从 URL 删掉，条件真正下线
            onSearch={(val) => patch({ q: val.trim() || undefined, page: 1 })}
          />
          <Input.Search
            placeholder="供应商"
            style={{ width: isMobile ? "100%" : 200 }}
            allowClear
            value={supplier}
            onChange={(e) => setSupplier(e.target.value)}
            onSearch={(val) => patch({ supplier: val.trim() || undefined, page: 1 })}
          />
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span style={{ fontSize: 13, color: "var(--mb-text-3)" }}>状态</span>
            <Segmented options={STATUS_FILTER} value={status} onChange={(v) => patch({ status: v as string, page: 1 })} />
          </span>
        </div>

        {isMobile ? (
          <List
            loading={loading}
            dataSource={rows}
            locale={{ emptyText: "没有符合条件的采购记录" }}
            pagination={pagination}
            renderItem={(r) => (
              <List.Item
                key={r.line_id}
                {...activatableProps(() => setDetail(r), `查看采购记录 ${r.pn_std} 详情`)}
                style={{ cursor: "pointer" }}
              >
                <div style={{ width: "100%" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                    <span style={{ fontWeight: 500 }}>{r.pn_std}</span>
                    <span style={{ color: "var(--mb-text-3)", fontSize: 12.5 }}>{r.order_date || "—"}</span>
                  </div>
                  <div style={{ marginTop: 4 }}>
                    <PoolIdentityLink groupId={r.pool_group_id} name={r.pool_name} pn={r.pn_std} />
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 8, marginTop: 4, fontSize: 13 }}>
                    <span>数量 {r.qty == null ? "—" : Number(r.qty)} · {mobileUnitPrice(r, basis)}</span>
                    {r.data_status && <Tag color={STATUS_COLOR[r.data_status] || "default"}>{r.data_status}</Tag>}
                  </div>
                </div>
              </List.Item>
            )}
          />
        ) : (
          <ResizableTable<RecentPurchaseRow>
            storageKey="purchases-recent"
            size="small"
            rowKey={(r) => r.line_id}
            loading={loading}
            dataSource={rows}
            scroll={{ x: 1100 }}
            pagination={pagination}
            columns={desktopColumns}
            expandable={{
              expandedRowRender: (row) => <PoolReferencePanel partId={row.part_id} side="purchase" compact />,
              rowExpandable: (row) => Number.isInteger(row.part_id) && row.part_id > 0,
            }}
          />
        )}
      </Card>

      <MobileDetailDrawer
        open={detail != null}
        title={detail ? detail.pn_std : ""}
        fields={detail ? detailFields(detail) : []}
        onClose={() => setDetail(null)}
      >
        {detail && <PoolReferencePanel partId={detail.part_id} side="purchase" compact />}
      </MobileDetailDrawer>
    </>
  );
}
