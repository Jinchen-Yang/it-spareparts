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
  HIGHLIGHT_BG,
  HIGHLIGHT_TEXT,
  HOVER_BG,
  RATIO_DOWN,
  RATIO_UP,
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
const P_GUARANTEED = ["--mb-surface", "--mb-inset", "--mb-page", "--mb-text", "--mb-text-2", "--mb-text-3"];

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
    for (const c of [RATIO_UP, RATIO_DOWN, HIGHLIGHT_TEXT, HIGHLIGHT_BG, HOVER_BG]) {
      const defined = tokens.get(c.token);
      if (defined == null) continue; // P：token 缺失，fallback 即渲染值（下方组合直接覆盖）
      const [dr, dg, db, da] = parseColor(defined);
      const [fr, fg, fb, fa] = parseColor(c.fallback);
      expect([fr, fg, fb, fa], c.token).toEqual([dr, dg, db, da]);
    }
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
});
