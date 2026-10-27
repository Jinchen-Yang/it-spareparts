import { Badge, Space } from "antd";
import { useWorkBadge } from "./useWorkBadge";

export default function PresalesWorkLabel() {
  const counts = useWorkBadge();
  return <Space>报价工作台<Badge count={counts.assigned + counts.claimed + counts.pool} /></Space>;
}
