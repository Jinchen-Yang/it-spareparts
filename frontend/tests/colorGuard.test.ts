import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import baseline from "./color-baseline.json";

const sourceRoot = join(dirname(fileURLToPath(import.meta.url)), "../src");

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? sourceFiles(path) : /\.(tsx?|css)$/.test(path) ? [path] : [];
  });
}

// 包含注释和测试夹具，防止旧示例被复制回生产。index.css 只豁免同源测试覆盖的 :root 块。
function colorCount(source: string, path: string): number {
  if (path === "tokens.ts") return 0;
  if (path === "index.css") {
    source = source.replace(/:root\s*\{[^}]+\}/, (block) =>
      block.replace(/--mb-[\w-]+:\s*[^;]+;/g, ""));
  }
  return [...source.matchAll(/#[0-9a-f]{3,8}(?![0-9a-f])/gi)].length;
}

function exceedsBaseline(counts: Record<string, number>, limits: Record<string, number>): string[] {
  return Object.entries(counts).filter(([path, count]) => count > (limits[path] ?? 0))
    .map(([path, count]) => `${path}: ${count} > ${limits[path] ?? 0}`);
}

describe("十六进制颜色只减不增 (#408)", () => {
  it("覆盖大小写、3～8 位色值、CSS/TS，且只豁免 token 与 root 定义块", () => {
    expect(colorCount('#ABC #1234 #12345 #aBcDef #abcdef1 #ABCDEF12', 'new.tsx')).toBe(6);
    expect(colorCount('#123456789', 'new.ts')).toBe(0);
    expect(colorCount(':root { --mb-page: #fff; } .x { color: #abc; }', 'index.css')).toBe(1);
    expect(colorCount(':root { --x: #fff; }', 'new.css')).toBe(1);
    expect(colorCount(':root { --mb-page: #fff; color: #abc; }', 'index.css')).toBe(1);
    expect(colorCount('// #123456\nconst color = "#FFF";', 'new.ts')).toBe(2);
    expect(colorCount('const x = "#fff";', 'tokens.ts')).toBe(0);
  });

  it("新文件默认零，已有文件可减少，不可借别的文件额度增加", () => {
    expect(exceedsBaseline({ 'old.ts': 1, 'new.css': 0 }, { 'old.ts': 2 })).toEqual([]);
    expect(exceedsBaseline({ 'old.ts': 3, 'new.css': 1 }, { 'old.ts': 2 }))
      .toEqual(['old.ts: 3 > 2', 'new.css: 1 > 0']);
  });

  it("扫描全部 src；迁完的文件清零，从 baseline 删除后不能回流", () => {
    const counts = Object.fromEntries(sourceFiles(sourceRoot).map((path) => [
      relative(sourceRoot, path), colorCount(readFileSync(path, "utf8"), relative(sourceRoot, path)),
    ]));
    expect(exceedsBaseline(counts, baseline)).toEqual([]);
    // 配额随减少同步收紧，不保留已经用不上的旧额度。
    for (const [path, limit] of Object.entries(baseline)) expect(counts[path], path).toBe(limit);
  });
});
