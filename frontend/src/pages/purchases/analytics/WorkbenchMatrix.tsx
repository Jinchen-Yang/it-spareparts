import { type Cube, type CubeQuery, type WbDimension } from "../../../api/purchaseWorkbench";
import {
  CubeBlock,
  dimensionNames,
  display,
  GroupName,
  MeasureTools,
  percent,
  Segments,
  useMeasure,
} from "./WorkbenchMeasures";
import { Drill, EmptyNote, Guide, Pick, useSetting, useWorkbench } from "./WorkbenchUI";

function pageOf(raw: string) {
  const n = Number(raw);
  return Number.isSafeInteger(n) && n >= 1 ? n : 1;
}
export function Pager({
  page,
  size,
  total,
  onChange,
  name = "页",
}: {
  page: number;
  size: number;
  total: number;
  onChange: (v: number) => void;
  name?: string;
}) {
  if (total <= size && page === 1) return null;
  const pages = Math.max(1, Math.ceil(total / size));
  return (
    <div className="pw-controls">
      <button
        type="button"
        className="pw-small"
        aria-label={"上一" + name}
        disabled={page <= 1}
        onClick={() => onChange(page - 1)}
      >
        ‹
      </button>
      <span>
        {name} {page} / {pages}
      </span>
      <button
        className="pw-small"
        type="button"
        aria-label={"下一" + name}
        disabled={page >= pages}
        onClick={() => onChange(page + 1)}
      >
        ›
      </button>
    </div>
  );
}
function HeatTable({
  data,
  query,
  unit,
  share,
  compact,
}: {
  data: Cube;
  query: CubeQuery;
  unit: string;
  share: boolean;
  compact: boolean;
}) {
  if (!data.rows.length || !data.columns.length) return <EmptyNote />;
  const cells = new Map(data.cells.map((c) => [JSON.stringify([c.row_key, c.column_key]), c]));
  const peak = Math.max(0, ...data.cells.map((c) => c.value ?? 0));
  return (
    <>
      <div className="pw-scroll">
        <table className="pw-table pw-heat">
          <thead>
            <tr>
              <th>
                {dimensionNames[query.row]} ＼ {dimensionNames[query.column!]}
              </th>
              {data.columns.map((c) => (
                <th key={JSON.stringify(c.key)}>
                  <GroupName item={c} dim={query.column!} />
                </th>
              ))}
              {!compact && <th>全列合计</th>}
            </tr>
          </thead>
          <tbody>
            {data.rows.map((r) => (
              <tr key={JSON.stringify(r.key)}>
                <th>
                  <GroupName item={r} dim={query.row} />
                </th>
                {data.columns.map((c) => {
                  const g = cells.get(JSON.stringify([r.key, c.key]));
                  const title = r.label + " × " + c.label + " · " + display(g?.value, query.metric, unit);
                  const shareValue = r.value != null && r.value > 0 && g?.value != null ? g.value / r.value : null;
                  return (
                    <td
                      key={JSON.stringify(c.key)}
                      title={g ? title : "当前范围无匹配记录"}
                      style={{
                        background:
                          g?.value != null && peak > 0
                            ? `color-mix(in srgb, var(--mb-success) ${5 + (38 * Math.max(0, g.value)) / peak}%, transparent)`
                            : undefined,
                      }}
                    >
                      {g ? (
                        <Drill scope={g.scope} label={title}>
                          {share
                            ? percent(shareValue)
                            : compact && query.metric === "amount" && g.value != null
                              ? (g.value / 10000).toFixed(2)
                              : display(g.value, query.metric, unit)}
                        </Drill>
                      ) : (
                        "—"
                      )}
                    </td>
                  );
                })}
                {!compact && (
                  <td>
                    <Drill scope={r.scope} label={r.label + " 所有列"}>
                      {display(r.value, query.metric, unit)}
                    </Drill>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
          {!compact && (
            <tfoot>
              <tr>
                <th>全行合计</th>
                {data.columns.map((c) => (
                  <td key={JSON.stringify(c.key)}>
                    <Drill scope={c.scope} label={c.label + " 所有行"}>
                      {display(c.value, query.metric, unit)}
                    </Drill>
                  </td>
                ))}
                <td>{display(data.total.value, query.metric, unit)}</td>
              </tr>
            </tfoot>
          )}
        </table>
      </div>
      <div className="pw-foot">
        <span>
          {data.rows.length}/{data.total_rows} 行 · {data.columns.length}/{data.total_columns} 列
        </span>
        <span>色阶 0 至 {display(peak, query.metric, unit)} · — 无记录或无金额</span>
      </div>
    </>
  );
}
export function MatrixCard({
  row,
  column,
  compact = false,
  metricOverride,
}: {
  row: WbDimension;
  column: WbDimension;
  compact?: boolean;
  metricOverride?: CubeQuery["metric"];
}) {
  const { metric: chosen, basis, unit: chosenUnit, get, set } = useMeasure(!compact),
    { scope } = useWorkbench();
  const unit = scope?.unit || chosenUnit,
    metric = metricOverride || chosen;
  const page = compact ? 1 : pageOf(get("rpage", "1")),
    cp = compact ? 1 : pageOf(get("cpage", "1"));
  const q: CubeQuery = {
    row,
    column,
    basis,
    metric,
    page,
    page_size: compact ? 5 : 15,
    column_page: cp,
    column_size: compact ? 5 : 8,
    scope: metric === "qty" ? { unit } : {},
  };
  const share = !compact && ["amount", "qty", "lines"].includes(metric) && get("show", "value") === "share";
  const section = dimensionNames[row] + " × " + dimensionNames[column];
  return (
    <Guide
      title={section}
      note={
        compact
          ? "两轴各取前五，点交叉格查看采购证据。" + (metric === "amount" ? "格内金额为万元。" : "")
          : "各行列单独去重，行内占比以该行全部记录为分母。"
      }
      extra={
        compact ? (
          <button
            className="pw-small"
            type="button"
            onClick={() => set({ matrix: "pivot", row, column, rpage: "", cpage: "" }, true)}
          >
            展开全表 ↗
          </button>
        ) : undefined
      }
    >
      <CubeBlock query={q}>
        {(data) => (
          <>
            <HeatTable data={data} query={q} unit={unit} compact={compact} share={share} />
            {!compact && (
              <div className="pw-tools">
                <Pager
                  name="行页"
                  page={page}
                  size={15}
                  total={data.total_rows}
                  onChange={(v) => set({ rpage: String(v) })}
                />
                <Pager
                  name="列页"
                  page={cp}
                  size={8}
                  total={data.total_columns}
                  onChange={(v) => set({ cpage: String(v) })}
                />
              </div>
            )}
          </>
        )}
      </CubeBlock>
    </Guide>
  );
}
export default function WorkbenchMatrix() {
  const { supplierRestricted } = useWorkbench();
  const { metric, get, set } = useMeasure(true);
  const mode = get("matrix", "wall") === "pivot" ? "pivot" : "wall";
  const axes: WbDimension[] = [
    "buyer",
    "type",
    ...(supplierRestricted ? [] : ["supplier" as const]),
    "pn",
    "category",
    "pool",
    "week",
  ];
  const row = axes.find((d) => d === get("row", "buyer")) || "buyer";
  const column = axes.find((d) => d === get("column", "type") && row !== d) || axes.find((d) => d !== row)!;
  const axisChoices = (exclude: WbDimension) =>
    axes.filter((x) => x !== exclude).map((x) => [x, dimensionNames[x]] as const);
  return (
    <>
      <div className="pw-tools">
        <Segments
          options={[
            ["wall", "四矩阵联览"],
            ["pivot", "自选交叉表"],
          ]}
          value={mode}
          onChange={(v) => set({ matrix: v }, true)}
        />
        <MeasureTools average={mode === "pivot"} />
      </div>
      {mode === "wall" ? (
        <div className="pw-grid">
          <MatrixCard row="buyer" column="type" compact />
          {!supplierRestricted && (
            <>
              <MatrixCard row="supplier" column="type" compact />
              <MatrixCard row="buyer" column="supplier" compact />
              <MatrixCard row="pn" column="supplier" compact />
            </>
          )}
        </div>
      ) : (
        <>
          <div className="pw-tools">
            <Pick
              label="行维度"
              value={row}
              options={axisChoices(column)}
              onChange={(v) => set({ row: v, rpage: "", cpage: "" })}
            />
            <button
              className="pw-small"
              type="button"
              onClick={() => set({ row: column, column: row, rpage: "", cpage: "" })}
            >
              交换行列
            </button>
            <Pick
              label="列维度"
              value={column}
              options={axisChoices(row)}
              onChange={(v) => set({ column: v, rpage: "", cpage: "" })}
            />
            {["amount", "qty", "lines"].includes(metric) && (
              <Segments
                value={get("show", "value")}
                options={[
                  ["value", "实际值"],
                  ["share", "行内占比"],
                ]}
                onChange={(v) => set({ show: v })}
              />
            )}
          </div>
          <MatrixCard row={row} column={column} />
        </>
      )}
    </>
  );
}
