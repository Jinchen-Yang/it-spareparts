import { Popover } from "antd";
import {
  warehouseSources,
  type Product,
  type TestCounts,
  type TestingStatus,
  type WarehouseDocument,
  type Drill,
} from "../../api/warehouseAnalytics";
import { pageNumber, pageSize } from "./warehouseState";
import {
  DataTable,
  DrillLink,
  Pager,
  Panel,
  Pick,
  Result,
  Tile,
  format,
  quantity,
  useWarehouse,
  useWarehouseRead,
} from "./WarehouseShared";
import { reach } from "./Trend";

export function EquipmentBoard() {
  const { filters, params, patch } = useWarehouse(),
    page = pageNumber(params.get("eqPage"));
  const read = useWarehouseRead("equipment", { ...filters, page, page_size: pageSize });
  return (
    <Panel
      title="整机 PN 与匹配备件"
      why="查看哪些整机带来的备件处理量较大。单头按整机 PN 与原单位分组，每张单只计头一次；明细筛选后显示包含匹配货品的关联单头。"
    >
      <Result read={read}>
        {(data) => (
          <>
            <DataTable
              labels={["整机 PN / 描述", "整机单位", "关联单据", "整机数量", "匹配子行", "最近业务日"]}
              empty={!data.items.length}
            >
              {data.items.map((r) => (
                <tr key={JSON.stringify([r.whole_pn, r.whole_unit])}>
                  <th scope="row">
                    <DrillLink
                      label={(r.whole_pn || "整机 PN 未填") + " · 关联单据"}
                      to={{ scope: { whole_pn: r.whole_pn, whole_unit: r.whole_unit } }}
                    >
                      {r.whole_pn || "PN 未填"}
                    </DrillLink>
                    <small>{r.whole_description || "描述未填"}</small>
                  </th>
                  <td>{r.whole_unit || "单位未填"}</td>
                  <td>{format(r.documents)}</td>
                  <td>{r.whole_unit ? format(r.whole_qty) : "—"}</td>
                  <td>{format(r.matched_lines)}</td>
                  <td>{r.last_date || "—"}</td>
                </tr>
              ))}
            </DataTable>
            <Pager page={page} total={data.total} change={(n) => patch({ eqPage: String(n) })} />
          </>
        )}
      </Result>
    </Panel>
  );
}
const states: Record<TestingStatus, string> = {
  required: "需测试",
  completed: "已完成",
  pending: "记录待测",
  unknown: "完成状态未填",
  attributed: "完成且实名",
};
export function TestingBoard() {
  const { filters, coverage, params, patch } = useWarehouse();
  const by = params.get("testBy") === "tester" ? "tester" : "center";
  const read = useWarehouseRead("testing", { ...filters, by });
  return (
    <>
      <Panel
        title="需测与完成情况"
        why="按原单业务日选取需测单，再查看当前记录的完成状态和实际测试人署名。"
      >
        <Result read={read}>
          {(data) => (
            <div className="wa-tiles wa-five">
              {(Object.keys(states) as TestingStatus[]).map((k) => (
                <Tile
                  key={k}
                  label={states[k]}
                  value={
                    <DrillLink to={{ testing: k }} label={states[k] + " · 单据"}>
                      {format(data[k])}
                    </DrillLink>
                  }
                />
              ))}
            </div>
          )}
        </Result>
      </Panel>
      <Panel
        title={by === "tester" ? "实际测试人署名" : "各仓储中心的测试任务"}
        why="每列按符合条件的原单去重。测试人员按整份署名归组，填写的业务日期用于选单，实际完成日可能跨期。"
        actions={
          <Pick
            label="测试分组"
            value={by}
            options={{ center: "仓储中心", tester: "实际测试人" }}
            change={(v) => patch({ testBy: v })}
          />
        }
      >
        <Result read={read}>
          {(data) => (
            <DataTable
              labels={[by === "center" ? "仓储中心" : "实际测试人", ...Object.values(states)]}
              empty={!data.items.length}
            >
              {data.items.map((row) => (
                <tr key={JSON.stringify(row.key)}>
                  <th scope="row">{row.label}</th>
                  {(Object.keys(states) as (keyof TestCounts)[]).map((k) => (
                    <td key={k}>
                      <DrillLink
                        label={row.label + " · " + states[k]}
                        to={{ scope: { [by]: row.key }, testing: k }}
                      >
                        {format(row[k])}
                      </DrillLink>
                    </td>
                  ))}
                </tr>
              ))}
            </DataTable>
          )}
        </Result>
      </Panel>
      <Panel
        title="来源覆盖"
        why="首末日期取每个来源全部已导入单据的业务日，包含非生效单据。全期单数、行数和各看板业务量只计已生效记录。"
      >
        <DataTable
          labels={["业务来源", "首个业务日", "最近业务日", "全期单数", "全期行数", "当前区间"]}
          empty={!coverage?.sources.length}
        >
          {coverage?.sources.map((c) => {
            const span = reach(c, filters.date_from, filters.date_to);
            return (
              <tr key={c.source}>
                <th scope="row">{warehouseSources[c.source]}</th>
                <td>{c.min_date || "—"}</td>
                <td>{c.max_date || "—"}</td>
                <td>{c.available ? format(c.documents) : "—"}</td>
                <td>{c.available ? format(c.lines) : "—"}</td>
                <td>
                  {
                    {
                      absent: "待接入",
                      outside: "区间无覆盖",
                      partial: "部分日期覆盖",
                      covered: "在来源首末日内",
                    }[span]
                  }
                </td>
              </tr>
            );
          })}
        </DataTable>
      </Panel>
    </>
  );
}
function SampleLines({ sample }: { sample: Product[] }) {
  if (!sample.length) return <span>当前范围没有可见货品样本。</span>;
  return (
    <div className="wa-pop">
      {sample.map((p, i) => (
        <div key={i}>
          <strong>{p.pn || "PN 未填"}</strong> · {p.description || "描述未填"}
          <small>{quantity(p.quantity, p.unit)}</small>
        </div>
      ))}
    </div>
  );
}
export function DocumentList({
  selection,
  documentId,
}: {
  selection: Drill;
  documentId: string | null;
}) {
  const { filters, params, patch } = useWarehouse(),
    isDetail = documentId !== null;
  const page = pageNumber(params.get(isDetail ? "linePage" : "listPage"));
  const query = { ...filters, ...selection, page, page_size: pageSize };
  const list = useWarehouseRead("documents", query, !isDetail);
  const detail = useWarehouseRead("lines", query, isDetail, documentId ?? undefined);
  function open(doc: WarehouseDocument) {
    patch({ doc: String(doc.id), docNo: doc.number, linePage: null }, true);
  }
  if (isDetail)
    return (
      <Panel
        title={params.get("docNo") || "单据货品"}
        why="仅显示与上一级日期、仓库、人员及货品条件相符的来源行。"
      >
        <Result read={detail}>
          {(data) => (
            <>
              <DataTable
                labels={["PN / 描述", "数量", "单位", "品类", "仓库", "库位", "测试结果"]}
                empty={!data.items.length}
              >
                {data.items.map((l, i) => (
                  <tr key={i}>
                    <th scope="row">
                      {l.pn || "PN 未填"}
                      <small>{l.description || "描述未填"}</small>
                    </th>
                    <td>{format(l.quantity)}</td>
                    <td>{l.unit || "单位未填"}</td>
                    <td>{l.category || "—"}</td>
                    <td>{l.warehouse || "—"}</td>
                    <td>{l.bin_name || "—"}</td>
                    <td>{l.test_result || "—"}</td>
                  </tr>
                ))}
              </DataTable>
              <Pager page={page} total={data.total} change={(n) => patch({ linePage: String(n) })} />
            </>
          )}
        </Result>
      </Panel>
    );
  return (
    <Panel
      title="匹配单据"
      why="每张业务单展示一次。悬停或聚焦单号查看匹配货品样本，点击单号查看分页明细。"
    >
      <Result read={list}>
        {(data) => (
          <>
            <DataTable
              labels={["单号 / 来源", "业务日期", "类别", "仓储中心", "作业人", "匹配行 / 原单行"]}
              empty={!data.items.length}
            >
              {data.items.map((doc) => (
                <tr key={doc.id}>
                  <th scope="row">
                    <Popover
                      trigger={["hover", "focus"]}
                      title={doc.number}
                      content={<SampleLines sample={doc.sample} />}
                    >
                      <button className="wa-link" type="button" onClick={() => open(doc)}>
                        {doc.number}
                      </button>
                    </Popover>
                    <small>{warehouseSources[doc.source]}</small>
                  </th>
                  <td>{doc.date}</td>
                  <td>{doc.kind || "未填"}</td>
                  <td>{doc.center || "未登记"}</td>
                  <td>{doc.operator || "未登记"}</td>
                  <td>
                    {format(doc.matched_lines)} / {format(doc.total_lines)}
                  </td>
                </tr>
              ))}
            </DataTable>
            <Pager page={page} total={data.total} change={(n) => patch({ listPage: String(n) })} />
          </>
        )}
      </Result>
    </Panel>
  );
}
