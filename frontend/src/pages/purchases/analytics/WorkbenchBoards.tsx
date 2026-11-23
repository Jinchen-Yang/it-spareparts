import type { CubeQuery, WbDimension } from "../../../api/purchaseWorkbench";
import {
  CubeBlock,
  MeasureTools,
  RankCard,
  dimensionNames,
  cash,
  num,
  readTop,
  quantityUnit,
  useMeasure,
} from "./WorkbenchMeasures";
import { EmptyNote, Guide, useWorkbench } from "./WorkbenchUI";

export default function WorkbenchBoards() {
  const { costRestricted, supplierRestricted, scope, filters } = useWorkbench();
  const { metric, basis, unit: chosen, get } = useMeasure();
  const top = readTop(get("top", ""));
  const unit = quantityUnit(scope, chosen);
  const dims: WbDimension[] = [
    "buyer",
    "type",
    ...(supplierRestricted ? [] : ["supplier" as const]),
    "category",
    "pn",
    "pool",
  ];
  function rank(row: WbDimension, title: string, note: string, exact: CubeQuery["scope"] = {}) {
    return (
      <RankCard
        key={title}
        title={title}
        note={note}
        unit={unit}
        query={{ row, metric, basis, page_size: top, scope: { ...(metric === "qty" ? { unit } : {}), ...exact } }}
      />
    );
  }
  const sourceBoards = ["维保需求", "销售订单"].map((type) => {
    if (
      (scope && Object.prototype.hasOwnProperty.call(scope, "type") && scope.type !== type) ||
      (filters.source_type?.length && !filters.source_type.includes(type))
    )
      return (
        <Guide key={type} title={type + " · PN"} note="按采购类型原文筛选单头批次行。">
          <EmptyNote>当前类型筛选未包含{type}。</EmptyNote>
        </Guide>
      );
    return rank("pn", type + " · PN", "该采购类型下哪些型号排在前面？", { type });
  });
  return (
    <>
      <CubeBlock
        query={{
          row: "buyer",
          metric: metric === "qty" ? "lines" : metric,
          basis,
          page_size: top,
          scope: {},
        }}
      >
        {(d) => (
          <div className="pw-kpis" aria-label="当前筛选摘要">
            <div>
              <small>采购单数</small>
              <b>{num(d.total.orders)} 单</b>
            </div>
            <div>
              <small>单头批次明细</small>
              <b>{num(d.total.lines)} 行</b>
            </div>
            {!costRestricted && (
              <>
                <div>
                  <small>实付金额</small>
                  <b>{cash(d.total.amount_paid)}</b>
                </div>
                <div>
                  <small>含税金额</small>
                  <b>{cash(d.total.amount_inc)}</b>
                </div>
                <div>
                  <small>不含税金额</small>
                  <b>{cash(d.total.amount_ex)}</b>
                </div>
              </>
            )}
            {!supplierRestricted && (
              <div>
                <small>成交供应商</small>
                <b>{num(d.total.supplier_count)} 家</b>
              </div>
            )}
          </div>
        )}
      </CubeBlock>
      <div className="pw-tools">
        <MeasureTools top />
      </div>
      <div className="pw-grid three">
        {dims.map((d) =>
          rank(d, dimensionNames[d] + "排行", "按" + dimensionNames[d] + "比较所选指标，点数值核对采购单。"),
        )}
      </div>
      <div className="pw-grid pw-gap">{sourceBoards}</div>
    </>
  );
}
