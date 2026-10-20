import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// 不支持 dvh 的浏览器（较旧 Safari / 企业内嵌 WebView）会丢弃整条 dvh 声明；
// 每处 dvh 都必须紧跟在同属性的 vh 回退之后。内联 style 对象无法写两条同名属性，
// 因此 dvh 只允许出现在 CSS 文本（.css 或 <style> 模板）里。
const sourceRoot = join(dirname(fileURLToPath(import.meta.url)), "../src");

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return entry.name === "__tests__" ? [] : sourceFiles(path);
    return /\.(tsx?|css)$/.test(path) && !/\.test\.tsx?$/.test(path) ? [path] : [];
  });
}

describe("视口高度 dvh 回退", () => {
  it("每条 dvh 声明前都有同属性的 vh 回退，且不出现在内联 style 中", () => {
    const problems: string[] = [];
    for (const file of sourceFiles(sourceRoot)) {
      const text = readFileSync(file, "utf8");
      const name = relative(sourceRoot, file);
      const declaration = /([a-z-]+)\s*:\s*([^;{}\n]*\b\d+dvh\b[^;{}\n]*);/g;
      for (const match of text.matchAll(declaration)) {
        const [, property, value] = match;
        const before = text.slice(0, match.index).trimEnd();
        const fallback = value.replace(/(\d+)dvh/g, "$1vh");
        if (!before.endsWith(`${property}: ${fallback};`)) {
          problems.push(`${name}: ${property}: ${value} 缺少前置回退 ${property}: ${fallback};`);
        }
      }
      for (const match of text.matchAll(/[A-Za-z]+\s*:\s*["`][^"`\n]*\b\d+dvh\b/g)) {
        problems.push(`${name}: 内联 style 使用 dvh（无法写 vh 回退）：${match[0]}`);
      }
    }
    expect(problems).toEqual([]);
  });
});
