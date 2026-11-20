import { useState } from "react";
import { Button, Card, Drawer, Empty } from "antd";
import type { RepairOrder } from "../../../api/inspectionRepair";
import { orderStage, pendingQty, stages, type Stage } from "./workOrders";
import "./workOrderAnalytics.css";

type Selection = { stage: Stage } | { pn: string };
interface PartSummary {
  pn: string;
  description: string;
  lines: number;
  qty: number;
  fixed: number;
  unfixed: number;
}

export default function WorkOrderAnalytics({
  orders, onOpen, pendingOnly = false,
}: {
  orders: RepairOrder[];
  onOpen: (id: string) => void;
  pendingOnly?: boolean;
}) {
  const [selection, setSelection] = useState<Selection | null>(null);
  const counts: Record<Stage, number> = { 待领取: 0, 维修中: 0, 待定去向: 0, 已完成: 0, 已退回主管: 0 };
  const byPart = new Map<string, PartSummary>();
  let total = 0, fixed = 0, unfixed = 0;
  for (const order of orders) {
    if (!pendingOnly) counts[orderStage(order)] += 1;
    for (const line of order.lines) {
      const lineFixed = pendingOnly ? pendingQty(line, "fixed") : line.fixed;
      const lineUnfixed = pendingOnly ? pendingQty(line, "unfixed") : line.unfixed;
      const lineQty = pendingOnly ? lineFixed + lineUnfixed : line.qty;
      if (pendingOnly && !lineQty) continue;
      const part = byPart.get(line.pn) ?? {
        pn: line.pn, description: line.description, lines: 0, qty: 0, fixed: 0, unfixed: 0,
      };
      part.lines += 1;
      part.qty += lineQty;
      part.fixed += lineFixed;
      part.unfixed += lineUnfixed;
      byPart.set(part.pn, part);
      total += lineQty;
      fixed += lineFixed;
      unfixed += lineUnfixed;
    }
  }
  const parts = [...byPart.values()].sort((a, b) => b.qty - a.qty || a.pn.localeCompare(b.pn));
  const results = [
    { name: "修好", count: fixed, color: "var(--mb-success)" },
    { name: "没修好", count: unfixed, color: "var(--mb-warning)" },
  ];
  if (!pendingOnly) results.push({ name: "待登记", count: total - fixed - unfixed, color: "var(--mb-scrollbar)" });
  const related = selection ? orders.filter((order) =>
    "stage" in selection ? !pendingOnly && orderStage(order) === selection.stage : order.lines.some((line) =>
      line.pn === selection.pn && (!pendingOnly || pendingQty(line, "fixed") + pendingQty(line, "unfixed") > 0))
  ) : [];

  if (!orders.length || (pendingOnly && !total)) {
    return <Card><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={pendingOnly ? "当前没有待分配去向的维修结果" : "当前角色暂无可分析的维修单"} /></Card>;
  }

  return (
    <div className="wo-analysis">
      <div className={pendingOnly ? "" : "wo-analysis__charts"}>
        {!pendingOnly && <Card title="任务进度" size="small" extra={<span className="wo-analysis__unit">{orders.length} 单</span>}>
          {stages.map((stage, index) => (
            <button className="wo-analysis__stage" type="button" key={stage}
              aria-label={`查看${stage}的 ${counts[stage]} 张维修单`}
              onClick={() => setSelection({ stage })} disabled={!counts[stage]}>
              <span>{stage}</span>
              <span className="wo-analysis__track" aria-hidden="true">
                <span className={`wo-analysis__fill wo-analysis__fill--${index}`}
                  style={{ width: `${100 * counts[stage] / orders.length}%` }} />
              </span>
              <strong>{counts[stage]}</strong>
            </button>
          ))}
        </Card>}
        <Card title={pendingOnly ? "待分配去向" : "维修结果"} size="small">
          <span className="wo-analysis__main-value">{total}</span><span className="wo-analysis__unit">{pendingOnly ? " 件" : " 件次"}</span>
          <div className="wo-analysis__stack" aria-hidden="true">
            {results.map((result) => <span key={result.name} style={{ flexGrow: result.count, background: result.color }} />)}
          </div>
          <div className="wo-analysis__legend">
            {results.map((result) => (
              <span key={result.name}><i style={{ background: result.color }} />{result.name} <strong>{result.count}</strong></span>
            ))}
          </div>
          {!pendingOnly && <p className="wo-analysis__note">按当前可见维修单汇总。备件进入二次维修单时，按新一轮数量再计件次。</p>}
        </Card>
      </div>
      <Card title="按备件 PN 查看" size="small" extra={<span className="wo-analysis__unit">{parts.length} 种 PN</span>}>
        <div className="wo-analysis__part-list">
          {parts.map((part) => (
            <div key={part.pn} className="wo-analysis__part">
              <div className="wo-analysis__part-name">
                <Button size="small" type="link" onClick={() => setSelection({ pn: part.pn })}>{part.pn}</Button>
                <small>{part.description}</small>
              </div>
              <div className="wo-analysis__numbers">
                <span>{part.lines} 条任务条</span>
                <span><b>{part.qty}</b>{pendingOnly ? " 件待分" : " 件次"}</span>
                <span>修好 {part.fixed}</span>
                <span>没修好 {part.unfixed}</span>
              </div>
            </div>
          ))}
        </div>
      </Card>
      <Drawer open={!!selection} width="min(480px, 100vw)" onClose={() => setSelection(null)}
        title={selection ? `${"pn" in selection ? selection.pn : selection.stage} · 关联维修单` : ""}>
        {related.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无对应维修单" />}
        {related.map((order) => {
          const lines = selection && "pn" in selection ? order.lines.filter((line) => line.pn === selection.pn) : order.lines;
          const qty = lines.reduce((sum, line) => sum + (pendingOnly ? pendingQty(line, "fixed") + pendingQty(line, "unfixed") : line.qty), 0);
          return (
            <div key={order.id} className="wo-analysis__related">
              <Button type="link" onClick={() => { setSelection(null); onOpen(order.id); }}>{order.id}</Button>
              <span>{pendingOnly ? `待分配去向 ${qty} 件` : `${orderStage(order)} · ${lines.length} 条任务条 · ${qty} 件`}</span>
            </div>
          );
        })}
      </Drawer>
    </div>
  );
}
