import { useSearchParams } from "react-router-dom";
import { useLocalRestrictions } from "../../boss/shared";
import { fetchConcentration, type CubeQuery, type WbDimension } from "../../../api/purchaseWorkbench";
import { fetchPoolParts } from "../../../api/purchasePoolAnalytics";
import {
  CubeBlock,
  MeasureTools,
  RankCard,
  Segments,
  dimensionNames,
  cash,
  num,
  percent,
  readTop,
  quantityUnit,
  useMeasure,
} from "./WorkbenchMeasures";
import { Drill, EmptyNote, Guide, Pick, QueryBlock, useWorkbench } from "./WorkbenchUI";

function PriceRanking({ focus, top }: { focus: "above_pool" | "above_cap"; top: number }) {
  const { filters, scope } = useWorkbench();
  const [, setSp] = useSearchParams();
  function openPart(id: number, pn: string, unit: string | null) {
    setSp(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set("view", "pool");
        next.set("wb_poolPart", String(id));
        next.set("wb_poolQuery", pn);
        next.delete("wb_poolId");
        next.delete("wb_poolPage");
        next.delete("wb_poolFocus");
        if (unit) next.set("wb_poolUnit", unit);
        else next.delete("wb_poolUnit");
        return next;
      },
      { state: { procurement: true } },
    );
  }
  const pool = focus === "above_pool";
  return (
    <Guide
      title={pool ? "同池价格偏离" : "人工上限超额"}
      note={pool ? "哪些型号的未税加权均价高于同池均价？" : "哪些型号的未税加权均价超过了人工采购上限？"}
    >
      <QueryBlock
        id={"prices:" + focus + ":" + top}
        load={() => fetchPoolParts(filters, { focus, page_size: top, scope: JSON.stringify(scope || {}) })}
      >
        {(result) => (
          <>
            {!result.items.length ? (
              <EmptyNote>当前范围没有对应价格样本。</EmptyNote>
            ) : (
              <ol className="pw-list">
                {result.items.map((p, i) => (
                  <li key={JSON.stringify([p.part_id, p.unit])}>
                    <span className="pw-rank">{i + 1}　</span>
                    <button
                      type="button"
                      className="pw-link"
                      onClick={() => openPart(p.part_id, p.pn, p.unit)}
                      aria-label={p.pn + " 的互通池取价样本"}
                    >
                      {p.pn}
                    </button>{" "}
                    · {percent(pool ? p.delta_pool : p.delta_cap)}
                    <small>
                      {p.description || "目录描述未填"} · {num(p.price_orders)} 张取价单 · {p.unit || "单位缺失"}
                    </small>
                    <Drill
                      scope={{ ...scope, pn: p.pn.replace(/^ +| +$/g, "") || null, partId: String(p.part_id), unit: p.unit, _line: "1" }}
                      label={"查看 " + p.pn + " 的采购行"}
                    >
                      查看采购行
                    </Drill>
                  </li>
                ))}
              </ol>
            )}
          </>
        )}
      </QueryBlock>
    </Guide>
  );
}
export default function WorkbenchBoards() {
  const { costRestricted, supplierRestricted, scope, filters } = useWorkbench();
  const { governance } = useLocalRestrictions();
  const { metric, basis, unit: chosen, get, set } = useMeasure();
  const top = readTop(get("top", "5")),
    unit = quantityUnit(scope, chosen);
  const mode = get("boards", "dimensions") === "special" ? "special" : "dimensions";
  const dims: WbDimension[] = [
    "buyer",
    "type",
    ...(supplierRestricted ? [] : ["supplier" as const]),
    "category",
    "pn",
    "pool",
  ];
  function rank(row: WbDimension, title: string, note: string, override: Partial<CubeQuery> = {}) {
    return (
      <RankCard
        key={title}
        title={title}
        note={note}
        unit={unit}
        query={{ row, metric, basis, page_size: top, scope: metric === "qty" ? { unit } : {}, ...override }}
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
    return rank("pn", type + " · PN", "该采购类型下哪些型号排在前面？", {
      scope: { ...(metric === "qty" ? { unit } : {}), type },
    });
  });
  return (
    <>
      <CubeBlock
        query={{
          row: "buyer",
          metric: mode === "dimensions" && metric !== "qty" ? metric : "lines",
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
        <Segments
          options={[
            ["dimensions", "多维排行"],
            ["special", "专题榜组"],
          ]}
          value={mode}
          onChange={(v) => set({ boards: v }, true)}
        />
        {mode === "dimensions" ? (
          <MeasureTools top />
        ) : (
          <Pick
            label="榜单行数"
            value={String(top)}
            options={["3", "5", "10"].map((v) => [v, "前 " + v + " 项"])}
            onChange={(v) => set({ top: v })}
          />
        )}
      </div>
      {mode === "dimensions" ? (
        <>
          <div className="pw-grid three">
            {dims.map((d) =>
              rank(d, dimensionNames[d] + "排行", "按" + dimensionNames[d] + "比较所选指标，点数值核对采购单。"),
            )}
          </div>
          <div className="pw-grid pw-gap">{sourceBoards}</div>
        </>
      ) : (
        <div className="pw-grid three">
          {!costRestricted &&
            rank("orderId", "大额采购单", "按当前金额口径比较可见采购单。", { metric: "amount", scope: {} })}
          {rank("buyer", "人员处理行数", "按单头批次的明细行数观察经手量。", { metric: "lines", scope: {} })}
          {!supplierRestricted &&
            rank("supplier", "供应商覆盖型号", "按成交的不同 PN 数比较供货范围。", { metric: "pns", scope: {} })}
          {!supplierRestricted && (
            <Guide title="PN 主供应商数量占比" note="同一型号和单位下，已知供应商的最大供货量占该组总采购量多少？">
              <QueryBlock id={"concentration:" + top} load={() => fetchConcentration(filters, scope || {}, top)}>
                {(data) =>
                  data.items.length ? (
                    <ol className="pw-list">
                      {data.items.map((p) => (
                        <li key={JSON.stringify([p.pn, p.unit])}>
                          <Drill scope={p.scope} label={p.pn + " 主供应商集中度证据"}>
                            {p.pn} · {percent(p.share)}
                          </Drill>
                          <small>
                            {p.description || "目录描述未填"} · {p.leading_supplier} · {num(p.leading_qty)} / {num(p.total_qty)}{" "}
                            {p.unit}
                          </small>
                        </li>
                      ))}
                    </ol>
                  ) : (
                    <EmptyNote />
                  )
                }
              </QueryBlock>
            </Guide>
          )}
          {!costRestricted && !governance && (
            <>
              <PriceRanking focus="above_pool" top={top} />
              <PriceRanking focus="above_cap" top={top} />
            </>
          )}
        </div>
      )}
    </>
  );
}
