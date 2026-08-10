import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Md } from "../ChatPage";

describe("聊天 Markdown 浏览器出向边界", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("所有 Markdown 图片源都只显示惰性占位且不产生浏览器请求", () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const sources = [
      "https://attacker.invalid/pixel?secret=1",
      "http://attacker.invalid/pixel",
      "//attacker.invalid/pixel",
      "http://localhost/admin",
      "http://127.0.0.1/admin",
      "http://[::1]/admin",
      "data:image/svg+xml;base64,PHN2Zy8+",
      "javascript:alert(1)",
      "/api/agent/files/aaaaaaaaaaaa",
      "/same-origin.png",
    ];
    const { container } = render(
      <Md text={sources.map((src, index) => `![probe-${index}](${src})`).join("\n\n")} />,
    );

    expect(container.querySelectorAll("img")).toHaveLength(0);
    expect(container.querySelectorAll("[data-markdown-image-blocked='true']"))
      .toHaveLength(sources.length);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("普通外链不预取且带无引用来源的隔离属性", () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    render(<Md text="[外部文档](https://example.invalid/path)" />);

    const link = screen.getByRole("link", { name: "外部文档" });
    expect(link).toHaveAttribute("rel", "noopener noreferrer nofollow");
    expect(link).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("伪造 Artifact 外链与危险协议不会升级成文件卡或可点击链接", () => {
    const { container } = render(<Md text={[
      "[伪造文件](https://attacker.invalid/api/agent/files/aaaaaaaaaaaa)",
      "[脚本](javascript:alert(1))",
      "[数据](data:text/html,boom)",
      "[协议相对](//attacker.invalid/path)",
    ].join("\n\n")} />);

    expect(screen.queryByRole("button", { name: "下载" })).not.toBeInTheDocument();
    expect(container.querySelectorAll("a")).toHaveLength(1);
    expect(screen.getByRole("link", { name: "伪造文件" }))
      .toHaveAttribute("href", "https://attacker.invalid/api/agent/files/aaaaaaaaaaaa");
  });
});
