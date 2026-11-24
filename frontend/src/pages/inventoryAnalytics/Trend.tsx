import OverviewLeaders from "./OverviewLeaders";
import { useEffect, useState } from "react";
import { Popover } from "antd";
import dayjs from "dayjs";
import {
  warehouseSources,
  metrics,
  type Drill,
  type Metric,
  type Source,
} from "../../api/warehouseAnalytics";
import { exact, hasKey, reach, sourceCoverage } from "./warehouseState";
import {
  DataTable,
  DrillLink,
  Panel,
  Pick,
  Result,
  Tile,
  format,
  quantity,
  useWarehouse,
  useWarehouseRead,
} from "./WarehouseShared";

export function metricOptions(unit?: string, withPns = true): Partial<Record<Metric, string>> {
  return {
    documents: metrics.documents,
    lines: metrics.lines,
    ...(withPns ? { pns: metrics.pns } : {}),
    ...(unit ? { quantity: `数量（${unit}）` } : {}),
  };
}
function weekOf(date: string) {
  const d = dayjs(date);
  return d.subtract((d.day() + 6) % 7, "day").format("YYYY-MM-DD");
}
export function calendarBuckets(
  from: string | undefined,
  to: string | undefined,
  grain: "day" | "week",
  max = 60,
) {
  if (!from || !to || from > to) return [];
  const end = grain === "week" ? weekOf(to) : to;
  const start = grain === "week" ? weekOf(from) : from;
  const keys: string[] = [];
  for (
    let cursor = dayjs(end);
    keys.length < max && cursor.format("YYYY-MM-DD") >= start;
    cursor = cursor.subtract(grain === "week" ? 7 : 1, "day")
  ) {
    keys.unshift(cursor.format("YYYY-MM-DD"));
  }
  return keys;
}

function Sample({ scope }: { scope: Drill }) {
  const { filters } = useWarehouse();
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setReady(true), 180);
    return () => clearTimeout(timer);
  }, []);
  const request = useWarehouseRead("documents", { ...filters, ...scope, page: 1, page_size: 1 }, ready);
  if (!request?.data) return <small>{request?.error || "正在读取匹配单据…"}</small>;
  const doc = request.data.items[0],
    line = doc?.sample?.[0];
  if (!doc) return <small>当前范围没有可见单据。</small>;
  return (
    <div className="wa-sample">
      <span>单据示例 · {doc.number}</span>
      {line && (
        <span>
          {line.pn || "PN 未填"} · {line.description || "描述未填"} ·{" "}
          {quantity(line.quantity, line.unit)}
        </span>
      )}
    </div>
  );
}
function Point({
  scope,
  label,
  style,
  className,
}: {
  scope: Drill;
  label: string;
  style: React.CSSProperties;
  className: string;
}) {
  const [open, setOpen] = useState(false);
  const { drill } = useWarehouse();
  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      trigger={["hover", "focus", "click"]}
      mouseEnterDelay={0.08}
      destroyOnHidden
      content={
        <div className="wa-pop">
          <strong>{label}</strong>
          {open && <Sample scope={scope} />}
          <button
            type="button"
            className="wa-link"
            onClick={() => {
              setOpen(false);
              drill(scope, label);
            }}
          >
            查看匹配单据
          </button>
        </div>
      }
    >
      <button
        className={className}
        style={style}
        type="button"
        aria-label={label}
        onKeyDown={(e) => {
          if (e.key === "Escape") setOpen(false);
          if (e.key === "Enter") {
            e.preventDefault();
            drill(scope, label);
          }
        }}
      />
    </Popover>
  );
}
const colors: Record<Source, string> = {
  inbound: "var(--mb-success)",
  return: "var(--mb-accent)",
  direct: "var(--mb-warning)",
  outbound: "var(--mb-presales-purple)",
};
export function Trend({
  base = {},
  title = "日周单据趋势",
  enabled = true,
}: {
  base?: Drill;
  title?: string;
  enabled?: boolean;
}) {
  const { params, patch, filters, coverage } = useWarehouse();
  const grain = params.get("grain") === "week" ? "week" : "day";
  const opts = metricOptions(filters.unit, false),
    selected = params.get("trendMetric");
  const metric = hasKey(opts, selected) ? selected : "documents";
  const curve = params.get("chart") === "line";
  const sources = (Object.keys(warehouseSources) as Source[]).filter(
    (s) =>
      (!filters.source || filters.source === s) &&
      (!base.scope?.source || base.scope.source === s) &&
      (!base.operator_role ||
        (base.operator_role === "pick"
          ? s === "inbound" || s === "return"
          : s === (base.operator_role === "direct" ? "direct" : "outbound"))),
  );
  const c = sourceCoverage(coverage, filters.center)?.filter((s) => sources.includes(s.source));
  const from = filters.date_from || c?.flatMap((s) => (s.min_date ? [s.min_date] : [])).sort()[0];
  const to =
    filters.date_to ||
    c
      ?.flatMap((s) => (s.max_date ? [s.max_date] : []))
      .sort()
      .reverse()[0];
  const axis = calendarBuckets(from, to, grain);
  // Limit the query to the displayed calendar buckets, including the first partial week.
  const first = axis[0];
  const read = useWarehouseRead(
    "cube",
    {
      ...filters,
      ...base,
      date_from: from && first ? (from > first ? from : first) : from,
      row: grain,
      col: "source",
      metric,
      row_limit: 60,
      col_limit: 4,
    },
    enabled && !!coverage && axis.length > 0,
  );
  const tools = (
    <>
      <Pick
        label="时间粒度"
        value={grain}
        options={{ day: "按日", week: "按周" }}
        change={(v) => patch({ grain: v })}
      />
      <Pick label="趋势指标" value={metric} options={opts} change={(v) => patch({ trendMetric: v })} />
      <Pick
        label="图形"
        value={curve ? "line" : "bar"}
        options={{ bar: "柱状", line: "折线" }}
        change={(v) => patch({ chart: v })}
      />
    </>
  );
  return (
    <Panel
      title={title}
      why="观察各来源随时间的业务量。周从周一开始，图中保留所选范围末尾最多 60 个自然时段。"
      actions={tools}
    >
      {!enabled || !axis.length || !sources.length ? (
        <p className="wa-empty">当前范围没有可绘制的时间序列。</p>
      ) : (
        <Result read={read}>
          {(cube) => {
            const values = new Map(
              cube.cells.map((x) => [JSON.stringify([x.row_key, x.col_key]), x.value]),
            );
            const peak = Math.max(1, ...cube.cells.map((x) => x.value || 0));
            const width = Math.max(axis.length * 24, 340);
            return (
              <>
                <div className="wa-legend">
                  {sources.map((s) => (
                    <span key={s}>
                      <i style={{ background: colors[s] }} />
                      {warehouseSources[s]}
                    </span>
                  ))}
                  <span>灰纹 / 断点为无覆盖或缺值</span>
                </div>
                <div className="wa-chart">
                  <div className="wa-y">
                    <span>{format(peak)}</span>
                    <span>0</span>
                  </div>
                  <div className="wa-plot-scroll">
                    <div className="wa-plot" style={{ minWidth: width }}>
                      {sources.map((source, j) => {
                        let path = "",
                          pen = false;
                        const marks = axis.map((key, i) => {
                          const end =
                            grain === "week" ? dayjs(key).add(6, "day").format("YYYY-MM-DD") : key;
                          const availability = reach(
                            c?.find((v) => v.source === source),
                            from && from > key ? from : key,
                            to && to < end ? to : end,
                          );
                          const id = JSON.stringify([key, source]),
                            amount = values.get(id);
                          const covered = availability !== "absent" && availability !== "outside";
                          const value =
                            covered && values.has(id) ? amount : availability === "covered" ? 0 : null;
                          const x =
                            ((i + 0.5 + (j - (sources.length - 1) / 2) * 0.17) / axis.length) * 100;
                          const h = value == null ? 0 : (value / peak) * 90;
                          const label = `${key}${grain === "week" ? " 起的一周" : ""} · ${warehouseSources[source]} · ${opts[metric]} ${format(value)}${availability === "partial" ? "（部分覆盖）" : ""}`;
                          if (value != null) {
                            path += `${pen ? "L" : "M"} ${x.toFixed(3)} ${(95 - h).toFixed(2)} `;
                            pen = true;
                          } else pen = false;
                          const style = curve
                            ? { left: x + "%", bottom: 5 + h + "%", color: colors[source] }
                            : {
                                left: x + "%",
                                height: Math.max(1, h) + "%",
                                backgroundColor: colors[source],
                                width: Math.min(1.3, 15 / axis.length) + "%",
                              };
                          if (!values.has(id) || value == null)
                            return (
                              <span
                                key={key}
                                className={value == null ? "wa-chart-gap" : "wa-chart-zero"}
                                style={{ left: x + "%" }}
                                title={label}
                              />
                            );
                          return (
                            <Point
                              key={key}
                              label={label}
                              className={curve ? "wa-dot" : "wa-bar"}
                              style={style}
                              scope={exact(base, { [grain]: key, source })}
                            />
                          );
                        });
                        return (
                          <div key={source}>
                            {curve && (
                              <svg aria-hidden="true" viewBox="0 0 100 100" preserveAspectRatio="none">
                                <path
                                  d={path}
                                  fill="none"
                                  stroke={colors[source]}
                                  strokeWidth="1.7"
                                  vectorEffect="non-scaling-stroke"
                                />
                              </svg>
                            )}
                            {marks}
                          </div>
                        );
                      })}
                    </div>
                    <div className="wa-x" style={{ minWidth: width }}>
                      {axis.map((v, i) => (
                        <span key={v}>
                          {i % Math.ceil(axis.length / 8) === 0 || i === axis.length - 1
                            ? v.slice(5)
                            : ""}
                        </span>
                      ))}
                    </div>
                  </div>
                </div>
              </>
            );
          }}
        </Result>
      )}
    </Panel>
  );
}
function OutboundCoverage() {
  const { coverage, filters } = useWarehouse();
  const rows = coverage?.outbound_warehouses?.filter(
    (row) => !filters.center || row.center === filters.center,
  );
  if ((filters.source && filters.source !== "outbound") || !rows?.length) return null;
  return (
    <details className="wa-panel wa-outbound-coverage">
      <summary>各出库仓的已导入业务日</summary>
      <div className="wa-panel-body">
        <p>记录日按 CKD 出库日期计算，含草稿。单据及数量只统计已生效记录。</p>
        <DataTable labels={["仓储中心", "单头出库仓", "首个记录日", "最近记录日", "当前区间记录日"]}>
          {rows.map(({ center, warehouse, days }) => {
            const matched = days.filter((day) =>
              (!filters.date_from || day >= filters.date_from) && (!filters.date_to || day <= filters.date_to),
            ).length;
            return (
              <tr key={JSON.stringify([center, warehouse])}>
                <td>{center || "未登记"}</td>
                <th scope="row">
                  {matched ? (
                    <DrillLink
                      to={{ scope: { source: "outbound", center, outbound_warehouse: warehouse } }}
                      label={(warehouse || "仓库未登记") + " · 匹配单据"}
                    >
                      {warehouse || "仓库未登记"}
                    </DrillLink>
                  ) : warehouse || "仓库未登记"}
                </th>
                <td>{days[0]}</td>
                <td>{days[days.length - 1]}</td>
                <td>{matched ? format(matched) : "无导入记录"}</td>
              </tr>
            );
          })}
        </DataTable>
      </div>
    </details>
  );
}

export function Overview() {
  const { filters, coverage } = useWarehouse();
  const sources = sourceCoverage(coverage, filters.center);
  const counts = useWarehouseRead("cube", {
    ...filters,
    row: "source",
    metric: "documents",
    row_limit: 4,
  });
  return (
    <>
      <Result read={counts}>
        {(cube) => (
          <div className="wa-tiles">
            {(Object.keys(warehouseSources) as Source[]).map((src) => {
              const entry = sources?.find((c) => c.source === src),
                span = reach(entry, filters.date_from, filters.date_to);
              const n = cube.rows.find((row) => row.key === src)?.value ?? 0;
              const available =
                !!coverage &&
                (!filters.source || filters.source === src) &&
                (span === "covered" || span === "partial");
              return (
                <Tile
                  key={src}
                  label={warehouseSources[src]}
                  value={
                    available ? (
                      <DrillLink
                        to={{ scope: { source: src } }}
                        label={`${warehouseSources[src]} · 匹配单据`}
                      >
                        {format(n)}
                      </DrillLink>
                    ) : (
                      "—"
                    )
                  }
                  detail={
                    !coverage
                      ? "正在核对覆盖"
                      : filters.source && filters.source !== src
                        ? "已筛选其他来源"
                        : src === "outbound" && !entry?.available
                          ? "仓库实际出库单待接入"
                          : span === "outside"
                            ? "当前区间无来源覆盖"
                            : span === "absent"
                              ? "尚无来源记录"
                              : span === "partial"
                                ? "单 · 部分日期有覆盖"
                                : "单 · 按业务日期"
                  }
                />
              );
            })}
          </div>
        )}
      </Result>
      <Trend />
      <OutboundCoverage />
      <OverviewLeaders />
    </>
  );
}
