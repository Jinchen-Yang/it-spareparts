import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { message } from "antd";

const listChatSessions = vi.fn();
const getChatMessages = vi.fn();
const attachChatStream = vi.fn();

vi.mock("../../api", () => ({
  agentDownload: vi.fn(),
  agentFileBlobUrl: vi.fn(),
  agentPreview: vi.fn(),
  agentUpload: vi.fn(),
  attachChatStream: (...args: unknown[]) => attachChatStream(...args),
  cancelChatStream: vi.fn(),
  createChatSession: vi.fn(),
  deleteChatSession: vi.fn(),
  getChatMessages: (...args: unknown[]) => getChatMessages(...args),
  listChatSessions: (...args: unknown[]) => listChatSessions(...args),
  sessionChatStream: vi.fn(),
}));

import ChatPage from "../ChatPage";

describe("会话实时订阅驱逐", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    HTMLElement.prototype.scrollIntoView = vi.fn();
    listChatSessions.mockResolvedValue({
      data: {
        items: [{
          id: 7,
          title: "后台回答",
          updated_at: "2026-08-10T00:00:00Z",
          generating: true,
        }],
      },
    });
    getChatMessages.mockResolvedValue({
      data: { id: 7, title: "后台回答", items: [] },
    });
    attachChatStream.mockImplementation(
      async (_id: number, onEvent: (event: object) => void) => {
        onEvent({ type: "subscriber_evicted", retry_attach: true });
      },
    );
  });

  afterEach(() => {
    cleanup();
    message.destroy();
  });

  it("不把驱逐当完成，并保留后台生成标记后重载持久化历史", async () => {
    const warning = vi.spyOn(message, "warning").mockImplementation(() => undefined as never);
    render(<ChatPage />);

    fireEvent.click(await screen.findByRole("button", { name: /后台回答/ }));

    await waitFor(() => expect(attachChatStream).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(getChatMessages).toHaveBeenCalledTimes(2));
    expect(screen.getByTitle("生成中（后台）")).toBeInTheDocument();
    expect(warning).toHaveBeenCalledWith(expect.stringContaining("仍在后台生成"));
  });
});
