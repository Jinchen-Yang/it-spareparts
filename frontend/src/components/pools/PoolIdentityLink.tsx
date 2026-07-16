import { Tag } from "antd";
import { Link } from "react-router-dom";

export default function PoolIdentityLink({
  groupId,
  name,
  pn,
  range = "90d",
}: {
  groupId: number | null | undefined;
  name: string | null | undefined;
  pn?: string | null;
  range?: string;
}) {
  if (!groupId || !name) return null;
  const qs = new URLSearchParams({ range });
  if (pn) qs.set("pn", pn);
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
