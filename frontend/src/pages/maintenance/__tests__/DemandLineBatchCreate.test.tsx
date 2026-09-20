import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  create: vi.fn(),
  listProjects: vi.fn(),
  searchProjects: vi.fn(),
  unifiedSearch: vi.fn(),
}));

vi.mock("../../../api/maintenanceDemands", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("../../../api/maintenanceDemands");
  return { ...actual, createDemandLine: (...args: unknown[]) => mocks.create(...args) };
});

vi.mock("../../../api/maintenanceProjects", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("../../../api/maintenanceProjects");
  return {
    ...actual,
    listMaintenanceProjects: (...args: unknown[]) => mocks.listProjects(...args),
    searchMaintenanceProjects: (...args: unknown[]) => mocks.searchProjects(...args),
  };
});

vi.mock("../../../api/search", () => ({
  unifiedSearch: (...args: unknown[]) => mocks.unifiedSearch(...args),
}));

import DemandLineBatchCreate from "../DemandLineBatchCreate";

const part = {
  part_id: 42, pn_std: "PN-STANDARD", description: "部件", brand: "品牌",
  category: "备件", category_major: "备件", needs_review: false, is_excluded: false,
  match_type: "exact_pn", matched_text: "PN", score: 1, match_reason: "PN精确匹配",
  pool_group_id: null, pool_name: null,
};

function created(orderNo: string) {
  return {
    data: {
      changed: true, digest: "a".repeat(64), raw_line_id: `LINE-${orderNo}`,
      part_id: 42, qty: "2.000", return_qty: "0.000", serial_numbers: null,
      description: null, pn_raw: "PN-STANDARD", pn_std: "PN-STANDARD",
      edited_source: "page_manual", manual_override: {}, order_no: orderNo,
    },
  };
}

async function pickPart(index: number) {
  const combo = screen.getAllByRole("combobox")[index];
  fireEvent.mouseDown(combo);
  fireEvent.change(combo, { target: { value: "PN-STANDARD" } });
  let option: HTMLElement | null = null;
  await waitFor(() => {
    option = screen.getAllByText("PN-STANDARD")
      .map((node) => node.closest(".ant-select-item-option") as HTMLElement | null)
      .find((node) => node && !node.closest(".ant-select-dropdown-hidden")) ?? null;
    expect(option).toBeTruthy();
  }, { timeout: 3000 });
  fireEvent.click(option!);
}

async function fillTwoRows() {
  fireEvent.click(screen.getByRole("button", { name: "添加一行" }));
  await pickPart(0);
  await pickPart(1);
  fireEvent.change(screen.getByRole("spinbutton", { name: "第1行需求数量" }), {
    target: { value: "2" },
  });
  fireEvent.change(screen.getByRole("spinbutton", { name: "第2行需求数量" }), {
    target: { value: "3" },
  });
  fireEvent.change(screen.getByRole("textbox", { name: "共同创建原因" }), {
    target: { value: "现场批量补录" },
  });
}

beforeEach(() => {
  mocks.create.mockReset();
  mocks.listProjects.mockReset();
  mocks.searchProjects.mockReset();
  mocks.unifiedSearch.mockReset();
  mocks.unifiedSearch.mockResolvedValue({
    total: 1, page: 1, page_size: 20, exact: true, ambiguous: false,
    low_confidence: false, items: [part], similar_items: [],
  });
});

afterEach(() => cleanup());

describe("DemandLineBatchCreate", () => {
  it("两行一成一败后只重试失败行，且请求内容与幂等键完全不变", async () => {
    const networkError = Object.assign(new Error("Network Error"), { isAxiosError: true });
    mocks.create
      .mockResolvedValueOnce(created("PAGE-1"))
      .mockRejectedValueOnce(networkError)
      .mockResolvedValueOnce(created("PAGE-2"));
    const onCommitted = vi.fn().mockResolvedValue(true);
    render(
      <DemandLineBatchCreate fixedProjectId="P1" onClose={vi.fn()} onCommitted={onCommitted} />,
    );
    await fillTwoRows();
    fireEvent.click(screen.getByRole("button", { name: "提交整批" }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2));
    await screen.findByText(/批次结果：成功 1 行，失败 0 行，结果未知 1 行/);

    const successfulRequest = mocks.create.mock.calls[0][0];
    const failedRequest = mocks.create.mock.calls[1][0];
    expect(successfulRequest.idempotency_key).not.toBe(failedRequest.idempotency_key);

    fireEvent.click(screen.getByRole("button", { name: /重试未完成行/ }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(3));
    expect(mocks.create.mock.calls[2][0]).toEqual(failedRequest);
    expect(mocks.create.mock.calls.filter((call) => call[0] === successfulRequest)).toHaveLength(1);
    await waitFor(() => expect(onCommitted).toHaveBeenCalledTimes(2));
  });

  it("全成功后按钮禁用，成功行不会再次发送", async () => {
    mocks.create
      .mockResolvedValueOnce(created("PAGE-1"))
      .mockResolvedValueOnce(created("PAGE-2"));
    render(
      <DemandLineBatchCreate fixedProjectId="P1" onClose={vi.fn()} onCommitted={vi.fn()} />,
    );
    await fillTwoRows();
    fireEvent.click(screen.getByRole("button", { name: "提交整批" }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2));
    const retry = screen.getByRole("button", { name: /重试未完成行/ });
    await waitFor(() => expect(retry).toBeDisabled());
    fireEvent.click(retry);
    expect(mocks.create).toHaveBeenCalledTimes(2);
  });

  it("快速多击提交只启动一次写入", async () => {
    let resolveCreate!: (value: unknown) => void;
    mocks.create.mockImplementationOnce(() => new Promise((resolve) => { resolveCreate = resolve; }));
    render(
      <DemandLineBatchCreate fixedProjectId="P1" onClose={vi.fn()} onCommitted={vi.fn()} />,
    );
    await pickPart(0);
    fireEvent.change(screen.getByRole("spinbutton", { name: "第1行需求数量" }), {
      target: { value: "2" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "共同创建原因" }), {
      target: { value: "补录" },
    });
    const submit = screen.getByRole("button", { name: "提交整批" });
    fireEvent.click(submit);
    fireEvent.click(submit);
    fireEvent.click(submit);
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    await act(async () => { resolveCreate(created("PAGE-1")); });
    expect(mocks.create).toHaveBeenCalledTimes(1);
  });

  it("保存成功但列表刷新抛错时保留成功结果并提示手动刷新", async () => {
    mocks.create.mockResolvedValue(created("PAGE-1"));
    render(
      <DemandLineBatchCreate fixedProjectId="P1" onClose={vi.fn()}
        onCommitted={vi.fn().mockRejectedValue(new Error("refresh failed"))} />,
    );
    await pickPart(0);
    fireEvent.change(screen.getByRole("spinbutton", { name: "第1行需求数量" }), {
      target: { value: "2" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "共同创建原因" }), {
      target: { value: "补录" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交整批" }));
    expect(await screen.findByText(/需求已保存，但列表刷新失败/)).toBeTruthy();
    expect(screen.getByText("PAGE-1")).toBeTruthy();
    expect(screen.getByText("成功")).toBeTruthy();
  });

  it("运行中切换固定项目会停止后续行，旧请求不刷新新项目", async () => {
    let resolveFirst!: (value: unknown) => void;
    mocks.create.mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }));
    const onCommitted = vi.fn();
    const { rerender } = render(
      <DemandLineBatchCreate fixedProjectId="P1" onClose={vi.fn()} onCommitted={onCommitted} />,
    );
    await fillTwoRows();
    fireEvent.click(screen.getByRole("button", { name: "提交整批" }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));

    rerender(
      <DemandLineBatchCreate fixedProjectId="P2" onClose={vi.fn()} onCommitted={onCommitted} />,
    );
    await act(async () => { resolveFirst(created("PAGE-OLD")); });
    await act(async () => { await Promise.resolve(); });
    expect(mocks.create).toHaveBeenCalledTimes(1);
    expect(onCommitted).not.toHaveBeenCalled();
  });

  it.each([401, 403, 408, 409])("首次408未知后收到%s仍保留原幂等键，核实前不能关闭", async (status) => {
    const onClose = vi.fn();
    mocks.create
      .mockRejectedValueOnce({ response: { status: 408 } })
      .mockRejectedValueOnce({ response: { status } })
      .mockResolvedValueOnce(created("PAGE-REPLAY"));
    render(<DemandLineBatchCreate fixedProjectId="P1" onClose={onClose} onCommitted={vi.fn()} />);
    await pickPart(0);
    fireEvent.change(screen.getByRole("spinbutton", { name: "第1行需求数量" }), {
      target: { value: "2" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "共同创建原因" }), {
      target: { value: "补录" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交整批" }));
    expect(await screen.findByText("结果未知")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: /重试未完成行/ })).not.toHaveClass("ant-btn-loading"));
    const original = mocks.create.mock.calls[0][0];
    expect(original.idempotency_key).toBeTruthy();
    expect(screen.getByRole("button", { name: /关\s*闭/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Close" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /重试未完成行/ }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole("button", { name: /重试未完成行/ })).not.toHaveClass("ant-btn-loading"));
    expect(screen.getByText("结果未知")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /关\s*闭/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /关\s*闭/ }));
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape", keyCode: 27 });
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /重试未完成行/ }));
    expect(await screen.findByText("PAGE-REPLAY")).toBeInTheDocument();
    expect(mocks.create.mock.calls.map((call) => call[0])).toEqual([original, original, original]);
    await waitFor(() => expect(screen.getByRole("button", { name: /关\s*闭/ })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /关\s*闭/ }));
    expect(onClose).toHaveBeenCalledOnce();
  });
});
