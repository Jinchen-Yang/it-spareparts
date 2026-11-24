import { Fragment, useId, useState } from "react";
import { Button } from "antd";
import {
  warehouseSources,
  warehouseParams,
  type TestCounts,
  type TestingStatus,
  type WarehouseDocument,
  type Drill,
  type WarehouseQuery,
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
import { reach, sourceCoverage } from "./Trend";

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
  const sources = sourceCoverage(coverage, filters.center);
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
          empty={!sources?.length}
        >
          {sources?.map((c) => {
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
function DocumentLines({
  documentId,
  query,
  page,
  change,
}: {
  documentId: string;
  query: WarehouseQuery;
  page: number;
  change: (page: number) => void;
}) {
  const [attempt, setAttempt] = useState(0);
  const read = useWarehouseRead("lines", { ...query, page, page_size: pageSize }, true, documentId, attempt);
  return (
    <Result read={read} retry={() => setAttempt((previous) => previous + 1)}>
      {(data) => (
        <>
          <DataTable
            labels={["PN / 描述", "数量", "单位", "品类", "仓库", "库位", "测试结果"]}
            empty={!data.items.length}
          >
            {data.items.map((line, i) => (
              <tr key={i}>
                <th scope="row">
                  {line.pn || "PN 未填"}
                  <small>{line.description || "描述未填"}</small>
                </th>
                <td>{format(line.quantity)}</td>
                <td>{line.unit || "单位未填"}</td>
                <td>{line.category || "—"}</td>
                <td>{line.warehouse || "—"}</td>
                <td>{line.bin_name || "—"}</td>
                <td>{line.test_result || "—"}</td>
              </tr>
            ))}
          </DataTable>
          <Pager page={page} total={data.total} change={change} />
        </>
      )}
    </Result>
  );
}

function ExpandedDocumentLines({ documentId, query }: { documentId: string; query: WarehouseQuery }) {
  const [page, setPage] = useState(1);
  return <DocumentLines documentId={documentId} query={query} page={page} change={setPage} />;
}

const documentLabels = ["单号 / 来源", "业务日期", "类别", "仓储中心", "作业人", "匹配行 / 原单行"];

function DocumentRows({ items, query }: { items: WarehouseDocument[]; query: WarehouseQuery }) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const groupId = useId();
  const expandable = items.filter((doc) => doc.matched_lines > 0).map((doc) => String(doc.id));
  function toggle(id: string) {
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  const count = expandable.filter((id) => expanded.has(id)).length;
  return (
    <>
      {!!expandable.length && (
        <div className="wa-document-tools" role="group" aria-label="本页单据明细">
          <Button disabled={count === expandable.length} onClick={() => setExpanded(new Set(expandable))}>
            全部展开（本页）
          </Button>
          <Button disabled={count === 0} onClick={() => setExpanded(new Set())}>
            全部折叠（本页）
          </Button>
        </div>
      )}
      <DataTable labels={documentLabels} empty={!items.length}>
        {items.map((doc) => {
          const id = String(doc.id);
          const open = expanded.has(id);
          const detailsId = `${groupId}-document-${id}`;
          return (
            <Fragment key={id}>
              <tr>
                <th scope="row">
                  {doc.matched_lines > 0 ? (
                    <button
                      className="wa-link wa-document-toggle"
                      type="button"
                      aria-expanded={open}
                      aria-controls={detailsId}
                      onClick={() => toggle(id)}
                    >
                      <span aria-hidden="true">{open ? "▾" : "▸"}</span>
                      {doc.number}
                    </button>
                  ) : (
                    doc.number
                  )}
                  <small>{warehouseSources[doc.source]}</small>
                  {doc.source === "outbound" && doc.whole_pn && (
                    <small>整机 {doc.whole_pn} · {quantity(doc.whole_qty ?? null, doc.whole_unit ?? null)}</small>
                  )}
                </th>
                <td>{doc.date}</td>
                <td>{doc.kind || "未填"}</td>
                <td>
                  {doc.center || "未登记"}
                  {doc.source === "outbound" && <small className="wa-outbound-detail">出库仓 · {doc.outbound_warehouse || "未登记"}</small>}
                </td>
                <td>
                  {doc.operator || "未登记"}
                  {doc.source === "outbound" && <small className="wa-outbound-detail">出库拣货</small>}
                </td>
                <td>
                  {format(doc.matched_lines)} / {format(doc.total_lines)}
                </td>
              </tr>
              {doc.matched_lines > 0 && (
                <tr id={detailsId} className="wa-document-details" hidden={!open}>
                  <td colSpan={documentLabels.length}>
                    {open && (
                      <div className="wa-document-children" role="group" aria-label={doc.number + " · 货品明细"}>
                        <ExpandedDocumentLines documentId={id} query={query} />
                      </div>
                    )}
                  </td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </DataTable>
    </>
  );
}

export function DocumentList({
  selection,
  documentId,
}: {
  selection: Drill;
  documentId: string | null;
}) {
  const { filters, params, patch } = useWarehouse();
  const isDetail = documentId !== null;
  const page = pageNumber(params.get(isDetail ? "linePage" : "listPage"));
  const query = { ...filters, ...selection };
  const listQuery = { ...query, page, page_size: pageSize };
  const list = useWarehouseRead("documents", listQuery, !isDetail);
  if (isDetail) {
    return (
      <Panel
        title={params.get("docNo") || "单据货品"}
        why="显示与上一级日期、仓库、人员及货品条件相符的来源行。"
      >
        <DocumentLines documentId={documentId} query={query} page={page}
          change={(n) => patch({ linePage: String(n) })} />
      </Panel>
    );
  }
  return (
    <Panel
      title="匹配单据"
      why="展开单号可在下方查看符合筛选条件的货品明细。"
    >
      <Result read={list}>
        {(data) => (
          <>
            <DocumentRows key={warehouseParams(listQuery).toString()} items={data.items} query={query} />
            <Pager page={page} total={data.total} change={(n) => patch({ listPage: String(n) })} />
          </>
        )}
      </Result>
    </Panel>
  );
}
