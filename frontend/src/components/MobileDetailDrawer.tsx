import { Drawer } from "antd";
import type { ReactNode } from "react";

export interface DetailField {
  label: string;
  value: ReactNode;
}

/**
 * 移动端行详情抽屉：列表在小屏只展示核心字段，点行用这个看全部字段。
 * 通用组件——各页把"次要字段"组装成 fields 传进来即可。可关闭、可滚动。
 */
export default function MobileDetailDrawer({
  open, title, fields, onClose, children,
}: {
  open: boolean;
  title: ReactNode;
  fields: DetailField[];
  onClose: () => void;
  /** 字段列表下方的附加内容（如逐笔下钻列表） */
  children?: ReactNode;
}) {
  return (
    <Drawer
      placement="bottom"
      height="70%"
      open={open}
      onClose={onClose}
      title={title}
      styles={{ body: { padding: 16, overflowY: "auto" } }}
    >
      <dl style={{ margin: 0 }}>
        {fields.map((f, i) => (
          <div
            key={i}
            style={{
              display: "flex", justifyContent: "space-between", gap: 16,
              padding: "10px 0",
              borderBottom: i < fields.length - 1 ? "1px solid var(--mb-border)" : "none",
            }}
          >
            <dt style={{ color: "var(--mb-text-3)", flex: "none", fontSize: 13 }}>{f.label}</dt>
            <dd style={{ margin: 0, textAlign: "right", wordBreak: "break-word", minWidth: 0 }}>
              {f.value == null || f.value === "" ? <span style={{ color: "var(--mb-text-3)" }}>—</span> : f.value}
            </dd>
          </div>
        ))}
      </dl>
      {children}
    </Drawer>
  );
}
