// 对比度工具与看板局部配色回归（A-AC14 / DL-025）：utils/contrast 的计算口径
// （hex / rgb / rgba、alpha 先按 Source-Over 合成）+ 看板实际局部配色组合 ≥ 4.5:1。
//
// P 投影兼容（本轮返工）：生产 P（319e25f6）的 index.css 只有 14 个基础 token，
// 缺全部 --mb-tag-* 与 --mb-hover——看板语义色写在 dashboardColors.ts 的
// var(token, fallback) 里。本测试按「token 在当前 index.css 有定义取定义值，否则取
// fallback」解析（resolve），因此同一文件无需修改即可在 P 上运行：M0 走 token 值，
// P 自动落 fallback；且在 M0 上另有漂移守卫（token 有定义时 fallback 必须与 token
// 同色），保证投影到 P 后渲染与 M0 逐通道一致。
//
// 本测试文件自身不写十六进制字面量（tests/colorGuard 只减不增约束）。
// 真实浏览器渲染（含 AntD hover 态级联）由 Codex 最终验收；本测试锁静态组合。
// 注：--mb-text-3（on white 4.78:1）不达标 hover/inset 面（4.39:1），因此看板内
// hover/inset 面上的说明文字一律用 --mb-text-2（见各组件注释）。
import { describe, expect, it } from "vitest";
// @ts-expect-error 测试内读仓库 CSS 源文本（仓库未装 @types/node；vitest 运行于 node）
import { readFileSync } from "node:fs";
import { composite, contrastRatio, parseColor } from "../utils/contrast";
import {
  CONTROL_HINT_TEXT,
  CONTROL_HOVER_TEXT,
  HIGHLIGHT_BG,
  HIGHLIGHT_TEXT,
  HOVER_BG,
  RATIO_DOWN,
  RATIO_UP,
  analyticsControlTheme,
  type DashboardColor,
} from "../pages/sales/analytics/dashboardColors";

// vitest 的 cwd 恒为 frontend/（npm scripts 运行根）；jsdom 会把 import.meta.url
// 改写成 http:// 方案，不能用它定位文件
const css = readFileSync("src/index.css", "utf8") as string;

const tokens = new Map<string, string>();
for (const [, name, value] of css.matchAll(/(--mb-[a-z0-9-]+)\s*:\s*(#[0-9A-Fa-f]{6})\s*;/g)) {
  tokens.set(name, value);
}

// P 投影解析：var(token, fallback) 的实际渲染值——token 有定义取定义值，否则取
// fallback（P 缺 --mb-tag-*/--mb-hover 时这些键自动落 fallback 分支）
const resolve = (c: DashboardColor) => tokens.get(c.token) ?? c.fallback;

// 生产 P（319e25f6）保证存在的 14 个基础 token 中，看板用到的颜色键——只对这些
// 做可解析守卫；--mb-tag-*/--mb-hover 不要求存在（P 靠 fallback）
const P_GUARANTEED = ["--mb-surface", "--mb-inset", "--mb-page", "--mb-text", "--mb-text-2", "--mb-text-3", "--mb-accent-strong"];

describe("对比度计算口径（utils/contrast）", () => {
  it("黑白极值 21、同色 1、顺序无关", () => {
    expect(contrastRatio("rgb(0, 0, 0)", "rgb(255, 255, 255)")).toBeCloseTo(21, 1);
    expect(contrastRatio("rgb(80, 120, 200)", "rgb(80, 120, 200)")).toBe(1);
    expect(contrastRatio("rgb(255, 255, 255)", "rgb(0, 0, 0)"))
      .toBeCloseTo(contrastRatio("rgb(0, 0, 0)", "rgb(255, 255, 255)"), 10);
  });

  it("alpha 前景按 Source-Over 合成到背景再算（半透明黑在白底 ≈ 中灰）", () => {
    expect(composite("rgba(0, 0, 0, 0.5)", "rgb(255, 255, 255)")).toEqual([128, 128, 128]);
    // 0% / 100% alpha 两个退化端
    expect(composite("rgba(10, 20, 30, 0)", "rgb(255, 255, 255)")).toEqual([255, 255, 255]);
    expect(composite("rgba(10, 20, 30, 1)", "rgb(255, 255, 255)")).toEqual([10, 20, 30]);
    // 带透明度的前景对比度 = 先合成再算，不按原始色算
    expect(contrastRatio("rgba(0, 0, 0, 0.5)", "rgb(255, 255, 255)"))
      .toBeCloseTo(contrastRatio("rgb(128, 128, 128)", "rgb(255, 255, 255)"), 1);
  });

  it("背景带 alpha 时先合成到页面底色（白）再算", () => {
    // KpiCard 默认卡底 rgba(0,0,0,0.025) 叠白底 ≈ 250 灰：对比度按合成结果计
    const onCard = contrastRatio("rgb(107, 102, 94)", "rgba(0, 0, 0, 0.025)");
    const onNearWhite = contrastRatio("rgb(107, 102, 94)", "rgb(250, 250, 250)");
    expect(onCard).toBeCloseTo(onNearWhite, 1);
  });
});

describe("看板局部配色（dashboardColors）：P 投影与漂移守卫", () => {
  it("P 保证存在的基础 token 可解析（index.css 改名/挪走时失败，而不是静默跳过）", () => {
    for (const name of P_GUARANTEED) {
      expect(tokens.get(name), name).toMatch(/^#[0-9A-Fa-f]{6}$/);
    }
  });

  it("fallback 与 token 同色（M0 上定义存在时逐通道相等；P 落 fallback 渲染不变）", () => {
    for (const c of [RATIO_UP, RATIO_DOWN, HIGHLIGHT_TEXT, HIGHLIGHT_BG, HOVER_BG, CONTROL_HINT_TEXT, CONTROL_HOVER_TEXT]) {
      const defined = tokens.get(c.token);
      if (defined == null) continue; // P：token 缺失，fallback 即渲染值（下方组合直接覆盖）
      const [dr, dg, db, da] = parseColor(defined);
      const [fr, fg, fb, fa] = parseColor(c.fallback);
      expect([fr, fg, fb, fa], c.token).toEqual([dr, dg, db, da]);
    }
  });

  it("局部主题 colorPrimary 字面量 ≡ --mb-accent-strong（M0 token 值与 P fallback 同色，两端渲染一致）", () => {
    // colorPrimary 是 seed token：var() 覆盖会被 antd 默认算法调色板派生污染成
    // 纯黑（theme/themes/shared/genColorMapToken.js 以 primaryColors[6] 覆写
    // colorPrimary），因此局部主题只能用字面量。本守卫锁「字面量与 var 写法同值」：
    // M0 上等于 token 定义值；P 上等于 fallback（两者已被上一用例锁同色）——
    // 字面量若偏离 token / fallback，此测试红。
    const literal = (analyticsControlTheme.token ?? {}).colorPrimary;
    expect(typeof literal).toBe("string");
    expect(literal).not.toContain("var(");
    expect(parseColor(literal as string)).toEqual(parseColor(resolve(CONTROL_HOVER_TEXT)));
  });
});

describe("看板所用配色对对比度 ≥ 4.5:1（A-AC14，正常 + hover + alpha）", () => {
  it("KPI 高亮卡（毛利待配）：warning 文字 / 背景对，及文字在白卡上", () => {
    expect(contrastRatio(resolve(HIGHLIGHT_TEXT), resolve(HIGHLIGHT_BG))).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(resolve(HIGHLIGHT_TEXT), tokens.get("--mb-surface")!)).toBeGreaterThanOrEqual(4.5);
  });

  it("KPI 环比增减文字（RATIO_COLOR）：success/danger 文字在白卡、KpiCard 底与 hover 面上", () => {
    for (const fg of [RATIO_UP, RATIO_DOWN]) {
      expect(contrastRatio(resolve(fg), tokens.get("--mb-surface")!)).toBeGreaterThanOrEqual(4.5);
      // KpiCard 默认底 rgba(0,0,0,0.025)（alpha 合成）与悬停底（hover 面级联）
      expect(contrastRatio(resolve(fg), "rgba(0, 0, 0, 0.025)")).toBeGreaterThanOrEqual(4.5);
      expect(contrastRatio(resolve(fg), resolve(HOVER_BG))).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("次级说明文字（「其他」/「上月无」/ 空态 / 口径说明）：text-2 在白卡、hover 面、inset 面与页面底上", () => {
    expect(contrastRatio(tokens.get("--mb-text-2")!, tokens.get("--mb-surface")!)).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(tokens.get("--mb-text-2")!, resolve(HOVER_BG))).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(tokens.get("--mb-text-2")!, tokens.get("--mb-inset")!)).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(tokens.get("--mb-text-2")!, tokens.get("--mb-page")!)).toBeGreaterThanOrEqual(4.5);
  });

  it("KpiCard 默认底（rgba 叠白）：正文与次级文字仍达标", () => {
    expect(contrastRatio(tokens.get("--mb-text")!, "rgba(0, 0, 0, 0.025)")).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(tokens.get("--mb-text-2")!, "rgba(0, 0, 0, 0.025)")).toBeGreaterThanOrEqual(4.5);
  });

  it("正文文字在白卡上；弱提示灰（text-3）只用于白面（hover/inset 面不达标，见组件注释）", () => {
    expect(contrastRatio(tokens.get("--mb-text")!, tokens.get("--mb-surface")!)).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(tokens.get("--mb-text-3")!, tokens.get("--mb-surface")!)).toBeGreaterThanOrEqual(4.5);
  });

  it("禁用 / 占位控件文字（A3 返工）：--mb-text-2 对真实 disabled 底与白底 ≥ 4.5", () => {
    // 真实 disabled 底 = AntD 禁用控件背景 rgba(0,0,0,0.04) 叠白（独立探针合成
    // rgb(244.8,244.8,244.8)）；禁用弹层项 / 占位的底是白面。旧色（全站
    // colorTextQuaternary）在这两底上分别只有 1.97:1 / 2.15:1。
    expect(contrastRatio(resolve(CONTROL_HINT_TEXT), "rgba(0, 0, 0, 0.04)")).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(resolve(CONTROL_HINT_TEXT), "rgb(255, 255, 255)")).toBeGreaterThanOrEqual(4.5);
    // 弹层内禁用项 / 占位也可能落在嵌套卡 inset 底（采购趋势卡 radio 等）
    expect(contrastRatio(resolve(CONTROL_HINT_TEXT), tokens.get("--mb-inset")!)).toBeGreaterThanOrEqual(4.5);
  });

  it("hover / 主色文字（DL-025 稳态矩阵）：--mb-accent-strong 对白底 / 白卡 / inset 底 ≥ 4.5", () => {
    // radio（未选中与选中）hover、重置 / 导出按钮 hover、链接 hover 全部落
    // accent-strong：白底 6.89、白卡 6.89、嵌套卡 inset 底（rgb(246,243,238)）6.22
    expect(contrastRatio(resolve(CONTROL_HOVER_TEXT), "rgb(255, 255, 255)")).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(resolve(CONTROL_HOVER_TEXT), tokens.get("--mb-surface")!)).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(resolve(CONTROL_HOVER_TEXT), tokens.get("--mb-inset")!)).toBeGreaterThanOrEqual(4.5);
  });

  it("导出按钮 loading 态（DL-025 loading 追加）：局部 opacityLoading=1 消除 0.65 整体降透明依赖", () => {
    // 静态依赖：antd 别名 token opacityLoading 默认 0.65（theme/util/alias.js），
    // button/style/index.js 把整个 loading 按钮置该 opacity（`&-loading{opacity}`）。
    // 本仓 --mb-text 足够深，0.65 合成白底仍有 4.73:1（未破线，root 已注明是静态
    // 依赖而非实测失败）；但这是踩线的余量依赖——局部 token=1 后导出按钮
    // loading={exporting} 期间「导出」文字保持全 alpha（colorText 白底 ≥4.5 且
    // 余量充足），loading 语义（旋转图标 / 禁点）不动。规则级回归见
    // controlsContrastScope（.ant-btn-loading 规则 opacity:1 生成）。
    const [tr, tg, tb] = parseColor(tokens.get("--mb-text")!);
    const faded = `rgba(${tr}, ${tg}, ${tb}, 0.65)`;
    const full = tokens.get("--mb-text")!;
    // 依赖方向：0.65 确实拉低对比度（合成 < 全 alpha），且全 alpha 才是交付态
    expect(contrastRatio(faded, "rgb(255, 255, 255)")).toBeLessThan(contrastRatio(full, "rgb(255, 255, 255)"));
    expect(contrastRatio(full, "rgb(255, 255, 255)")).toBeGreaterThanOrEqual(4.5);
    expect((analyticsControlTheme.token ?? {}).opacityLoading).toBe(1);
  });
});
