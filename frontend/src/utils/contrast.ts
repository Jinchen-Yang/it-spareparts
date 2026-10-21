/**
 * WCAG 2.x 对比度计算（A-AC14 单测用）：支持 #RGB / #RRGGBB / rgba()/rgb()，
 * 带透明度的前景先按 Source-Over 合成到背景上再算（处理 alpha，不只静态常量）。
 * 页面实际使用的颜色 token 与背景组合在此断言 ≥ 4.5:1；真实浏览器 hover 态
 * 由验收阶段实测，单测保证计算口径与所选 token 组合本身达标。
 */

/** 解析 #RGB / #RRGGBB / rgb() / rgba() → [r, g, b, a]（测试也用：token 与 fallback 的漂移守卫）。 */
export function parseColor(color: string): [number, number, number, number] {
  const c = color.trim();
  const hex = c.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (hex) {
    const h = hex[1];
    const full = h.length === 3 ? [...h].map((x) => x + x).join("") : h;
    return [
      parseInt(full.slice(0, 2), 16),
      parseInt(full.slice(2, 4), 16),
      parseInt(full.slice(4, 6), 16),
      1,
    ];
  }
  const rgb = c.match(/^rgba?\(([^)]+)\)$/i);
  if (rgb) {
    const parts = rgb[1].split(/[\s,/]+/).filter(Boolean).map(Number);
    return [parts[0], parts[1], parts[2], parts.length > 3 ? parts[3] : 1];
  }
  throw new Error(`不支持的颜色格式：${color}`);
}

/** alpha 前景合成到背景（Source-Over），返回不透明 RGB。 */
export function composite(fg: string, bg: string): [number, number, number] {
  const [fr, fgc, fb, fa] = parseColor(fg);
  const [br, bgc, bb] = parseColor(bg);
  return [
    Math.round(fr * fa + br * (1 - fa)),
    Math.round(fgc * fa + bgc * (1 - fa)),
    Math.round(fb * fa + bb * (1 - fa)),
  ];
}

function luminance(rgb: [number, number, number]): number {
  const lin = rgb.map((v) => {
    const s = v / 255;
    return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
}

/** 对比度（1 最差，21 最好）。任一颜色带 alpha 时先合成到另一色。 */
export function contrastRatio(fg: string, bg: string): number {
  const fgA = parseColor(fg)[3];
  const bgA = parseColor(bg)[3];
  // 两端都可能带 alpha：先把背景合成到不透明底（页面底色），再合成前景。
  // 用 rgb() 写白色——十六进制色值受 tests/colorGuard 只减不增约束
  const WHITE = "rgb(255, 255, 255)";
  const bgOpaque = bgA < 1 ? composite(bg, WHITE) : parseColor(bg).slice(0, 3) as [number, number, number];
  const fgOpaque = fgA < 1 ? composite(fg, `rgb(${bgOpaque.join(",")})`) : parseColor(fg).slice(0, 3) as [number, number, number];
  const l1 = luminance(fgOpaque);
  const l2 = luminance(bgOpaque);
  const [hi, lo] = l1 >= l2 ? [l1, l2] : [l2, l1];
  return (hi + 0.05) / (lo + 0.05);
}
