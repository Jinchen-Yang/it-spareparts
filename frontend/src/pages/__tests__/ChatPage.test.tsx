import { COLORS } from "../../theme";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { App, ConfigProvider } from "antd";
import ChatPage from "../ChatPage";

const api = vi.hoisted(() => ({
  listChatSessions: vi.fn(),
  getChatMessages: vi.fn(),
  deleteChatSession: vi.fn(),
  createChatSession: vi.fn(),
  sessionChatStream: vi.fn(),
  agentDownload: vi.fn(), agentFileBlobUrl: vi.fn(), agentPreview: vi.fn(),
  agentUpload: vi.fn(), attachChatStream: vi.fn(), cancelChatStream: vi.fn(),
}));
vi.mock("../../api", () => api);

function viewport(width: number) {
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    matches: query.includes("max-width")
      ? width <= Number(query.match(/\d+/)?.[0])
      : width >= Number(query.match(/\d+/)?.[0]),
    media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
}

function setup() {
  return render(<ConfigProvider theme={{ token: { motion: false } }}><App><ChatPage /></App></ConfigProvider>);
}

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  api.listChatSessions.mockResolvedValue({ data: { items: [
    { id: 7, title: "上次报价", generating: false },
  ] } });
  api.getChatMessages.mockResolvedValue({ data: { items: [
    { role: "assistant", content: "上次报价记录" },
  ] } });
  api.deleteChatSession.mockResolvedValue({});
  Element.prototype.scrollIntoView = vi.fn();
  viewport(375);
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("AI 助手的响应式会话入口", () => {
  it("助手图标沿用本仓蓝色，样式不再由页面内联 style 注入", async () => {
    viewport(1024);
    const { container } = setup();
    const main = await screen.findByRole("main", { name: "AI 助手对话区" });
    const icon = within(main).getByRole("img", { name: "robot" });
    expect(icon.parentElement).toHaveStyle({
      background: `linear-gradient(135deg,${COLORS.status.ai},${COLORS.accentStrong})`,
    });
    expect(container.querySelector("style")).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: "上次报价" }));
    expect(await screen.findByText("上次报价记录")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上次报价" })).toHaveStyle({ color: COLORS.status.ai });
  });

  it("手机会话藏在抽屉中，选择后关闭抽屉并保留可输入的对话区", async () => {
    setup();
    const trigger = await screen.findByRole("button", { name: "会话" });
    expect(screen.queryByRole("complementary", { name: "会话列表" })).toBeNull();
    expect(screen.queryByText("上次报价")).toBeNull();
    fireEvent.click(trigger);
    const drawer = await screen.findByRole("dialog", { name: "会话" });
    fireEvent.click(within(drawer).getByRole("button", { name: "上次报价" }));
    expect(await screen.findByText("上次报价记录")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "继续询价" } });
    expect(input).toHaveValue("继续询价");
  });

  it.each([768, 1024])("%i 宽保持桌面会话栏，选择会话可读取历史", async (width) => {
    viewport(width);
    setup();
    const sidebar = await screen.findByRole("complementary", { name: "会话列表" });
    expect(screen.queryByRole("button", { name: "会话" })).toBeNull();
    fireEvent.click(await within(sidebar).findByRole("button", { name: "上次报价" }));
    expect(await screen.findByText("上次报价记录")).toBeInTheDocument();
  });

  it("移动会话删除按钮可聚焦，确认前不删除且不误选会话", async () => {
    setup();
    fireEvent.click(await screen.findByRole("button", { name: "会话" }));
    const remove = await screen.findByRole("button", { name: "删除对话：上次报价" });
    expect(remove.tagName).toBe("BUTTON");
    remove.focus();
    expect(remove).toHaveFocus();
    fireEvent.click(remove);
    expect(await screen.findByText("删除该对话？")).toBeInTheDocument();
    expect(api.deleteChatSession).not.toHaveBeenCalled();
    expect(api.getChatMessages).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "OK" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "上次报价" })).toBeNull());
    expect(api.deleteChatSession).toHaveBeenCalledWith(7);
  });

  it("工具轨迹折叠头可聚焦并公开展开状态", async () => {
    viewport(1024);
    api.getChatMessages.mockResolvedValue({ data: { items: [
      { role: "assistant", content: "上次报价记录", tools: [{ name: "search_parts", args: { query: "TEST-PN-7" } }] },
    ] } });
    setup();
    fireEvent.click(await screen.findByRole("button", { name: "上次报价" }));
    const toggle = await screen.findByRole("button", { name: /用了 1 次工具/ });
    toggle.focus();
    expect(toggle).toHaveFocus();
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("（TEST-PN-7）")).toBeInTheDocument();
  });
});
