import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { tokens, tokenCssVariables } from "../src/tokens";
import { COLORS, SHELL_LAYOUT, themeConfig } from "../src/theme";

const css = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../src/index.css"), "utf8");

describe("设计 token 的公共契约 (#408 / I18)", () => {
  it("保留兼容导出，并让 AntD 与壳层消费同一份 token", () => {
    expect(COLORS).toBe(tokens.color);
    expect(SHELL_LAYOUT).toBe(tokens.layout);
    expect(tokens.space).toEqual({ xs: 4, sm: 8, md: 12, lg: 16, xl: 24, xxl: 32 });
    expect(tokens.fontSize).toEqual({ xs: 12, sm: 13, md: 14, lg: 16, xl: 20, xxl: 24 });
    expect(themeConfig.token).toMatchObject({
      colorPrimary: tokens.color.accent, colorBgLayout: tokens.color.page,
      colorSuccess: tokens.color.success, colorError: tokens.color.danger,
      colorWarning: tokens.color.warning, colorBorderSecondary: tokens.color.borderSoft,
      borderRadius: tokens.radius.md, fontSize: tokens.fontSize.md,
    });
    expect(themeConfig.components?.Layout?.headerHeight).toBe(tokens.layout.headerHeight);
  });

  it("CSS 定义块与 token 映射完全一致，所有 --mb 变量都有定义", () => {
    const root = css.match(/:root\s*\{([^}]+)\}/)?.[1] ?? "";
    const declarations = [...root.matchAll(/(--mb-[\w-]+):\s*([^;]+);/g)];
    const definitions = Object.fromEntries(declarations.map(([, name, value]) => [name, value.trim()]));
    expect(declarations).toHaveLength(Object.keys(definitions).length);
    expect(definitions).toEqual(tokenCssVariables);
    expect(definitions["--mb-border-soft"]).toBe(tokens.color.borderSoft);
    for (const [, name] of css.matchAll(/var\((--mb-[\w-]+)/g)) {
      expect(definitions, name).toHaveProperty(name);
    }
    expect(css).not.toContain("!important");
  });

  it("排名探索器局部卡片边框沿用暖边 token，避免覆盖全局卡片规则", () => {
    const explorerCss = readFileSync(join(dirname(fileURLToPath(import.meta.url)),
      "../src/pages/maintenance/maintenanceRankingsExplorer.css"), "utf8");
    const cardRule = explorerCss.match(/\.maintenance-explorer \.ant-card\s*\{([^}]+)\}/)?.[1];
    expect(cardRule).toMatch(/border-color:\s*var\(--mb-border\)/);
  });

  it("互通池表头口径按钮与节省金额用深色 token，在表头底与白底上都 ≥ 4.5:1 (#433)", () => {
    const channel = (v: number) => (v /= 255) <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    const luminance = (hex: string) => [1, 3, 5].map((i) => channel(parseInt(hex.slice(i, i + 2), 16)))
      .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
    const contrast = (a: string, b: string) => {
      const [dark, light] = [luminance(a), luminance(b)].sort((x, y) => x - y);
      return (light + 0.05) / (dark + 0.05);
    };
    for (const fg of [tokens.color.accentStrong, tokens.color.tag.warning.text]) {
      for (const bg of [tokens.color.inset, tokens.color.surface]) {
        expect(contrast(fg, bg), `${fg} on ${bg}`).toBeGreaterThanOrEqual(4.5);
      }
    }
    const read = (file: string) => readFileSync(join(dirname(fileURLToPath(import.meta.url)),
      `../src/pages/boss/${file}`), "utf8");
    const poolsCss = read("PoolsBlock.css");
    expect(poolsCss.match(/\.pool-metric-toggle\s*\{([^}]+)\}/)?.[1]).toMatch(/color:\s*var\(--mb-accent-strong\)/);
    expect(poolsCss).toMatch(/\.pool-metric-toggle--average\s*\{\s*color:\s*var\(--mb-tag-warning-text\)/);
    const poolsTsx = read("PoolsBlock.tsx");
    expect(poolsTsx).toMatch(/total:\s*tokens\.color\.accentStrong,\s*average:\s*tokens\.color\.tag\.warning\.text/);
    expect(poolsTsx).not.toMatch(/tokens\.color\.(accent|warning)\b(?!Strong)/);
  });
});
