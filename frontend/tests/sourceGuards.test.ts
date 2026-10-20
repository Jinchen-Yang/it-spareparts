import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const sourceRoot = join(dirname(fileURLToPath(import.meta.url)), "../src");

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? sourceFiles(path) : /\.(tsx?|css)$/.test(path) ? [path] : [];
  });
}

function violations(source: string, path: string): string[] {
  const failures: string[] = [];
  // Include comments and test fixtures so copying old examples cannot reintroduce the color.
  if (/#9c968b/i.test(source)) failures.push("deprecated text color");
  if (path.endsWith(".css")) return failures;
  const ast = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  function visit(node: ts.Node) {
    if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) && node.tagName.getText(ast) === "a") {
      const href = node.attributes.properties.find((prop) => ts.isJsxAttribute(prop) && prop.name.getText(ast) === "href");
      if (!href) failures.push("anchor without href; use a Button for actions");
    }
    if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier) && node.moduleSpecifier.text === "antd") {
      const bindings = node.importClause?.namedBindings;
      if (bindings && ts.isNamedImports(bindings) && bindings.elements.some((item) => (item.propertyName ?? item.name).text === "message")) {
        failures.push("static message import; use App.useApp()");
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(ast);
  return failures;
}

describe("UX source guards (#406 / #407 / #409 I12)", () => {
  it("detects multiline action anchors, wrapped anchors, aliased messages and case-insensitive colors", () => {
    const source = `import { message as toast } from "antd";
      <><a\n style={{ color: "red" }}\n onClick={() => {}}>操作</a>
      <Popconfirm><a>停用</a></Popconfirm><span style={{ color: "#9C968B" }} /></>`;
    expect(violations(source, "example.tsx")).toEqual([
      "deprecated text color",
      "static message import; use App.useApp()",
      "anchor without href; use a Button for actions",
      "anchor without href; use a Button for actions",
    ]);
    expect(violations('import { App, Button } from "antd"; <><a href="/help">帮助</a><Button onClick={() => {}}>操作</Button></>', "example.tsx")).toEqual([]);
  });

  it("keeps all src files free of inaccessible anchors, obsolete colors and static message imports", () => {
    const failures = sourceFiles(sourceRoot).flatMap((path) => violations(readFileSync(path, "utf8"), path).map((failure) => `${relative(sourceRoot, path)}: ${failure}`));
    expect(failures).toEqual([]);
  });
});
