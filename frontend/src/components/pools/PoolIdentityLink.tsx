import { Tag } from "antd";
import { Link } from "react-router-dom";
import type { PoolAnalysisSide } from "../../api/poolAnalysis";

export default function PoolIdentityLink({
  groupId,
  name,
  pn,
  range = "90d",
  dateFrom,
  dateTo,
  side,
}: {
  groupId: number | null | undefined;
  name: string | null | undefined;
  pn?: string | null;
  range?: string;
  dateFrom?: string | null;
  dateTo?: string | null;
  side?: PoolAnalysisSide;
}) {
  if (!groupId || !name) return null;
  const qs = new URLSearchParams({ range });
  if (range === "custom" && dateFrom && dateTo) {
    qs.set("from", dateFrom);
    qs.set("to", dateTo);
  }
  if (pn) qs.set("pn", pn);
  if (side) qs.set("side", side);
  return (
    <Link
      to={`/pool-analysis/${groupId}?${qs.toString()}`}
      aria-label={`查看互通池 ${name}`}
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => event.stopPropagation()}
    >
      <Tag color="geekblue" style={{ marginInlineStart: 6 }}>互通池：{name}</Tag>
    </Link>
  );
}
