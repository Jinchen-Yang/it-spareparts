import type { CubeQuery, Group, WbDimension } from "../../../api/purchaseWorkbench";
import {
  basisNames,
  basisValue,
  cash,
  CubeBlock,
  dimensionNames,
  display,
  GroupName,
  num,
  percent,
  positivePage,
  useMeasure,
} from "./WorkbenchMeasures";
import { Drill, EmptyNote, Guide, Pick, useWorkbench } from "./WorkbenchUI";
import { Pager } from "./WorkbenchMatrix";
export default function WorkbenchCompare() {
  const { filters, costRestricted, supplierRestricted } = useWorkbench();
  const { basis, get, set } = useMeasure();
  const options: WbDimension[] = [
    "type",
    "buyer",
    ...(supplierRestricted ? [] : ["supplier" as const]),
    "pn",
    "category",
  ];
  const row = options.find((v) => v === get("cmpRow", "type")) || "type";
  const available: CubeQuery["metric"][] = costRestricted ? ["orders", "lines"] : ["amount", "orders", "lines"];
  const metric = available.find((m) => m === get("cmpMetric", available[0])) || available[0];
  const sort =
    (["absolute", "increase", "decrease"] as const).find((m) => m === get("cmpSort", "absolute")) || "absolute";
  const page = positivePage(get("cmpPage", "1"));
  if (!filters.date_from || !filters.date_to)
    return <EmptyNote>请选择有起止日期的区间，以便与紧邻的等长上期比较。</EmptyNote>;
  return (
    <Guide title="期间变动" note="两期先在全量分组上对齐，再按增减排序；上期基数为零或缺失时不计算变动率。">
      <div className="pw-tools">
        <Pick
          label="比较维度"
          value={row}
          options={options.map((d) => [d, dimensionNames[d]])}
          onChange={(v) => set({ cmpRow: v, cmpPage: "" })}
        />
        <Pick
          label="比较指标"
          value={metric}
          options={available.map((m) => [
            m,
            m === "amount" ? basisNames[basis] + "金额" : m === "orders" ? "订单数" : "明细行数",
          ])}
          onChange={(v) => set({ cmpMetric: v, cmpPage: "" })}
        />
        <Pick
          label="比较排序"
          value={sort}
          options={[
            ["absolute", "绝对变动"],
            ["increase", "增量优先"],
            ["decrease", "降量优先"],
          ]}
          onChange={(v) => set({ cmpSort: v, cmpPage: "" })}
        />
      </div>
      <CubeBlock query={{ row, metric, basis, compare: true, sort, page, page_size: 20 }}>
        {(d) => {
          const prev = (r: Group) => ({
            ...r.scope,
            date_from: d.previous_from || null,
            date_to: d.previous_to || null,
          });
          const shown = (v: number | null | undefined) => display(v, metric, "");
          return (
            <>
              <div className="pw-compare-summary">
                {[
                  { label: "本期", start: filters.date_from, end: filters.date_to, summary: d.total },
                  { label: "上期", start: d.previous_from, end: d.previous_to, summary: d.previous_total },
                ].map(({ label, start, end, summary }) => (
                  <div key={label}>
                    <b>
                      {label}　{start || "—"} — {end || "—"}
                    </b>
                    {summary && (
                      <div>
                        <Drill
                          scope={{ ...summary.scope, ...(start && end ? { date_from: start, date_to: end } : {}) }}
                          label={label + "全部采购记录"}
                        >
                          {num(summary.orders)} 单 · {num(summary.lines)} 行
                        </Drill>
                        {!costRestricted && (
                          <strong>
                            {basisNames[basis]} {cash(basisValue(summary, basis))}
                          </strong>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
              {!d.rows.length ? (
                <EmptyNote />
              ) : (
                <div className="pw-scroll">
                  <table className="pw-table">
                    <thead>
                      <tr>
                        <th>{dimensionNames[row]}</th>
                        <th>本期</th>
                        <th>上期</th>
                        <th>增减值</th>
                        <th>变动率</th>
                      </tr>
                    </thead>
                    <tbody>
                      {d.rows.map((r) => (
                        <tr key={JSON.stringify(r.key)}>
                          <th>
                            <GroupName item={r} dim={row} />
                            {r.orders === 0 ? (
                              <small>本期无单</small>
                            ) : r.previous_orders === 0 ? (
                              <small>本期新增</small>
                            ) : null}
                          </th>
                          <td>
                            {r.orders ? (
                              <Drill scope={r.scope} label={"本期 " + r.label}>
                                {shown(r.value)}
                              </Drill>
                            ) : (
                              shown(r.value)
                            )}
                          </td>
                          <td>
                            {r.previous_orders && d.previous_from && d.previous_to ? (
                              <Drill scope={prev(r)} label={"上期 " + r.label}>
                                {shown(r.previous_value)}
                              </Drill>
                            ) : (
                              shown(r.previous_value)
                            )}
                          </td>
                          <td>
                            {r.change != null && r.change > 0 ? "+" : ""}
                            {shown(r.change)}
                          </td>
                          <td>{percent(r.change_rate)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <Pager total={d.total_rows} size={20} page={page} onChange={(v) => set({ cmpPage: String(v) })} />
            </>
          );
        }}
      </CubeBlock>
    </Guide>
  );
}
