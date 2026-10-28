/** 返件实收排行 Top 40（2026-10-22）：按项目 / 按登记人两个视角。
 *
 * - 按项目：后端 top_projects（返件实收降序前 40）。
 * - 按登记人：后端 registrants（created_by 聚合全量，上限 500）；默认展示前
 *   40，输入人名/账号检索时在全量里匹配（不受前 40 截断）。
 * - 行内比例条只在平面数量可用时绘制；混合单位按单位分行，不跨单位求和。
 * - 点击项目 / 登记人 → 返件明细 Drawer（原筛选 ∩ 点击维度，DL-022）。
 */
import { useMemo, useState } from "react";
import { Button, Input, Segmented, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import type {
  DashboardFilters,
  RegistrantRow,
  SummaryResponse,
} from "../../../api/maintenanceDashboard";
import type { ReceiptsDrill } from "./ReceiptsDrillDrawer";
import {
  QtyBreakdownCell,
  drillByProject,
  drillByRegistrant,
  qtyNumber,
  receiptSourceText,
  registrantLabel,
  registrantText,
} from "./dashboardShared";

export const RANKING_TOP_N = 40;

type ProjectRow = SummaryResponse["top_projects"][number];
type RankingMode = "project" | "registrant";

function RankBadge({ rank }: { rank: number }) {
  return <span className={`mdb-rank${rank <= 3 ? " is-top" : ""}`}>{rank}</span>;
}

/** 数量 + 行内比例条（max ≤ 0 或混合单位不画条）。 */
function BarCell<T extends { unit: string | null }>({ flat, max, breakdown, pick }: {
  flat: string | null;
  max: number;
  breakdown?: T[] | null;
  pick: (entry: T) => string | null | undefined;
}) {
  const value = qtyNumber(flat);
  return (
    <div className="mdb-barcell">
      <span className="mdb-barcell-value">
        <QtyBreakdownCell flat={flat} breakdown={breakdown} pick={pick} />
      </span>
      {value !== null && max > 0 ? (
        <span className="mdb-barcell-track" aria-hidden>
          <span
            className="mdb-barcell-fill"
            style={{ display: "block", width: `${Math.max(0, Math.min(100, (value / max) * 100))}%` }}
          />
        </span>
      ) : null}
    </div>
  );
}

function shortDateTime(value: string | null): string {
  return value ? value.slice(0, 10) : "—";
}

export default function ReceiptRankingPanel({
  summary, loading, params, onReceiptsDrill,
}: {
  summary: SummaryResponse | null;
  loading: boolean;
  params: DashboardFilters;
  onReceiptsDrill?: (drill: ReceiptsDrill) => void;
}) {
  const [mode, setMode] = useState<RankingMode>("project");
  const [keyword, setKeyword] = useState("");
  const term = keyword.trim().toLowerCase();

  const projects = useMemo(() => {
    const rows = summary?.top_projects ?? [];
    if (!term) return rows;
    return rows.filter((row) => `${row.display_name} ${row.project_code ?? ""}`
      .toLowerCase().includes(term));
  }, [summary, term]);

  const allRegistrants = summary?.registrants ?? [];
  const registrants = useMemo(() => {
    if (!term) return allRegistrants.slice(0, RANKING_TOP_N);
    return allRegistrants.filter((row) => `${registrantLabel(row.created_by, row.created_by_name)} ${row.created_by}`
      .toLowerCase().includes(term));
  }, [allRegistrants, term]);

  const projectMax = Math.max(0, ...(summary?.top_projects ?? [])
    .map((row) => qtyNumber(row.receipt_qty) ?? 0));
  const registrantMax = Math.max(0, ...allRegistrants
    .map((row) => qtyNumber(row.receipt_qty) ?? 0));

  // 名次按完整排行计（检索后仍显示原名次，不重新编号）。
  const projectRank = useMemo(() => new Map(
    (summary?.top_projects ?? []).map((row, index) => [row.project_id, index + 1]),
  ), [summary]);
  const registrantRank = useMemo(() => new Map(
    allRegistrants.map((row, index) => [row.created_by, index + 1]),
  ), [allRegistrants]);

  const projectColumns: ColumnsType<ProjectRow> = [
    { title: "#", key: "rank", width: 52, align: "center",
      render: (_: unknown, row) => <RankBadge rank={projectRank.get(row.project_id) ?? 0} /> },
    {
      title: "项目", dataIndex: "display_name", ellipsis: true,
      render: (v: string, row) => (onReceiptsDrill ? (
        <Button
          type="link" size="small" style={{ padding: 0, height: "auto" }}
          aria-label={`查看 ${v} 返件明细`}
          onClick={() => onReceiptsDrill({
            title: v,
            params: drillByProject(params, row.project_id),
          })}
        >
          {v}
        </Button>
      ) : v),
    },
    { title: "返件实收", dataIndex: "receipt_qty", width: 180,
      render: (v: string | null, row) => (
        <BarCell flat={v} max={projectMax} breakdown={row.unit_breakdown} pick={(e) => e.receipt_qty} />
      ) },
    { title: "坏件实收", dataIndex: "bad_receipt_qty", align: "right", width: 110,
      render: (v: string | null, row) => (
        <QtyBreakdownCell flat={v} breakdown={row.unit_breakdown} pick={(e) => e.bad_receipt_qty} />
      ) },
    { title: "实际领用", dataIndex: "issued_qty", align: "right", width: 110,
      render: (v: string | null, row) => (
        <QtyBreakdownCell flat={v} breakdown={row.unit_breakdown} pick={(e) => e.issued_qty} />
      ) },
  ];

  const registrantColumns: ColumnsType<RegistrantRow> = [
    { title: "#", key: "rank", width: 52, align: "center",
      render: (_: unknown, row) => <RankBadge rank={registrantRank.get(row.created_by) ?? 0} /> },
    {
      title: "登记人", dataIndex: "created_by", ellipsis: true,
      render: (v: string, row) => {
        const label = registrantLabel(v, row.created_by_name);
        const body = (
          <span className="mdb-person">
            <span>{label}</span>
            {label !== v ? <small>{v}</small> : null}
          </span>
        );
        return onReceiptsDrill ? (
          <Button
            type="link" size="small" style={{ padding: 0, height: "auto", textAlign: "left" }}
            aria-label={`查看 ${label} 登记的返件明细`}
            onClick={() => onReceiptsDrill({
              title: `登记人 ${registrantText(v, row.created_by_name)}`,
              params: drillByRegistrant(params, v),
            })}
          >
            {body}
          </Button>
        ) : body;
      },
    },
    { title: "来源", dataIndex: "sources", width: 150,
      render: (sources: string[]) => sources.map((source) => (
        <Tag key={source} color={source === "manual" ? "processing" : "default"}>
          {receiptSourceText(source)}
        </Tag>
      )) },
    { title: "返件实收", dataIndex: "receipt_qty", width: 180,
      render: (v: string | null, row) => (
        <BarCell flat={v} max={registrantMax} breakdown={row.unit_breakdown} pick={(e) => e.receipt_qty} />
      ) },
    { title: "坏件实收", dataIndex: "bad_receipt_qty", align: "right", width: 100,
      render: (v: string | null, row) => (
        <QtyBreakdownCell flat={v} breakdown={row.unit_breakdown} pick={(e) => e.bad_receipt_qty} />
      ) },
    { title: "登记行数", dataIndex: "line_count", align: "right", width: 90 },
    { title: "涉及项目", dataIndex: "project_count", align: "right", width: 90 },
    { title: "最近业务日", dataIndex: "last_occurred_at", width: 110,
      render: (v: string | null) => shortDateTime(v) },
  ];

  const registrantTotal = allRegistrants.length;

  return (
    <section className="mdb-panel" data-testid="receipt-ranking">
      <div className="mdb-panel-head">
        <h3 className="mdb-panel-title">
          返件实收排行
          <small>
            {mode === "project"
              ? `Top ${RANKING_TOP_N} 项目`
              : `Top ${RANKING_TOP_N} 登记人 · 共 ${registrantTotal} 人`}
          </small>
        </h3>
        <div className="mdb-panel-tools">
          <Segmented<RankingMode>
            aria-label="排行维度"
            value={mode}
            onChange={(value) => { setMode(value); setKeyword(""); }}
            options={[
              { label: "按项目", value: "project" },
              { label: "按登记人", value: "registrant" },
            ]}
          />
          <Input.Search
            allowClear
            aria-label={mode === "project" ? "按项目名检索" : "按人名检索"}
            placeholder={mode === "project" ? "检索项目名 / 编码" : "检索人名 / 账号"}
            style={{ width: 200 }}
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
          />
        </div>
      </div>
      {mode === "project" ? (
        <Table<ProjectRow>
          rowKey="project_id"
          size="small"
          loading={loading}
          dataSource={projects}
          columns={projectColumns}
          scroll={{ x: 640 }}
          pagination={false}
          locale={{ emptyText: term ? "没有匹配的项目" : "当前窗口没有返件数据" }}
        />
      ) : (
        <Table<RegistrantRow>
          rowKey="created_by"
          size="small"
          loading={loading}
          dataSource={registrants}
          columns={registrantColumns}
          scroll={{ x: 900 }}
          pagination={false}
          locale={{ emptyText: term ? "没有匹配的登记人" : "当前窗口没有返件数据" }}
        />
      )}
      <div className="mdb-panel-foot">
        {mode === "project"
          ? "按返件实收降序；点击项目名查看该项目返件明细（与当前筛选求交）。"
          : (
            <>
              登记人 = 返件行的登记账号（手工登记为登记人；RKD 导入未记录操作人，统一显示为「导入（未记录操作人）」）；
              检索在全部登记人中匹配，不受前 {RANKING_TOP_N} 限制；点击姓名查看其登记的返件明细。
              {summary?.registrants_truncated ? (
                <Tooltip title="登记人超过 500 人，仅返回返件实收最多的 500 人">
                  <Tag color="warning" style={{ marginLeft: 6 }}>已截断</Tag>
                </Tooltip>
              ) : null}
            </>
          )}
      </div>
    </section>
  );
}
