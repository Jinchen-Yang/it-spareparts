import { Badge, Space } from "antd";
import { useWorkBadge } from "./useWorkBadge";

export default function PresalesWorkLabel({ label = "报价工作台" }: { label?: string }) {
  const counts = useWorkBadge();
  return <Space>{label}<Badge count={counts.assigned + counts.claimed + counts.pool} /></Space>;
}
