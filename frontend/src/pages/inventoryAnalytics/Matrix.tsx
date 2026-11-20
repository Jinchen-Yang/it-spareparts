import dayjs from "dayjs";
import {
  dimensions,
  metrics,
  type Cube,
  type CoverageEntry,
  type Dimension,
  type Drill,
  type Metric,
} from "../../api/warehouseAnalytics";
import { exact, hasKey, operatorRole, roles } from "./warehouseState";
import {
  DrillLink,
  Panel,
  Pick,
  Result,
  format,
  useWarehouse,
  useWarehouseRead,
} from "./WarehouseShared";
import { metricOptions, calendarBuckets, reach } from "./Trend";

const lineDimensions: ReadonlySet<Dimension> = new Set([
  "pn",
  "unit",
  "category",
  "brand",
  "warehouse",
  "bin_name",
  "test_result",
]);

function MatrixTable({
  data,
  row,
  col,
  base: parentScope,
}: {
  data: Cube;
  row: Dimension;
  col: Dimension;
  base: Drill;
}) {
  const base =
    lineDimensions.has(row) || lineDimensions.has(col)
      ? { ...parentScope, require_lines: true }
      : parentScope;
  const cols = data.columns;
  const cells = new Map(
    data.cells.map((cell) => [JSON.stringify([cell.row_key, cell.col_key]), cell.value]),
  );
  const peak = Math.max(0, ...data.cells.map((c) => c.value ?? 0));
  if (!data.rows.length || !cols.length) return <p className="wa-empty">当前筛选没有匹配的交叉记录。</p>;
  return (
    <div className="wa-scroll">
      <table className="wa-matrix">
        <thead>
          <tr>
            <th>
              {dimensions[row]} × {dimensions[col]}
            </th>
            {cols.map((c) => (
              <th key={JSON.stringify(c.key)} scope="col">
                {c.label}
                {col === "pn" && <small>{c.description || "描述未填"}</small>}
              </th>
            ))}
            <th>全列合计</th>
          </tr>
        </thead>
        <tbody>
          {data.rows.map((r) => (
            <tr key={JSON.stringify(r.key)}>
              <th scope="row">
                {r.label}
                {row === "pn" && <small>{r.description || "描述未填"}</small>}
              </th>
              {cols.map((c) => {
                const key = JSON.stringify([r.key, c.key]),
                  value = cells.get(key),
                  text = `${r.label} × ${c.label}`;
                return (
                  <td
                    key={key}
                    style={{
                      backgroundColor:
                        value != null && peak
                          ? `color-mix(in srgb, var(--mb-success) ${5 + (34 * value) / peak}%, transparent)`
                          : undefined,
                    }}
                  >
                    {cells.has(key) ? (
                      <DrillLink to={exact(base, { [row]: r.key, [col]: c.key })} label={text}>
                        {format(value)}
                      </DrillLink>
                    ) : (
                      "—"
                    )}
                  </td>
                );
              })}
              <td>
                <DrillLink to={exact(base, { [row]: r.key })} label={`${r.label} · 全列`}>
                  {format(r.value)}
                </DrillLink>
              </td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th>全行合计</th>
            {cols.map((c) => (
              <td key={JSON.stringify(c.key)}>
                <DrillLink to={exact(base, { [col]: c.key })} label={`${c.label} · 全行`}>
                  {format(c.value)}
                </DrillLink>
              </td>
            ))}
            <td>
              <DrillLink to={base} label="全集单据">
                {format(data.total)}
              </DrillLink>
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}
export function MatrixBoard() {
  const { filters, params, patch, coverage } = useWarehouse();
  const dims = { ...dimensions },
    rowRaw = params.get("rows"),
    colRaw = params.get("cols");
  const row = hasKey(dims, rowRaw) ? rowRaw : "center";
  const col = hasKey(dims, colRaw) && colRaw !== row ? colRaw : row === "kind" ? "source" : "kind";
  const cols = Object.fromEntries(Object.entries(dims).filter(([k]) => k !== row));
  const opts = metricOptions(filters.unit),
    m = params.get("measure"),
    metric: Metric = hasKey(opts, m) ? m : "documents";
  const role = operatorRole(filters.source, params.get("role"));
  const withRole = row === "operator" || col === "operator",
    base: Drill = withRole ? { operator_role: role } : {};
  const missingRole =
    withRole &&
    role === "outbound" &&
    !coverage?.sources.some((c) => c.source === "outbound" && c.available);
  const read = useWarehouseRead(
    "cube",
    { ...filters, ...base, row, col, metric, row_limit: 10, col_limit: 8 },
    !missingRole,
  );
  return (
    <Panel
      title="多维交叉矩阵"
      why="查找在哪个仓、哪种业务或哪个岗位集中。行列在筛选全集中分别截取，时间轴取最近有业务的时段。"
      actions={
        <>
          <Pick label="行维度" value={row} options={dims} change={(v) => patch({ rows: v })} />
          <Pick label="列维度" value={col} options={cols} change={(v) => patch({ cols: v })} />
          <Pick label="矩阵指标" value={metric} options={opts} change={(v) => patch({ measure: v })} />
          {withRole && (
            <Pick
              label="矩阵岗位"
              value={role}
              options={filters.source ? { [role]: roles[role] } : roles}
              change={(v) => patch({ role: v })}
            />
          )}
        </>
      }
    >
      {missingRole ? (
        <p className="wa-empty">实际出库（CKD）待接入，暂无该岗位数据。</p>
      ) : (
        <Result read={read}>
          {(data) => (
            <>
              <MatrixTable data={data} row={row} col={col} base={base} />
              <p className="wa-note">
                显示 {data.rows.length} / {data.total_rows} 行、{data.columns.length} /{" "}
                {data.total_columns} 列。{metrics[metric]}
                的边际与全集各自聚合。货品、单位等明细维度只计有子行的单据。
              </p>
            </>
          )}
        </Result>
      )}
    </Panel>
  );
}
export function PnWeeks({
  base,
  pn,
  description,
}: {
  base: Drill;
  pn: string;
  description?: string | null;
}) {
  const { filters, coverage, params, patch } = useWarehouse(),
    scope = exact({ ...base, require_lines: true }, { pn });
  const metric = params.get("weekMetric") === "documents" ? "documents" : "lines";
  const source = coverage?.sources.find((c) => c.source === scope.scope?.source);
  const start = filters.date_from || source?.min_date || undefined;
  const end = filters.date_to || source?.max_date || undefined;
  const weeks = calendarBuckets(start, end, "week", 8);
  const dateFrom = start && weeks[0] ? (start > weeks[0] ? start : weeks[0]) : undefined;
  const read = useWarehouseRead(
    "cube",
    {
      ...filters,
      ...scope,
      date_from: dateFrom,
      date_to: end,
      row: "week",
      col: "center",
      metric,
      row_limit: 8,
      col_limit: 20,
    },
    !!dateFrom && !!end && !!source?.available,
  );
  return (
    <Panel
      title="单型号周分布"
      why={
        pn +
        " · " +
        (description || "描述未填") +
        "。查看所选区间末尾最多八个自然周内，退货集中在哪些仓储中心。"
      }
      actions={
        <Pick
          label="周分布指标"
          value={metric}
          options={{ lines: "明细行数", documents: "单据数" }}
          change={(value) => patch({ weekMetric: value })}
        />
      }
    >
      {source && dateFrom && end && source.available ? (
        <Result read={read}>
          {(data) => (
            <WeekDistribution
              data={data}
              weeks={weeks}
              from={dateFrom}
              to={end}
              entry={source}
              selection={scope}
            />
          )}
        </Result>
      ) : (
        <p className="wa-empty">当前区间缺少可用的来源日期。</p>
      )}
    </Panel>
  );
}
function WeekDistribution({
  data,
  weeks,
  from,
  to,
  entry,
  selection,
}: {
  data: Cube;
  weeks: string[];
  from: string;
  to: string;
  entry: CoverageEntry;
  selection: Drill;
}) {
  const values = new Map(data.cells.map((c) => [JSON.stringify([c.row_key, c.col_key]), c.value]));
  const totals = new Map(data.rows.map((r) => [r.key, r.value]));
  const spans = weeks.map((week) => {
    const endOfWeek = dayjs(week).add(6, "day").format("YYYY-MM-DD");
    const start = from > week ? from : week;
    const end = to < endOfWeek ? to : endOfWeek;
    return { week, availability: reach(entry, start, end), dates: start + " 至 " + end };
  });
  const groups = [{ key: undefined, label: "全部中心" }, ...data.columns];
  return (
    <>
      <div className="wa-scroll">
        <table className="wa-matrix">
          <thead>
            <tr>
              <th>仓储中心 × 自然周</th>
              {spans.map((s) => (
                <th key={s.week} scope="col" title={s.dates}>
                  {s.week}
                  {s.availability === "partial" && <small>部分日期覆盖</small>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => (
              <tr key={g.key === undefined ? "all" : JSON.stringify(g.key)}>
                <th scope="row">{g.label}</th>
                {spans.map(({ week, availability }) => {
                  const value =
                    g.key === undefined ? totals.get(week) : values.get(JSON.stringify([week, g.key]));
                  const shown = value ?? (availability === "covered" ? 0 : null);
                  const cell = g.key === undefined ? { week } : { week, center: g.key };
                  return (
                    <td key={week}>
                      {value != null && value > 0 ? (
                        <DrillLink label={g.label + " × " + week} to={exact(selection, cell)}>
                          {format(value)}
                        </DrillLink>
                      ) : (
                        format(shown)
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="wa-note">
        周一为起点。0 表示来源首末日内没有匹配记录，—
        表示日期覆盖不足。全部中心含所有匹配单据，分行最多展示本区间前二十个中心。
      </p>
    </>
  );
}
