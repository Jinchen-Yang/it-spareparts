import { useState } from "react";
import { Button, Input } from "antd";
import {
  fetchPoolMembers,
  fetchPoolParts,
  fetchPoolSources,
  type PoolFocus,
  type PoolPart,
  type PoolSource,
} from "../../../api/purchasePoolAnalytics";
import { useLocalRestrictions } from "../../boss/shared";
import {
  Drill,
  EmptyNote,
  Guide,
  Pick,
  QueryBlock,
  useSetting,
  useWorkbench,
  WorkbenchProvider,
  type WorkbenchProps,
} from "./WorkbenchUI";
import "./analyticsWorkbench.css";
import "./purchasePoolAnalytics.css";

const count = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 });
const money = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const num = (n: number | null) => (n == null ? "—" : count.format(n));
const price = (n: number | null) => (n == null ? "—" : money.format(n));
const pct = (n: number | null) => (n == null ? "—" : (n > 0 ? "+" : "") + (n * 100).toFixed(1) + "%");
function exactPart(part: Pick<PoolPart, "part_id" | "pn">, unit: string | null) {
  // SQL btrim(pn_std) removes ordinary spaces; the source identity disambiguates aliases.
  return { pn: part.pn.replace(/^ +| +$/g, "") || null, partId: String(part.part_id), unit, _line: "1" };
}
const size = 20;
const options: [PoolFocus, string][] = [
  ["all", "全部 PN"],
  ["single", "仅 1 家已知供应商"],
  ["unpooled", "尚未入池"],
  ["above_pool", "均价高于同池"],
  ["above_cap", "均价超过人工上限"],
];

function Pager({ page, more, go }: { page: number; more: boolean; go: (n: number) => void }) {
  if (page === 1 && !more) return null;
  return (
    <div className="pw-foot">
      <span>第 {page} 页</span>
      <span>
        <Button size="small" disabled={page === 1} onClick={() => go(page - 1)}>
          上一页
        </Button>{" "}
        <Button size="small" disabled={!more} onClick={() => go(page + 1)}>
          下一页
        </Button>
      </span>
    </div>
  );
}

function PartName({ pn, description }: { pn: string; description: string | null }) {
  return (
    <span className="pw-name">
      {pn}
      <small title={description || undefined}>
        {description ? "目录 · " + description : "目录描述未填"}
      </small>
    </span>
  );
}

function rangeRows(source: PoolSource[], visible: boolean, part: PoolPart) {
  const minima = source.flatMap((s) => (s.price_min == null ? [] : [s.price_min]));
  const maxima = source.flatMap((s) => (s.price_max == null ? [] : [s.price_max]));
  const lower = Math.min(...minima),
    upper = Math.max(...maxima);
  const position = (value: number) => (upper === lower ? 50 : (100 * (value - lower)) / (upper - lower));
  return (
    <div className="pw-scroll">
      <table className="pw-table">
        <thead>
          <tr>
            <th>供应商</th>
            <th>采购量</th>
            <th>单数</th>
            {visible && (
              <>
                <th>未税区间 / 加权均价</th>
                <th>价格样本</th>
              </>
            )}
          </tr>
        </thead>
        <tbody>
          {source.map((s) => (
            <tr key={s.supplier_id ?? "empty"}>
              <td>
                <Drill
                  scope={{
                    ...exactPart(part, part.unit),
                    supplier: s.supplier_id == null ? null : String(s.supplier_id),
                  }}
                  label={part.pn + " · " + (s.supplier || "未填供应商") + " · 采购单"}
                >
                  {s.supplier || "未填供应商"}
                </Drill>
              </td>
              <td>
                {num(s.quantity)} {part.unit}
              </td>
              <td>{num(s.orders)}</td>
              {visible && (
                <>
                  <td>
                    {s.price_min != null && s.price_max != null && s.price != null ? (
                      <>
                        <span className="ppa-range" aria-hidden="true">
                          <i
                            style={{
                              left: position(s.price_min) + "%",
                              width: position(s.price_max) - position(s.price_min) + "%",
                            }}
                          />
                          <b style={{ left: position(s.price) + "%" }} />
                        </span>
                        {price(s.price_min)} – {price(s.price_max)} · 均 {price(s.price)}
                      </>
                    ) : (
                      "无可比样本"
                    )}
                  </td>
                  <td>
                    {num(s.price_orders)} 单 · {num(s.price_qty)} {part.unit}
                  </td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Evidence({ part, prices, suppliers }: { part: PoolPart; prices: boolean; suppliers: boolean }) {
  const { filters, scope } = useWorkbench();
  const [sp, setSp] = useState(1),
    [mp, setMp] = useState(1);
  const base = { scope: JSON.stringify(scope || {}), unit: part.unit, page_size: size };
  const sourceQuery = { ...base, part_id: part.part_id, page: sp };
  const memberQuery = { ...base, pool_id: part.pool_id ?? undefined, page: mp };
  return (
    <div className="pw-grid ppa-evidence">
      {suppliers && (
        <Guide
          title={part.pn + " · 采购来源"}
          note={
            (part.description || "目录描述未填") +
            "。区间端点为有效未税单价的极值，圆点为数量加权均价。本页共用横轴。"
          }
        >
          <QueryBlock
            id={"sources" + JSON.stringify(sourceQuery)}
            load={() => fetchPoolSources(filters, sourceQuery)}
          >
            {(data) => (
              <>
                {data.items.length ? rangeRows(data.items, prices, part) : <EmptyNote />}
                <Pager page={sp} more={data.has_more} go={setSp} />
              </>
            )}
          </QueryBlock>
        </Guide>
      )}
      {part.pool_id != null && (
        <Guide
          title={"同池成员 · " + part.pool_name}
          note={"当前筛选范围，按 " + (part.unit || "单位未填") + " 核对各成员采购事实。"}
        >
          <QueryBlock
            id={"members" + JSON.stringify(memberQuery)}
            load={() => fetchPoolMembers(filters, memberQuery)}
          >
            {(data) => (
              <>
                <div className="pw-scroll">
                  <table className="pw-table">
                    <thead>
                      <tr>
                        <th>成员 PN</th>
                        <th>单数</th>
                        <th>数量</th>
                        {prices && <th>未税均价</th>}
                      </tr>
                    </thead>
                    <tbody>
                      {data.items.map((m) => (
                        <tr key={m.part_id}>
                          <td>
                            {m.orders ? (
                              <Drill scope={exactPart(m, part.unit)} label={m.pn + " 的采购单"}>
                                <PartName pn={m.pn} description={m.description} />
                              </Drill>
                            ) : (
                              <PartName pn={m.pn} description={m.description} />
                            )}
                          </td>
                          <td>{m.orders}</td>
                          <td>{num(m.quantity)}</td>
                          {prices && <td>{price(m.price)}</td>}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <Pager page={mp} more={data.has_more} go={setMp} />
              </>
            )}
          </QueryBlock>
        </Guide>
      )}
    </div>
  );
}

function PoolView() {
  const { filters, scope, costRestricted, supplierRestricted } = useWorkbench();
  const { get, set } = useSetting();
  const { governance } = useLocalRestrictions();
  const q = get("poolQuery", ""),
    pool = Number(get("poolId", "0")) || undefined;
  const page = Math.min(10000, Math.max(1, Math.trunc(Number(get("poolPage", "1")) || 1)));
  const choices = options.filter(
    ([key]) =>
      !(key === "single" && supplierRestricted) &&
      !(key.startsWith("above") && (costRestricted || governance)),
  );
  const focus = choices.find(([k]) => k === get("poolFocus", "all"))?.[0] || "all";
  const query = { scope: JSON.stringify(scope || {}), q, pool_id: pool, focus, page, page_size: size };
  function narrow(values: Record<string, string>) {
    set({ poolPage: "", poolPart: "", poolUnit: "", ...values });
  }
  return (
    <div className="purchase-workbench">
      <Guide
        title="PN 与互通池"
        note="采购单数、行数与数量统计经营事实。未税参考价取正单价、正数量、已知单位的有效样本，剔除已确认源错。池均价按同单位采购量加权。人工上限需池成员目录单位一致。"
      >
        <div className="pw-controls">
          <Input.Search
            key={q}
            aria-label="搜索 PN、型号或目录描述"
            placeholder="PN 或目录描述"
            defaultValue={q}
            maxLength={200}
            onSearch={(v) => narrow({ poolQuery: v.trim() })}
            allowClear
            style={{ maxWidth: 300 }}
          />
          <Pick
            label="观察项"
            value={focus}
            options={choices}
            onChange={(v) => narrow({ poolFocus: v })}
          />
          {pool && (
            <Button size="small" onClick={() => narrow({ poolId: "" })}>
              清除池筛选
            </Button>
          )}
        </div>
        {scope?.supplier || filters.supplier ? (
          <p className="pw-note">已限定供应商，来源数与池均价仅取所选供应商范围。</p>
        ) : null}
        {scope && Object.prototype.hasOwnProperty.call(scope, "pn") && (
          <p className="pw-note">
            已限定 PN，池均价仅取该 PN 的匹配样本。可在产品筛选中清除 PN 查看同池对照。
          </p>
        )}
        <QueryBlock id={"parts" + JSON.stringify(query)} load={() => fetchPoolParts(filters, query)}>
          {(data) => {
            const selected = data.items.find(
              (p) => String(p.part_id) === get("poolPart", "") && (p.unit || "") === get("poolUnit", ""),
            );
            return (
              <>
                {!data.price_visible && <p className="pw-note">当前账号无采购成本或池价格权限。</p>}
                {data.items.length ? (
                  <div className="pw-scroll">
                    <table className="pw-table ppa-list">
                      <thead>
                        <tr>
                          <th>PN / 型号</th>
                          <th>所属池</th>
                          <th>单数</th>
                          <th>明细行</th>
                          <th>采购量 / 单位</th>
                          {data.supplier_visible && <th>已知供应商数</th>}
                          {data.price_visible && (
                            <>
                              <th>PN 均价（元）</th>
                              <th>池均价</th>
                              <th>较池均价</th>
                              <th>人工上限</th>
                              <th>较上限</th>
                              <th>价格样本单数</th>
                            </>
                          )}
                        </tr>
                      </thead>
                      <tbody>
                        {data.items.map((p) => (
                          <tr
                            key={JSON.stringify([p.part_id, p.unit])}
                            className={p === selected ? "ppa-selected" : ""}
                          >
                            <td>
                              <button
                                type="button"
                                className="pw-link"
                                aria-pressed={p === selected}
                                onClick={() =>
                                  set({ poolPart: String(p.part_id), poolUnit: p.unit || "" }, true)
                                }
                              >
                                <PartName pn={p.pn} description={p.description} />
                              </button>
                            </td>
                            <td>
                              {p.pool_id == null ? (
                                "未入池"
                              ) : (
                                <button
                                  type="button"
                                  className="pw-link"
                                  onClick={() => narrow({ poolId: String(p.pool_id) })}
                                >
                                  {p.pool_name}
                                </button>
                              )}
                            </td>
                            <td>
                              <Drill scope={exactPart(p, p.unit)} label={p.pn + " · 采购单"}>
                                {num(p.orders)}
                              </Drill>
                            </td>
                            <td>{p.lines}</td>
                            <td>
                              {num(p.quantity)} {p.unit || "单位未填"}
                            </td>
                            {data.supplier_visible && <td>{num(p.suppliers)}</td>}
                            {data.price_visible && (
                              <>
                                <td>{price(p.price)}</td>
                                <td>{price(p.pool_price)}</td>
                                <td>{pct(p.delta_pool)}</td>
                                <td>{price(p.ceiling)}</td>
                                <td>{pct(p.delta_cap)}</td>
                                <td>{num(p.price_orders)}</td>
                              </>
                            )}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <EmptyNote>当前筛选没有符合条件的 PN。</EmptyNote>
                )}
                <Pager
                  page={page}
                  more={data.has_more}
                  go={(n) => set({ poolPage: String(n), poolPart: "", poolUnit: "" })}
                />
                {selected && (
                  <Evidence
                    key={JSON.stringify([query, selected.part_id, selected.unit])}
                    part={selected}
                    prices={data.price_visible}
                    suppliers={data.supplier_visible}
                  />
                )}
              </>
            );
          }}
        </QueryBlock>
      </Guide>
    </div>
  );
}

export default function PurchasePoolAnalytics(props: Omit<WorkbenchProps, "view"> & { view?: string }) {
  return (
    <WorkbenchProvider {...props} view="pool">
      <PoolView />
    </WorkbenchProvider>
  );
}
