import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Table } from "antd";
import type { TableProps } from "antd";
import { Resizable, type ResizeCallbackData } from "react-resizable";

/**
 * 可拖拽列宽的 AntD Table 封装（解决"表头不能拖、内容显示不全"）。
 *
 * - 拖动表头右边界即可改列宽（react-resizable 标准做法）。
 * - 没设 width 的列自动补默认宽（文字列更宽），这样截断列也能被拉宽。
 * - scroll.x 按各列宽之和自动算 → 列多 / 拉宽后横向滚动，绝不裁切。
 * - 分组表头（多级 columns.children）递归处理，只让叶子列可拖。
 * - fixed 列保持 sticky、不参与拖拽（避免覆盖其定位），用户主要拖中间内容列。
 * - 传 storageKey 则把列宽持久化到 localStorage，刷新后保留。
 *
 * 用法：把页面里的 <Table .../> 换成 <ResizableTable storageKey="xxx" .../>，其余 props 不变。
 */

const MIN_WIDTH = 60;

type AnyColumn = any;

const seedWidth = (c: AnyColumn): number =>
  typeof c.width === "number" ? c.width : c.ellipsis ? 240 : 160;

const leafKey = (c: AnyColumn, path: string): string => {
  const k = c.key ?? (Array.isArray(c.dataIndex) ? c.dataIndex.join(".") : c.dataIndex);
  return k != null ? String(k) : path;
};

// 收集所有叶子列 key（用于签名：列集变化时重新 seed 宽度）
const collectKeys = (cols: AnyColumn[], prefix = ""): string[] => {
  const out: string[] = [];
  cols.forEach((c, i) => {
    const path = `${prefix}${i}`;
    if (c.children?.length) out.push(...collectKeys(c.children, `${path}-`));
    else out.push(leafKey(c, path));
  });
  return out;
};

const ResizableTitle: React.FC<any> = (props) => {
  const { onResize, width, ...rest } = props;
  if (typeof width !== "number") return <th {...rest} />; // fixed / 无宽列：原样
  return (
    <Resizable
      width={width}
      height={0}
      handle={
        <span
          className="rt-resize-handle"
          onClick={(e) => e.stopPropagation()} // 不触发该列排序
        />
      }
      onResize={onResize}
      draggableOpts={{ enableUserSelectHack: false }}
    >
      <th {...rest} />
    </Resizable>
  );
};

const readStored = (key: string): Record<string, number> => {
  try {
    return JSON.parse(localStorage.getItem(`rtw:${key}`) || "{}");
  } catch {
    return {};
  }
};
const writeStored = (key: string, w: Record<string, number>) => {
  try {
    localStorage.setItem(`rtw:${key}`, JSON.stringify(w));
  } catch {
    /* 忽略（隐私模式等） */
  }
};

export interface ResizableTableProps<T> extends TableProps<T> {
  /** 设了则把各列宽度持久化到 localStorage（按此 key 隔离不同表）。 */
  storageKey?: string;
}

export default function ResizableTable<T extends object = any>({
  columns = [],
  components,
  scroll,
  storageKey,
  ...rest
}: ResizableTableProps<T>) {
  const baseColumns = columns as AnyColumn[];
  const signature = useMemo(() => collectKeys(baseColumns).join("|"), [baseColumns]);

  const [widths, setWidths] = useState<Record<string, number>>({});

  // 列集变化时（按 signature）重新 seed：保留已有 key 的宽度，新 key 取存储/默认值
  useEffect(() => {
    const stored = storageKey ? readStored(storageKey) : {};
    setWidths((prev) => {
      const next: Record<string, number> = {};
      const seed = (cols: AnyColumn[], prefix = "") =>
        cols.forEach((c, i) => {
          const path = `${prefix}${i}`;
          if (c.children?.length) return seed(c.children, `${path}-`);
          const key = leafKey(c, path);
          next[key] = stored[key] ?? prev[key] ?? seedWidth(c);
        });
      seed(baseColumns);
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, storageKey]);

  const onResize = useCallback(
    (key: string) => (_e: React.SyntheticEvent, { size }: ResizeCallbackData) => {
      setWidths((prev) => {
        const w = Math.max(MIN_WIDTH, Math.round(size.width));
        if (prev[key] === w) return prev;
        const next = { ...prev, [key]: w };
        if (storageKey) writeStored(storageKey, next);
        return next;
      });
    },
    [storageKey]
  );

  const wrap = useCallback(
    (cols: AnyColumn[], prefix = ""): AnyColumn[] =>
      cols.map((c, i) => {
        const path = `${prefix}${i}`;
        if (c.children?.length) return { ...c, children: wrap(c.children, `${path}-`) };
        const key = leafKey(c, path);
        const width = widths[key] ?? seedWidth(c);
        if (c.fixed) return { ...c, width }; // fixed 列：定宽但不拖（保 sticky）
        return {
          ...c,
          width,
          onHeaderCell: () => ({ width, onResize: onResize(key) }),
        };
      }),
    [widths, onResize]
  );

  const wrappedColumns = useMemo(() => wrap(baseColumns), [wrap, baseColumns]);

  // 横向滚动宽度 = 各叶子列宽之和（拉宽后增大，列多时滚动不裁切）；widths 未填充时按默认兜底
  const totalWidth = useMemo(() => {
    const seedTotal = (cols: AnyColumn[], prefix = ""): number =>
      cols.reduce((sum, c, i) => {
        const path = `${prefix}${i}`;
        if (c.children?.length) return sum + seedTotal(c.children, `${path}-`);
        const key = leafKey(c, path);
        return sum + (widths[key] ?? seedWidth(c));
      }, 0);
    return seedTotal(baseColumns);
  }, [baseColumns, widths]);

  const mergedScroll = {
    ...(scroll || {}),
    x: Math.max(totalWidth, typeof scroll?.x === "number" ? scroll.x : 0) || "max-content",
  };

  return (
    <Table<T>
      {...rest}
      columns={wrappedColumns}
      scroll={mergedScroll}
      components={{
        ...components,
        header: { ...(components?.header as any), cell: ResizableTitle },
      }}
    />
  );
}
