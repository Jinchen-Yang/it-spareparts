import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Card, Empty, Table, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";

import {
  getProjectProcurement,
  type ProcurementOrder,
  type ProcurementLine,
} from "../../api/maintenanceProjectProcurement";
import { TERM } from "./maintenanceLanguage";
import { useLatestRead } from "../../pages/maintenance/panel/panelUtils";

const { Text } = Typography;

const PAGE_SIZE = 10;

/**
 * 单价三态（#259）：脱敏置空与源表本来没价在接口上都是 null，
 * 只能靠 unit_price_masked 区分——「无权查看」不是「缺失」。
 */
export function unitPriceText(line: Pick<ProcurementLine, "unit_price" | "unit_price_masked">) {
  if (line.unit_price_masked) return "无权查看";
  if (line.unit_price == null || line.unit_price === "") return "—（缺失）";
  return `¥${Number(line.unit_price).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

const orderColumns: ColumnsType<ProcurementOrder> = [
  {
    title: "采购单号",
    dataIndex: "purchase_order_no",
    width: 150,
  },
  {
    title: "采购日期",
    dataIndex: "purchase_date",
    width: 110,
    render: (value: string | null) => value || "—",
  },
  {
    title: "采购人",
    dataIndex: "purchaser",
    width: 100,
    render: (value: string | null) => value || "—",
  },
  {
    title: "关联维保单",
    dataIndex: "demand_order_no",
    width: 150,
    render: (value: string | null) => value || "—",
  },
  {
    title: "维保日期",
    dataIndex: "demand_date",
    width: 110,
    render: (value: string | null) => value || "—",
  },
  {
    title: "备件明细",
    dataIndex: "line_count",
    width: 90,
    align: "right",
    render: (count: number) => `${count} 行`,
  },
];

const lineColumns: ColumnsType<ProcurementLine> = [
  { title: TERM.pn, dataIndex: "pn", width: 150, render: (v: string | null) => v || "—" },
  {
    title: "描述",
    dataIndex: "description",
    width: 200,
    render: (v: string | null) => v || "—",
  },
  { title: "数量", dataIndex: "qty", width: 80, align: "right" },
  {
    title: `单价（${TERM.exTax}）`,
    dataIndex: "unit_price",
    width: 120,
    align: "right",
    render: (_: unknown, line) => unitPriceText(line),
  },
];

/**
 * 项目采购链（采购单 → 维保需求单，只认稳定归属）。挂在「备件与需求单」tab 第三段；
 * 加载 / 空 / 错误 / 重试全部自持，任何一段失败都不影响需求单与 PN 明细两段（#259）。
 * 读回函数通过 registerRefresh 交给所在 tab：落库后的读回屏障要把采购段一起算进去——
 * 03 回传改派需求单、概览挂靠都会改采购链唯一的输入（需求单归属）。
 */
export default function ProjectProcurementPanel({
  projectId,
  sourceOrderId = null,
  sourceOrderNo = null,
  registerRefresh,
}: {
  projectId: string;
  /** 选中需求单的 raw id：给定即只看挂在这张单上的采购单。 */
  sourceOrderId?: string | null;
  /** 仅用于文案：选中需求单的单号。 */
  sourceOrderNo?: string | null;
  /** 向所在 tab 登记「按当前范围重读」（resolve false＝这次读回没完成）；卸载时登记 null。 */
  registerRefresh?: (refresh: (() => Promise<boolean>) | null) => void;
}) {
  const [orders, setOrders] = useState<ProcurementOrder[]>([]);
  const [total, setTotal] = useState(0);
  // 页码与所属范围绑在一起：范围（项目 / 选中需求单）一换，页码自然回到 1，
  // 不需要额外 effect 去“重置”，也就不会多打一次旧范围的请求。
  const scope = `${projectId}|${sourceOrderId ?? ""}`;
  const [paging, setPaging] = useState({ scope, page: 1 });
  const page = paging.scope === scope ? paging.page : 1;
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const read = useLatestRead();

  const load = useCallback(() => read(async (isCurrent) => {
    setLoading(true);
    setError(false);
    try {
      const { data } = await getProjectProcurement(projectId, {
        page,
        page_size: PAGE_SIZE,
        source_order_id: sourceOrderId ?? undefined,
      });
      if (!isCurrent()) return false;
      setOrders(data.purchases ?? []);
      setTotal(data.total ?? data.purchases?.length ?? 0);
      return true;
    } catch {
      if (isCurrent()) {
        // 失败段自己清旧快照（屏障约定），告警替代表格
        setOrders([]);
        setTotal(0);
        setError(true);
      }
      return false;
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }), [projectId, sourceOrderId, page, read]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    registerRefresh?.(load);
    return () => { registerRefresh?.(null); };
  }, [load, registerRefresh]);

  return (
    <Card size="small" title="采购订单与维保需求">
      {/* 范围提示放卡体而不是 extra：375px 下长 extra 会把标题挤成省略号 */}
      {sourceOrderNo ? (
        <Text type="secondary" style={{ display: "block", fontSize: 12, marginBottom: 8 }}>
          当前范围：需求单 {sourceOrderNo}（再点单号取消）
        </Text>
      ) : null}
      {error ? (
        <Alert
          type="warning"
          showIcon
          message="采购订单关联数据加载失败"
          description="采购订单关联数据暂时不可用；需求单与备件明细不受影响。"
          action={(
            <Button size="small" onClick={() => { void load(); }}>
              重试
            </Button>
          )}
        />
      ) : (
        <Table
          rowKey="purchase_order_no"
          size="small"
          loading={loading}
          columns={orderColumns}
          dataSource={orders}
          locale={{
            emptyText: (
              <Empty
                description={sourceOrderNo
                  ? `需求单 ${sourceOrderNo} 尚未找到关联采购订单`
                  : "尚未找到关联采购订单"}
              />
            ),
          }}
          expandable={{
            expandedRowRender: (order) => (
              <Table
                rowKey={(_, i) => `${order.purchase_order_no}-line-${i}`}
                size="small"
                columns={lineColumns}
                dataSource={order.lines}
                pagination={false}
              />
            ),
            rowExpandable: (order) => order.line_count > 0,
          }}
          pagination={{
            current: page,
            pageSize: PAGE_SIZE,
            total,
            showSizeChanger: false,
            onChange: (next) => setPaging({ scope, page: next }),
          }}
        />
      )}
      <div style={{ marginTop: 8, fontSize: 12, color: "var(--mb-text-3)" }}>
        数据来源于采购订单与维保需求单的只读关联，不会自动猜测或创建关联。
      </div>
    </Card>
  );
}
