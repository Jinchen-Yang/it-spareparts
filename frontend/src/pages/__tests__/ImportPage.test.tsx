import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { message } from "antd";
import { StrictMode } from "react";
import type { ImportPrecheckResult } from "../../api/imports";

const get = vi.fn();
const precheckImportFiles = vi.fn();
const uploadImportBatch = vi.fn();

vi.mock("../../api", () => ({
  default: { get: (...args: unknown[]) => get(...args) },
}));

vi.mock("../../api/imports", () => ({
  precheckImportFiles: (...args: unknown[]) => precheckImportFiles(...args),
  uploadImportBatch: (...args: unknown[]) => uploadImportBatch(...args),
}));

import ImportPage from "../ImportPage";

const cleanResult: ImportPrecheckResult = {
  contract: "v2",
  decision: "clean",
  blocked: false,
  files: [{
    filename: "采购.xlsx",
    file_type: "purchase",
    warning: null,
    severity: "info",
    can_import: true,
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

describe("导入预检状态机", () => {
  it("正常文件也必须先展示预检结果，不能在预检 handler 中自动上传", async () => {
    render(<ImportPage />);
    const file = stage();
    expect(await screen.findByText("采购.xlsx")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /预检文件/ }));

    expect(await screen.findByText("采购明细")).toBeInTheDocument();
    expect(screen.getByText("将导入")).toBeInTheDocument();
    expect(screen.getByText("表头行：2")).toBeInTheDocument();
    expect(screen.getByText("数据行：8")).toBeInTheDocument();
    expect(precheckImportFiles).toHaveBeenCalledWith([file], expect.any(AbortSignal));
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
      files: [{
        filename: "旧版.xlsx", file_type: "purchase", warning: "旧版缺价格提示",
        severity: "unknown", can_import: null, issues: [], sheets: [],
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

  it.each(["replace", "delete", "clear", "mode"] as const)(
    "%s 会立即使旧预检结果失效",
    async (action) => {
      render(<ImportPage />);
      const oldFile = stage();
      fireEvent.click(await screen.findByRole("button", { name: /预检文件/ }));
      await screen.findByText("采购明细");

      if (action === "replace") {
        const replacement = new File(["xlsx"], oldFile.name);
        expect(replacement.size).toBe(oldFile.size);
        stage(replacement);
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

      if (action === "replace") {
        fireEvent.click(screen.getByRole("button", { name: /预检文件/ }));
        await waitFor(() => expect(precheckImportFiles).toHaveBeenCalledTimes(2));
        expect(precheckImportFiles.mock.calls[1][0][0]).not.toBe(oldFile);
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
    const signal = precheckImportFiles.mock.calls[0][1] as AbortSignal;
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
    const signal = precheckImportFiles.mock.calls[0][1] as AbortSignal;

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
