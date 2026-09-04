import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { message } from "antd";
import { StrictMode } from "react";
import type { ImportPrecheckResult } from "../../api/imports";

const get = vi.fn();
const downloadImportErrors = vi.fn();
const precheckImportFiles = vi.fn();
const uploadImportBatch = vi.fn();
const previewExpenseVoid = vi.fn();

vi.mock("../../api", () => ({
  default: { get: (...args: unknown[]) => get(...args) },
}));

vi.mock("../../api/imports", () => ({
  downloadImportErrors: (...args: unknown[]) => downloadImportErrors(...args),
  precheckImportFiles: (...args: unknown[]) => precheckImportFiles(...args),
  uploadImportBatch: (...args: unknown[]) => uploadImportBatch(...args),
  previewExpenseVoid: (...args: unknown[]) => previewExpenseVoid(...args),
}));

import ImportPage from "../ImportPage";

const cleanResult: ImportPrecheckResult = {
  contract: "v2",
  decision: "clean",
  blocked: false,
  mode: "skip",
  files: [{
    filename: "采购.xlsx",
    file_type: "purchase",
    warning: null,
    severity: "info",
    can_import: true,
    exact_success_match: null,
    blocked_reason: null,
    issues: [],
    sheets: [{
      sheet_name: "采购明细",
      detected_type: "purchase",
      action: "selected",
      header_row: 2,
      data_rows: 8,
      duplicate_headers: [],
      issues: [],
    }],
  }],
};

function blockedResult(code: string, messageText: string): ImportPrecheckResult {
  return {
    ...cleanResult,
    decision: "blocked",
    blocked: true,
    files: [{
      ...cleanResult.files[0],
      severity: "error",
      can_import: false,
      issues: [{ severity: "error", code, message: messageText }],
    }],
  };
}

const warningResult: ImportPrecheckResult = {
  ...cleanResult,
  decision: "warning",
  files: [{
    ...cleanResult.files[0],
    warning: "未识别到价格列",
    severity: "warning",
    issues: [{ severity: "warning", code: "missing_price_columns", message: "导入后将没有金额" }],
  }],
};

const skipDuplicateResult: ImportPrecheckResult = {
  ...cleanResult,
  decision: "blocked",
  blocked: true,
  files: [{
    ...cleanResult.files[0],
    can_import: false,
    exact_success_match: { batch_id: 42 },
    blocked_reason: "exact_success_duplicate",
  }],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

function stage(file = new File(["xlsx"], "采购.xlsx")) {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(input, { target: { files: [file] } });
  return file;
}

beforeEach(() => {
  vi.clearAllMocks();
  get.mockResolvedValue({ data: [] });
  precheckImportFiles.mockResolvedValue(cleanResult);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  cleanup();
  message.destroy();
});

describe("批量文件数量提示", () => {
  it("明确提示每批最多 20 个文件", () => {
    render(<ImportPage />);

    expect(screen.getByText(/每批最多 20 个文件/)).toBeInTheDocument();
  });

  it("保留同名同大小但内容不同的文件并按选择顺序预检", async () => {
    render(<ImportPage />);
    const first = stage(new File([Uint8Array.of(1)], "同名.xlsx"));
    const second = stage(new File([Uint8Array.of(2)], "同名.xlsx"));

    expect(await screen.findByText("待导入 2 个文件")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /预检文件/ }));

    await waitFor(() => expect(precheckImportFiles).toHaveBeenCalledTimes(1));
    expect(precheckImportFiles.mock.calls[0][0][0]).toBe(first);
    expect(precheckImportFiles.mock.calls[0][0][1]).toBe(second);
    expect(precheckImportFiles).toHaveBeenCalledWith(
      expect.any(Array), "skip", expect.any(AbortSignal),
    );
  });

  it("超出数量限制后保留全部文件并允许精确删除一个后重试", async () => {
    const files = Array.from({ length: 21 }, (_, index) => (
      new File([Uint8Array.of(index)], "同名.xlsx")
    ));
    precheckImportFiles
      .mockRejectedValueOnce({
        response: { status: 400, data: { detail: "一次最多导入 20 个文件，请分批处理" } },
      })
      .mockResolvedValueOnce(cleanResult);
    render(<ImportPage />);

    files.forEach((file) => stage(file));
    expect(await screen.findByText("待导入 21 个文件")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "delete" })).toHaveLength(20);
    expect(screen.getByText("另有 1 个文件未展示，请删除或清空后分批处理")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /预检文件/ }));
    expect(await screen.findByText("一次最多导入 20 个文件，请分批处理")).toBeInTheDocument();
    expect(screen.getByText("待导入 21 个文件")).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "delete" })[0]);
    expect(screen.getByText("待导入 20 个文件")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "delete" })).toHaveLength(20);
    expect(screen.queryByText(/个文件未展示，请删除或清空后分批处理/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /预检文件/ }));

    await waitFor(() => expect(precheckImportFiles).toHaveBeenCalledTimes(2));
    expect(precheckImportFiles.mock.calls[0][0]).toHaveLength(21);
    precheckImportFiles.mock.calls[0][0].forEach((file: File, index: number) => {
      expect(file).toBe(files[index]);
    });
    expect(precheckImportFiles.mock.calls[1][0]).toHaveLength(20);
    precheckImportFiles.mock.calls[1][0].forEach((file: File, index: number) => {
      expect(file).toBe(files[index + 1]);
    });
  });
});

describe("完整导入问题明细下载", () => {
  it("批次有问题时显示完整数量和可忽略提示说明", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") return Promise.resolve({ data: [{
        id: 7, filename: "错误.xlsx", file_type: "purchase", status: "failed",
        uploaded_at: "2026-07-27T00:00:00Z", uploaded_by: "admin",
        rows_total: 501, rows_inserted: 0, rows_skipped: 0, rows_error: 501,
        rows_inactive: 0,
      }] });
      if (url === "/import/batches/7") return Promise.resolve({ data: {
        id: 7, filename: "错误.xlsx", report: {}, errors: [], issue_count: 501,
      } });
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    render(<ImportPage />);

    fireEvent.click(await screen.findByText("详情"));

    expect(await screen.findByRole("button", {
      name: "下载完整导入问题明细（501 条）",
    })).toBeInTheDocument();
    expect(screen.getByText(
      "可能包含草稿/取消单等可忽略提示，不代表源数据错误",
    )).toBeInTheDocument();
  });

  it("无错误批次不显示下载按钮", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") return Promise.resolve({ data: [{
        id: 8, filename: "正常.xlsx", file_type: "purchase", status: "success",
        uploaded_at: "2026-07-27T00:00:00Z", uploaded_by: "admin",
        rows_total: 1, rows_inserted: 1, rows_skipped: 0, rows_error: 0,
        rows_inactive: 0,
      }] });
      if (url === "/import/batches/8") return Promise.resolve({ data: {
        id: 8, filename: "正常.xlsx", report: {}, errors: [], issue_count: 0,
      } });
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    render(<ImportPage />);

    fireEvent.click(await screen.findByText("详情"));
    await screen.findByText("批次 #8 · 正常.xlsx");

    expect(screen.queryByRole("button", { name: /下载完整导入问题明细/ })).toBeNull();
    expect(screen.getByText("无问题行")).toBeInTheDocument();
  });

  it("下载中同步双击只请求一次，完成后按钮恢复且详情保持打开", async () => {
    const download = deferred<void>();
    downloadImportErrors.mockReturnValueOnce(download.promise);
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") return Promise.resolve({ data: [{
        id: 9, filename: "错误.xlsx", file_type: "purchase", status: "failed",
        uploaded_at: "2026-07-27T00:00:00Z", uploaded_by: "admin",
        rows_total: 3, rows_inserted: 0, rows_skipped: 0, rows_error: 3,
        rows_inactive: 0,
      }] });
      if (url === "/import/batches/9") return Promise.resolve({ data: {
        id: 9, filename: "错误.xlsx", report: {}, errors: [], issue_count: 3,
      } });
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    render(<ImportPage />);
    fireEvent.click(await screen.findByText("详情"));
    const button = await screen.findByRole("button", {
      name: "下载完整导入问题明细（3 条）",
    });

    fireEvent.click(button);
    fireEvent.click(button);

    expect(downloadImportErrors).toHaveBeenCalledTimes(1);
    expect(downloadImportErrors).toHaveBeenCalledWith(9);
    expect(button).toBeDisabled();
    download.resolve();
    await waitFor(() => expect(button).toBeEnabled());
    expect(screen.getByText("批次 #9 · 错误.xlsx")).toBeInTheDocument();
  });

  it.each([
    ["403", { response: { status: 403 } }, "无权下载问题明细，请联系管理员开通数据导入权限"],
    ["404", { response: { status: 404 } }, "未找到批次，无法下载问题明细"],
    ["网络", { message: "Network Error" }, "网络连接失败，请检查网络后重试下载"],
  ])("下载遇到%s错误时明确提示且不关闭详情", async (_label, error, expected) => {
    downloadImportErrors.mockRejectedValueOnce(error);
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") return Promise.resolve({ data: [{
        id: 10, filename: "错误.xlsx", file_type: "purchase", status: "failed",
        uploaded_at: "2026-07-27T00:00:00Z", uploaded_by: "admin",
        rows_total: 1, rows_inserted: 0, rows_skipped: 0, rows_error: 1,
        rows_inactive: 0,
      }] });
      if (url === "/import/batches/10") return Promise.resolve({ data: {
        id: 10, filename: "错误.xlsx", report: {}, errors: [], issue_count: 1,
      } });
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    render(<ImportPage />);
    fireEvent.click(await screen.findByText("详情"));

    fireEvent.click(await screen.findByRole("button", {
      name: "下载完整导入问题明细（1 条）",
    }));

    expect(await screen.findByText(expected)).toBeInTheDocument();
    expect(screen.getByText("批次 #10 · 错误.xlsx")).toBeInTheDocument();
  });
});

describe("导入预检状态机", () => {
  it("skip 精确重复显示成功事实、移除文案并回链原批次", async () => {
    precheckImportFiles.mockResolvedValueOnce(skipDuplicateResult);
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") return Promise.resolve({ data: [] });
      if (url === "/import/batches/42") return Promise.resolve({ data: {
        id: 42, filename: "原采购.xlsx", report: {}, errors: [], issue_count: 0,
      } });
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));

    expect(await screen.findByText("已成功导入")).toBeInTheDocument();
    expect(screen.getByText("文件字节完全相同，系统不会再次导入")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "请移除已导入文件后重新预检" })).toBeDisabled();
    expect(screen.queryByText("请修正后重新预检")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "查看原批次 #42" }));
    expect(await screen.findByText("批次 #42 · 原采购.xlsx")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith(
      "/import/batches/42", { timeout: 30_000, signal: expect.any(AbortSignal) },
    );
  });

  it("skip 精确重复与内容错误并存时提示同时处理两类问题", async () => {
    precheckImportFiles.mockResolvedValueOnce({
      ...skipDuplicateResult,
      files: [
        skipDuplicateResult.files[0],
        {
          ...cleanResult.files[0],
          filename: "错误.xlsx",
          severity: "error",
          can_import: false,
          issues: [{ severity: "error", code: "invalid_workbook", message: "工作簿已损坏" }],
        },
      ],
    });
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));

    expect(await screen.findByText("工作簿已损坏")).toBeInTheDocument();
    expect(screen.getByRole("button", {
      name: "请移除已导入文件并处理其他预检问题后重新预检",
    })).toBeDisabled();
  });

  it("upsert 精确重复只显示修复事实，不增加确认或 warning", async () => {
    precheckImportFiles.mockResolvedValueOnce({
      ...cleanResult,
      mode: "upsert",
      files: [{ ...cleanResult.files[0], exact_success_match: { batch_id: 42 } }],
    });
    render(<ImportPage />);
    fireEvent.click(screen.getByRole("switch"));
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));

    expect(await screen.findByText(
      "当前为修复模式，继续后会重新处理；仅新批次完整成功后原批次才标记为已替代",
    )).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "我已阅读并确认以上警告" })).toBeNull();
    expect(screen.getByRole("button", { name: "开始导入" })).toBeEnabled();
    expect(precheckImportFiles).toHaveBeenCalledWith(
      expect.any(Array), "upsert", expect.any(AbortSignal),
    );
  });

  it("原批次回链同步双击只复用一次详情请求", async () => {
    const detailRequest = deferred<{ data: unknown }>();
    precheckImportFiles.mockResolvedValueOnce(skipDuplicateResult);
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") return Promise.resolve({ data: [] });
      if (url === "/import/batches/42") return detailRequest.promise;
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    const link = await screen.findByRole("button", { name: "查看原批次 #42" });

    fireEvent.click(link);
    fireEvent.click(link);

    expect(get.mock.calls.filter(([url]) => url === "/import/batches/42")).toHaveLength(1);
    detailRequest.resolve({ data: {
      id: 42, filename: "原采购.xlsx", report: {}, errors: [], issue_count: 0,
    } });
    expect(await screen.findByText("批次 #42 · 原采购.xlsx")).toBeInTheDocument();
  });

  it("原批次详情超时后提示重试并允许再次请求同一批次", async () => {
    const firstDetailRequest = deferred<{ data: unknown }>();
    precheckImportFiles.mockResolvedValueOnce(skipDuplicateResult);
    let detailCallCount = 0;
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") return Promise.resolve({ data: [] });
      if (url === "/import/batches/42") {
        detailCallCount += 1;
        return detailCallCount === 1 ? firstDetailRequest.promise : new Promise(() => {});
      }
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    const link = await screen.findByRole("button", { name: "查看原批次 #42" });

    fireEvent.click(link);
    expect(get).toHaveBeenCalledWith("/import/batches/42", {
      timeout: 30_000,
      signal: expect.any(AbortSignal),
    });

    firstDetailRequest.reject({ code: "ECONNABORTED", message: "timeout of 30000ms exceeded" });
    expect(await screen.findByText("原批次详情加载超时，请重试")).toBeInTheDocument();
    fireEvent.click(link);

    expect(get.mock.calls.filter(([url]) => url === "/import/batches/42")).toHaveLength(2);
  });

  it("模式变化后新预检的原批次回链取代悬挂请求", async () => {
    const oldDetailRequest = deferred<{ data: unknown }>();
    precheckImportFiles
      .mockResolvedValueOnce(skipDuplicateResult)
      .mockResolvedValueOnce({
        ...cleanResult,
        mode: "upsert",
        files: [{ ...cleanResult.files[0], exact_success_match: { batch_id: 42 } }],
      });
    let detailCallCount = 0;
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") return Promise.resolve({ data: [] });
      if (url === "/import/batches/42") {
        detailCallCount += 1;
        if (detailCallCount === 1) return oldDetailRequest.promise;
        return Promise.resolve({ data: {
          id: 42, filename: "新请求.xlsx", report: {}, errors: [], issue_count: 0,
        } });
      }
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    fireEvent.click(await screen.findByRole("button", { name: "查看原批次 #42" }));

    fireEvent.click(screen.getByRole("switch"));
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    fireEvent.click(await screen.findByRole("button", { name: "查看原批次 #42" }));

    const detailCalls = get.mock.calls.filter(([url]) => url === "/import/batches/42");
    expect(detailCalls).toHaveLength(2);
    expect((detailCalls[0][1] as { signal: AbortSignal }).signal.aborted).toBe(true);
    expect(await screen.findByText("批次 #42 · 新请求.xlsx")).toBeInTheDocument();
  });

  it("模式变化后丢弃迟到的原批次详情响应", async () => {
    const detailRequest = deferred<{ data: unknown }>();
    precheckImportFiles.mockResolvedValueOnce(skipDuplicateResult);
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") return Promise.resolve({ data: [] });
      if (url === "/import/batches/42") return detailRequest.promise;
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    fireEvent.click(await screen.findByRole("button", { name: "查看原批次 #42" }));

    fireEvent.click(screen.getByRole("switch"));
    detailRequest.resolve({ data: {
      id: 42, filename: "原采购.xlsx", report: {}, errors: [], issue_count: 0,
    } });
    await act(async () => {});

    expect(screen.queryByText("批次 #42 · 原采购.xlsx")).toBeNull();
    expect(screen.queryByText("已成功导入")).toBeNull();
  });

  it.each([
    ["403", { response: { status: 403 } }, "无权查看原批次详情，请联系管理员开通数据导入权限"],
    ["404", { response: { status: 404 } }, "原批次不存在或已无法访问"],
    ["网络", { message: "Network Error" }, "网络连接失败，请检查网络后重试查看原批次"],
    ["其他", { response: { status: 500 } }, "原批次详情加载失败，请稍后重试"],
  ])("原批次回链遇到%s错误时明确提示且保留预检", async (_label, error, expected) => {
    precheckImportFiles.mockResolvedValueOnce(skipDuplicateResult);
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") return Promise.resolve({ data: [] });
      if (url === "/import/batches/42") return Promise.reject(error);
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    fireEvent.click(await screen.findByRole("button", { name: "查看原批次 #42" }));

    expect(await screen.findByText(expected)).toBeInTheDocument();
    expect(screen.getByText("已成功导入")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "请移除已导入文件后重新预检" })).toBeDisabled();
  });

  it("正常文件也必须先展示预检结果，不能在预检 handler 中自动上传", async () => {
    render(<ImportPage />);
    const file = stage();
    expect(await screen.findByText("采购.xlsx")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /预检文件/ }));

    expect(await screen.findByText("采购明细")).toBeInTheDocument();
    expect(screen.getByText("将导入")).toBeInTheDocument();
    expect(screen.getByText("表头行：2")).toBeInTheDocument();
    expect(screen.getByText("数据行：8")).toBeInTheDocument();
    expect(precheckImportFiles).toHaveBeenCalledWith([file], "skip", expect.any(AbortSignal));
    expect(uploadImportBatch).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "开始导入" })).toBeEnabled();
  });

  it.each([
    ["duplicate_headers", "存在重复表头"],
    ["no_recognized_sheet", "无法识别可导入工作表"],
    ["invalid_workbook", "工作簿已损坏"],
    ["row_limit_exceeded", "数据行超过限制"],
  ])("%s 致命问题会保留文件和详情但绝不上传", async (code, issueText) => {
    precheckImportFiles.mockResolvedValueOnce(blockedResult(code, issueText));
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));

    expect(await screen.findByText(issueText)).toBeInTheDocument();
    expect(screen.getAllByText("采购.xlsx")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "请修正后重新预检" })).toBeDisabled();
    expect(uploadImportBatch).not.toHaveBeenCalled();
  });

  it("重复表头问题提示删除或重命名表头后重新预检", async () => {
    precheckImportFiles.mockResolvedValueOnce(blockedResult("duplicate_headers", "存在重复表头"));
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));

    const issueItem = (await screen.findByText("存在重复表头")).closest(".ant-list-item");
    expect(issueItem).toHaveTextContent("请删除或重命名重复表头后重新预检");
  });

  it("未识别工作表问题提示重新导出支持的文件或按模板修正表头", async () => {
    precheckImportFiles.mockResolvedValueOnce(blockedResult(
      "no_recognized_sheet", "无法识别可导入工作表",
    ));
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));

    const issueItem = (await screen.findByText("无法识别可导入工作表")).closest(".ant-list-item");
    expect(issueItem).toHaveTextContent("请重新导出支持的文件或按模板修正表头后重新预检");
  });

  it("未知问题使用不声称自动修复的安全通用建议", async () => {
    precheckImportFiles.mockResolvedValueOnce(blockedResult("future_issue", "发现未知文件问题"));
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));

    const issueItem = (await screen.findByText("发现未知文件问题")).closest(".ant-list-item");
    expect(issueItem).toHaveTextContent("请根据问题说明检查文件，修正后重新预检");
    expect(issueItem).not.toHaveTextContent(/自动修复|自动修改/);
  });

  it("缺价格警告未确认或返回修改时都不会上传", async () => {
    precheckImportFiles.mockResolvedValueOnce(warningResult);
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));

    expect(await screen.findByText("导入后将没有金额")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认警告并导入" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "返回修改" }));
    expect(screen.queryByText("导入后将没有金额")).toBeNull();
    expect(screen.getByRole("button", { name: /预检文件/ })).toBeEnabled();
    expect(uploadImportBatch).not.toHaveBeenCalled();
  });

  it("缺价格问题提示重新导出含价格字段的视图，并仅在确认无需金额时继续", async () => {
    precheckImportFiles.mockResolvedValueOnce(warningResult);
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));

    const issue = await screen.findByText("导入后将没有金额");
    const issueItem = issue.closest(".ant-list-item");
    expect(issueItem).toHaveTextContent("请使用包含单价、金额或税字段的视图重新导出");
    expect(issueItem).toHaveTextContent("仅确认确实无需金额时才继续");
  });

  it("确认警告后同事件循环双击也只正式提交一次", async () => {
    const upload = deferred<{ job_id: number; total_files: number }>();
    precheckImportFiles.mockResolvedValueOnce(warningResult);
    uploadImportBatch.mockReturnValueOnce(upload.promise);
    render(<ImportPage />);
    const file = stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    await screen.findByText("导入后将没有金额");

    fireEvent.click(screen.getByRole("checkbox", { name: "我已阅读并确认以上警告" }));
    const submit = screen.getByRole("button", { name: "确认警告并导入" });
    fireEvent.click(submit);
    fireEvent.click(submit);

    expect(uploadImportBatch).toHaveBeenCalledTimes(1);
    expect(uploadImportBatch).toHaveBeenCalledWith([file], "skip");
  });

  it("多工作表完整展示三种 action、行数、重复表头和全部 issues", async () => {
    precheckImportFiles.mockResolvedValueOnce({
      ...warningResult,
      files: [{
        ...warningResult.files[0],
        sheets: [
          {
            sheet_name: "采购", detected_type: "purchase", action: "selected",
            header_row: 2, data_rows: 12, duplicate_headers: ["产品名称"],
            issues: [{ severity: "warning", code: "missing_price", message: "采购缺价格" }],
          },
          {
            sheet_name: "销售", detected_type: "sales", action: "ignored_recognized",
            header_row: 3, data_rows: 7, duplicate_headers: [],
            issues: [{ severity: "warning", code: "ignored", message: "销售已识别但跳过" }],
          },
          {
            sheet_name: "说明", detected_type: null, action: "ignored_unrecognized",
            header_row: 1, data_rows: 2, duplicate_headers: [],
            issues: [{ severity: "info", code: "unknown", message: "说明页无法识别" }],
          },
        ],
      }],
    } satisfies ImportPrecheckResult);
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));

    for (const text of [
      "采购", "销售", "说明", "将导入", "已识别但不导入", "无法识别且不导入",
      "表头行：2", "数据行：12", "重复表头：产品名称",
      "采购缺价格", "销售已识别但跳过", "说明页无法识别",
    ]) expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it("已识别但跳过的工作表提示当前只导入选中页及单独上传方式", async () => {
    precheckImportFiles.mockResolvedValueOnce({
      ...warningResult,
      files: [{
        ...warningResult.files[0],
        issues: [],
        sheets: [{
          sheet_name: "销售", detected_type: "sales", action: "ignored_recognized",
          header_row: 3, data_rows: 7, duplicate_headers: [],
          issues: [{
            severity: "warning", code: "sheet_ignored_recognized", message: "销售页已识别但跳过",
          }],
        }],
      }],
    } satisfies ImportPrecheckResult);
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));

    const issueItem = (await screen.findByText("销售页已识别但跳过")).closest(".ant-list-item");
    expect(issueItem).toHaveTextContent("当前规则只导入选中页");
    expect(issueItem).toHaveTextContent("如需该页请单独导出并上传");
  });

  it.each(["legacy", "invalid"] as const)("%s 结果只兼容展示且绝不显示通过或允许上传", async (contract) => {
    precheckImportFiles.mockResolvedValueOnce({
      contract,
      decision: "unknown",
      blocked: true,
      mode: null,
      files: [{
        filename: "旧版.xlsx", file_type: "purchase", warning: "旧版缺价格提示",
        severity: "unknown", can_import: null, exact_success_match: null,
        blocked_reason: null, issues: [], sheets: [],
      }],
    } satisfies ImportPrecheckResult);
    render(<ImportPage />);
    stage(new File(["old"], "旧版.xlsx"));
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));

    expect(await screen.findByText("旧版缺价格提示")).toBeInTheDocument();
    expect(screen.getByText("预检结果无法安全确认")).toBeInTheDocument();
    expect(screen.queryByText(/通过/)).toBeNull();
    expect(screen.getByRole("button", { name: "请修正后重新预检" })).toBeDisabled();
    expect(uploadImportBatch).not.toHaveBeenCalled();
  });

  it.each(["add", "delete", "clear", "mode"] as const)(
    "%s 会立即使旧预检结果失效",
    async (action) => {
      render(<ImportPage />);
      const oldFile = stage();
      let addedFile: File | undefined;
      fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
      await screen.findByText("采购明细");

      if (action === "add") {
        addedFile = new File(["xlsx"], oldFile.name);
        expect(addedFile.size).toBe(oldFile.size);
        stage(addedFile);
      } else if (action === "delete") {
        fireEvent.click(screen.getByRole("button", { name: "delete" }));
      } else if (action === "clear") {
        fireEvent.click(screen.getByRole("button", { name: /清\s*空/ }));
      } else {
        fireEvent.click(screen.getByRole("switch"));
      }

      expect(screen.queryByText("采购明细")).toBeNull();
      expect(screen.queryByRole("button", { name: "开始导入" })).toBeNull();
      expect(uploadImportBatch).not.toHaveBeenCalled();

      if (action === "add") {
        fireEvent.click(screen.getByRole("button", { name: /预检文件/ }));
        await waitFor(() => expect(precheckImportFiles).toHaveBeenCalledTimes(2));
        expect(precheckImportFiles.mock.calls[1][0]).toEqual([oldFile, addedFile]);
      }
    },
  );

  it("预检按钮同步双击只发送一次请求", async () => {
    const request = deferred<ImportPrecheckResult>();
    precheckImportFiles.mockReturnValueOnce(request.promise);
    render(<ImportPage />);
    stage();
    const button = await screen.findByRole("button", { name: /预检文件/ });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(precheckImportFiles).toHaveBeenCalledTimes(1);
    request.resolve(cleanResult);
    expect(await screen.findByText("采购明细")).toBeInTheDocument();
  });

  it("输入 revision 改变后丢弃迟到的预检响应", async () => {
    const request = deferred<ImportPrecheckResult>();
    precheckImportFiles.mockReturnValueOnce(request.promise);
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    expect(document.querySelector(".ant-upload-disabled")).toBeInTheDocument();

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [new File(["new"], "新文件.xlsx")] } });
    request.resolve(cleanResult);
    await waitFor(() => expect(screen.queryByText("采购明细")).toBeNull());
    expect(uploadImportBatch).not.toHaveBeenCalled();
  });

  it("输入失效时 abort 正在进行的预检请求", async () => {
    const request = deferred<ImportPrecheckResult>();
    precheckImportFiles.mockReturnValueOnce(request.promise);
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    const signal = precheckImportFiles.mock.calls[0][2] as AbortSignal;
    expect(signal.aborted).toBe(false);

    stage(new File(["new"], "新文件.xlsx"));

    expect(signal.aborted).toBe(true);
    request.reject(new DOMException("aborted", "AbortError"));
    await act(async () => {});
    expect(screen.queryByText(/网络连接失败/)).toBeNull();
  });

  it("页面卸载时 abort 正在进行的预检请求", async () => {
    const request = deferred<ImportPrecheckResult>();
    precheckImportFiles.mockReturnValueOnce(request.promise);
    const view = render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    const signal = precheckImportFiles.mock.calls[0][2] as AbortSignal;

    view.unmount();

    expect(signal.aborted).toBe(true);
    request.reject(new DOMException("aborted", "AbortError"));
    await act(async () => {});
  });

  it.each([
    ["403", { response: { status: 403, data: { detail: "仅管理员可导入" } } }, "无权限：仅管理员可导入"],
    ["network", { message: "Network Error" }, "网络连接失败：请检查网络后重新预检"],
    ["timeout", { code: "ECONNABORTED", message: "timeout of 30000ms exceeded" }, "预检超时：请检查文件大小后重新预检"],
    ["detail", { response: { status: 400, data: { detail: "文件数量超过限制" } } }, "文件数量超过限制"],
  ])("预检 %s 失败显示持久明确 Alert 且不显示成功", async (_label, error, expected) => {
    precheckImportFiles.mockRejectedValueOnce(error);
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));

    expect(await screen.findByText(expected)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "开始导入" })).toBeNull();
    expect(screen.queryByText(/成功|通过/)).toBeNull();
    expect(uploadImportBatch).not.toHaveBeenCalled();
  });

  it("正式提交失败不伪造 job、不重试，并提示先查导入历史", async () => {
    uploadImportBatch.mockRejectedValueOnce({ response: { data: { detail: "服务拒绝提交" } } });
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    fireEvent.click(await screen.findByRole("button", { name: "开始导入" }));

    expect(await screen.findByText(/服务拒绝提交。请先查看导入历史/)).toBeInTheDocument();
    expect(screen.queryByText(/导入作业 #/)).toBeNull();
    expect(uploadImportBatch).toHaveBeenCalledTimes(1);
  });

  it("成功提交只创建一次 job，使用预检 snapshot 的文件和模式并进入轮询", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") return Promise.resolve({ data: [] });
      if (url === "/import/jobs/42") return Promise.resolve({ data: {
        id: 42, status: "done", mode: "upsert", total_files: 1,
        done_files: 1, error_files: 0, note: null, batches: [],
      } });
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    uploadImportBatch.mockResolvedValueOnce({ job_id: 42, total_files: 1 });
    render(<ImportPage />);
    fireEvent.click(screen.getByRole("switch"));
    const file = stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    const submit = await screen.findByRole("button", { name: "开始导入" });
    fireEvent.click(submit);
    fireEvent.click(submit);

    expect(await screen.findByText("导入作业 #42")).toBeInTheDocument();
    expect(uploadImportBatch).toHaveBeenCalledTimes(1);
    expect(uploadImportBatch).toHaveBeenCalledWith([file], "upsert");
    await waitFor(() => expect(get).toHaveBeenCalledWith("/import/jobs/42"));
  });

  it("upload 返回 job_id 时即使输入 revision 已变化也保留新输入并启动轮询", async () => {
    const upload = deferred<{ job_id: number; total_files: number }>();
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") return Promise.resolve({ data: [] });
      if (url === "/import/jobs/73") return Promise.resolve({ data: {
        id: 73, status: "processing", mode: "skip", total_files: 1,
        done_files: 0, error_files: 0, note: null, batches: [],
      } });
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    uploadImportBatch.mockReturnValueOnce(upload.promise);
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    fireEvent.click(await screen.findByRole("button", { name: "开始导入" }));

    const newFile = new File(["new"], "新采购.xlsx");
    stage(newFile);
    upload.resolve({ job_id: 73, total_files: 1 });

    expect(await screen.findByText("导入作业 #73")).toBeInTheDocument();
    expect(screen.getByText("新采购.xlsx")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /预检文件/ })).toBeDisabled();
    await waitFor(() => expect(get).toHaveBeenCalledWith("/import/jobs/73"));
    expect(uploadImportBatch).toHaveBeenCalledTimes(1);
  });

  it("旧 upload 失败不覆盖 revision 变化后的新输入状态", async () => {
    const upload = deferred<{ job_id: number; total_files: number }>();
    uploadImportBatch.mockReturnValueOnce(upload.promise);
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    fireEvent.click(await screen.findByRole("button", { name: "开始导入" }));
    stage(new File(["new"], "新采购.xlsx"));

    upload.reject({ response: { data: { detail: "旧请求失败" } } });
    await act(async () => {});

    expect(screen.getByRole("button", { name: /预检文件/ })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "开始导入" })).toBeNull();
    expect(screen.queryByText(/旧请求失败/)).toBeNull();
  });

  it("StrictMode 页面卸载后迟到的 upload 成功不启动 job 查询或 timer", async () => {
    const upload = deferred<{ job_id: number; total_files: number }>();
    uploadImportBatch.mockReturnValueOnce(upload.promise);
    const view = render(<StrictMode><ImportPage /></StrictMode>);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    fireEvent.click(await screen.findByRole("button", { name: "开始导入" }));

    view.unmount();
    const timeout = vi.spyOn(window, "setTimeout");
    upload.resolve({ job_id: 85, total_files: 1 });
    await act(async () => {});

    expect(get.mock.calls.filter(([url]) => url === "/import/jobs/85")).toHaveLength(0);
    expect(timeout).not.toHaveBeenCalled();
  });

  it("卸载时使 in-flight poll 失效且响应后不再调度查询", async () => {
    const pollRequest = deferred<{ data: {
      id: number; status: string; mode: string; total_files: number;
      done_files: number; error_files: number; note: null; batches: never[];
    } }>();
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") return Promise.resolve({ data: [] });
      if (url === "/import/jobs/83") return pollRequest.promise;
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    uploadImportBatch.mockResolvedValueOnce({ job_id: 83, total_files: 1 });
    const view = render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    fireEvent.click(await screen.findByRole("button", { name: "开始导入" }));
    await screen.findByText("导入作业 #83");
    expect(get.mock.calls.filter(([url]) => url === "/import/jobs/83")).toHaveLength(1);

    vi.useFakeTimers();
    view.unmount();
    pollRequest.resolve({ data: {
      id: 83, status: "processing", mode: "skip", total_files: 1,
      done_files: 0, error_files: 0, note: null, batches: [],
    } });
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });

    expect(get.mock.calls.filter(([url]) => url === "/import/jobs/83")).toHaveLength(1);
  });

  it("单次 polling 网络失败后在 deadline 前继续查询同一 job", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") return Promise.resolve({ data: [] });
      if (url === "/import/jobs/81") {
        const jobCalls = get.mock.calls.filter(([calledUrl]) => calledUrl === url).length;
        if (jobCalls === 1) return Promise.reject(new Error("transient"));
        return Promise.resolve({ data: {
          id: 81, status: "done", mode: "skip", total_files: 1,
          done_files: 1, error_files: 0, note: null, batches: [],
        } });
      }
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    uploadImportBatch.mockResolvedValueOnce({ job_id: 81, total_files: 1 });
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    const submit = await screen.findByRole("button", { name: "开始导入" });
    vi.useFakeTimers();
    fireEvent.click(submit);
    await act(async () => {});
    expect(screen.getByText("导入作业 #81")).toBeInTheDocument();
    expect(get.mock.calls.filter(([url]) => url === "/import/jobs/81")).toHaveLength(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });

    expect(get.mock.calls.filter(([url]) => url === "/import/jobs/81")).toHaveLength(2);
    expect(screen.getByText("全部完成")).toBeInTheDocument();
    expect(uploadImportBatch).toHaveBeenCalledTimes(1);
  });

  it("polling 到 deadline 后解除 busy 并允许继续查询而不创建新 upload", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") return Promise.resolve({ data: [] });
      if (url === "/import/jobs/82") return Promise.reject(new Error("offline"));
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    uploadImportBatch.mockResolvedValueOnce({ job_id: 82, total_files: 1 });
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    const submit = await screen.findByRole("button", { name: "开始导入" });
    vi.useFakeTimers();
    vi.setSystemTime(1_000);
    fireEvent.click(submit);
    await act(async () => {});
    expect(screen.getByText("导入作业 #82")).toBeInTheDocument();
    expect(get.mock.calls.filter(([url]) => url === "/import/jobs/82")).toHaveLength(1);

    vi.setSystemTime(1_000 + 15 * 60 * 1000 + 1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });

    expect(screen.getByText("作业查询已中断，可继续查询当前作业")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "继续查询" })).toBeEnabled();
    expect(document.querySelector(".ant-upload-disabled")).toBeNull();

    vi.setSystemTime(2_000);
    fireEvent.click(screen.getByRole("button", { name: "继续查询" }));
    await act(async () => {});
    expect(get.mock.calls.filter(([url]) => url === "/import/jobs/82")).toHaveLength(2);
    expect(uploadImportBatch).toHaveBeenCalledTimes(1);
  });

  it("继续查询同步双击只发一个 GET，后续再次中断仍可恢复", async () => {
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") return Promise.resolve({ data: [] });
      if (url === "/import/jobs/86") return new Promise(() => {});
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    uploadImportBatch.mockResolvedValueOnce({ job_id: 86, total_files: 1 });
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    const submit = await screen.findByRole("button", { name: "开始导入" });
    vi.useFakeTimers();
    vi.setSystemTime(1_000);
    fireEvent.click(submit);
    await act(async () => {});

    await act(async () => { await vi.advanceTimersByTimeAsync(15 * 60 * 1000); });
    const resume = screen.getByRole("button", { name: "继续查询" });
    act(() => {
      resume.click();
      resume.click();
    });
    await act(async () => {});
    expect(get.mock.calls.filter(([url]) => url === "/import/jobs/86")).toHaveLength(2);

    await act(async () => { await vi.advanceTimersByTimeAsync(15 * 60 * 1000); });
    fireEvent.click(screen.getByRole("button", { name: "继续查询" }));
    await act(async () => {});
    expect(get.mock.calls.filter(([url]) => url === "/import/jobs/86")).toHaveLength(3);
  });

  it("polling 请求一直 pending 时 deadline 仍会中断并解除 busy", async () => {
    const pollRequest = deferred<never>();
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") return Promise.resolve({ data: [] });
      if (url === "/import/jobs/84") return pollRequest.promise;
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    uploadImportBatch.mockResolvedValueOnce({ job_id: 84, total_files: 1 });
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    const submit = await screen.findByRole("button", { name: "开始导入" });
    vi.useFakeTimers();
    vi.setSystemTime(1_000);
    fireEvent.click(submit);
    await act(async () => {});
    expect(screen.getByText("导入作业 #84")).toBeInTheDocument();

    await act(async () => { await vi.advanceTimersByTimeAsync(15 * 60 * 1000); });

    expect(screen.getByText("作业查询已中断，可继续查询当前作业")).toBeInTheDocument();
    expect(document.querySelector(".ant-upload-disabled")).toBeNull();
    expect(uploadImportBatch).toHaveBeenCalledTimes(1);
  });

  it("polling 中断后的历史刷新失败不会产生 unhandled rejection", async () => {
    let batchRequests = 0;
    get.mockImplementation((url: string) => {
      if (url === "/import/batches") {
        batchRequests += 1;
        return batchRequests === 1
          ? Promise.resolve({ data: [] })
          : Promise.reject(new Error("history offline"));
      }
      if (url === "/import/jobs/87") return new Promise(() => {});
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    uploadImportBatch.mockResolvedValueOnce({ job_id: 87, total_files: 1 });
    render(<ImportPage />);
    stage();
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    const submit = await screen.findByRole("button", { name: "开始导入" });
    vi.useFakeTimers();
    fireEvent.click(submit);
    await act(async () => {});

    await act(async () => { await vi.advanceTimersByTimeAsync(15 * 60 * 1000); });

    expect(screen.getByText("作业查询已中断，可继续查询当前作业")).toBeInTheDocument();
    expect(batchRequests).toBe(2);
  });
});

// ---- 作废预演（修复模式 · D-10 预演即承诺）----

const armedResult: ImportPrecheckResult = {
  ...cleanResult,
  decision: "warning",
  mode: "upsert",
  files: [{
    ...cleanResult.files[0],
    filename: "报销.xlsx",
    file_type: "expense",
    severity: "warning",
    issues: [{ severity: "warning", code: "upsert_void_armed",
      message: "本表为单合同（XSDD-P）项目工作簿报销页且无错误行：修复模式将把该合同名下未出现在本表的旧报销行作废。" }],
  }],
};

const readyPreview = {
  filename: "报销.xlsx", status: "ready", reason: null, contract: "XSDD-P", contracts: ["XSDD-P"],
  rows_incoming: 2, dropped_no_contract: 0, blocking_error_types: [],
  void: { rows: 1, amount: "1200.00", already_void_rows: 0 },
  void_rows: [{ raw_line_id: "BXD-3#1@abc", linked_sales_order_no: "XSDD-P", bxd_no: "BXD-3",
    line_no: 1, expense_date: "2026-05-03", person: "丙", reason: "租金",
    data_status: "已结束", amount: "1200.00" }],
  void_rows_truncated: false, preview_token: "tok.1", error: null,
};

describe("作废预演（修复模式）", () => {
  it("预检说会作废的文件：向服务端要预演、展示逐行清单，令牌随正式提交带回", async () => {
    precheckImportFiles.mockResolvedValueOnce(armedResult);
    previewExpenseVoid.mockResolvedValueOnce(readyPreview);
    uploadImportBatch.mockResolvedValueOnce({ job_id: 7, total_files: 1 });
    get.mockImplementation((url: string) => url === "/import/jobs/7"
      ? Promise.resolve({ data: { id: 7, status: "done", mode: "upsert", total_files: 1,
        done_files: 1, error_files: 0, note: null, batches: [] } })
      : Promise.resolve({ data: [] }));
    render(<ImportPage />);
    fireEvent.click(screen.getByRole("switch"));
    const file = stage(new File(["xlsx"], "报销.xlsx"));
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));

    expect(await screen.findByText(/将作废 1 条旧报销行，合计 1200\.00/)).toBeInTheDocument();
    expect(screen.getByText("租金")).toBeInTheDocument();
    expect(previewExpenseVoid).toHaveBeenCalledWith(file, "upsert", expect.anything());
    fireEvent.click(screen.getByRole("checkbox", { name: /我已逐行核对作废预演清单（共 1 行）/ }));
    fireEvent.click(screen.getByRole("button", { name: "确认警告并导入" }));

    expect(await screen.findByText("导入作业 #7")).toBeInTheDocument();
    expect(uploadImportBatch).toHaveBeenCalledWith([file], "upsert", ["tok.1"]);
  });

  it("预演拿不到 ready（会被整批拒绝）就不允许提交，不让用户对着没有清单的「将作废」按确认", async () => {
    precheckImportFiles.mockResolvedValueOnce(armedResult);
    previewExpenseVoid.mockResolvedValueOnce({
      ...readyPreview, status: "will_be_rejected", void: null, void_rows: [],
      preview_token: null, blocking_error_types: ["duplicate_key"],
    });
    render(<ImportPage />);
    fireEvent.click(screen.getByRole("switch"));
    stage(new File(["xlsx"], "报销.xlsx"));
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));

    expect(await screen.findByText("导入将被整批拒绝")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认警告并导入" })).toBeNull();
    expect(screen.queryByRole("button", { name: "开始导入" })).toBeNull();
    expect(uploadImportBatch).not.toHaveBeenCalled();
  });

  it("提交时预演已失效（void_plan_drift）：提示重新预检，不伪造作业", async () => {
    precheckImportFiles.mockResolvedValueOnce(armedResult);
    previewExpenseVoid.mockResolvedValueOnce(readyPreview);
    uploadImportBatch.mockRejectedValueOnce({ response: {
      status: 409, headers: { "x-error-code": "void_plan_drift" },
      data: { detail: "作废预演已失效：预演之后相关报销行发生变化，本批未导入，请重新预演" },
    } });
    render(<ImportPage />);
    fireEvent.click(screen.getByRole("switch"));
    stage(new File(["xlsx"], "报销.xlsx"));
    fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
    await screen.findByText(/将作废 1 条旧报销行/);
    fireEvent.click(screen.getByRole("checkbox", { name: /我已逐行核对作废预演清单/ }));
    fireEvent.click(screen.getByRole("button", { name: "确认警告并导入" }));

    expect(await screen.findByText(/作废预演已失效.*重新预检/)).toBeInTheDocument();
    expect(screen.queryByText(/导入作业 #/)).toBeNull();
  });
});

