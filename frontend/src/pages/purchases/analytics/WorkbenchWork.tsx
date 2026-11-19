import {
  fetchBuyerRanks,
  type BuyerMetric,
  type BuyerRank,
  type BuyerRankQuery,
} from "../../../api/purchaseBuyerRanks";
import { qty } from "../../../utils/format";
import type { ExactScope, WbDimension } from "../../../api/purchaseWorkbench";
import {
  basisNames,
  basisValue,
  cash,
  CubeBlock,
  GroupName,
  num,
  positivePage,
  useMeasure,
} from "./WorkbenchMeasures";
import { Drill, EmptyNote, Guide, Pick, QueryBlock, useWorkbench } from "./WorkbenchUI";
import { Pager } from "./WorkbenchMatrix";
import WorkbenchTimeChart from "./WorkbenchTimeChart";
import "./workPeople.css";

const PAGE_SIZE = 20;
const metrics: BuyerMetric[] = ["orders", "amount", "qty", "lines", "pns", "suppliers"];
const labels: Record<BuyerMetric, string> = {
  orders: "采购单数",
  amount: "采购额",
  qty: "采购数量",
  lines: "明细行数",
  pns: "不同 PN 数",
  suppliers: "供应商数",
};
const fields: Record<
  BuyerMetric,
  keyof Pick<BuyerRank, "orders" | "amount" | "quantity" | "lines" | "pn_count" | "supplier_count">
> = {
  orders: "orders",
  amount: "amount",
  qty: "quantity",
  lines: "lines",
  pns: "pn_count",
  suppliers: "supplier_count",
};

function workValue(row: BuyerRank, metric: BuyerMetric) {
  if (metric === "qty") return qty(row.quantity);
  return metric === "amount" ? cash(row.amount) : num(row[fields[metric]]);
}

function readPerson(raw: string): string | null | undefined {
  if (!raw) return;
  try {
    const value: unknown = JSON.parse(raw);
    if ((typeof value === "string" && value.length) || value === null) return value;
  } catch {
    // A missing or malformed selection opens the overall ranking.
  }
}

function WorkUnit({ unit, change }: { unit: string; change: (v: string) => void }) {
  const { scope } = useWorkbench();
  if ((scope && "unit" in scope) || !unit) return null;
  return (
    <CubeBlock query={{ row: "unit", metric: "lines", basis: "paid", page_size: 100 }}>
      {(data) => {
        const choices = new Set([unit, ...data.rows.flatMap((r) => (r.key ? [r.key] : []))]);
        return (
          <Pick label="数量列单位" value={unit} options={[...choices].map((u) => [u, u])} onChange={change} />
        );
      }}
    </CubeBlock>
  );
}

function PeopleTable({
  query,
  columns,
  unit,
  sort,
  select,
  pageTo,
}: {
  query: BuyerRankQuery;
  columns: BuyerMetric[];
  unit: string;
  sort: (metric: BuyerMetric) => void;
  select: (row: BuyerRank) => void;
  pageTo: (page: number) => void;
}) {
  const { filters } = useWorkbench();
  return (
    <QueryBlock id={"buyer-ranks:" + JSON.stringify(query)} load={() => fetchBuyerRanks(filters, query)}>
      {(data) => (
        <>
          <div className="pw-people-caption">
            <strong>排行共 {num(data.total)} 项</strong>
            <span>按{labels[query.metric]}从高到低，同值并列，缺值不排位</span>
          </div>
          {data.items.length ? (
            <div className="pw-scroll">
              <table className="pw-table pw-people-table" aria-label="采购员总排行">
                <thead>
                  <tr>
                    <th scope="col">名次</th>
                    <th scope="col">采购员</th>
                    {columns.map((metric) => (
                      <th
                        key={metric}
                        scope="col"
                        aria-sort={query.metric === metric ? "descending" : "none"}
                      >
                        <button
                          type="button"
                          aria-label={"按" + labels[metric] + "排序"}
                          onClick={() => sort(metric)}
                        >
                          {labels[metric]}
                          {metric === "qty"
                            ? "（" + unit + "）"
                            : metric === "amount"
                              ? "（" + basisNames[query.basis] + "）"
                              : ""}
                          <span aria-hidden="true">{query.metric === metric ? " ↓" : " ↕"}</span>
                        </button>
                      </th>
                    ))}
                    <th scope="col">行/单</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((row) => (
                    <tr key={JSON.stringify(row.key)}>
                      <td>{num(row.rank)}</td>
                      <th scope="row">
                        <button
                          type="button"
                          className="pw-link"
                          onClick={() => select(row)}
                          aria-label={"查看" + row.label + "的人员分析"}
                        >
                          {row.label}
                        </button>
                      </th>
                      {columns.map((metric) => (
                        <td key={metric} data-current={query.metric === metric || undefined}>
                          {metric === "orders" ? (
                            <Drill scope={row.scope} label={row.label + " 经手采购单"}>
                              {workValue(row, metric)}
                            </Drill>
                          ) : (
                            workValue(row, metric)
                          )}
                        </td>
                      ))}
                      <td>{row.orders ? (row.lines / row.orders).toFixed(1) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyNote />
          )}
          <Pager
            total={data.total}
            page={query.page}
            size={query.page_size}
            name="人员页"
            onChange={pageTo}
          />
        </>
      )}
    </QueryBlock>
  );
}

function PersonBreakdown({
  title,
  note,
  axis,
  scope,
  basis,
  metric,
  pageKey,
}: {
  title: string;
  note: string;
  axis: WbDimension;
  scope: ExactScope;
  basis: string;
  metric: "orders" | "amount";
  pageKey: string;
}) {
  const { costRestricted } = useWorkbench();
  const { get, set } = useMeasure();
  const page = positivePage(get(pageKey, "1"));
  const size = 8;
  return (
    <Guide title={title} note={note}>
      <CubeBlock query={{ row: axis, metric, basis, scope, page, page_size: size }}>
        {(data) => (
          <>
            {data.rows.length ? (
              <div className="pw-scroll">
                <table className="pw-table pw-person-detail" aria-label={title}>
                  <thead>
                    <tr>
                      <th scope="col">名次</th>
                      <th scope="col">{title}</th>
                      <th scope="col">采购单数</th>
                      {!costRestricted && <th scope="col">{basisNames[basis]}采购额</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {data.rows.map((row, i) => (
                      <tr key={JSON.stringify(row.key)}>
                        <td>{(page - 1) * size + i + 1}</td>
                        <th scope="row">
                          <GroupName dim={axis} item={row} />
                        </th>
                        <td>
                          <Drill scope={row.scope} label={row.label + "的关联采购单"}>
                            {num(row.orders)}
                          </Drill>
                        </td>
                        {!costRestricted && <td>{cash(basisValue(row, basis))}</td>}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <EmptyNote />
            )}
            <Pager
              total={data.total_rows}
              page={page}
              size={size}
              onChange={(p) => set({ [pageKey]: String(p) })}
            />
          </>
        )}
      </CubeBlock>
    </Guide>
  );
}

function WorkTrend({
  scope,
  person,
  rank,
  choices,
  unit,
}: {
  scope: ExactScope;
  person?: string;
  rank: BuyerMetric;
  choices: BuyerMetric[];
  unit: string;
}) {
  const { filters } = useWorkbench();
  const { basis, get, set } = useMeasure();
  const span =
    filters.date_from && filters.date_to
      ? (Date.parse(filters.date_to) - Date.parse(filters.date_from)) / 86400000
      : Infinity;
  const time = span <= 90 ? "day" : "month";
  const metric = choices.find((m) => m === get("trend", rank)) || rank;
  const page = positivePage(get("trendPage", "1"));
  const size = time === "day" ? 42 : 36;
  const period = time === "day" ? "日" : "月";
  return (
    <Guide
      title={person ? person + "的采购节奏" : "全员采购节奏"}
      note={"按采购" + period + "从早到晚排列，悬停查看该时段的指标，点击查看采购单。"}
      extra={
        <Pick
          label="趋势指标"
          value={metric}
          options={choices.map((m) => [m, m === "qty" ? labels[m] + "（" + unit + "）" : labels[m]])}
          onChange={(v) => set({ trend: v, trendPage: "" })}
        />
      }
    >
      <CubeBlock
        query={{
          row: time,
          metric,
          basis,
          sort: "key",
          page,
          page_size: size,
          scope: metric === "qty" ? { ...scope, unit } : scope,
        }}
      >
        {(data) => {
          const rows = data.rows.filter((r) => r.key !== null);
          return (
            <>
              {rows.length ? (
                <WorkbenchTimeChart {...{ rows, time, metric, basis, unit, person }} />
              ) : (
                <EmptyNote>当前时段没有带采购日期的记录。</EmptyNote>
              )}
              {data.rows.some((r) => r.key === null) && (
                <p className="pw-note">采购日期未填的记录不在时间轴上显示。</p>
              )}
              <Pager
                total={data.total_rows}
                page={page}
                size={size}
                name="时间页"
                onChange={(p) => set({ trendPage: String(p) })}
              />
            </>
          );
        }}
      </CubeBlock>
    </Guide>
  );
}

export default function WorkbenchWork() {
  const { filters, scope, costRestricted, supplierRestricted } = useWorkbench();
  const { basis, get, set } = useMeasure();
  const unit = scope && "unit" in scope ? scope.unit || "" : get("workUnit", "件");
  const choices = metrics.filter(
    (m) =>
      (m !== "amount" || !costRestricted) &&
      (m !== "suppliers" || !supplierRestricted) &&
      (m !== "qty" || !!unit),
  );
  const rank = choices.find((m) => m === get("workRank", "orders")) || "orders";
  const page = positivePage(get("peoplePage", "1"));
  const selected = readPerson(get("workBuyer", ""));
  const person = selected === null ? "未填写采购员" : selected;
  const peopleScope = scope || {};
  const selectedScope = selected === undefined ? peopleScope : { ...peopleScope, buyer: selected };
  const query = {
    scope: peopleScope,
    metric: rank,
    basis,
    quantity_unit: unit,
    page,
    page_size: PAGE_SIZE,
  };
  const detailMetric = !costRestricted && get("personRank", "amount") === "amount" ? "amount" : "orders";
  const detailOrder = detailMetric === "amount" ? basisNames[basis] + "采购额" : "采购单数";
  const dimensions = [
    ["type", "采购类型"],
    ["category", "商品大类"],
    ["pn", "采购 PN"],
  ] as const;
  const [goodsAxis, goodsName] =
    dimensions.find(([key]) => key === get("personGoods", "type")) || dimensions[0];
  const sortBy = (metric: string) => set({ workRank: metric, peoplePage: "", trendPage: "" });
  const select = (row: BuyerRank) =>
    set({ workBuyer: JSON.stringify(row.key), trendPage: "", supplierPage: "", goodsPage: "" }, true);
  const clear = () => set({ workBuyer: "", trendPage: "", goodsPage: "", supplierPage: "" });
  const unitPicker = (
    <WorkUnit unit={unit} change={(v) => set({ workUnit: v, peoplePage: "", trendPage: "" })} />
  );
  const selectionInScope =
    selected === undefined ||
    ((!Object.prototype.hasOwnProperty.call(peopleScope, "buyer") || peopleScope.buyer === selected) &&
      (!filters.purchaser?.length || (selected !== null && filters.purchaser.includes(selected))));
  return (
    <div className="pw-people">
      {selected === undefined ? (
        <Guide
          title="采购员总排行"
          note="列出当前筛选内有生效采购单的经手人。点姓名看供应商和采购构成，点单数核对采购记录。"
          extra={
            <div className="pw-controls">
              <Pick
                label="排名依据"
                value={rank}
                options={choices.map((m) => [m, labels[m]])}
                onChange={sortBy}
              />
              {unitPicker}
            </div>
          }
        >
          <PeopleTable
            query={query}
            columns={choices}
            unit={unit}
            sort={sortBy}
            select={select}
            pageTo={(p) => set({ peoplePage: String(p) })}
          />
          <p className="pw-note">
            {unit ? "采购数量只统计以「" + unit + "」登记的明细。" : "当前筛选为单位未填，无法比较采购数量。"}
            不同 PN 和供应商分别去重计数。
          </p>
        </Guide>
      ) : (
        <>
          <div className="pw-people-nav">
            <button type="button" className="pw-small" onClick={clear}>
              ‹ 返回人员总榜
            </button>
            <strong>{person}</strong>
            {selectionInScope && (
              <Drill scope={selectedScope} label={person + "的采购单据"}>
                查看采购单据
              </Drill>
            )}
          </div>
          {!selectionInScope ? (
            <EmptyNote>当前采购员筛选未包含所选人员。</EmptyNote>
          ) : (
            <>
              <Guide
                title="个人采购概览"
                note="在当前期间和筛选范围内核对经手单据、采购规模与覆盖面。"
                extra={unitPicker}
              >
                <QueryBlock
                  id={"buyer-summary:" + JSON.stringify([selectedScope, basis, unit])}
                  load={() =>
                    fetchBuyerRanks(filters, {
                      ...query,
                      scope: selectedScope,
                      metric: "orders",
                      page: 1,
                      page_size: 1,
                    })
                  }
                >
                  {(d) =>
                    d.items[0] ? (
                      <div className="pw-kpis" aria-label="个人采购指标">
                        {choices.map((m) => (
                          <div key={m}>
                            <small>
                              {m === "amount"
                                ? basisNames[basis] + labels[m]
                                : m === "qty"
                                  ? labels[m] + "（" + unit + "）"
                                  : labels[m]}
                            </small>
                            <b>{workValue(d.items[0], m)}</b>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <EmptyNote />
                    )
                  }
                </QueryBlock>
              </Guide>
              <div className="pw-controls pw-person-tools">
                {!costRestricted && (
                  <Pick
                    label="明细排行依据"
                    value={detailMetric}
                    options={[
                      ["amount", basisNames[basis] + "采购额"],
                      ["orders", "采购单数"],
                    ]}
                    onChange={(v) => set({ personRank: v, goodsPage: "", supplierPage: "" })}
                  />
                )}
                <Pick
                  label="采购构成维度"
                  value={goodsAxis}
                  options={dimensions}
                  onChange={(v) => set({ personGoods: v, goodsPage: "" })}
                />
              </div>
              <div className={supplierRestricted ? "" : "pw-grid"}>
                {!supplierRestricted && (
                  <PersonBreakdown
                    title="往来供应商"
                    note={"按该人员与各供应商的" + detailOrder + "降序，点单数核对往来记录。"}
                    axis="supplier"
                    scope={selectedScope}
                    basis={basis}
                    metric={detailMetric}
                    pageKey="supplierPage"
                  />
                )}
                <PersonBreakdown
                  title={goodsName}
                  note={"按" + detailOrder + "降序，查看该人员的采购构成。"}
                  axis={goodsAxis}
                  scope={selectedScope}
                  basis={basis}
                  metric={detailMetric}
                  pageKey="goodsPage"
                />
              </div>
            </>
          )}
        </>
      )}
      {selectionInScope && <WorkTrend {...{ scope: selectedScope, rank, choices, unit, person }} />}
    </div>
  );
}
