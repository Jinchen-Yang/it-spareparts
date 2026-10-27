import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, Key, PointerEvent as ReactPointerEvent } from "react";
import type { DataGridColumn, DataGridProps, DataGridSort } from "./types";
import { clampColumnWidth, columnMinWidth, columnValue } from "./types";
import { sortGridRows } from "./sort";
import { useGridView } from "./useGridView";
import "./DataGrid.css";

export type { DataGridColumn, DataGridProps, DataGridSort, DataGridSortType } from "./types";

const EXPAND_WIDTH = 32;
const SELECT_WIDTH = 32;
interface ResizeSession {
  key: string;
  index: number;
  pointerId: number;
  startX: number;
  startWidth: number;
  width: number;
  handle: HTMLSpanElement;
}

export default function DataGrid<T>({
  id, accountId, columns, rows, rowKey, loading = false, error, onRetry,
  emptyText = "暂无数据", renderExpanded, expandedRowKeys, defaultExpandedRowKeys = [],
  onExpandedRowKeysChange, defaultSort = null, sort: controlledSort, onSortChange,
  serverSort = false, toolbarExtra, className = "", ariaLabel = "数据表格",
  selectable = false, selectedRowKeys, onSelectedRowKeysChange, isRowSelectable,
}: DataGridProps<T>) {
  const { view, update, reset, storageKey } = useGridView(id, accountId, columns, defaultSort);
  const sort = controlledSort === undefined ? view.sort : controlledSort;
  const visible = columns.filter((column) => !view.hidden.includes(column.key));
  const identity = columns.find((column) => column.identity)?.key ?? columns[0]?.key;
  const [ownExpanded, setOwnExpanded] = useState<Key[]>(defaultExpandedRowKeys);
  const expanded = expandedRowKeys ?? ownExpanded;
  const [ownSelected, setOwnSelected] = useState<Key[]>([]);
  const selected = selectedRowKeys ?? ownSelected;
  const [columnsOpen, setColumnsOpen] = useState(false);
  const canvas = useRef<HTMLDivElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const guide = useRef<HTMLDivElement>(null);
  const resizing = useRef<ResizeSession | null>(null);
  const frame = useRef<number | null>(null);
  const sortCallback = useRef(onSortChange);
  sortCallback.current = onSortChange;
  const width = (column: DataGridColumn<T>) => view.widths[column.key] ?? clampColumnWidth(column, column.width ?? 160);
  const fixedWidth = (selectable ? SELECT_WIDTH : 0) + (renderExpanded ? EXPAND_WIDTH : 0);
  const totalWidth = visible.reduce((total, column) => total + width(column), fixedWidth);
  const columnCount = visible.length + (selectable ? 1 : 0) + (renderExpanded ? 1 : 0);
  const ordered = useMemo(() => serverSort ? rows : sortGridRows(rows, columns, sort), [rows, columns, sort, serverSort]);
  const selectableKeys = ordered.filter((row) => isRowSelectable?.(row) !== false).map(rowKey);
  const selectedOnPage = selectableKeys.filter((key) => selected.includes(key)).length;
  const allSelected = selectableKeys.length > 0 && selectedOnPage === selectableKeys.length;
  const someSelected = selectedOnPage > 0 && !allSelected;
  const setSelected = (keys: Key[]) => {
    if (selectedRowKeys === undefined) setOwnSelected(keys);
    onSelectedRowKeysChange?.(keys);
  };
  const toggleSelection = () => setSelected(allSelected
    ? selected.filter((key) => !selectableKeys.includes(key))
    : [...new Set([...selected, ...selectableKeys])]);
  const allExpanded = ordered.length > 0 && ordered.every((row) => expanded.includes(rowKey(row)));
  const currentSortColumn = sort && columns.find((column) => column.key === sort.key);

  useEffect(() => {
    // 服务端列表必须在初次挂载时收到持久化排序，不能只排列当前页。
    if (serverSort && controlledSort === undefined) sortCallback.current?.(view.sort);
  }, [storageKey, serverSort]);
  useEffect(() => { setOwnSelected([]); }, [storageKey]);
  useEffect(() => () => { if (frame.current != null) cancelAnimationFrame(frame.current); }, []);

  const setExpanded = (keys: Key[]) => {
    if (expandedRowKeys === undefined) setOwnExpanded(keys);
    onExpandedRowKeysChange?.(keys);
  };
  const toggleAll = () => setExpanded(allExpanded ? [] : ordered.map(rowKey));
  const changeSort = (column: DataGridColumn<T>) => {
    const next: DataGridSort | null = sort?.key !== column.key ? { key: column.key, dir: "asc" }
      : sort.dir === "asc" ? { key: column.key, dir: "desc" } : null;
    update({ ...view, sort: next });
    onSortChange?.(next);
  };
  const saveWidth = (column: DataGridColumn<T>, next: number) => {
    update({ ...view, widths: { ...view.widths, [column.key]: clampColumnWidth(column, next) } });
  };
  const drawWidth = (session: ResizeSession) => {
    if (!canvas.current) return;
    canvas.current.style.setProperty(`--dg-column-${session.index}`, `${session.width}px`);
    canvas.current.style.setProperty("--dg-total-width", `${totalWidth + session.width - width(columns[session.index])}px`);
    session.handle.setAttribute("aria-valuenow", String(session.width));
    const naturalBefore = visible.slice(0, visible.findIndex((column) => column.key === session.key))
      .reduce((total, column) => total + width(column), fixedWidth);
    const before = session.key === identity
      ? Math.max(naturalBefore, (scroll.current?.scrollLeft ?? 0) + fixedWidth) : naturalBefore;
    if (guide.current) guide.current.style.left = `${before + session.width - 1}px`;
  };
  const endResize = (event: ReactPointerEvent<HTMLSpanElement>, cancelled = false) => {
    event.stopPropagation();
    const session = resizing.current;
    if (!session || session.pointerId !== event.pointerId) return;
    if (frame.current != null) { cancelAnimationFrame(frame.current); frame.current = null; }
    resizing.current = null;
    if (cancelled) session.width = session.startWidth;
    drawWidth(session);
    canvas.current?.removeAttribute("data-resizing");
    if (session.handle.hasPointerCapture?.(session.pointerId)) session.handle.releasePointerCapture(session.pointerId);
    if (!cancelled) saveWidth(columns[session.index], session.width);
  };
  const autoFit = (column: DataGridColumn<T>) => {
    if (!canvas.current) return;
    let widest = columnMinWidth(column);
    const viewport = scroll.current?.getBoundingClientRect();
    const visibleTop = Math.max(0, viewport?.top ?? 0);
    const visibleBottom = Math.min(window.innerHeight, viewport?.bottom ?? window.innerHeight);
    const cells = canvas.current.querySelectorAll<HTMLElement>("[data-grid-measure]");
    for (const cell of cells) {
      // 展开行可以包含另一个 DataGrid，不能把子表同名列算进当前列。
      if (cell.dataset.gridMeasure !== column.key || cell.closest(".data-grid-canvas") !== canvas.current) continue;
      const rect = cell.getBoundingClientRect();
      if (viewport && viewport.height > 0 && rect.height > 0 && (rect.bottom <= visibleTop || rect.top >= visibleBottom)) continue;
      // 表头是 flex 子项，测量时同时解除伸展约束，避免旧列宽撑大 scrollWidth。
      const previous = { width: cell.style.width, flex: cell.style.flex, maxWidth: cell.style.maxWidth };
      try {
        cell.style.flex = "none";
        cell.style.maxWidth = "none";
        cell.style.width = "max-content";
        const tableCell = cell.closest("td, th");
        const style = tableCell ? getComputedStyle(tableCell) : undefined;
        const borders = (Number.parseFloat(style?.borderLeftWidth ?? "") || 0)
          + (Number.parseFloat(style?.borderRightWidth ?? "") || 0);
        widest = Math.max(widest, cell.scrollWidth + (cell.dataset.gridHeader ? 36 : 16) + borders);
      } finally {
        cell.style.width = previous.width;
        cell.style.flex = previous.flex;
        cell.style.maxWidth = previous.maxWidth;
      }
    }
    saveWidth(column, widest);
  };
  const resetView = () => { const next = reset(); onSortChange?.(next.sort); };
  const canvasStyle = {
    "--dg-total-width": `${totalWidth}px`,
    ...Object.fromEntries(columns.map((column, index) => [`--dg-column-${index}`, `${width(column)}px`])),
  } as CSSProperties;
  const stickyStyle = (column: DataGridColumn<T>): CSSProperties | undefined => column.key === identity
    ? { left: fixedWidth } : undefined;
  const expandStyle = { left: selectable ? SELECT_WIDTH : 0 };

  return <section className={`data-grid ${className}`} data-grid-id={id} aria-label={ariaLabel}>
    <div className="data-grid-toolbar">
      <span className="data-grid-sort-status">排序：{currentSortColumn && sort ? `${currentSortColumn.title} ${sort.dir === "asc" ? "升序" : "降序"}` : "默认"}</span>
      <div className="data-grid-toolbar-actions">
        {toolbarExtra}
        {renderExpanded && <button type="button" className="data-grid-text-button" onClick={toggleAll}>{allExpanded ? "全部折叠" : "全部展开"}</button>}
        <div className="data-grid-columns">
          <button type="button" className="data-grid-text-button" aria-expanded={columnsOpen} aria-label="显示列" onClick={() => setColumnsOpen(!columnsOpen)}>列 {visible.length}/{columns.length}</button>
          {columnsOpen && <div className="data-grid-column-menu" role="group" aria-label="选择显示列">
            {columns.map((column) => <label key={column.key}><input type="checkbox" checked={!view.hidden.includes(column.key)} disabled={column.key === identity}
              onChange={(event) => update({ ...view, hidden: event.target.checked ? view.hidden.filter((key) => key !== column.key) : [...view.hidden, column.key] })} />
              {column.title}{column.key === identity && <span aria-label="身份列已锁定"> · 固定</span>}</label>)}
            <button type="button" className="data-grid-text-button" onClick={() => setColumnsOpen(false)}>完成</button>
          </div>}
        </div>
        <button type="button" className="data-grid-text-button" onClick={resetView}>恢复默认视图</button>
      </div>
    </div>
    <div className="data-grid-scroll" ref={scroll} tabIndex={0} aria-label={`${ariaLabel}，可横向滚动`}>
      <div className="data-grid-canvas" ref={canvas} style={canvasStyle}>
        <table className="data-grid-table" aria-label={ariaLabel} aria-busy={loading}>
          <colgroup>
            {selectable && <col style={{ width: SELECT_WIDTH }} />}
            {renderExpanded && <col style={{ width: EXPAND_WIDTH }} />}
            {visible.map((column) => <col key={column.key} style={{ width: `var(--dg-column-${columns.indexOf(column)})` }} />)}
          </colgroup>
          <thead><tr>
            {selectable && <th className="data-grid-select-cell"><input type="checkbox" aria-label="选择当前页可选行"
              checked={allSelected} aria-checked={someSelected ? "mixed" : allSelected}
              ref={(input) => { if (input) input.indeterminate = someSelected; }}
              disabled={loading || !selectableKeys.length} onChange={toggleSelection} /></th>}
            {renderExpanded && <th className="data-grid-expand-cell" style={expandStyle}><button type="button" aria-label={allExpanded ? "折叠全部行" : "展开全部行"} aria-expanded={allExpanded} onClick={toggleAll}>{allExpanded ? "▾" : "▸"}</button></th>}
            {visible.map((column) => {
              const sortable = column.sortable ?? !!column.type;
              return <th key={column.key} className={column.key === identity ? "data-grid-identity" : undefined} style={{ ...stickyStyle(column), textAlign: column.align }}
                aria-sort={sort?.key === column.key ? sort.dir === "asc" ? "ascending" : "descending" : "none"}>
                {sortable ? <button className="data-grid-sort-button" type="button" aria-label={`${column.title}排序`} onClick={() => changeSort(column)}>
                  <span className="data-grid-header-text" data-grid-measure={column.key} data-grid-header="true" title={column.title}>{column.title}</span>
                  <span className={sort?.key === column.key ? "data-grid-sort-active" : "data-grid-sort-icon"} aria-hidden="true">{sort?.key === column.key ? sort.dir === "asc" ? "▲" : "▼" : "⇅"}</span>
                </button> : <span className="data-grid-header-text" data-grid-measure={column.key} data-grid-header="true" title={column.title}>{column.title}</span>}
                <span className="data-grid-resize" role="separator" tabIndex={0} aria-label={`调整 ${column.title} 列宽`} aria-orientation="vertical"
                  aria-valuenow={width(column)} aria-valuemin={columnMinWidth(column)} aria-valuemax={Math.max(columnMinWidth(column), Math.min(800, column.maxWidth ?? 800))}
                  onClick={(event) => event.stopPropagation()} onDoubleClick={(event) => { event.stopPropagation(); autoFit(column); }}
                  onKeyDown={(event) => {
                    event.stopPropagation();
                    if (!["ArrowLeft", "ArrowRight", "Enter"].includes(event.key)) return;
                    event.preventDefault();
                    if (event.key === "Enter") autoFit(column);
                    else saveWidth(column, width(column) + (event.key === "ArrowRight" ? 1 : -1) * (event.shiftKey ? 32 : 8));
                  }}
                  onPointerDown={(event) => {
                    event.preventDefault(); event.stopPropagation();
                    if (event.button !== 0) return;
                    const session = { key: column.key, index: columns.indexOf(column), pointerId: event.pointerId, startX: event.clientX, startWidth: width(column), width: width(column), handle: event.currentTarget };
                    resizing.current = session;
                    event.currentTarget.setPointerCapture?.(event.pointerId);
                    canvas.current?.setAttribute("data-resizing", "true");
                    drawWidth(session);
                  }}
                  onPointerMove={(event) => {
                    event.stopPropagation();
                    const session = resizing.current;
                    if (!session || session.pointerId !== event.pointerId) return;
                    session.width = clampColumnWidth(column, session.startWidth + event.clientX - session.startX);
                    if (frame.current == null) frame.current = requestAnimationFrame(() => {
                      frame.current = null;
                      if (resizing.current) drawWidth(resizing.current);
                    });
                  }}
                  onPointerUp={(event) => endResize(event)} onPointerCancel={(event) => endResize(event, true)} onLostPointerCapture={(event) => endResize(event)} />
              </th>;
            })}
          </tr></thead>
          <tbody>
            {error ? <tr><td colSpan={columnCount} className="data-grid-state"><div role="alert">{error} {onRetry && <button type="button" onClick={onRetry}>重试</button>}</div></td></tr>
              : loading && !ordered.length ? <tr><td colSpan={columnCount} className="data-grid-state"><span role="status">加载中…</span></td></tr>
                : !ordered.length ? <tr><td colSpan={columnCount} className="data-grid-state">{emptyText}</td></tr>
                  : ordered.map((row) => {
                    const key = rowKey(row);
                    const open = expanded.includes(key);
                    return <Fragment key={key}><tr className="data-grid-row" data-row-key={key} aria-selected={selectable ? selected.includes(key) : undefined}>
                      {selectable && <td className="data-grid-select-cell"><input type="checkbox"
                        aria-label={`选择 ${String(columnValue(columns.find((column) => column.key === identity)!, row) ?? key)}`}
                        checked={selected.includes(key)} disabled={loading || isRowSelectable?.(row) === false}
                        onChange={(event) => setSelected(event.target.checked ? [...selected, key] : selected.filter((item) => item !== key))} /></td>}
                      {renderExpanded && <td className="data-grid-expand-cell" style={expandStyle}><button type="button" aria-label={`${open ? "折叠" : "展开"} ${String(columnValue(columns.find((column) => column.key === identity)!, row) ?? key)}`} aria-expanded={open}
                        onClick={() => setExpanded(open ? expanded.filter((item) => item !== key) : [...expanded, key])}>{open ? "▾" : "▸"}</button></td>}
                      {visible.map((column) => {
                        const value = columnValue(column, row);
                        const title = typeof value === "string" || typeof value === "number" ? String(value) : undefined;
                        return <td key={column.key} className={`${column.key === identity ? "data-grid-identity " : ""}${column.type === "code" ? "data-grid-code" : ""}`} style={{ ...stickyStyle(column), textAlign: column.align }}>
                          <div className="data-grid-cell-content" data-grid-measure={column.key} title={title}>{column.render ? column.render(row) : value == null ? "—" : String(value)}</div>
                        </td>;
                      })}
                    </tr>{open && renderExpanded && <tr className="data-grid-expanded"><td colSpan={columnCount}>{renderExpanded(row)}</td></tr>}</Fragment>;
                  })}
          </tbody>
        </table>
        <div ref={guide} className="data-grid-guide" aria-hidden="true" />
      </div>
    </div>
    {loading && ordered.length > 0 && <span className="data-grid-refresh-status" role="status">正在更新…</span>}
  </section>;
}
