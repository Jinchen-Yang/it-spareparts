import type { Axis, Dimension, Drill } from "../../api/warehouseAnalytics";
import { exact } from "./warehouseState";
import { DrillLink, Panel, Result, format, useWarehouse, useWarehouseRead } from "./WarehouseShared";

const salesReturns: Drill = { scope: { source: "return", kind: "销售退货" } };
const warehousePick: Drill = { operator_role: "pick" };

function Leaders({
  rows,
  dimension,
  selection,
}: {
  rows: Axis[];
  dimension: Dimension;
  selection: Drill;
}) {
  const peak = rows[0]?.value || 1;
  if (!rows.length) return <p className="wa-empty">当前范围没有匹配记录。</p>;
  return (
    <ol className="wa-leaders">
      {rows.map((row) => (
        <li key={JSON.stringify(row.key)}>
          <DrillLink label={row.label + " · 查看单据"} to={exact(selection, { [dimension]: row.key })}>
            <span>{row.label}</span>
            <b>{format(row.value)}</b>
            {dimension === "pn" && <small>{row.description || "描述未填"}</small>}
            <i
              aria-hidden="true"
              style={{ width: Math.max(0, Math.min(100, (100 * (row.value || 0)) / peak)) + "%" }}
            />
          </DrillLink>
        </li>
      ))}
    </ol>
  );
}
export default function OverviewLeaders() {
  const { filters } = useWarehouse();
  const pickingScope = !filters.source || filters.source === "inbound" || filters.source === "return";
  const returnScope = !filters.source || filters.source === "return";
  const centers = useWarehouseRead("cube", {
    ...filters,
    row: "center",
    metric: "documents",
    row_limit: 5,
  });
  const people = useWarehouseRead(
    "rankings",
    { ...filters, ...warehousePick, by: "operator", metric: "documents", page: 1, page_size: 5 },
    pickingScope,
  );
  const pns = useWarehouseRead(
    "rankings",
    { ...filters, ...salesReturns, by: "pn", metric: "lines", page: 1, page_size: 5 },
    returnScope,
  );
  return (
    <div className="wa-board-grid">
      <Panel title="仓储中心前五" why="按匹配单据数排序，保留未填中心。">
        <Result read={centers}>
          {(c) => <Leaders rows={c.rows} dimension="center" selection={{}} />}
        </Result>
      </Panel>
      <Panel title="拣货人员前五" why="按已署名人员的入库和返库单据数排序。">
        {pickingScope ? (
          <Result read={people}>
            {(c) => (
              <Leaders
                rows={c.items.filter((r) => r.key !== null).map((r) => ({ ...r, value: r.documents }))}
                dimension="operator"
                selection={warehousePick}
              />
            )}
          </Result>
        ) : (
          <p className="wa-empty">当前来源不含仓库拣货业务。</p>
        )}
      </Panel>
      <Panel title="销售退货 PN 前五" why="按销售退货返库单的明细行数排序，仅计已填写的 PN。">
        {returnScope ? (
          <Result read={pns}>
            {(r) => (
              <Leaders
                rows={r.items.map((x) => ({ ...x, value: x.lines }))}
                dimension="pn"
                selection={salesReturns}
              />
            )}
          </Result>
        ) : (
          <p className="wa-empty">当前来源不含销售退货返库单。</p>
        )}
      </Panel>
    </div>
  );
}
