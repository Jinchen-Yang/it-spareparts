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
});
