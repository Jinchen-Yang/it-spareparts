import { useEffect, useRef, useState } from "react";
import { Card, Input, Segmented, Tag, message } from "antd";
import ResizableTable from "../components/ResizableTable";
import { listRecentPurchases } from "../api";
import type { RecentPurchaseRow } from "../api";

const DAY_OPTIONS = [
  { label: "近 7 天", value: 7 },
  { label: "近 30 天", value: 30 },
  { label: "近 90 天", value: 90 },
  { label: "近一年", value: 365 },
];

const fmtMoney = (v: number | null) =>
  v == null ? "—" : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export default function PurchasesPage() {
  const [q, setQ] = useState("");
  const [supplier, setSupplier] = useState("");
  const [days, setDays] = useState(30);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [total, setTotal] = useState(0);
  const [rows, setRows] = useState<RecentPurchaseRow[]>([]);
  const [loading, setLoading] = useState(false);
  const loadSeqRef = useRef(0); // 防乱序：快速切天数/搜索时丢弃过期响应

  const load = async (p = page, ps = pageSize) => {
    const seq = ++loadSeqRef.current;
    setLoading(true);
    try {
      const { data } = await listRecentPurchases({
        q: q.trim() || undefined,
        supplier: supplier.trim() || undefined,
        days, page: p, page_size: ps,
      });
      if (seq !== loadSeqRef.current) return;
      setRows(data.items);
      setTotal(data.total);
    } catch {
      if (seq === loadSeqRef.current) message.error("查询失败，请稍后重试");
    } finally {
      if (seq === loadSeqRef.current) setLoading(false);
    }
  };

  useEffect(() => {
    setPage(1);
    load(1, pageSize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days]);

  return (
    <Card
      title="采购记录"
      extra={<span style={{ color: "var(--mb-text-3, #92A1A8)", fontSize: 12 }}>
        共 {total} 条 · 按采购日期倒序 · 型号为治理后口径
      </span>}
    >
      <div style={{ display: "flex", gap: 12, marginBottom: 16, flexWrap: "wrap" }}>
        <Segmented options={DAY_OPTIONS} value={days} onChange={(v) => setDays(v as number)} />
        <Input.Search
          placeholder="型号 / 描述 / 品牌关键词"
          style={{ width: 280 }}
          allowClear
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onSearch={() => { setPage(1); load(1, pageSize); }}
        />
        <Input.Search
          placeholder="供应商"
          style={{ width: 200 }}
          allowClear
          value={supplier}
          onChange={(e) => setSupplier(e.target.value)}
          onSearch={() => { setPage(1); load(1, pageSize); }}
        />
      </div>
      <ResizableTable<RecentPurchaseRow>
        storageKey="purchases-recent"
        size="small"
        rowKey={(r) => r.line_id}
        loading={loading}
        dataSource={rows}
        scroll={{ x: 1100 }}
        pagination={{
          current: page, pageSize, total, showSizeChanger: true,
          showTotal: (t) => `共 ${t} 条`,
          onChange: (p, ps) => { setPage(p); setPageSize(ps); load(p, ps); },
        }}
        columns={[
          { title: "采购日期", dataIndex: "order_date", width: 110, fixed: "left",
            render: (v) => v || "—" },
          { title: "型号", dataIndex: "pn_std", width: 190,
            render: (v, r) => (
              <span>
                {v}
                {r.needs_review && <Tag style={{ marginLeft: 6 }} color="orange">待复核</Tag>}
              </span>
            ) },
          { title: "描述", dataIndex: "description", ellipsis: true },
          { title: "品牌", dataIndex: "brand", width: 110, ellipsis: true },
          { title: "数量", dataIndex: "qty", width: 80, align: "right",
            render: (v) => (v == null ? "—" : Number(v)) },
          { title: "单价(含税)", dataIndex: "unit_price", width: 110, align: "right",
            render: fmtMoney },
          { title: "金额", dataIndex: "line_amount", width: 120, align: "right",
            render: fmtMoney },
          { title: "供应商", dataIndex: "supplier", width: 160, ellipsis: true },
          { title: "采购类型", dataIndex: "source_type", width: 100,
            render: (v) => v ? <Tag>{v}</Tag> : "—" },
          { title: "采购员", dataIndex: "purchaser", width: 90, render: (v) => v || "—" },
          { title: "单号", dataIndex: "order_no", width: 170,
            render: (v) => <span style={{ fontFamily: "monospace", fontSize: 12 }}>{v}</span> },
        ]}
      />
    </Card>
  );
}
