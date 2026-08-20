import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Input,
  Row,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs, { type Dayjs } from "dayjs";
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

const { Text } = Typography;

/** 六态信封渲染：restricted 🔒、not_imported ●、绝不渲染 0（铁律 5）。 */
function statText(stat: { state: string; value: unknown } | undefined): string {
  if (!stat) return "—";
  if (stat.state === "restricted") return "🔒 无权限";
  if (stat.state === "not_imported") return "尚未导入";
  if (stat.state === "error") return "暂不可用";
  return stat.value === null || stat.value === "" ? "—" : String(stat.value);
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
  { label: "有效数量", value: "qty" },
  { label: "行次数", value: "occurrences" },
  { label: "坏件返还量", value: "bad_qty" },
];

function KpiCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <Card size="small" style={{ flex: "1 1 180px" }}>
      <Text type="secondary" style={{ fontSize: 12 }}>{label}</Text>
      <div style={{ fontSize: 20, fontWeight: 600, marginTop: 4 }}>{value}</div>
      {sub ? <Text type="secondary" style={{ fontSize: 12 }}>{sub}</Text> : null}
    </Card>
  );
}

/** 维保数据分析看板：PN 成本排名 + 损坏频率（2026-08-21，全栈新功能）。 */
export function MaintenanceAnalyticsPage() {
  const [rangeKey, setRangeKey] = useState<string>("ytd");
  const [customRange, setCustomRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);
  const [sort, setSort] = useState("cost_inc");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<PnRanking | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const perms = readPermissionMap();
  const canCost = !!perms.data_purchase_cost;

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params: Record<string, unknown> = { range: rangeKey, sort, page, page_size: 20 };
      if (q.trim()) params.q = q.trim();
      if (rangeKey === "custom" && customRange?.[0] && customRange?.[1]) {
        params.date_from = customRange[0].format("YYYY-MM-DD");
        params.date_to = customRange[1].format("YYYY-MM-DD");
      }
      const resp = await fetchPnRanking(params);
      setData(resp);
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: { message?: string } | string } } })
        .response?.data?.detail;
      const msg = typeof detail === "string" ? detail : detail?.message;
      setError(msg || "加载失败");
      message.error(msg || "维保分析数据加载失败");
    } finally {
      setLoading(false);
    }
  }, [rangeKey, customRange, sort, q, page]);

  useEffect(() => {
    void load();
  }, [load]);

  const columns: ColumnsType<PnRankingRow> = [
    { title: "#", dataIndex: "rank", width: 60 },
    {
      title: "PN / 描述",
      dataIndex: "pn",
      width: 300,
      render: (v: string, r: PnRankingRow) => (
        <Space direction="vertical" size={0}>
          <Text strong copyable>{raw(v)}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>{raw(r.description)}</Text>
        </Space>
      ),
    },
    { title: "行次数", dataIndex: "occurrences", width: 90, sorter: false },
    { title: "单数", dataIndex: "order_count", width: 80 },
    { title: "项目数", dataIndex: "project_count", width: 80 },
    { title: "需求数量", dataIndex: "qty", width: 100, render: raw },
    { title: "退货", dataIndex: "return_qty", width: 90, render: raw },
    { title: "有效数量", dataIndex: "effective_qty", width: 100, render: raw },
    {
      title: "月均",
      dataIndex: "monthly_avg_qty",
      width: 80,
      render: (v: number | null) => (v === null ? "—" : String(v)),
    },
    { title: "含税成本", dataIndex: "cost_inc", width: 130, render: (_: unknown, r) => statText(r.cost_inc) },
    { title: "未税成本", dataIndex: "cost_ex", width: 130, render: (_: unknown, r) => statText(r.cost_ex) },
    {
      title: "成本占比",
      dataIndex: "cost_share_pct",
      width: 100,
      render: (v: number | null) => (v === null ? "—" : `${v}%`),
    },
    { title: "坏件返还", dataIndex: "bad_return_qty", width: 100, render: raw },
    {
      title: "坏返率",
      dataIndex: "bad_return_rate_pct",
      width: 90,
      render: (v: number | null) => (v === null ? "—" : <Tag color={v > 50 ? "red" : v > 20 ? "orange" : "default"}>{v}%</Tag>),
    },
    { title: "缺价行", dataIndex: "missing_lines", width: 80 },
  ];

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
        title={
          <Space>
            <BarChartOutlined />
            <span>维保数据分析</span>
          </Space>
        }
        subtitle="全项目 PN 维度：备件消耗成本排名 + 损坏频率（RKD 坏件返还佐证）"
        extra={(
          <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
        )}
      />

      <Card size="small">
        <Space wrap size={12}>
          <Segmented options={RANGE_OPTIONS} value={rangeKey}
            onChange={(v) => { setRangeKey(String(v)); setPage(1); }} />
          {rangeKey === "custom" ? (
            <Input.Group compact>
              {[0, 1].map((i) => (
                <Input key={i} style={{ width: 130 }} placeholder={i === 0 ? "起 YYYY-MM-DD" : "止 YYYY-MM-DD"}
                  onChange={(e) => {
                    const v = dayjs(e.target.value);
                    if (!v.isValid()) return;
                    setCustomRange((prev) => {
                      const next: [Dayjs | null, Dayjs | null] = [prev?.[0] ?? null, prev?.[1] ?? null];
                      next[i] = v;
                      return next;
                    });
                  }} />
              ))}
            </Input.Group>
          ) : null}
          <Select options={SORT_OPTIONS} value={sort} style={{ width: 140 }}
            onChange={(v) => { setSort(v); setPage(1); }} />
          <Input.Search allowClear placeholder="搜 PN / 描述" style={{ width: 220 }}
            onSearch={(v) => { setQ(v); setPage(1); }} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            成本口径：系统回填的已知成本（缺价行单列，不按 0 计）；损坏佐证：RKD 坏件返还量
          </Text>
        </Space>
      </Card>

      {error ? <Alert type="error" showIcon message={error} /> : null}
      {summary && !summary.wbdd_ready ? (
        <Alert type="warning" showIcon message="维保需求单尚未导入，暂无分析数据" />
      ) : null}

      <Row gutter={12} style={{ display: "flex", flexWrap: "wrap" }}>
        <KpiCard label="备件总成本（含税）" value={statText(summary?.total_cost_inc)}
          sub={canCost ? undefined : "需要成本查看权限"} />
        <KpiCard label="涉及 PN 数" value={String(summary?.part_count ?? "—")} />
        <KpiCard label="总有效消耗量" value={raw(summary?.total_effective_qty)}
          sub="需求数量 − 退货数量" />
        <KpiCard label="坏件返还总量" value={raw(summary?.total_bad_return_qty)}
          sub="RKD 入库确认的坏品/坏件/故障" />
      </Row>

      <Row gutter={16}>
        <Col xs={24} lg={12}>
          <Card size="small">
            <PnTopBar items={costItems} title="Top PN 成本" metricLabel="金额合计（含税，元）"
              loading={loading} error={error} testId="pn-cost-chart" />
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card size="small">
            <PnTopBar items={qtyItems} title="Top PN 消耗频率" metricLabel="数量合计（有效数量）"
              loading={loading} error={error} testId="pn-qty-chart" />
          </Card>
        </Col>
      </Row>

      <Card size="small" title={`PN 排名（共 ${data?.total ?? 0} 个）`}>
        <Table<PnRankingRow>
          rowKey="part_id"
          size="small"
          loading={loading}
          dataSource={data?.rows ?? []}
          columns={columns}
          scroll={{ x: 1500 }}
          pagination={{
            current: page,
            pageSize: 20,
            total: data?.total ?? 0,
            showSizeChanger: false,
            showTotal: (t) => `共 ${t} 个 PN`,
            onChange: setPage,
          }}
          locale={{ emptyText: "当前窗口没有分析数据" }}
        />
      </Card>
    </Space>
  );
}

export default MaintenanceAnalyticsPage;
