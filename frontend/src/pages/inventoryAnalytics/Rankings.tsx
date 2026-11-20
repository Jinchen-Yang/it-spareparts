import { metrics, type Drill, type Metric, type RankRow } from "../../api/warehouseAnalytics";
import { exact, hasKey, operatorRole, pageNumber, pageSize, roles } from "./warehouseState";
import {
  DataTable,
  DrillLink,
  Pager,
  Panel,
  Pick,
  Result,
  Tile,
  format,
  useWarehouse,
  useWarehouseRead,
} from "./WarehouseShared";
import { metricOptions, Trend } from "./Trend";
import { PnWeeks } from "./Matrix";

export function RankBoard({ people }: { people: boolean }) {
  const { filters, params, patch, coverage } = useWarehouse();
  const by = people ? "operator" : "pn",
    prefix = people ? "people" : "ret";
  const page = pageNumber(params.get(prefix + "Page"));
  const options = {
      ...metricOptions(filters.unit, people),
      ...(people ? { active_days: metrics.active_days } : {}),
    },
    metricParam = params.get(prefix + "Sort");
  const metric: Metric = hasKey(options, metricParam) ? metricParam : people ? "documents" : "lines";
  const role = operatorRole(filters.source, params.get("role"));
  const returnOptions: Record<string, string> = {};
  if (filters.source !== "direct")
    Object.assign(returnOptions, { sales: "返库 · 销售退货", all: "返库 · 全部类别" });
  if (!filters.source || filters.source === "direct") returnOptions.direct = "直采直退";
  const rawReturns = params.get("returns");
  const ret = hasKey(returnOptions, rawReturns)
    ? rawReturns
    : filters.source === "direct"
      ? "direct"
      : "sales";
  const returnCompatible =
    people || !filters.source || filters.source === "return" || filters.source === "direct";
  const base: Drill = people
    ? { operator_role: role }
    : {
        scope:
          ret === "direct"
            ? { source: "direct" }
            : ret === "all"
              ? { source: "return" }
              : { source: "return", kind: "销售退货" },
      };
  const missingRole =
    people &&
    role === "outbound" &&
    !coverage?.sources.some((c) => c.source === "outbound" && c.available);
  const requested = useWarehouseRead(
    "rankings",
    { ...filters, ...base, by, metric, page, page_size: pageSize },
    !missingRole && returnCompatible,
  );
  const frameTitle = people ? "岗位工作量排行" : "退货 PN 排行";
  const dimName = people ? "人员" : ret === "direct" ? "货品原文" : "PN";
  function focus(row: RankRow) {
    patch({ [prefix + "Focus"]: row.key });
  }
  const selectedKey = params.get(prefix + "Focus");
  const resetPage = (changes: Record<string, string | null>) =>
    patch({ ...changes, [prefix + "Page"]: null, [prefix + "Focus"]: null });
  if (!returnCompatible)
    return (
      <Panel title="退货 PN 聚集" why="比较返库和直采直退的型号、频次与仓储中心分布。">
        <p className="wa-empty">当前来源没有退货业务，可选择返库单或直采直退。</p>
      </Panel>
    );
  return (
    <>
      <Panel
        title={frameTitle}
        why={
          people
            ? "按去重单数、匹配行数、不同 PN 或指定单位数量比较同岗位人员，角色分别归集。"
            : "查找反复出现在多张退货单上的型号；前十集中度按所有有 PN 明细的行数计算。"
        }
        actions={
          <>
            {people ? (
              <Pick
                label="岗位范围"
                value={role}
                options={filters.source ? { [role]: roles[role] } : roles}
                change={(v) => resetPage({ role: v })}
              />
            ) : (
              <Pick
                label="退货范围"
                value={ret}
                options={returnOptions}
                change={(v) => resetPage({ returns: v })}
              />
            )}
            <Pick
              label="排行指标"
              value={metric}
              options={options}
              change={(v) => resetPage({ [prefix + "Sort"]: v })}
            />
          </>
        }
      >
        {missingRole ? (
          <p className="wa-empty">实际出库（CKD）待接入，暂无该岗位数据。</p>
        ) : (
          <Result read={requested}>
            {(data) => (
              <>
                {!people && (
                  <div className="wa-tiles wa-three">
                    <Tile label={dimName + "数"} value={format(data.total)} />
                    <Tile
                      label="跨单重复型号"
                      value={format(data.repeat_pns)}
                      detail="至少两张来源单据"
                    />
                    <Tile
                      label="前十型号明细占比"
                      value={
                        data.top10_line_share == null
                          ? "—"
                          : (data.top10_line_share * 100).toFixed(1) + "%"
                      }
                    />
                  </div>
                )}
                <DataTable
                  labels={[
                    "名次",
                    dimName,
                    "单据数",
                    "明细行数",
                    ...(people ? ["不同 PN"] : []),
                    filters.unit || "指定单位量",
                    "活跃日",
                    "查看",
                  ]}
                  empty={!data.items.length}
                >
                  {data.items.map((row) => (
                    <tr key={JSON.stringify(row.key)}>
                      <td>{format(row.rank)}</td>
                      <th scope="row">
                        <DrillLink to={exact(base, { [by]: row.key })} label={row.label + " · 匹配单据"}>
                          {row.label}
                        </DrillLink>
                        {!people && <small>{row.description || "描述未填"}</small>}
                      </th>
                      <td>{format(row.documents)}</td>
                      <td>{format(row.lines)}</td>
                      {people && <td>{format(row.pns)}</td>}
                      <td>{filters.unit ? format(row.quantity) : "—"}</td>
                      <td>{format(row.active_days)}</td>
                      <td>
                        {row.key !== null && (
                          <button className="wa-link" onClick={() => focus(row)}>
                            {people ? "个人节奏" : "型号周分布"}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </DataTable>
                <Pager
                  page={page}
                  total={data.total}
                  change={(n) => patch({ [prefix + "Page"]: String(n), [prefix + "Focus"]: null })}
                />
                <p className="wa-note">
                  {people
                    ? "活跃日按业务日期去重。多人仓管名单仅作业务上下文。"
                    : ret === "direct"
                      ? "直退按退货明细的货品名称原文分组，正式目录 PN 需另行映射。"
                      : "集中度反映退货记录分布；故障率或退货率还需可比出库分母。"}
                </p>
              </>
            )}
          </Result>
        )}
      </Panel>
      {requested?.data &&
        (() => {
          const items = requested.data.items;
          const row =
            (selectedKey !== null && items.find((r) => r.key === selectedKey)) ||
            items.find((r) => r.key !== null);
          if (!row || row.key === null) return null;
          return people ? (
            <Trend base={exact(base, { operator: row.key })} title={row.label + " · 个人作业节奏"} />
          ) : (
            <PnWeeks base={base} pn={row.key} description={row.description} />
          );
        })()}
      {!people && <ReturnBreakdown base={base} />}
    </>
  );
}
function ReturnBreakdown({ base }: { base: Drill }) {
  const { filters, params, patch } = useWarehouse();
  const choices = {
    category: "产品大类",
    brand: "品牌",
    center: "仓储中心",
    test_result: "行测试结果",
  } as const;
  const raw = params.get("distribution"),
    dim = hasKey(choices, raw) ? raw : "category";
  const read = useWarehouseRead("cube", {
    ...filters,
    ...base,
    row: dim,
    metric: "lines",
    row_limit: 10,
  });
  return (
    <Panel
      title="退货分布前十"
      why="按匹配明细行数查看退货聚集的类别、品牌和仓储中心。"
      actions={
        <Pick
          label="分布维度"
          value={dim}
          options={choices}
          change={(v) => patch({ distribution: v })}
        />
      }
    >
      <Result read={read}>
        {(data) => (
          <DataTable labels={[choices[dim], metrics.lines]} empty={!data.rows.length}>
            {data.rows.map((row) => (
              <tr key={JSON.stringify(row.key)}>
                <th scope="row">{row.label}</th>
                <td>
                  <DrillLink to={exact(base, { [dim]: row.key })} label={row.label + " · 退货明细"}>
                    {format(row.value)}
                  </DrillLink>
                </td>
              </tr>
            ))}
          </DataTable>
        )}
      </Result>
    </Panel>
  );
}
