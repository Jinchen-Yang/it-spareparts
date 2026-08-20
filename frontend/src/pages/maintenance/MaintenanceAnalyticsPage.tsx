import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Input,
  Row,
  Segmented,
  Select,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import type { ColumnsType, TableProps } from "antd/es/table";
import { useSearchParams } from "react-router-dom";
import dayjs from "dayjs";
import { BarChartOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  fetchPnRanking,
  type PnRanking,
  type PnRankingRow,
} from "../../api/maintenanceAnalytics";
import { readPermissionMap } from "../../nav";
import PageHeader from "../../components/PageHeader";
import { PnTopBar } from "../../components/charts/PnTopBar";
import { raw } from "../maintenance/panel/panelUtils";
import { moneyExact, qty as qtyFmt } from "../../utils/format";

const { Text } = Typography;

/** 六态信封渲染：restricted 🔒、not_imported ●、绝不渲染 0（铁律 5）。 */
function statText(stat: { state: string; value: unknown } | undefined): string {
  if (!stat) return "—";
  if (stat.state === "restricted") return "🔒 无权限";
  if (stat.state === "not_imported") return "尚未导入";
  if (stat.state === "error") return "暂不可用";
  return stat.value === null || stat.value === "" ? "—" : String(stat.value);
}

/** 金额信封 → 千分位金额（¥1,586,637.81），无值/受限走 statText。 */
function statMoney(stat: { state: string; value: unknown } | undefined): string {
  if (stat && stat.state === "ready" && stat.value !== null && stat.value !== "") {
    return moneyExact(Number(stat.value));
  }
  return statText(stat);
}

const RANGE_OPTIONS = [
  { label: "本年", value: "ytd" },
  { label: "近12月", value: "12m" },
  { label: "全部", value: "all" },
  { label: "自定义", value: "custom" },
];

const SORT_OPTIONS = [
  { label: "含税成本", value: "cost_inc" },
  { label: "未税成本", value: "cost_ex" },
  { label: "有效数量", value: "effective_qty" },
  { label: "需求数量", value: "qty" },
  { label: "行次数", value: "occurrences" },
  { label: "坏件返还量", value: "bad_qty" },
];

/** 表头排序 → 服务端排序键（服务端分页：必须整库排序，不能只排当页）。 */
const SORTER_TO_KEY: Record<string, string> = {
  pn: "pn",
  occurrences: "occurrences",
  order_count: "order_count",
  project_count: "project_count",
  qty: "qty",
  return_qty: "return_qty",
  effective_qty: "effective_qty",
  monthly_avg_qty: "monthly_avg",
  cost_inc: "cost_inc",
  cost_ex: "cost_ex",
  cost_share_pct: "cost_share",
  bad_return_qty: "bad_qty",
  bad_return_rate_pct: "bad_rate",
  missing_lines: "missing_lines",
};

const PAGE_SIZE_OPTIONS = [20, 50, 100];

function KpiCard({ label, value, sub, loading }: {
  label: string; value: string; sub?: string; loading?: boolean;
}) {
  return (
    <Card size="small" style={{ flex: "1 1 180px" }}>
      <Text type="secondary" style={{ fontSize: 12 }}>{label}</Text>
      <div style={{ fontSize: 20, fontWeight: 600, marginTop: 4, lineHeight: 1.4 }}>
        {loading ? <Skeleton.Button active size="small" style={{ width: 140 }} /> : value}
      </div>
      {sub && !loading ? <Text type="secondary" style={{ fontSize: 12 }}>{sub}</Text> : null}
    </Card>
  );
}

/**
 * 维保数据分析看板：PN 成本排名 + 损坏频率（2026-08-21）。
 * URL 即筛选状态（PoolAnalysis 范式）：range/sort/q/page/ps/from/to 全入 query，
 * 刷新/分享不丢上下文。
 */
export function MaintenanceAnalyticsPage() {
  const [sp, setSp] = useSearchParams();
  const rangeKey = sp.get("range") ?? "ytd";
  const sort = sp.get("sort") ?? "cost_inc";
  const q = sp.get("q") ?? "";
  const page = Number(sp.get("page") ?? "1") || 1;
  const pageSize = Number(sp.get("ps") ?? "20") || 20;
  const customFrom = sp.get("from");
  const customTo = sp.get("to");
  const perms = readPermissionMap();
  const canCost = !!perms.data_purchase_cost;

  const patch = useCallback((next: Record<string, string | null>) => {
    setSp((prev) => {
      const merged = new URLSearchParams(prev);
      for (const [k, v] of Object.entries(next)) {
        if (v === null || v === "") merged.delete(k);
        else merged.set(k, v);
      }
      return merged;
    }, { replace: true });
  }, [setSp]);

  const [data, setData] = useState<PnRanking | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    const seq = seqRef.current + 1;
    seqRef.current = seq;
    setLoading(true);
    setError(null);
    try {
      const payload: Record<string, unknown> = {
        range: rangeKey, sort, page, page_size: pageSize,
      };
      if (q.trim()) payload.q = q.trim();
      if (rangeKey === "custom") {
        if (customFrom) payload.date_from = customFrom;
        if (customTo) payload.date_to = customTo;
      }
      const resp = await fetchPnRanking(payload);
      if (seqRef.current !== seq) return; // 代次守卫：旧响应不覆盖新请求
      setData(resp);
    } catch (err) {
      if (seqRef.current !== seq) return;
      const detail = (err as { response?: { data?: { detail?: { message?: string } | string } } })
        .response?.data?.detail;
      const msg = typeof detail === "string" ? detail : detail?.message;
      setError(msg || "加载失败");
      message.error(msg || "维保分析数据加载失败");
    } finally {
      if (seqRef.current === seq) setLoading(false);
    }
  }, [rangeKey, sort, q, page, pageSize, customFrom, customTo]);

  useEffect(() => {
    void load();
  }, [load]);

  const onTableChange: TableProps<PnRankingRow>["onChange"] = (pg, _fl, sorter) => {
    const field = Array.isArray(sorter) ? sorter[0]?.field : sorter?.field;
    const key = field ? SORTER_TO_KEY[String(field)] : undefined;
    if (key && key !== sort) {
      patch({ sort: key, page: null });
      return;
    }
    if (pg.pageSize && pg.pageSize !== pageSize) {
      patch({ ps: String(pg.pageSize), page: null });
    } else if (pg.current && pg.current !== page) {
      patch({ page: String(pg.current) });
    }
  };

  const columns: ColumnsType<PnRankingRow> = useMemoColumns(sort);

  const summary = data?.summary;
  const costItems = (data?.rows ?? []).map((r) => ({
    pn: r.pn,
    value: r.cost_inc.state === "ready" && r.cost_inc.value !== null
      ? Number(r.cost_inc.value) : null,
  }));
  const qtyItems = (data?.rows ?? []).map((r) => ({
    pn: r.pn,
    value: Number(r.effective_qty) || null,
  }));

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <PageHeader
        title={(
          <Space>
            <BarChartOutlined />
            <span>维保数据分析</span>
          </Space>
        )}
        subtitle="全项目 PN 维度：备件消耗成本排名 + 损坏频率（RKD 坏件返还佐证）"
        extra={(
          <Space>
            <Button onClick={() => setSp(new URLSearchParams(), { replace: true })}>
              重置筛选
            </Button>
            <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
              刷新
            </Button>
          </Space>
        )}
      />

      <Card size="small">
        <Space wrap size={12}>
          <Segmented options={RANGE_OPTIONS} value={rangeKey}
            onChange={(v) => patch({ range: String(v), page: null })} />
          {rangeKey === "custom" ? (
            <DatePicker.RangePicker
              value={[customFrom ? dayjs(customFrom) : null, customTo ? dayjs(customTo) : null]}
              onChange={(v) => patch({
                from: v?.[0] ? v[0].format("YYYY-MM-DD") : null,
                to: v?.[1] ? v[1].format("YYYY-MM-DD") : null,
              })}
              allowEmpty={[true, true]}
            />
          ) : null}
          <Select options={SORT_OPTIONS} value={sort} style={{ width: 140 }}
            onChange={(v) => patch({ sort: v, page: null })} />
          <Input.Search allowClear defaultValue={q} placeholder="搜 PN / 描述" style={{ width: 220 }}
            onSearch={(v) => patch({ q: v || null, page: null })} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            成本＝系统回填已知成本（缺价行单列，不按 0 计）；损坏佐证＝RKD 坏件返还
          </Text>
        </Space>
      </Card>

      {error ? <Alert type="error" showIcon message={error} /> : null}
      {summary && !summary.wbdd_ready ? (
        <Alert type="warning" showIcon message="维保需求单尚未导入，暂无分析数据" />
      ) : null}

      <Row gutter={12} style={{ display: "flex", flexWrap: "wrap" }}>
        <KpiCard label="备件总成本（含税）" loading={loading}
          value={statMoney(summary?.total_cost_inc)}
          sub={canCost ? "点表格含税成本列头可按成本排序" : "需要成本查看权限"} />
        <KpiCard label="涉及 PN 数" loading={loading}
          value={qtyFmt(summary?.part_count ?? null)} />
        <KpiCard label="总有效消耗量" loading={loading}
          value={qtyFmt(summary ? Number(summary.total_effective_qty) : null)}
          sub="需求数量 − 退货数量" />
        <KpiCard label="坏件返还总量" loading={loading}
          value={qtyFmt(summary ? Number(summary.total_bad_return_qty) : null)}
          sub="RKD 入库确认的坏品/坏件/故障" />
      </Row>

      <Row gutter={16}>
        <Col xs={24} lg={12}>
          <Card size="small">
            <PnTopBar items={costItems} title="Top PN 成本" kind="money"
              metricLabel="金额合计（含税）" loading={loading} error={error}
              testId="pn-cost-chart" />
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card size="small">
            <PnTopBar items={qtyItems} title="Top PN 消耗频率" kind="qty"
              metricLabel="数量合计（有效数量）" loading={loading} error={error}
              testId="pn-qty-chart" />
          </Card>
        </Col>
      </Row>

      <Card size="small" title={`PN 排名（共 ${qtyFmt(data?.total ?? null)} 个）`}>
        <Table<PnRankingRow>
          rowKey="part_id"
          size="small"
          loading={loading}
          dataSource={data?.rows ?? []}
          columns={columns}
          onChange={onTableChange}
          scroll={{ x: 1600 }}
          pagination={{
            current: page,
            pageSize,
            total: data?.total ?? 0,
            pageSizeOptions: PAGE_SIZE_OPTIONS,
            showSizeChanger: true,
            showTotal: (t, range) => `第 ${range[0]}–${range[1]} 条 / 共 ${t} 个 PN`,
          }}
          locale={{ emptyText: "当前窗口没有分析数据" }}
        />
      </Card>
    </Space>
  );
}

/** 列定义工厂：memo 化，避免每次渲染重建 15 列。 */
function useMemoColumns(_sort: string): ColumnsType<PnRankingRow> {
  return useMemo<ColumnsType<PnRankingRow>>(() => [
    { title: "#", dataIndex: "rank", width: 60, fixed: "left" as const },
    {
      title: "PN / 描述", dataIndex: "pn", width: 300, fixed: "left" as const,
      sorter: true,
      render: (v: string, r: PnRankingRow) => (
        <Space direction="vertical" size={0}>
          <Text strong copyable>{raw(v)}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>{raw(r.description)}</Text>
        </Space>
      ),
    },
    { title: "行次数", dataIndex: "occurrences", sorter: true, width: 90,
      render: (v: number) => qtyFmt(v) },
    { title: "单数", dataIndex: "order_count", sorter: true, width: 80,
      render: (v: number) => qtyFmt(v) },
    { title: "项目数", dataIndex: "project_count", sorter: true, width: 80,
      render: (v: number) => qtyFmt(v) },
    { title: "需求数量", dataIndex: "qty", sorter: true, width: 100,
      render: (v: string | null) => qtyFmt(v === null ? null : Number(v)) },
    { title: "退货", dataIndex: "return_qty", sorter: true, width: 90,
      render: (v: string | null) => qtyFmt(v === null ? null : Number(v)) },
    { title: "有效数量", dataIndex: "effective_qty", sorter: true, width: 100,
      render: (v: string | null) => qtyFmt(v === null ? null : Number(v)) },
    { title: "月均", dataIndex: "monthly_avg_qty", sorter: true, width: 80,
      render: (v: number | null) => (v === null ? "—" : qtyFmt(v)) },
    { title: "含税成本", dataIndex: "cost_inc", sorter: true, width: 140,
      render: (_: unknown, r: PnRankingRow) => statMoney(r.cost_inc) },
    { title: "未税成本", dataIndex: "cost_ex", sorter: true, width: 140,
      render: (_: unknown, r: PnRankingRow) => statMoney(r.cost_ex) },
    { title: "成本占比", dataIndex: "cost_share_pct", sorter: true, width: 100,
      render: (v: number | null) => (v === null ? "—" : `${v}%`) },
    { title: "坏件返还", dataIndex: "bad_return_qty", sorter: true, width: 100,
      render: (v: string | null) => qtyFmt(v === null ? null : Number(v)) },
    { title: "坏返率", dataIndex: "bad_return_rate_pct", sorter: true, width: 90,
      render: (v: number | null) => (v === null ? "—"
        : <Tag color={v > 50 ? "red" : v > 20 ? "orange" : "default"}>{v}%</Tag>) },
    { title: "缺价行", dataIndex: "missing_lines", sorter: true, width: 80,
      render: (v: number) => qtyFmt(v) },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ], []);
}

export default MaintenanceAnalyticsPage;
