import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Alert, Card, Grid, List, Segmented, Table, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import PageHeader from "../../components/PageHeader";
import MobileDetailDrawer from "../../components/MobileDetailDrawer";
import { fetchCancellationStats } from "../../api";
import type { CancellationStats, CancellationPeriodRow } from "../../api";
import { GRAN_OPTIONS, fmtMoney, activatableProps } from "./shared";

// 采购异常页：集中展示取消/作废/未成功。当前唯一有可靠数据的口径 = 取消单统计。
// 其它异常（长期未完成等）后端暂无对应字段，不在前端伪造/推算——见页内说明。
export default function PurchaseExceptionsPage() {
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false;

  const [sp, setSp] = useSearchParams();
  const gran = sp.get("granularity") || "month";

  const [data, setData] = useState<CancellationStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [detail, setDetail] = useState<CancellationPeriodRow | null>(null);
  const seqRef = useRef(0);

  const setGran = (v: string) => {
    const merged = new URLSearchParams(sp);
    if (v === "month") merged.delete("granularity");
    else merged.set("granularity", v);
    setSp(merged, { replace: true });
  };

  useEffect(() => {
    const seq = ++seqRef.current;
    setLoading(true);
    fetchCancellationStats({ granularity: gran })
      .then(({ data }) => { if (seq === seqRef.current) setData(data); })
      .catch(() => { if (seq === seqRef.current) message.error("取消单统计加载失败"); })
      .finally(() => { if (seq === seqRef.current) setLoading(false); });
  }, [gran]);

  const columns: ColumnsType<CancellationPeriodRow> = [
    { title: "期间", dataIndex: "period", width: 110 },
    { title: "总单数", dataIndex: "total", width: 88, align: "right" },
    { title: "已生效", key: "active", width: 84, align: "right", render: (_, r) => r.by_status["已生效"]?.count ?? 0 },
    { title: "进行中", key: "doing", width: 84, align: "right", render: (_, r) => r.by_status["进行中"]?.count ?? 0 },
    { title: "取消/作废", dataIndex: "cancelled", width: 98, align: "right",
      render: (v: number) => <span style={{ color: v > 0 ? "#c0524a" : undefined }}>{v}</span> },
    { title: "取消率", dataIndex: "cancel_rate", width: 88, align: "right", render: (v: number) => `${v}%` },
    { title: "取消金额", dataIndex: "cancelled_amount", width: 130, align: "right", render: fmtMoney },
  ];

  const summary = data?.summary;

  return (
    <>
      <PageHeader
        title="采购异常"
        subtitle="集中看采购取消 / 作废 / 未成功；按周期统计取消率与涉及金额"
        extra={!isMobile ? <Segmented options={GRAN_OPTIONS} value={gran} onChange={(v) => setGran(v as string)} /> : undefined}
      />
      <Card>
        {isMobile && (
          <div style={{ marginBottom: 14 }}>
            <Segmented options={GRAN_OPTIONS} value={gran} onChange={(v) => setGran(v as string)} block />
          </div>
        )}

        {summary && (
          <Alert
            type={summary.cancelled > 0 ? "warning" : "info"}
            showIcon
            style={{ marginBottom: 14 }}
            message={`累计取消 ${summary.cancelled} 单 · 取消率 ${summary.cancel_rate}% · 取消金额 ${fmtMoney(summary.cancelled_amount)}`}
          />
        )}

        {isMobile ? (
          <List
            loading={loading}
            dataSource={data?.rows || []}
            locale={{ emptyText: "暂无取消/作废记录" }}
            renderItem={(r) => (
              <List.Item
                key={r.period}
                {...activatableProps(() => setDetail(r), `查看 ${r.period} 采购异常详情`)}
                style={{ cursor: "pointer" }}
              >
                <div style={{ width: "100%" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                    <span style={{ fontWeight: 500 }}>{r.period}</span>
                    <span style={{ color: r.cancelled > 0 ? "#c0524a" : "var(--mb-text-3)" }}>取消 {r.cancelled} · {r.cancel_rate}%</span>
                  </div>
                  <div style={{ marginTop: 4, fontSize: 13, color: "var(--mb-text-2)" }}>
                    总单 {r.total} · 取消金额 {fmtMoney(r.cancelled_amount)}
                  </div>
                </div>
              </List.Item>
            )}
          />
        ) : (
          <Table<CancellationPeriodRow>
            size="small" rowKey={(r) => r.period} loading={loading}
            dataSource={data?.rows || []} pagination={false} columns={columns}
            locale={{ emptyText: "暂无取消/作废记录" }}
          />
        )}

        <div style={{ marginTop: 8, fontSize: 12, color: "var(--mb-text-3)" }}>
          含全部状态的采购单（含已取消/作废）。取消单仅用于此处统计，<b>不计入</b>成本/库存/利润——那些口径只算「已生效」。
        </div>
      </Card>

      <MobileDetailDrawer
        open={detail != null}
        title={detail ? detail.period : ""}
        fields={detail ? [
          { label: "总单数", value: String(detail.total) },
          { label: "已生效", value: String(detail.by_status["已生效"]?.count ?? 0) },
          { label: "进行中", value: String(detail.by_status["进行中"]?.count ?? 0) },
          { label: "取消/作废", value: String(detail.cancelled) },
          { label: "取消率", value: `${detail.cancel_rate}%` },
          { label: "取消金额", value: fmtMoney(detail.cancelled_amount) },
        ] : []}
        onClose={() => setDetail(null)}
      />
    </>
  );
}
