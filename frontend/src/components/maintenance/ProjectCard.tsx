import { Link } from "react-router-dom";
import { Button, Card, Progress, Space, Tag, Typography } from "antd";
import type {
  BoardProjectRow,
  CardStatus,
  KnownCostStat,
  Stat,
} from "../../api/maintenanceBossBoard";
import { UNASSIGNED_BUCKET } from "../../api/maintenanceBossBoard";
import { qty } from "../../utils/format";

const { Text, Title } = Typography;

/** 三态语义（#35/#43）：正常=绿、提醒=黄（成本≥80% 合同额）、报警=红（>100%）。 */
const STATUS_META: Record<CardStatus, { color: string; label: string }> = {
  normal: { color: "#52c41a", label: "正常" },
  warning: { color: "#faad14", label: "提醒" },
  alert: { color: "#ff4d4f", label: "报警" },
};

/**
 * 数值渲染唯一出口：**任何非 ready 状态都不渲染 0 或数字**（铁律 5）。
 * 「未导入」「无权限」「算不出」各说各的，绝不合并成一个空字符串或 0。
 */
function statText(stat: Stat<string | number> | undefined, unit = ""): string {
  if (!stat) return "—";
  switch (stat.state) {
    case "ready":
    case "partial":
    case "stale":
      return stat.value === null || stat.value === "" ? "—" : `${stat.value}${unit}`;
    case "not_imported":
      return "尚未导入";
    case "restricted":
      return "无权限";
    default:
      return "暂不可用";
  }
}

function money(stat: Stat<string | number> | undefined): string {
  return statText(stat, " 元");
}

/** 金额 Stat → 数字（ready/partial/state 且有值时）；其余状态返回 null。 */
function statNumber(stat: Stat<string | number> | undefined): number | null {
  if (!stat) return null;
  if (!["ready", "partial", "stale"].includes(stat.state)) return null;
  if (stat.value === null || stat.value === "") return null;
  const n = Number(stat.value);
  return Number.isFinite(n) ? n : null;
}

/** 数量位（2026-08-21 客户反馈）：千分位 + 「个」，不展示后端 Decimal 的 ".000"。 */
function statQty(stat: Stat<string | number> | undefined): string {
  if (!stat) return "—";
  switch (stat.state) {
    case "ready":
    case "partial":
    case "stale":
      if (stat.value === null || stat.value === "") return "—";
      return `${qty(Number(stat.value))} 个`;
    case "not_imported":
      return "尚未导入";
    case "restricted":
      return "无权限";
    default:
      return "暂不可用";
  }
}

/** 成本五件套：partial/stale 仍可展示已知下限；阻塞态绝不落回 0。 */
function fromCostBundle(
  stat: KnownCostStat | undefined,
  pick: (value: NonNullable<KnownCostStat["value"]>) => string,
): string {
  if (!stat) return "—";
  if (!["ready", "partial", "stale"].includes(stat.state) || stat.value === null) {
    return statText({ state: stat.state, value: null, as_of: stat.as_of });
  }
  return pick(stat.value);
}

const costAmount = (stat: KnownCostStat | undefined) =>
  fromCostBundle(stat, (value) => {
    if (value.known_amount == null
        || (value.quality === "incomplete"
        && value.missing_lines > 0
        && Number(value.coverage_pct ?? 0) === 0)) {
      return "暂无可计算成本";
    }
    return `${value.known_amount} 元${value.quality === "incomplete" ? "（已知下限）" : ""}`;
  });

const missingLines = (stat: KnownCostStat | undefined) =>
  fromCostBundle(stat, (value) => value.known_amount == null
    ? "暂无有效需求明细"
    : `${value.missing_lines} 行无参照价`);

/** 回款进度条（2026-08-22 客户反馈）：已回款/合同额，卡片直读。
 * 六态语义不破坏：无权限/未导入各说各的，算不出不画 0%。 */
function CollectionBar({
  collected,
  contract,
}: {
  collected: Stat<string | number> | undefined;
  contract: Stat<string | number> | undefined;
}) {
  const collectedNum = statNumber(collected);
  // 回款率的分母必须是完整、当前的合同事实。partial/stale 即使携带数值，
  // 也只能作为已知小计/过期参考展示，不能参与百分比计算。
  const contractNum = contract?.state === "ready" ? statNumber(contract) : null;
  const receivableNum = collected?.state === "ready"
    && contractNum !== null
    && collectedNum !== null
    ? contractNum - collectedNum
    : null;
  if (collectedNum === null) {
    return (
      <div style={{ color: "rgba(0,0,0,.45)", fontSize: 11.5 }}>
        回款：{statText(collected)}
      </div>
    );
  }
  const pct = contractNum && contractNum > 0
    ? Math.round((collectedNum / contractNum) * 100)
    : null;
  let contractDetail: string;
  if (contract?.state === "partial") {
    const knownSubtotal = statNumber(contract);
    contractDetail = knownSubtotal === null
      ? "合同事实不完整（暂无已知小计）"
      : `已知小计 ¥${knownSubtotal.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}（合同事实不完整）`;
  } else if (contract?.state === "stale") {
    contractDetail = "合同额数据已过期";
  } else if (contractNum !== null) {
    contractDetail = contractNum > 0
      ? `¥${contractNum.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}（${pct}%）`
      : "合同额 ¥0（真实零，无法计算比例）";
  } else {
    contractDetail = `合同额${statText(contract)}`;
  }
  return (
    <div style={{ marginTop: 2 }}>
      <div style={{ fontSize: 11.5, color: "rgba(0,0,0,.55)", marginBottom: 2 }}>
        回款：
        <span style={{ color: "#52c41a", fontWeight: 600 }}>
          ¥{collectedNum.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}
        </span>
        <span>{" / "}{contractDetail}</span>
      </div>
      {receivableNum !== null ? (
        <div style={{ fontSize: 11.5, color: "rgba(0,0,0,.55)", marginBottom: 2 }}>
          应收：
          <span style={{ color: receivableNum < 0 ? "#fa8c16" : "#1677ff", fontWeight: 600 }}>
            {receivableNum < 0 ? "-" : ""}¥{Math.abs(receivableNum).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}
          </span>
          {receivableNum < 0 ? "（超额回款）" : ""}
        </div>
      ) : null}
      {pct !== null && (
        <Progress
          percent={Math.min(pct, 100)}
          showInfo={false}
          size="small"
          strokeColor={pct >= 100 ? "#52c41a" : "#1677ff"}
        />
      )}
    </div>
  );
}

export interface ProjectCardProps {
  row: BoardProjectRow;
}

export function ProjectCard({ row }: ProjectCardProps) {
  const isBucket = row.project_id === UNASSIGNED_BUCKET;
  const status = row.card_status ? STATUS_META[row.card_status] : null;
  const displayNames = Array.from(new Set([row.display_name, ...(row.peer_names ?? [])]));
  const displayNameKeys = new Set(displayNames);
  const secondaryNames = Array.from(new Set(
    (row.aliases ?? []).filter((name) => !displayNameKeys.has(name)),
  ));
  const ratioRaw = row.cost_ratio_pct;
  const ratio =
    ratioRaw?.state === "ready" && ratioRaw.value !== null
      ? Number(ratioRaw.value)
      : null;
  const ratioIsLowerBound = row.known_apply_cost_inc_tax.value?.quality === "incomplete"
    && Number(row.known_apply_cost_inc_tax.value?.coverage_pct ?? 0) > 0;

  return (
    <Card
      size="small"
      data-testid={`project-card-${row.project_id}`}
      styles={{ body: { padding: 14 } }}
      style={{ height: "100%", borderTop: `3px solid ${status?.color ?? "#d9d9d9"}` }}
    >
      <Space direction="vertical" size={6} style={{ width: "100%" }}>
        <div data-testid="maintenance-project-names">
          {displayNames.map((name) => (
            <Title key={name} level={5} style={{ margin: 0 }} ellipsis={{ tooltip: name }}>
              {name}
            </Title>
          ))}
        </div>
        {secondaryNames.length ? (
          <Text
            type="secondary"
            style={{ fontSize: 12 }}
            data-testid="maintenance-project-aliases"
          >
            其他名称：{secondaryNames.join("、")}
          </Text>
        ) : null}
        <Space size={4} wrap>
          {status ? <Tag color={status.color}>{status.label}</Tag> : null}
          {/* R5：台账没给项目周期时以明确状态示人，不让卡片看起来「什么都没说」 */}
          {!isBucket && row.lifecycle === "missing" ? <Tag>期限缺失</Tag> : null}
          {row.is_archived ? <Tag>已归档</Tag> : null}
        </Space>

        <Text type="secondary" style={{ fontSize: 12 }}>
          {row.contract_nos.length ? row.contract_nos.join("、") : "无合同号"}
        </Text>
        <Space size={12} wrap style={{ fontSize: 12 }}>
          {/* 2026-08-21 客户反馈：卡片显示销售名称，不再显示项目经理 */}
          <Text>销售：{row.salesperson || "—"}</Text>
          <Text>
            合同总额（含税）：{money(row.contract_amount_inc_tax)}
            {/* #51 诚实标注：XSDD 回退层的共用单/缺单，金额仅参考 */}
            {row.contract_shared ? "（共用单）" : ""}
            {row.contract_incomplete ? "（不完整）" : ""}
          </Text>
        </Space>
        {!isBucket ? (
          <Text type="secondary" style={{ fontSize: 12 }} data-testid="maintenance-period">
            维保期限：{row.period_from ?? "起始待补"} ～ {row.period_to ?? "终止待补"}
          </Text>
        ) : null}

        {/* 2026-08-22 客户反馈：成本/回款要上卡且用彩色，不再灰字小号 */}
        <div style={{ fontSize: 12.5, lineHeight: 2 }}>
          <div>
            <span style={{ color: "#1677ff" }}>
              备件成本 {costAmount(row.known_apply_cost_inc_tax)}
            </span>
            <span style={{ color: "rgba(0,0,0,.45)", fontSize: 11.5 }}>
              {" "}（缺失 {missingLines(row.known_apply_cost_inc_tax)}）
            </span>
          </div>
          <div>
            <span style={{ color: "#fa8c16" }}>
              报销成本 {money(row.expense_cost_inc_tax)}
            </span>
          </div>
          <div>
            <span style={{ color: "#722ed1" }}>
              已领用成本 {money(row.requisition_cost_inc_tax)}
            </span>
          </div>
          <div>
            <span style={{ color: "#13c2c2" }}>
              维保备件发货数 {statQty(row.procured_qty)}
            </span>
          </div>
          <CollectionBar
            collected={row.collection_preview_inc_tax}
            contract={row.contract_amount_inc_tax}
          />
        </div>

        {ratio === null ? (
          <Text type="secondary" style={{ fontSize: 11.5 }} data-testid="ratio-unknown">
            {/* 铁律 5：算不出来就说算不出来，不画一条 0% 的绿条 */}
            成本率：数据不足（缺合同额或成本）
          </Text>
        ) : (
          <Progress
            percent={Math.min(ratio, 100)}
            strokeColor={status?.color}
            size="small"
            format={() => `${ratio}%${ratioIsLowerBound ? "（已知下限）" : ""}`}
          />
        )}

        {isBucket ? (
          <Text type="secondary" style={{ fontSize: 11.5 }}>
            未归属单据：需在项目面板确认挂靠
          </Text>
        ) : (
          <Link to={`/maintenance/projects/${encodeURIComponent(row.project_id)}`}>
            <Button type="primary" size="small" block>
              进入面板
            </Button>
          </Link>
        )}
      </Space>
    </Card>
  );
}

export default ProjectCard;
