import { Card, Space, Tag, Tooltip, Typography } from "antd";
import type { BoardHealth, SourceHealth } from "../../../api/maintenanceBossBoard";

const { Text } = Typography;

/**
 * 四源健康条（plan v1.3 §5.1 首屏第一段）。
 *
 * 事实源分离（铁律 5）：实发=发货单、未用件收回=返库单(成品)、
 * 坏件回收=入库单(返件类)；各源独立 readiness，未导入显示「尚未导入」绝不显示 0。
 */
const ORDER: Array<keyof BoardHealth["sources"]> = [
  "wbdd",
  "ckd",
  "return_order",
  "rkd_inbound",
];

const STATE_STYLE: Record<string, { color: string; text: string }> = {
  ready: { color: "green", text: "已接入" },
  partial: { color: "gold", text: "部分关联" },
  stale: { color: "orange", text: "数据偏旧" },
  not_imported: { color: "purple", text: "尚未导入" },
};

function SourceTag({ source }: { source: SourceHealth }) {
  const style = STATE_STYLE[source.readiness] ?? STATE_STYLE.not_imported;
  const detail =
    source.readiness === "not_imported"
      ? "该来源尚未导入，相关数字不显示（不以 0 代替）"
      : `截至 ${source.as_of ?? "—"}${
          source.unlinked_rows ? ` · ${source.unlinked_rows} 行未关联` : ""
        }`;
  return (
    <Tooltip title={detail}>
      <Tag color={style.color} data-testid={`source-${source.label}`}>
        {source.label}：{style.text}
        {source.as_of ? ` · ${source.as_of}` : ""}
      </Tag>
    </Tooltip>
  );
}

export function SourceHealthBar({ health }: { health?: BoardHealth | null }) {
  if (!health) return null;
  return (
    <Card size="small" title="来源健康" data-testid="source-health-bar">
      <Space wrap>
        {ORDER.map((key) => (
          <SourceTag key={key} source={health.sources[key]} />
        ))}
      </Space>
      <div style={{ marginTop: 8 }}>
        <Text type="secondary" style={{ fontSize: 11.5 }}>
          超过 {health.stale_days} 天未更新的来源标记为「数据偏旧」；未导入来源的数字一律留空。
        </Text>
      </div>
    </Card>
  );
}

export default SourceHealthBar;
