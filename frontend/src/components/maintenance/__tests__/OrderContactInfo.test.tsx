import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import OrderContactInfo from "../OrderContactInfo";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("WBDD contact display", () => {
  it("wraps long addresses and copies the full address and leading-zero phone", async () => {
    const address = "合成长地址路".repeat(80) + "\n1号楼 007室";
    const copied: string[] = [];
    // Exercise AntD's real copy control and inspect the selected clipboard text.
    Object.defineProperty(document, "execCommand", { configurable: true, value: () => true });
    vi.spyOn(document, "execCommand").mockImplementation(() => {
      copied.push(window.getSelection()?.toString() ?? "");
      return true;
    });
    render(<OrderContactInfo contact={{ contact_info_state: "visible", receiver_address: address,
      receiver: "合成联系人", receiver_phone: "00123456789" }} />);
    expect(screen.getByText("收货地址：").parentElement?.parentElement).toHaveStyle({
      whiteSpace: "pre-wrap", overflowWrap: "anywhere",
    });
    expect(screen.getByText("联系人：")).toBeInTheDocument();
    expect(screen.getByText("电话：")).toBeInTheDocument();
    const buttons = screen.getAllByRole("button");
    fireEvent.click(buttons[0]);
    await waitFor(() => expect(copied).toEqual([address]));
    fireEvent.click(buttons[2]);
    await waitFor(() => expect(copied).toEqual([address, "00123456789"]));
  });

  it("renders partial/blank contact fields without copy controls for absent data", () => {
    render(<OrderContactInfo contact={{ contact_info_state: "visible", receiver_address: "  ",
      receiver: "合成联系人", receiver_phone: null }} />);
    expect(screen.getAllByText("—")).toHaveLength(2);
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.queryByText("无权限查看联系信息")).toBeNull();
  });

  it("does not render or offer copying of restricted fields even if a stale payload contains values", () => {
    render(<OrderContactInfo contact={{ contact_info_state: "restricted", receiver_address: "隐藏地址",
      receiver: "隐藏联系人", receiver_phone: "000123" }} />);
    expect(screen.getByText("无权限查看联系信息")).toBeInTheDocument();
    expect(screen.queryByText("隐藏地址")).toBeNull();
    expect(screen.queryByText("隐藏联系人")).toBeNull();
    expect(screen.queryByText("000123")).toBeNull();
    expect(screen.queryByText("—")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
