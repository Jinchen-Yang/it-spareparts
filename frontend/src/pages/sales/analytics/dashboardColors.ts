// 看板局部配色作用域（DL-025 / P 投影兼容）：生产 P（319e25f6）的 index.css 只有
// 14 个基础 token（page / surface / inset / accent* / text* / border / danger /
// warning / success / shadow），缺全部 --mb-tag-* 与 --mb-hover。看板新增的语义色
// 一律写成 var(token, fallback)：M0 走主题 token；投影到 P 时落 fallback——fallback
// 取与 M0 token 完全相同的颜色（rgb() 字面量，colorGuard 新文件零 hex），两端渲染
// 一致，不改整站 CSS / 主题。组件与对比度测试（contrastTokens.test.ts）共用本模块：
// 测试按「token 有定义取定义值，否则取 fallback」解析，同一测试文件无需修改即可
// 在 P 上运行（P 缺 token 时自动落到 fallback 分支）。
import type { ThemeConfig } from "antd";

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

/**
 * 禁用 / 占位控件文字（A3 独立 Chromium 失败返工）：全站 theme 的
 * colorTextQuaternary=text4（rgb(182,176,166)）经 AntD 别名推导成
 * colorTextDisabled / colorTextPlaceholder，在真实 disabled 底（rgba(0,0,0,0.04)
 * 叠白 = rgb(244.8)，独立探针实测）只有 1.97:1、白底 2.15:1。全局 theme.ts /
 * tokens.ts / index.css 不可改——看板局部改用已过 4.5 校验的 --mb-text-2（P 的
 * 14 token 之一，两仓同值 rgb(107,102,94)），fallback 与 token 同色。
 */
export const CONTROL_HINT_TEXT = color("--mb-text-2", "rgb(107, 102, 94)");

/**
 * 主色 / 链接 / hover 文字（DL-025 稳态矩阵返工）：全站 theme 只给
 * colorPrimary/colorLink，AntD 派生 colorPrimaryHover = 淡化主色
 * rgb(102,146,222)——重置 / 导出按钮与日周月 / 指标 radio（选中态）稳态 hover
 * 白底实测 3.12:1；colorLinkHover=accent rgb(62,111,209) 在嵌套卡 inset 底
 * （index.css `.ant-card .ant-card`，rgb(246,243,238)）实测 4.31:1。局部改用
 * --mb-accent-strong（P 的 14 token 之一，两仓同值 rgb(44,86,174)）：白底
 * 6.89、inset 底 6.22，均 ≥4.5。fallback 与 token 同色。
 * colorPrimary 一并覆盖：antd 5.21 radio 按钮未选中态的 hover 文字直接取
 * colorPrimary（style/index.js `&:hover { color: colorPrimary }`），不随
 * colorPrimaryHover 派生——不覆盖则全站 accent rgb(62,111,209) 白底 4.31:1。
 * 覆盖后本作用域内取主色的面（选中 radio / Switch 选中 / 分页当前页 /
 * 实心按钮）全部变 accent-strong：色上文字为白（6.89）或 accent-strong 本身
 * （白底 6.89 / inset 6.22），无低于 4.5 的组合。
 *
 * ⚠ colorPrimary 是 seed token，与上面四个 map token 不同，只能用与 token 同值的
 * rgb() 字面量覆盖，不能用 var()：antd 默认算法（theme/themes/shared/
 * genColorMapToken.js）会用 seed 生成调色板并以 `colorPrimary: primaryColors[6]`
 * 覆写 colorPrimary 本身——var() 字符串进 @ant-design/fast-color 解析失败，整条
 * 调色板变黑，生成的规则文字色是纯黑（probe4 实测）。字面量 rgb(44, 86, 174)
 * 可正常解析，派生调色板与最终生效色都是 accent-strong 本身（cssinjs 会把派生
 * 值按 FastColor 的 hex 小写序列化，通道与字面量相同）；P 投影一致性由
 * contrastTokens.test.ts 守卫（字面量 ≡ resolve(CONTROL_HOVER_TEXT) 逐通道相等，
 * M0 token 定义与 P 14 token 同值），故两端渲染与 var() 写法一致。
 */
export const CONTROL_HOVER_TEXT = color("--mb-accent-strong", "rgb(44, 86, 174)");

/**
 * 看板控件局部主题（A3 返工 + DL-025 稳态 / loading 追加）：覆盖最终文字 / hover
 * token（colorTextDisabled / colorTextPlaceholder 是 AntD 别名 token、
 * colorPrimaryHover / colorLinkHover 是 map token，formatToken 末尾重并 override，
 * 用户值生效；colorPrimary 是 seed token，见上——只能字面量），不带任何全站主题。
 * 套在销售 / 采购分析页根：页内全部控件（筛选栏 / 页签 radio / 重置导出按钮 /
 * 链接按钮 / 抽屉）与其弹层（portal 经 React 上下文取同一 token 集，与触发控件
 * 同 hashId）同享覆盖。真实 disabled 底 rgba(0,0,0,0.04) 不动，仅换上述文字色。
 *
 * opacityLoading=1（DL-025 loading 追加）：antd 别名 token 默认 0.65
 * （theme/util/alias.js），button/style/index.js 把整个 loading 按钮置为该
 * opacity——销售页导出按钮 loading={exporting} 期间「导出」文字会以 0.65 合成
 * 到白底（colorText 0.65 合成 ≈ 2.7:1）。本作用域 token=1 覆写后 loading 态
 * 文字保持全 alpha（白底 ≥4.5），loading 语义（旋转图标 / 禁点）不动。真实
 * 浏览器 computed 由 Codex 复验。
 */
export const analyticsControlTheme: ThemeConfig = {
  token: {
    colorTextDisabled: cssVar(CONTROL_HINT_TEXT),
    colorTextPlaceholder: cssVar(CONTROL_HINT_TEXT),
    colorPrimary: CONTROL_HOVER_TEXT.fallback,
    colorPrimaryHover: cssVar(CONTROL_HOVER_TEXT),
    colorLinkHover: cssVar(CONTROL_HOVER_TEXT),
    opacityLoading: 1,
  },
};
