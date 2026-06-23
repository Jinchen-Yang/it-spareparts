import type { ReactNode } from "react";

/**
 * 统一页面头：标题 + 副标题/上下文 + 右侧动作区。
 * 给每个页面一致的顶部结构（此前各页标题散落在 Card 里、有的没有），是"全面美化"的结构基线。
 */
export default function PageHeader({
  title,
  subtitle,
  extra,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  extra?: ReactNode;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "flex-end",
        justifyContent: "space-between",
        gap: 16,
        flexWrap: "wrap",
        marginBottom: 18,
      }}
    >
      <div style={{ minWidth: 0 }}>
        <h1
          style={{
            margin: 0,
            fontSize: 21,
            fontWeight: 600,
            letterSpacing: "-0.2px",
            color: "var(--mb-text)",
            lineHeight: 1.25,
          }}
        >
          {title}
        </h1>
        {subtitle && (
          <div style={{ marginTop: 5, fontSize: 13, color: "var(--mb-text-2)", lineHeight: 1.5 }}>
            {subtitle}
          </div>
        )}
      </div>
      {extra && (
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexShrink: 0 }}>{extra}</div>
      )}
    </div>
  );
}
