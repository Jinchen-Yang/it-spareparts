import WorkbenchBoards from "./WorkbenchBoards";
import WorkbenchMatrix from "./WorkbenchMatrix";
import WorkbenchCompare from "./WorkbenchCompare";
import WorkbenchStructure from "./WorkbenchStructure";
import WorkbenchWork from "./WorkbenchWork";
import { basisNames, useMeasure } from "./WorkbenchMeasures";
import { Pick, WorkbenchProvider, type WorkbenchProps, useWorkbench } from "./WorkbenchUI";
import "./analyticsWorkbench.css";

export type { WorkbenchView, WorkbenchProps, DrillScope } from "./WorkbenchUI";
const views = {
  boards: ["采购排行", "哪些人员、类型、供应商和型号占据采购的大头？"],
  matrix: ["交叉分析", "采购集中在哪些组合，交叉格背后有哪些订单？"],
  compare: ["期间对比", "相比紧邻的上一段等长区间，哪些采购分组变化最大？"],
  work: ["人员工作量", "谁经手的单据最多、采购额最高、同单位采购量最大？每个人主要向谁采购？"],
  structure: ["采购结构与关联", "不同采购类型如何分布，供应商和销售单如何关联？"],
};
function BasisControl() {
  const { basis, set } = useMeasure();
  const { view } = useWorkbench();
  return (
    <Pick
      label="采购金额口径"
      value={basis}
      options={Object.entries(basisNames)}
      onChange={(v) =>
        set(
          view === "work"
            ? { basis: v, peoplePage: "", supplierPage: "", goodsPage: "", trendPage: "" }
            : { basis: v },
        )
      }
    />
  );
}
export default function AnalyticsWorkbench(props: WorkbenchProps) {
  if (props.view === "pool") return null;
  const [name, question] = views[props.view];
  return (
    <WorkbenchProvider {...props}>
      <div className="purchase-workbench">
        <div className="pw-heading">
          <div>
            <h2>{name}</h2>
            <p>{question}</p>
          </div>
          {!props.costRestricted && ["work", "compare", "structure"].includes(props.view) && <BasisControl />}
        </div>
        {props.view === "boards" ? (
          <WorkbenchBoards />
        ) : props.view === "matrix" ? (
          <WorkbenchMatrix />
        ) : props.view === "compare" ? (
          <WorkbenchCompare />
        ) : props.view === "work" ? (
          <WorkbenchWork />
        ) : (
          <WorkbenchStructure />
        )}
      </div>
    </WorkbenchProvider>
  );
}
