import WorkbenchChartPoint from "./WorkbenchChartPoint";
import { fetchSharedPns } from "../../../api/purchaseWorkbench";
import {
  basisNames,
  cash,
  CubeBlock,
  dimensionNames,
  display,
  GroupName,
  num,
  percent,
  positivePage,
  Segments,
  useMeasure,
} from "./WorkbenchMeasures";
import { Drill, EmptyNote, Guide, Pick, QueryBlock, useSetting, useWorkbench } from "./WorkbenchUI";
import { MatrixCard, Pager } from "./WorkbenchMatrix";
function SharedPns() {
  const { filters, scope } = useWorkbench(),
    { get, set } = useSetting();
  const page = positivePage(get("peerPage", "1"));
  return (
    <Guide title="供应商的共同成交 PN" note="两家供应商都成交过哪个型号？分别点两侧记录核对单头批次行。">
      <CubeBlock
        query={{
          row: "supplier",
          basis: "paid",
          metric: "orders",
          page_size: 50,
          page: positivePage(get("srcPage", "1")),
        }}
      >
        {(d) => {
          if (!d.rows.length) return <EmptyNote />;
          const center = d.rows.find((r) => r.key === get("center", "")) || d.rows.find((r) => r.key !== null);
          if (!center?.key) return <EmptyNote>没有可识别的供应商。</EmptyNote>;
          return (
            <>
              <div className="pw-tools">
                <Pick
                  label="中心供应商"
                  value={center.key}
                  options={d.rows.filter((r) => r.key !== null).map((r) => [r.key!, r.label])}
                  onChange={(v) => set({ center: v, peerPage: "" })}
                />
                <Pager
                  total={d.total_rows}
                  size={50}
                  page={positivePage(get("srcPage", "1"))}
                  onChange={(v) => set({ srcPage: String(v), center: "", peerPage: "" })}
                />
              </div>
              <QueryBlock
                id={JSON.stringify(["shared", center.key, page])}
                load={() => fetchSharedPns(filters, scope || {}, center.key!, page)}
              >
                {(result) => (
                  <>
                    {!result.items.length ? (
                      <EmptyNote>当前范围没有共同供应的 PN。</EmptyNote>
                    ) : (
                      <div className="pw-scroll">
                        <table className="pw-table">
                          <thead>
                            <tr>
                              <th>对方供应商</th>
                              <th>共同 PN 数</th>
                              <th>型号 / 目录描述</th>
                              <th>{center.label}</th>
                              <th>对方记录</th>
                            </tr>
                          </thead>
                          <tbody>
                            {result.items.map((r) => (
                              <tr key={JSON.stringify([r.supplier_id, r.pn])}>
                                <td>{r.supplier}</td>
                                <td>{r.shared_pns}</td>
                                <td className="pw-name">
                                  {r.pn}
                                  <small>{r.description || "目录描述未填"}</small>
                                </td>
                                <td>
                                  <Drill scope={r.center_scope} label={center.label + " · " + r.pn}>
                                    查看采购
                                  </Drill>
                                </td>
                                <td>
                                  <Drill scope={r.peer_scope} label={r.supplier + " · " + r.pn}>
                                    查看采购
                                  </Drill>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                    <Pager
                      page={page}
                      size={20}
                      total={result.total}
                      onChange={(v) => set({ peerPage: String(v) })}
                    />
                  </>
                )}
              </QueryBlock>
            </>
          );
        }}
      </CubeBlock>
    </Guide>
  );
}
function SpendCloud({ row }: { row: "customer" | "salesperson" }) {
  const { basis, get, set } = useMeasure(),
    { costRestricted } = useWorkbench();
  const page = positivePage(get("cloudPage", "1")),
    metric = costRestricted ? "orders" : "amount";
  return (
    <Guide
      title={dimensionNames[row] + "采购频次与金额"}
      note={"横轴为去重采购单数，纵轴为" + basisNames[basis] + "采购金额，展示当前页的对象。"}
    >
      <CubeBlock query={{ row, metric, basis, page, page_size: 20 }}>
        {(d) => {
          if (!d.rows.length) return <EmptyNote />;
          const maxX = Math.max(1, ...d.rows.map((r) => r.orders)),
            maxY = Math.max(0, ...d.rows.map((r) => r.value || 0)),
            minY = Math.min(0, ...d.rows.map((r) => r.value || 0));
          return (
            <>
              {!costRestricted && (
                <>
                  <div className="pw-cloud">
                    {d.rows.map((r, i) =>
                      r.value == null ? null : (
                        <div
                          key={JSON.stringify(r.key)}
                          style={{
                            left: (r.orders / maxX) * 95 + "%",
                            bottom: ((r.value - minY) / (maxY - minY || 1)) * 90 + "%",
                          }}
                        >
                          <WorkbenchChartPoint
                            measure={r}
                            label={r.label + " · " + r.orders + " 单 · " + cash(r.value)}
                            x={["采购单数", r.orders + " 单"]}
                            y={[basisNames[basis] + "采购金额", cash(r.value)]}
                            detail={[[dimensionNames[row], r.label]]}
                          >
                            {i + 1}
                          </WorkbenchChartPoint>
                        </div>
                      ),
                    )}
                  </div>
                  <p className="pw-note">
                    单数上限 {maxX} · 金额 {cash(minY)} 至 {cash(maxY)}
                  </p>
                </>
              )}
              <div className="pw-legend">
                {d.rows.map((r, i) => (
                  <Drill key={JSON.stringify(r.key)} scope={r.scope} label={r.label + " 采购记录"}>
                    {i + 1}. {r.label}（{r.orders} 单）
                  </Drill>
                ))}
              </div>
              <Pager page={page} size={20} total={d.total_rows} onChange={(v) => set({ cloudPage: String(v) })} />
            </>
          );
        }}
      </CubeBlock>
    </Guide>
  );
}

function CustomerMix() {
  const { costRestricted, scope } = useWorkbench();
  const { basis, get, set } = useMeasure();
  const mode = get("mixMetric", "amount") === "qty" ? "qty" : costRestricted ? "orders" : "amount";
  const unit = scope?.unit || get("mixUnit", "件"),
    colors = [
      "var(--mb-success)",
      "var(--mb-accent)",
      "var(--mb-warning)",
      "var(--mb-presales-purple)",
      "var(--mb-text-3)",
      "var(--mb-danger)",
    ];
  return (
    <Guide
      title="采购类型对应哪些客户？"
      note="各色段表示该客户占本类型的比例。保留未展示客户的份额，点色段可核对采购单。"
    >
      <div className="pw-tools">
        <Segments
          options={[
            [costRestricted ? "orders" : "amount", costRestricted ? "单数" : "金额"],
            ["qty", "采购量"],
          ]}
          value={mode}
          onChange={(v) => set({ mixMetric: v })}
        />
        {mode === "qty" && (
          <CubeBlock query={{ row: "unit", metric: "orders", basis, page_size: 100 }}>
            {(d) => (
              <Pick
                label="构成数量单位"
                value={unit}
                options={[...new Set([unit, ...d.rows.flatMap((r) => (r.key ? [r.key] : []))])].map((k) => [k, k])}
                onChange={(v) => set({ mixUnit: v })}
              />
            )}
          </CubeBlock>
        )}
      </div>
      <CubeBlock
        query={{
          row: "type",
          column: "customer",
          metric: mode,
          basis,
          page_size: 30,
          column_size: 6,
          scope: mode === "qty" ? { unit } : {},
        }}
      >
        {(d) => (
          <>
            {d.rows.map((r) => {
              const cells = d.cells.filter((c) => c.row_key === r.key);
              return (
                <div className="pw-mix-row" key={JSON.stringify(r.key)}>
                  <b>{r.label}</b>
                  <div className="pw-stack">
                    {r.value != null && r.value > 0 && cells.every((c) => c.value != null && c.value >= 0) ? (
                      cells.map((c) => {
                        const col = d.columns.findIndex((k) => k.key === c.column_key),
                          name = d.columns[col].label,
                          p = (c.value ?? 0) / r.value!;
                        return (
                          <span
                            key={JSON.stringify(c.column_key)}
                            style={{ width: percent(p), background: colors[col % colors.length] }}
                          >
                            <WorkbenchChartPoint
                              measure={c}
                              label={r.label + " × " + name}
                              x={["客户占该类型比例", percent(p)]}
                              y={["采购类型", r.label]}
                              detail={[
                                ["客户", name],
                                [mode === "amount" ? basisNames[basis] + "采购金额" : mode === "orders" ? "该客户采购单数" : "采购量", display(c.value, mode, unit)]
                              ]}
                            >
                              <span className="pw-sr">{name}</span>
                            </WorkbenchChartPoint>
                          </span>
                        );
                      })
                    ) : (
                      <small>当前值不适合计算占比</small>
                    )}
                  </div>
                  <span>{display(r.value, mode, unit)}</span>
                </div>
              );
            })}
            {d.rows.length === 0 && <EmptyNote />}
            <div className="pw-legend">
              {d.columns.map((c, i) => (
                <span key={JSON.stringify(c.key)}>
                  <i style={{ background: colors[i % colors.length] }} /> {c.label}
                </span>
              ))}
              {d.total_columns > d.columns.length && <span>灰底为其余客户</span>}
            </div>
          </>
        )}
      </CubeBlock>
    </Guide>
  );
}
function SalesLinks() {
  const { customerRestricted, costRestricted } = useWorkbench(),
    { basis, get, set } = useMeasure();
  const page = positivePage(get("linkPage", "1")),
    cloudDim = get("cloud", "customer") === "salesperson" || customerRestricted ? "salesperson" : "customer";
  return (
    <>
      {!customerRestricted && (
        <div className="pw-grid">
          <CustomerMix />
          <div>
            <Segments
              value={cloudDim}
              options={[
                ["customer", "客户"],
                ["salesperson", "销售人员"],
              ]}
              onChange={(v) => set({ cloud: v, cloudPage: "" })}
            />
            <SpendCloud row={cloudDim} />
          </div>
        </div>
      )}
      <Guide
        title="销售单关联的采购支出"
        note="以实际记录的关联销售单号归集，空号、缺少关联记录分别呈现。点击数值核对可见采购单。"
      >
        <CubeBlock query={{ row: "linkStatus", metric: "orders", basis, page_size: 5 }}>
          {(d) => (
            <div className="pw-tools">
              {d.rows.map((r) => (
                <Drill key={r.key} scope={r.scope} label={r.label + " 的采购单"}>
                  {r.label} · {num(r.orders)} 单
                </Drill>
              ))}
            </div>
          )}
        </CubeBlock>
        <CubeBlock
          query={{
            row: "linkedSalesOrderNo",
            metric: costRestricted ? "orders" : "amount",
            basis,
            page,
            page_size: 15,
          }}
        >
          {(d) => (
            <>
              {!d.rows.length ? (
                <EmptyNote />
              ) : (
                <div className="pw-scroll">
                  <table className="pw-table">
                    <thead>
                      <tr>
                        <th>关联销售单</th>
                        <th>采购单数</th>
                        <th>明细行</th>
                        {!costRestricted && <th>{basisNames[basis]}金额</th>}
                      </tr>
                    </thead>
                    <tbody>
                      {d.rows.map((r) => (
                        <tr key={JSON.stringify(r.key)}>
                          <th>
                            <GroupName item={r} dim="linkedSalesOrderNo" />
                          </th>
                          <td>
                            <Drill scope={r.scope} label={r.label + " 关联采购单"}>
                              {r.orders}
                            </Drill>
                          </td>
                          <td>{r.lines}</td>
                          {!costRestricted && <td>{cash(r.value)}</td>}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <Pager page={page} size={15} total={d.total_rows} onChange={(v) => set({ linkPage: String(v) })} />
            </>
          )}
        </CubeBlock>
      </Guide>
    </>
  );
}
export default function WorkbenchStructure() {
  const { supplierRestricted, customerRestricted, costRestricted } = useWorkbench(),
    { basis, get, set } = useMeasure();
  const choices = [
    ["types", "类型结构"],
    ...(!supplierRestricted
      ? [
          ["suppliers", "供应商矩阵"],
          ["relations", "共同供货"],
        ]
      : []),
    ...(!customerRestricted ? [["links", "销采关联"]] : []),
  ].map(([k, v]) => [k, v] as const);
  const selected = choices.find(([k]) => k === get("struct", "types"))?.[0] || "types";
  return (
    <>
      <div className="pw-tools">
        <Segments value={selected} options={choices} onChange={(v) => set({ struct: v }, true)} />
      </div>
      {selected === "relations" ? (
        <SharedPns />
      ) : selected === "links" ? (
        <SalesLinks />
      ) : selected === "suppliers" ? (
        <MatrixCard row="supplier" column="type" />
      ) : (
        <Guide title="各类采购的规模" note="并列原始采购类型的金额、去重单数、明细行和供应商数；按明细单位拆量。">
          <CubeBlock query={{ row: "type", metric: costRestricted ? "orders" : "amount", basis, page_size: 50 }}>
            {(d) => (
              <>
                {d.rows.length ? (
                  <div className="pw-scroll">
                    <table className="pw-table">
                      <thead>
                        <tr>
                          <th>采购类型</th>
                          <th>采购单数</th>
                          <th>明细行</th>
                          {!supplierRestricted && <th>供应商数</th>}
                          {!costRestricted && <th>{basisNames[basis]}金额</th>}
                        </tr>
                      </thead>
                      <tbody>
                        {d.rows.map((r) => (
                          <tr key={JSON.stringify(r.key)}>
                            <th>
                              <Drill scope={r.scope} label={r.label + " 的采购单"}>
                                {r.label}
                              </Drill>
                            </th>
                            <td>{r.orders}</td>
                            <td>{r.lines}</td>
                            {!supplierRestricted && <td>{num(r.supplier_count)}</td>}
                            {!costRestricted && <td>{cash(r.value)}</td>}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <EmptyNote />
                )}
              </>
            )}
          </CubeBlock>
          <CubeBlock query={{ row: "type", metric: costRestricted ? "orders" : "amount", basis, page_size: 50 }}>
            {(d) => {
              if (!d.rows.length) return null;
              // JSON encodes the genuine missing key independently from the literal text “未填写”.
              const type = d.rows.find((r) => JSON.stringify(r.key) === get("qtyType", "")) || d.rows[0];
              return (
                <>
                  <div className="pw-tools">
                    <Pick
                      label="查看分单位采购量"
                      value={JSON.stringify(type.key)}
                      options={d.rows.map((r) => [JSON.stringify(r.key), r.label])}
                      onChange={(v) => set({ qtyType: v })}
                    />
                  </div>
                  <CubeBlock
                    query={{ row: "unit", metric: "qty", basis, page_size: 100, scope: { type: type.key } }}
                  >
                    {(units) => (
                      <div className="pw-legend">
                        {units.rows.map((r) => (
                          <Drill key={JSON.stringify(r.key)} scope={r.scope} label={type.label + " · " + r.label}>
                            {num(r.value)} {r.label}
                          </Drill>
                        ))}
                        {!units.rows.length && <EmptyNote />}
                      </div>
                    )}
                  </CubeBlock>
                </>
              );
            }}
          </CubeBlock>
        </Guide>
      )}
    </>
  );
}
