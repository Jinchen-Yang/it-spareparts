// 汇总层：KPI 卡片（三口径按选中口径并排）+ 分组表（点行 → 下钻订单列表）。
import { Table, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { fmt, fmtMoney, KpiCard } from "../shared";
import { pct } from "../../../utils/format";
import type {
  PurchaseAnalyticsGroup,
  PurchaseAnalyticsSummary,
} from "../../../api/purchaseAnalytics";
import { GROUP_BY_OPTIONS, type AmountBasis } from "./urlState";

export interface SummaryViewProps {
  summary: PurchaseAnalyticsSummary | null;
  loading: boolean;
  basis: AmountBasis[];
  groupBy: string;
  onDrill: (key: string) => void;
}

const AMOUNT_FIELD: Record<AmountBasis, "amount_inc" | "amount_ex" | "amount_paid"> = {
  inc: "amount_inc",
  ex: "amount_ex",
  paid: "amount_paid",
};

const AMOUNT_TITLE: Record<AmountBasis, string> = {
  inc: "含税金额",
  ex: "不含税金额",
  paid: "实付金额",
};

export function groupLabelOf(group: PurchaseAnalyticsGroup): string {
  // customer 维度：label 为 null，客户名在登记过的脱敏键 customer 上
  return group.customer ?? group.label ?? group.key;
}

export function groupByTitle(groupBy: string): string {
  return GROUP_BY_OPTIONS.find((o) => o.value === groupBy)?.label ?? groupBy;
}

/** 名义占比角标（D-39 名义侧裁定，2026-10-18）：按侧分开——未标税率单据里，
 * 非含税单的推算含税占含税合计（inc），含税单的推算不含税占不含税合计（ex）。 */
function NominalShareNote({ side, share }: { side: "inc" | "ex"; share: number | null }) {
  if (share == null) return null;
  const inc = side === "inc";
  const title = inc
    ? "未标税率（或税率为 0）的非含税采购单，其含税金额按 13% 从不含税额推算（名义），不是票面金额。"
    : "未标税率（或税率为 0）的含税采购单，其不含税金额按 13% 从录入的含税额推算（名义）；录入的含税额本身是真实金额。";
  return (
    <span>
      名义{inc ? "含税" : "不含税"}占比 {pct(share)}
      <Tooltip title={title}>
        <span aria-label={inc ? "名义含税说明" : "名义不含税说明"} style={{ cursor: "help", borderBottom: "1px dotted", marginLeft: 4 }}>
          ?
        </span>
      </Tooltip>
    </span>
  );
}

export default function SummaryView({ summary, loading, basis, groupBy, onDrill }: SummaryViewProps) {
  const kpi = summary?.kpi;
  const columns: ColumnsType<PurchaseAnalyticsGroup> = [
    {
      title: groupByTitle(groupBy),
      dataIndex: "key",
      render: (_v, r) => groupLabelOf(r),
    },
    { title: "订单数", dataIndex: "orders", width: 90, align: "right", render: (v) => fmt(v) },
    { title: "明细行数", dataIndex: "lines", width: 90, align: "right", render: (v) => fmt(v) },
    ...basis.map((b) => ({
      title:
        b === "inc" ? (
          <Tooltip title="未标税率（或 0 税率）的非含税单据，其含税额按 13% 推算（名义）">{AMOUNT_TITLE[b]}</Tooltip>
        ) : b === "ex" ? (
          <Tooltip title="未标税率（或 0 税率）的含税单据，其不含税额按 13% 推算（名义）">{AMOUNT_TITLE[b]}</Tooltip>
        ) : (
          AMOUNT_TITLE[b]
        ),
      key: AMOUNT_FIELD[b],
      width: 130,
      align: "right" as const,
      render: (_v: unknown, r: PurchaseAnalyticsGroup) => fmtMoney(r[AMOUNT_FIELD[b]]),
    })),
  ];

  return (
    <div>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 14 }}>
        <KpiCard label="采购单数" value={kpi ? String(kpi.orders) : "—"} sub="已生效" />
        <KpiCard label="明细行数" value={kpi ? String(kpi.lines) : "—"} sub="单头所属批次的行" />
        {basis.includes("inc") && (
          <KpiCard
            label="含税金额"
            value={fmtMoney(kpi?.amount_inc ?? null)}
            sub={<NominalShareNote side="inc" share={kpi?.nominal_inc_share ?? null} />}
          />
        )}
        {basis.includes("ex") && (
          <KpiCard
            label="不含税金额"
            value={fmtMoney(kpi?.amount_ex ?? null)}
            sub={<NominalShareNote side="ex" share={kpi?.nominal_ex_share ?? null} />}
          />
        )}
        {basis.includes("paid") && (
          <KpiCard label="实付金额" value={fmtMoney(kpi?.amount_paid ?? null)} sub="原列「采购金额」" />
        )}
      </div>
      <Table<PurchaseAnalyticsGroup>
        size="small"
        rowKey="key"
        loading={loading}
        dataSource={summary?.groups ?? []}
        columns={columns}
        pagination={false}
        locale={{ emptyText: "当前筛选下没有采购数据" }}
        onRow={(r) => ({
          style: { cursor: "pointer" },
          onClick: () => onDrill(r.key),
          // 键盘可达：分组行可聚焦，Enter/Space 触发下钻（不覆写 role=button——会顶掉 tr 的行语义）
          tabIndex: 0,
          "aria-label": `查看分组 ${groupLabelOf(r)} 的订单列表`,
          onKeyDown: (e: React.KeyboardEvent) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onDrill(r.key);
            }
          },
        })}
      />
    </div>
  );
}
