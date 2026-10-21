// 看板局部配色作用域（DL-025 / P 投影兼容）：生产 P（319e25f6）的 index.css 只有
// 14 个基础 token（page / surface / inset / accent* / text* / border / danger /
// warning / success / shadow），缺全部 --mb-tag-* 与 --mb-hover。看板新增的语义色
// 一律写成 var(token, fallback)：M0 走主题 token；投影到 P 时落 fallback——fallback
// 取与 M0 token 完全相同的颜色（rgb() 字面量，colorGuard 新文件零 hex），两端渲染
// 一致，不改整站 CSS / 主题。组件与对比度测试（contrastTokens.test.ts）共用本模块：
// 测试按「token 有定义取定义值，否则取 fallback」解析，同一测试文件无需修改即可
// 在 P 上运行（P 缺 token 时自动落到 fallback 分支）。
export interface DashboardColor {
  /** CSS 自定义属性名（M0 index.css 定义；P 可能缺失）。 */
  token: string;
  /** 与 token 同值的兜底色（P 缺 token 时生效）。 */
  fallback: string;
}

const color = (token: string, fallback: string): DashboardColor => ({ token, fallback });

/** KPI 环比增减文本（↑ / ↓）：tag success / danger 文字色（SummaryView RATIO_COLOR）。 */
export const RATIO_UP = color("--mb-tag-success-text", "rgb(65, 112, 79)");
export const RATIO_DOWN = color("--mb-tag-danger-text", "rgb(162, 74, 64)");
/** KpiCard 高亮对（毛利待配成本等警示态）：tag warning 文字 / 背景。 */
export const HIGHLIGHT_TEXT = color("--mb-tag-warning-text", "rgb(138, 101, 38)");
export const HIGHLIGHT_BG = color("--mb-tag-warning-background", "rgb(250, 241, 223)");
/** 悬停底色（表格行 / 卡片 hover 态）：看板说明文字在 hover 面上也要 ≥ 4.5:1。 */
export const HOVER_BG = color("--mb-hover", "rgb(247, 245, 241)");

/** 组件用：var(token, fallback) 形式（M0 用 token，P 落 fallback）。 */
export const cssVar = (c: DashboardColor): string => `var(${c.token}, ${c.fallback})`;
