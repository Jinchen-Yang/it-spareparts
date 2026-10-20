import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const src = join(dirname(fileURLToPath(import.meta.url)), "../src");
const read = (path: string) => readFileSync(join(src, path), "utf8");
const root = Object.fromEntries([...(read("index.css").match(/:root\s*\{([^}]+)\}/)?.[1] ?? "")
  .matchAll(/(--mb-[\w-]+):\s*([^;]+);/g)].map(([, name, value]) => [name, value.trim()]));
const pageCss = read("pages/PartSearchPage.css");

/** 只解析本页用到的写法：#RRGGBB 或 var(--mb-*)；局部重定义优先于 :root。 */
function resolve(value: string, local: Record<string, string> = {}): string {
  const v = value.trim();
  const ref = v.match(/^var\((--[\w-]+)\)$/)?.[1];
  if (!ref) return v;
  const next = local[ref] ?? root[ref];
  expect(next, ref).toBeDefined();
  return resolve(next, local);
}
function rule(selector: string): Record<string, string> {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const body = pageCss.match(new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]+)\\}`))?.[1];
  expect(body, selector).toBeDefined();
  return Object.fromEntries([...body!.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([\w-]+):\s*([^;]+);/g)].map(([, k, v]) => [k, v.trim()]));
}
const channel = (v: number) => (v /= 255) <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
const luminance = (hex: string) => [1, 3, 5].map((i) => channel(parseInt(hex.slice(i, i + 2), 16)))
  .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
function contrast(a: string, b: string) {
  expect(a).toMatch(/^#[0-9a-f]{6}$/i);
  expect(b).toMatch(/^#[0-9a-f]{6}$/i);
  const [dark, light] = [luminance(a), luminance(b)].sort((x, y) => x - y);
  return (light + 0.05) / (dark + 0.05);
}

describe("型号摘要条在 accent-soft 底上的文字对比度 ≥ 4.5:1 (#428)", () => {
  const summary = rule(".part-summary");
  const local = Object.fromEntries(Object.entries(summary).filter(([k]) => k.startsWith("--")));
  const bg = resolve(summary.background, local);

  it("摘要底为 accent-soft，数值、说明文字、负库存都 ≥ 4.5", () => {
    expect(summary.background).toBe("var(--mb-accent-soft)");
    const colors = {
      数值: rule(".part-summary-metric > strong").color,
      小字说明: rule(".part-summary-metric small").color,
      负库存: rule(".part-summary-metric > strong.is-negative").color,
      "内联弱提示（TaxMoney 含/不含、池成员数）": "var(--mb-text-3)",
    };
    for (const [name, color] of Object.entries(colors)) {
      expect(contrast(resolve(color, local), bg), name).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("原 accent / text-3 / danger 在该底上确实不足，防止回退", () => {
    for (const token of ["--mb-accent", "--mb-text-3", "--mb-danger"]) {
      expect(contrast(root[token], root["--mb-accent-soft"]), token).toBeLessThan(4.5);
    }
    expect(read("pages/PartSearchPage.tsx")).not.toMatch(/dynamic_qty \?\? 0\) < 0 \? "var\(--mb-danger\)" : undefined/);
  });
});
