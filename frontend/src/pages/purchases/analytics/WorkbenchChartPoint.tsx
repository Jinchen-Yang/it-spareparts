import { Popover } from "antd";
import { useRef, useState, type ReactNode } from "react";
import type { Measure } from "../../../api/purchaseWorkbench";
import { sessionStale } from "../../../utils/session";
import ChartOrderEvidence from "./ChartOrderEvidence";
import { useWorkbench } from "./WorkbenchUI";
import "./workbenchCharts.css";

type Axis = readonly [name: string, value: string];

export default function WorkbenchChartPoint({
  measure,
  label,
  x,
  y,
  detail = [],
  className = "",
  children,
  tabIndex,
}: {
  measure: Measure;
  label: string;
  x: Axis;
  y: Axis;
  detail?: readonly Axis[];
  className?: string;
  children: ReactNode;
  tabIndex?: number;
}) {
  const { ownerToken, onOwnerStale, onDrill, scope, supplierRestricted } = useWorkbench();
  const [open, setOpen] = useState(false);
  const touch = useRef(false);
  const triggerButton = useRef<HTMLButtonElement>(null);
  const detailButton = useRef<HTMLButtonElement>(null);
  const exactScope = { ...scope, ...measure.scope };
  const drill = () => {
    setOpen(false);
    if (sessionStale(ownerToken)) onOwnerStale();
    else onDrill(exactScope, label);
  };
  const counts = [
    ["采购单数", measure.orders.toLocaleString("zh-CN") + " 单"],
    ["明细行数", measure.lines.toLocaleString("zh-CN") + " 行"],
  ];
  if (!supplierRestricted && measure.supplier_count != null) {
    counts.push(["供应商数", measure.supplier_count.toLocaleString("zh-CN") + " 家"]);
  }
  const fields = [...detail, ...counts.filter(([k]) => k !== x[0] && k !== y[0])];
  return (
    <Popover
      open={open}
      onOpenChange={next => {
        if (next || document.activeElement !== detailButton.current) setOpen(next);
      }}
      trigger="hover"
      mouseEnterDelay={0.12}
      mouseLeaveDelay={0.18}
      destroyOnHidden
      placement="top"
      autoAdjustOverflow
      content={
        <div
          className="pw-chart-tip"
          onKeyDown={(e) => {
            if (e.key === "Escape" || e.key === "Tab") {
              if (e.key === "Escape" || e.shiftKey) e.preventDefault();
              triggerButton.current?.focus();
              setOpen(e.key === "Tab" && e.shiftKey);
              e.stopPropagation();
            }
          }}
        >
          <dl>
            <dt>横轴 · {x[0]}</dt>
            <dd>{x[1]}</dd>
            <dt>纵轴 · {y[0]}</dt>
            <dd>{y[1]}</dd>
            {fields.map(([key, value]) => (
              <div className="pw-chart-fact" key={key}>
                <dt>{key}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
          {exactScope._line === "1" && (
            <small className="pw-chart-line-note">
              单数、行数按匹配明细统计{exactScope.unit ? "（单位：" + exactScope.unit + "）" : ""}。
            </small>
          )}
          {open && <ChartOrderEvidence scope={exactScope} />}
          <button ref={detailButton} type="button" className="pw-chart-drill"
            onPointerDown={event => event.preventDefault()}
            onBlur={event => { if (event.relatedTarget !== triggerButton.current) setOpen(false); }}
            onClick={drill}>
            查看该组采购单
          </button>
        </div>
      }
    >
      <button
        type="button"
        ref={triggerButton}
        onFocus={() => setOpen(true)}
        onBlur={event => { if (event.relatedTarget !== detailButton.current) setOpen(false); }}
        className={"pw-link " + className}
        aria-label={label}
        aria-expanded={open}
        tabIndex={tabIndex}
        onPointerDown={(event) => {
          touch.current = event.pointerType === "touch" || event.pointerType === "pen";
        }}
        onKeyDown={(event) => {
          touch.current = false;
          if (event.key === "Tab" && !event.shiftKey && open && detailButton.current) {
            event.preventDefault();
            detailButton.current.focus();
          } else if (event.key === "Escape") {
            setOpen(false);
            event.stopPropagation();
          }
        }}
        onClick={() => {
          if (touch.current) {
            setOpen(true);
            touch.current = false;
          } else drill();
        }}
      >
        {children}
      </button>
    </Popover>
  );
}
