// 两仓镜像语义结构，取值各自维护；新增颜色只在这里定义。
const shadow = {
  card: "0 1px 2px rgba(40,33,24,.05), 0 4px 14px rgba(40,33,24,.05)",
  input: "0 2px 10px rgba(0,0,0,0.04)",
  none: "none",
};

const color = {
  page: "#F4F1EC",
  surface: "#FFFFFF",
  inset: "#F6F3EE",
  header: "#FFFFFF",
  accent: "#3E6FD1",
  accentStrong: "#2C56AE",
  accentSoft: "#EEF3FC",
  accentSoftBorder: "#DBE6F8",
  text: "#2A2722",
  text2: "#6B665E",
  text3: "#787264",
  border: "#E9E5DE",
  borderSoft: "#F0EDE7",
  danger: "#BE564B",
  warning: "#B07C33",
  success: "#4F875C",
  shadow: shadow.card, // 保留 COLORS.shadow 兼容；新代码使用 tokens.shadow.card。
  // 既有 AntD / CSS 的细节色，避免收口时改变全站外观。
  text4: "#B6B0A6",
  hover: "#F7F5F1",
  scrollbar: "#DAD4CA",
  scrollbarHover: "#C7C0B4",
  onAccentSoft: "rgba(255,255,255,0.25)",
  onAccentSoftBorder: "rgba(255,255,255,0.4)",
  tag: {
    danger: { text: "#A24A40", background: "#FBEDEB", border: "#F2D8D3" },
    warning: { text: "#8A6526", background: "#FAF1DF", border: "#EFE0C4" },
    success: { text: "#41704F", background: "#E9F2EB", border: "#D2E4D6" },
    info: { text: "#2C56AE", background: "#EEF3FC", border: "#D8E4F7" },
  },
};

export const tokens = {
  color: {
    ...color,
    status: {
      pending: color.warning,
      ai: color.accent,
      confirmed: color.success,
      manual: color.warning,
      failed: color.danger,
    },
  },
  space: { xs: 4, sm: 8, md: 12, lg: 16, xl: 24, xxl: 32 },
  fontSize: { xs: 12, sm: 13, md: 14, lg: 16, xl: 20, xxl: 24 },
  radius: { sm: 4, md: 8, lg: 12 },
  shadow,
  breakpoint: { xs: 480, sm: 576, md: 768, lg: 992, xl: 1200, xxl: 1600 },
  layout: { headerHeight: 60, contentPadding: 24, mobileContentPadding: 12 },
} as const;

/** CSS 保留既有命名（text2 → text-2），其余按层级 kebab-case；尺寸单位为 px。 */
function cssVariables(value: object, prefix: string, unit = ""): Record<string, string> {
  return Object.fromEntries(Object.entries(value).flatMap(([key, item]) => {
    const name = `${prefix}-${key.replace(/([a-z])([A-Z0-9])/g, "$1-$2").toLowerCase()}`;
    return typeof item === "object"
      ? Object.entries(cssVariables(item, name, unit))
      : [[name, `${item}${typeof item === "number" ? unit : ""}`]];
  }));
}

// index.css 的 :root 块是本映射的静态副本；designTokens.test.ts 阻止漂移。
export const tokenCssVariables: Record<string, string> = {
  ...cssVariables(tokens.color, "--mb"),
  ...cssVariables(tokens.space, "--mb-space", "px"),
  ...cssVariables(tokens.fontSize, "--mb-font-size", "px"),
  ...cssVariables(tokens.radius, "--mb-radius", "px"),
  ...cssVariables(tokens.shadow, "--mb-shadow"),
  ...cssVariables(tokens.breakpoint, "--mb-breakpoint", "px"),
  ...cssVariables(tokens.layout, "--mb-layout", "px"),
  "--mb-shell-header-height": `${tokens.layout.headerHeight}px`,
  "--mb-shell-content-padding": `${tokens.layout.contentPadding}px`,
};
