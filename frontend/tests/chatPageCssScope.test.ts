import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// ChatPage.css 是全局样式表：类名必须带 chat- 前缀，避免 .cursor / .copy-btn 这类通用名污染其他页面。
const root = join(dirname(fileURLToPath(import.meta.url)), "../src/pages");

describe("ChatPage.css 类名作用域（#431 审查）", () => {
  it("所有类选择器都带 chat- 前缀，且 .chat-page-shell 同时声明 vh 回退与 dvh", () => {
    const css = readFileSync(join(root, "ChatPage.css"), "utf8");
    const classes = [...css.replace(/@keyframes[^{]*\{(?:[^{}]*\{[^}]*\})*[^}]*\}/g, "")
      .matchAll(/\.([A-Za-z_][\w-]*)/g)].map((m) => m[1]);
    expect(classes.length).toBeGreaterThan(0);
    expect(classes.filter((name) => !name.startsWith("chat-"))).toEqual([]);
    const shell = css.match(/\.chat-page-shell\s*\{([^}]+)\}/)?.[1] ?? "";
    expect(shell).toMatch(/height:\s*calc\(100vh - var\(--mb-shell-header-height\)\);\s*height:\s*calc\(100dvh - var\(--mb-shell-header-height\)\);/);
    const tsx = readFileSync(join(root, "ChatPage.tsx"), "utf8");
    expect(tsx).toContain("className=\"chat-page-shell\"");
    expect(tsx).not.toMatch(/\b100dvh\b/);
  });
});
