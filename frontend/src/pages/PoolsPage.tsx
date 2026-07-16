import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Card, Input, Segmented, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Link, useSearchParams } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import {
  fetchPoolAnalysisList,
  type PoolAnalysisListItem,
  type PoolAnalysisListResponse,
  type PoolAnalysisRange,
  type PoolReferenceSide,
} from "../api/poolAnalysis";

const RANGE_OPTIONS = [
  { label: "近 30 天", value: "30d" },
  { label: "近 90 天", value: "90d" },
  { label: "近 365 天", value: "365d" },
  { label: "全部", value: "all" },
];
const VALID_RANGES = new Set(RANGE_OPTIONS.map((option) => option.value));

const money = (value: number | null | undefined) => value == null ? null : `¥${Number(value).toLocaleString("zh-CN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})}`;

function PriceSummary({ side, kind }: { side: PoolReferenceSide; kind: "purchase" | "sales" }) {
  if (side.restricted) {
    return <Tag aria-label={`${kind === "purchase" ? "采购" : "销售"}价格无权限`}>无价格权限</Tag>;
  }
  const limitName = kind === "purchase" ? "人工上限" : "人工下限";
  const limit = side.constraint.status === "restricted"
    ? "无约束价权限"
    : side.constraint.status === "unset" ? "未设置" : money(side.constraint.value) || "未设置";
  return (
    <div style={{ display: "grid", gap: 3, fontSize: 12.5, lineHeight: 1.45 }}>
      <span>均价 <b>{money(side.pool_stats?.weighted_avg) || "暂无样本"}</b></span>
      <span>中位 <b>{money(side.pool_stats?.median) || "暂无样本"}</b></span>
      <span>{limitName} <b>{limit}</b></span>
      <span style={{ color: "var(--mb-text-3)" }}>
        {side.pool_stats?.order_count ?? 0} 单 / {side.pool_stats?.line_count ?? 0} 笔
      </span>
    </div>
  );
}

function readPage(value: string | null): number {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 1;
}

/**
 * 员工侧池价格分析入口。只读取已经发生的业务数据；不会提交价格、审批或拦截订单。
 * 筛选以 URL 为唯一真值源，复制链接、刷新、前进后退都会重放同一范围。
 */
export default function PoolsPage() {
  const [sp, setSp] = useSearchParams();
  const rawRange = sp.get("range") || "90d";
  const range = (VALID_RANGES.has(rawRange) ? rawRange : "90d") as PoolAnalysisRange;
  const q = sp.get("q") || "";
  const page = readPage(sp.get("page"));
  const [searchText, setSearchText] = useState(q);
  const [data, setData] = useState<PoolAnalysisListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [reload, setReload] = useState(0);
  const requestSeq = useRef(0);

  useEffect(() => setSearchText(q), [q]);

  useEffect(() => {
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(false);
    fetchPoolAnalysisList({ range, q: q || undefined, page, page_size: 20 })
      .then((result) => { if (seq === requestSeq.current) setData(result); })
      .catch(() => { if (seq === requestSeq.current) setError(true); })
      .finally(() => { if (seq === requestSeq.current) setLoading(false); });
    return () => { requestSeq.current += 1; };
  }, [range, q, page, reload]);

  const patchUrl = (next: Record<string, string | number | undefined>) => {
    const merged = new URLSearchParams(sp);
    Object.entries(next).forEach(([key, value]) => {
      if (value == null || value === "") merged.delete(key);
      else merged.set(key, String(value));
    });
    setSp(merged, { replace: false });
  };

  const columns = useMemo<ColumnsType<PoolAnalysisListItem>>(() => [
    {
      title: "互通池",
      key: "pool",
      width: 210,
      fixed: "left",
      render: (_, row) => (
        <div style={{ display: "grid", gap: 4 }}>
          <Link
            to={`/pool-analysis/${row.group_id}?range=${range}`}
            aria-label={`查看${row.name}价格详情`}
            style={{ fontWeight: 600 }}
          >
            {row.name}
          </Link>
          {row.description && <span style={{ color: "var(--mb-text-3)", fontSize: 12 }}>{row.description}</span>}
        </div>
      ),
    },
    { title: "成员 PN", dataIndex: "member_count", width: 86, align: "right" },
    {
      title: "采购参考（统一未税）",
      key: "purchase",
      width: 220,
      render: (_, row) => <PriceSummary side={row.purchase_reference} kind="purchase" />,
    },
    {
      title: "销售参考（统一未税）",
      key: "sales",
      width: 220,
      render: (_, row) => <PriceSummary side={row.sales_reference} kind="sales" />,
    },
  ], [range]);

  return (
    <>
      <PageHeader
        title="互通池价格分析"
        subtitle="复盘已经发生的采购与销售价格；所有记录都保留，越过人工约束只做警示。"
      />
      <Card>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center", marginBottom: 14 }}>
          <Segmented
            aria-label="统计时间范围"
            options={RANGE_OPTIONS}
            value={range}
            onChange={(value) => patchUrl({ range: value === "90d" ? undefined : String(value), page: undefined })}
          />
          <Input.Search
            aria-label="搜索互通池"
            placeholder="池名 / 成员 PN / 描述"
            allowClear
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
            onSearch={(value) => patchUrl({ q: value.trim() || undefined, page: undefined })}
            style={{ width: "min(100%, 320px)" }}
          />
          <span style={{ color: "var(--mb-text-3)", fontSize: 12.5 }}>
            默认近 90 天 · 价格统一换算为未税口径
          </span>
        </div>

        {error ? (
          <Alert
            type="error"
            showIcon
            message="互通池价格加载失败"
            action={<Button size="small" onClick={() => setReload((value) => value + 1)}>重试</Button>}
          />
        ) : (
          <Table<PoolAnalysisListItem>
            rowKey="group_id"
            size="small"
            loading={loading}
            columns={columns}
            dataSource={data?.items ?? []}
            scroll={{ x: 760 }}
            locale={{ emptyText: "当前条件下没有互通池" }}
            pagination={{
              current: data?.page ?? page,
              pageSize: 20,
              total: data?.total ?? 0,
              showSizeChanger: false,
              showTotal: (total) => `共 ${total} 个池`,
              onChange: (next) => patchUrl({ page: next === 1 ? undefined : next }),
            }}
          />
        )}
      </Card>
    </>
  );
}
