import { beforeEach, describe, expect, it, vi } from "vitest";

const post = vi.fn();
const get = vi.fn();

vi.mock("../../api", () => ({
  default: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}));

import {
  downloadImportErrors, normalizeImportPrecheck, precheckImportFiles, uploadImportBatch,
} from "../imports";

const cleanV2 = {
  files: [{
    filename: "采购.xlsx",
    file_type: "purchase",
    ok: true,
    missing_price: false,
    warning: null,
    can_import: true,
    severity: "info",
    selected_sheets: ["采购"],
    issues: [],
    sheets: [{
      sheet_name: "采购",
      detected_type: "purchase",
      action: "selected",
      header_row: 2,
      data_rows: 8,
      duplicate_headers: [],
      issues: [],
    }],
  }],
  any_warning: false,
  missing_price_any: false,
  has_errors: false,
  can_import_all: true,
};

beforeEach(() => vi.clearAllMocks());

describe("import error download", () => {
  it("downloads the authenticated Blob with the response filename and releases its URL", async () => {
    vi.useFakeTimers();
    const blob = new Blob(["csv"], { type: "text/csv" });
    get.mockResolvedValueOnce({
      data: blob,
      headers: { "content-disposition": "attachment; filename=import-batch-7-issues.csv" },
    });
    const createObjectURL = vi.fn(() => "blob:errors");
    const revokeObjectURL = vi.fn();
    Object.defineProperties(URL, {
      createObjectURL: { configurable: true, value: createObjectURL },
      revokeObjectURL: { configurable: true, value: revokeObjectURL },
    });
    let clickedAnchor: HTMLAnchorElement | null = null;
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clickedAnchor = this;
      expect(document.body.contains(this)).toBe(true);
    });

    await downloadImportErrors(7);

    expect(get).toHaveBeenCalledWith("/import/batches/7/errors.csv", { responseType: "blob" });
    expect(createObjectURL).toHaveBeenCalledWith(blob);
    expect(click).toHaveBeenCalledOnce();
    expect(clickedAnchor).toMatchObject({
      href: "blob:errors",
      download: "import-batch-7-issues.csv",
    });
    expect(document.body.contains(clickedAnchor)).toBe(false);
    expect(revokeObjectURL).not.toHaveBeenCalled();
    await vi.runAllTimersAsync();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:errors");
    vi.useRealTimers();
  });
});

describe("import precheck adapter", () => {
  it("accepts a complete v2 response and centralizes both multipart requests", async () => {
    expect(normalizeImportPrecheck(cleanV2)).toMatchObject({
      contract: "v2",
      decision: "clean",
      blocked: false,
      files: [{
        filename: "采购.xlsx",
        file_type: "purchase",
        severity: "info",
        can_import: true,
        sheets: [{ action: "selected", header_row: 2, data_rows: 8 }],
      }],
    });

    post.mockResolvedValueOnce({ data: cleanV2 });
    const file = new File(["xlsx"], "采购.xlsx");
    const controller = new AbortController();
    await expect(precheckImportFiles([file], controller.signal)).resolves.toMatchObject({
      contract: "v2",
      decision: "clean",
    });
    const precheckForm = post.mock.calls[0][1] as FormData;
    expect(post).toHaveBeenNthCalledWith(1, "/import/precheck", precheckForm, {
      signal: controller.signal,
      timeout: 30_000,
    });
    expect(precheckForm.getAll("files")).toEqual([file]);

    post.mockResolvedValueOnce({ data: { job_id: 7, total_files: 1 } });
    await expect(uploadImportBatch([file], "upsert")).resolves.toEqual({ job_id: 7, total_files: 1 });
    const uploadForm = post.mock.calls[1][1] as FormData;
    expect(post).toHaveBeenNthCalledWith(2, "/import/upload-batch", uploadForm, {
      params: { mode: "upsert" },
    });
    expect(uploadForm.getAll("files")).toEqual([file]);
  });

  it.each([
    ["legacy", { files: [{ filename: "旧版.xlsx", file_type: "purchase", warning: "缺价格" }] }, "legacy"],
    ["invalid", { ...cleanV2, files: [{ ...cleanV2.files[0], severity: "fatal" }] }, "invalid"],
    ["unknown action", {
      ...cleanV2,
      files: [{ ...cleanV2.files[0], sheets: [{ ...cleanV2.files[0].sheets[0], action: "skipped" }] }],
    }, "invalid"],
    ["missing v2 field", {
      ...cleanV2,
      files: [{ ...cleanV2.files[0], sheets: [{
        ...cleanV2.files[0].sheets[0], data_rows: undefined,
      }] }],
    }, "invalid"],
  ])("fails closed for %s responses", (_label, wire, contract) => {
    expect(normalizeImportPrecheck(wire)).toMatchObject({
      contract,
      decision: "unknown",
      blocked: true,
      files: [{
        filename: expect.any(String),
        file_type: "purchase",
        severity: "unknown",
        can_import: null,
      }],
    });
  });

  it.each([
    ["top-level has_errors", { has_errors: true }],
    ["top-level can_import_all", { can_import_all: false }],
    ["file can_import", { file: { can_import: false } }],
    ["file severity", { file: { severity: "error" } }],
    ["file issue", { file: { issues: [{ severity: "error", code: "broken", message: "损坏" }] } }],
    ["sheet issue", { sheet: { issues: [{ severity: "error", code: "duplicate", message: "重复" }] } }],
  ])("fails closed when blocking signal %s contradicts other fields", (_label, patch) => {
    const filePatch = "file" in patch ? patch.file : {};
    const sheetPatch = "sheet" in patch ? patch.sheet : {};
    const wire = {
      ...cleanV2,
      ...patch,
      files: [{
        ...cleanV2.files[0],
        ...filePatch,
        sheets: [{ ...cleanV2.files[0].sheets[0], ...sheetPatch }],
      }],
    };
    delete (wire as Record<string, unknown>).file;
    delete (wire as Record<string, unknown>).sheet;

    expect(normalizeImportPrecheck(wire)).toMatchObject({
      contract: "invalid",
      decision: "unknown",
      blocked: true,
    });
  });

  it("accepts a consistently blocked v2 response", () => {
    expect(normalizeImportPrecheck({
      ...cleanV2,
      any_warning: true,
      has_errors: true,
      can_import_all: false,
      files: [{
        ...cleanV2.files[0],
        severity: "error",
        can_import: false,
        ok: false,
        warning: "损坏",
        issues: [{ severity: "error", code: "broken", message: "损坏" }],
      }],
    })).toMatchObject({ contract: "v2", decision: "blocked", blocked: true });
  });

  it.each([
    ["ignored recognized sheet", {
      file: { severity: "warning", ok: false, warning: "已识别但跳过" },
      top: { any_warning: true },
      sheet: { action: "ignored_recognized", issues: [{
        severity: "warning", code: "sheet_ignored_recognized", message: "已识别但跳过",
      }] },
    }, "warning"],
    ["ignored unrecognized sheet", {
      sheet: { detected_type: null, action: "ignored_unrecognized", header_row: null, issues: [] },
    }, "clean"],
  ])("classifies %s by its required severity", (_label, patch, decision) => {
    const wire = {
      ...cleanV2,
      ...("top" in patch ? patch.top : {}),
      files: [{
        ...cleanV2.files[0],
        ...("file" in patch ? patch.file : {}),
        sheets: [
          cleanV2.files[0].sheets[0],
          { ...cleanV2.files[0].sheets[0], sheet_name: "其他", ...patch.sheet },
        ],
      }],
    };
    expect(normalizeImportPrecheck(wire)).toMatchObject({ contract: "v2", decision });
  });

  it("fails closed when a successful-looking v2 response has no file results", () => {
    expect(normalizeImportPrecheck({ ...cleanV2, files: [] })).toMatchObject({
      contract: "invalid",
      decision: "unknown",
      blocked: true,
    });
  });

  it.each([
    ["different order", ["销售", "采购"], [
      { ...cleanV2.files[0].sheets[0], sheet_name: "采购" },
      { ...cleanV2.files[0].sheets[0], sheet_name: "销售" },
    ]],
    ["duplicate selection", ["采购", "采购"], [cleanV2.files[0].sheets[0]]],
    ["missing selected sheet", ["不存在"], [cleanV2.files[0].sheets[0]]],
    ["omitted selected sheet", [], [cleanV2.files[0].sheets[0]]],
  ])("fails closed when selected_sheets has %s", (_label, selected_sheets, sheets) => {
    expect(normalizeImportPrecheck({
      ...cleanV2,
      files: [{ ...cleanV2.files[0], selected_sheets, sheets }],
    })).toMatchObject({ contract: "invalid", decision: "unknown", blocked: true });
  });

  it("fails closed when duplicate sheet names repeat consistently on both sides", () => {
    const duplicate = { ...cleanV2.files[0].sheets[0] };
    expect(normalizeImportPrecheck({
      ...cleanV2,
      files: [{
        ...cleanV2.files[0],
        selected_sheets: ["采购", "采购"],
        sheets: [cleanV2.files[0].sheets[0], duplicate],
      }],
    })).toMatchObject({ contract: "invalid", decision: "unknown", blocked: true });
  });

  it("fails closed when ignored_recognized has no warning issue", () => {
    expect(normalizeImportPrecheck({
      ...cleanV2,
      files: [{
        ...cleanV2.files[0],
        sheets: [
          cleanV2.files[0].sheets[0],
          { ...cleanV2.files[0].sheets[0], sheet_name: "销售", action: "ignored_recognized", issues: [] },
        ],
      }],
    })).toMatchObject({ contract: "invalid", decision: "unknown", blocked: true });
  });

  it.each([
    ["selected without detected_type", {
      ...cleanV2,
      files: [{ ...cleanV2.files[0], sheets: [{ ...cleanV2.files[0].sheets[0], detected_type: null }] }],
    }],
    ["selected without header_row", {
      ...cleanV2,
      files: [{ ...cleanV2.files[0], sheets: [{ ...cleanV2.files[0].sheets[0], header_row: null }] }],
    }],
    ["ignored_recognized without detected metadata", {
      ...cleanV2,
      any_warning: true,
      files: [{
        ...cleanV2.files[0], ok: false, severity: "warning", warning: "已识别但跳过",
        sheets: [cleanV2.files[0].sheets[0], {
          ...cleanV2.files[0].sheets[0], sheet_name: "销售", detected_type: "", header_row: null,
          action: "ignored_recognized", issues: [{
            severity: "warning", code: "sheet_ignored_recognized", message: "已识别但跳过",
          }],
        }],
      }],
    }],
    ["ignored_unrecognized with recognized metadata", {
      ...cleanV2,
      files: [{ ...cleanV2.files[0], sheets: [cleanV2.files[0].sheets[0], {
        ...cleanV2.files[0].sheets[0], sheet_name: "说明", action: "ignored_unrecognized",
      }] }],
    }],
    ["selected file without file_type", {
      ...cleanV2,
      files: [{ ...cleanV2.files[0], file_type: null }],
    }],
    ["ordinary file type differs from selected sheet", {
      ...cleanV2,
      files: [{ ...cleanV2.files[0], file_type: "sales" }],
    }],
    ["ordinary file selects more than one sheet", {
      ...cleanV2,
      files: [{
        ...cleanV2.files[0], selected_sheets: ["采购", "采购副本"],
        sheets: [cleanV2.files[0].sheets[0], {
          ...cleanV2.files[0].sheets[0], sheet_name: "采购副本",
        }],
      }],
    }],
    ["workbook selects a non-expense sheet", {
      ...cleanV2,
      files: [{ ...cleanV2.files[0], file_type: "workbook" }],
    }],
    ["expense file selects a non-expense sheet", {
      ...cleanV2,
      files: [{ ...cleanV2.files[0], file_type: "expense" }],
    }],
    ["expense file contains multiple recognized sheets", {
      ...cleanV2,
      files: [{
        ...cleanV2.files[0], file_type: "expense", selected_sheets: ["报销一", "报销二"],
        sheets: ["报销一", "报销二"].map((sheet_name) => ({
          ...cleanV2.files[0].sheets[0], sheet_name, detected_type: "expense",
        })),
      }],
    }],
    ["workbook contains only one recognized sheet", {
      ...cleanV2,
      files: [{
        ...cleanV2.files[0], file_type: "workbook", selected_sheets: ["报销"],
        sheets: [{ ...cleanV2.files[0].sheets[0], sheet_name: "报销", detected_type: "expense" }],
      }],
    }],
    ["expense file has an ignored recognized sheet", {
      ...cleanV2,
      any_warning: true,
      files: [{
        ...cleanV2.files[0], file_type: "expense", selected_sheets: ["报销"],
        ok: false, severity: "warning", warning: "已识别但跳过",
        sheets: [
          { ...cleanV2.files[0].sheets[0], sheet_name: "报销", detected_type: "expense" },
          {
            ...cleanV2.files[0].sheets[0], sheet_name: "采购", action: "ignored_recognized",
            issues: [{
              severity: "warning", code: "sheet_ignored_recognized", message: "已识别但跳过",
            }],
          },
        ],
      }],
    }],
    ["workbook ignores a recognized expense sheet", {
      ...cleanV2,
      any_warning: true,
      files: [{
        ...cleanV2.files[0], file_type: "workbook", selected_sheets: ["报销一"],
        ok: false, severity: "warning", warning: "已识别但跳过",
        sheets: [
          { ...cleanV2.files[0].sheets[0], sheet_name: "报销一", detected_type: "expense" },
          {
            ...cleanV2.files[0].sheets[0], sheet_name: "报销二", detected_type: "expense",
            action: "ignored_recognized", issues: [{
              severity: "warning", code: "sheet_ignored_recognized", message: "已识别但跳过",
            }],
          },
        ],
      }],
    }],
    ["ordinary file selects a later recognized sheet", {
      ...cleanV2,
      any_warning: true,
      files: [{
        ...cleanV2.files[0], selected_sheets: ["采购"],
        ok: false, severity: "warning", warning: "已识别但跳过",
        sheets: [
          {
            ...cleanV2.files[0].sheets[0], sheet_name: "销售", detected_type: "sales",
            action: "ignored_recognized", issues: [{
              severity: "warning", code: "sheet_ignored_recognized", message: "已识别但跳过",
            }],
          },
          cleanV2.files[0].sheets[0],
        ],
      }],
    }],
    ["ordinary file ignores a recognized expense sheet", {
      ...cleanV2,
      any_warning: true,
      files: [{
        ...cleanV2.files[0],
        ok: false, severity: "warning", warning: "已识别但跳过",
        sheets: [
          cleanV2.files[0].sheets[0],
          {
            ...cleanV2.files[0].sheets[0], sheet_name: "报销", detected_type: "expense",
            action: "ignored_recognized", issues: [{
              severity: "warning", code: "sheet_ignored_recognized", message: "已识别但跳过",
            }],
          },
        ],
      }],
    }],
  ])("fails closed for impossible backend sheet selection: %s", (_label, wire) => {
    expect(normalizeImportPrecheck(wire)).toMatchObject({
      contract: "invalid", decision: "unknown", blocked: true,
    });
  });

  it.each([
    ["failed_file_result", {
      files: [{
        filename: "损坏.xlsx", file_type: null, ok: false, missing_price: false,
        warning: "文件损坏", can_import: false, severity: "error", selected_sheets: [], sheets: [],
        issues: [{ severity: "error", code: "invalid_workbook", message: "文件损坏" }],
      }],
      any_warning: true, missing_price_any: false, has_errors: true, can_import_all: false,
    }],
    ["all sheets unrecognized", {
      files: [{
        filename: "未知.xlsx", file_type: null, ok: false, missing_price: false,
        warning: "无法识别任何可导入工作表", can_import: false, severity: "error",
        selected_sheets: [],
        issues: [{
          severity: "error", code: "no_recognized_sheet", message: "无法识别任何可导入工作表",
        }],
        sheets: [{
          sheet_name: "说明", detected_type: null, action: "ignored_unrecognized",
          header_row: null, data_rows: 5, duplicate_headers: [], issues: [{
            severity: "info", code: "sheet_ignored_unrecognized", message: "无法识别，本次不会导入",
          }],
        }],
      }],
      any_warning: true, missing_price_any: false, has_errors: true, can_import_all: false,
    }],
    ["workbook with multiple expense sheets", {
      ...cleanV2,
      files: [{
        ...cleanV2.files[0], file_type: "workbook", selected_sheets: ["报销一", "报销二"],
        sheets: ["报销一", "报销二"].map((sheet_name, index) => ({
          ...cleanV2.files[0].sheets[0], sheet_name, detected_type: "expense", header_row: index + 1,
        })),
      }],
    }],
  ])("accepts real backend structure: %s", (_label, wire) => {
    expect(normalizeImportPrecheck(wire)).toMatchObject({ contract: "v2" });
  });

  it("fails closed when duplicate_headers has no matching error issue", () => {
    expect(normalizeImportPrecheck({
      ...cleanV2,
      files: [{
        ...cleanV2.files[0],
        sheets: [{ ...cleanV2.files[0].sheets[0], duplicate_headers: ["产品名称"] }],
      }],
    })).toMatchObject({ contract: "invalid", decision: "unknown", blocked: true });
  });

  it.each([
    ["can_import=true without a selected sheet", {
      file: { selected_sheets: [], sheets: [{
        ...cleanV2.files[0].sheets[0], action: "ignored_unrecognized",
      }] },
    }],
    ["can_import_all disagrees with files", { can_import_all: false }],
    ["has_errors disagrees with files", { has_errors: true }],
    ["error severity remains importable", { file: { severity: "error" } }],
    ["non-error severity is marked non-importable", { file: { can_import: false } }],
  ])("fails closed when %s", (_label, patch) => {
    const filePatch = "file" in patch ? patch.file : {};
    const wire = {
      ...cleanV2,
      ...patch,
      files: [{ ...cleanV2.files[0], ...filePatch }],
    };
    delete (wire as Record<string, unknown>).file;
    expect(normalizeImportPrecheck(wire)).toMatchObject({
      contract: "invalid", decision: "unknown", blocked: true,
    });
  });

  it.each([
    ["negative data_rows", { data_rows: -1 }],
    ["fractional data_rows", { data_rows: 1.5 }],
    ["infinite data_rows", { data_rows: Number.POSITIVE_INFINITY }],
    ["zero header_row", { header_row: 0 }],
    ["negative header_row", { header_row: -1 }],
    ["fractional header_row", { header_row: 1.5 }],
    ["infinite header_row", { header_row: Number.POSITIVE_INFINITY }],
  ])("fails closed for %s", (_label, sheetPatch) => {
    expect(normalizeImportPrecheck({
      ...cleanV2,
      files: [{
        ...cleanV2.files[0],
        sheets: [{ ...cleanV2.files[0].sheets[0], ...sheetPatch }],
      }],
    })).toMatchObject({ contract: "invalid", decision: "unknown", blocked: true });
  });

  it("requires precheck results to correspond one-to-one with submitted files", async () => {
    const first = new File(["a"], "采购.xlsx");
    const second = new File(["b"], "销售.xlsx");
    post.mockResolvedValueOnce({ data: cleanV2 });
    await expect(precheckImportFiles([first, second])).resolves.toMatchObject({
      contract: "invalid", decision: "unknown", blocked: true,
    });

    post.mockResolvedValueOnce({ data: {
      ...cleanV2,
      files: [{ ...cleanV2.files[0], filename: "别的文件.xlsx" }],
    } });
    await expect(precheckImportFiles([first])).resolves.toMatchObject({
      contract: "invalid", decision: "unknown", blocked: true,
    });
  });

  it("treats contradictory legacy warning indicators as requiring confirmation", () => {
    expect(normalizeImportPrecheck({
      ...cleanV2,
      any_warning: true,
      missing_price_any: true,
      files: [{
        ...cleanV2.files[0], ok: false, missing_price: true, warning: "缺价格",
      }],
    })).toMatchObject({ contract: "invalid", decision: "unknown", blocked: true });
  });

  it.each([
    ["file severity understates issues", {
      file: { issues: [{ severity: "warning", code: "warn", message: "警告" }] },
    }],
    ["ok disagrees with warning", { file: { ok: false } }],
    ["warning disagrees with issues", { file: { warning: "不存在的警告", ok: false }, any_warning: true }],
    ["missing_price disagrees with issues", { file: { missing_price: true }, missing_price_any: true }],
    ["any_warning disagrees with files", { any_warning: true }],
    ["missing_price_any disagrees with files", { missing_price_any: true }],
  ])("fails closed when %s", (_label, patch) => {
    const filePatch = "file" in patch ? patch.file : {};
    const wire = {
      ...cleanV2,
      ...patch,
      files: [{ ...cleanV2.files[0], ...filePatch }],
    };
    delete (wire as Record<string, unknown>).file;
    expect(normalizeImportPrecheck(wire)).toMatchObject({
      contract: "invalid", decision: "unknown", blocked: true,
    });
  });

  it.each([
    {},
    { job_id: 0, total_files: 1 },
    { job_id: Number.POSITIVE_INFINITY, total_files: 1 },
    { job_id: 7, total_files: "1" },
    { job_id: 7, total_files: -1 },
    { job_id: 7, total_files: 2 },
  ])("rejects malformed successful upload responses without returning a fake job", async (data) => {
    post.mockResolvedValueOnce({ data });
    await expect(uploadImportBatch([new File(["x"], "采购.xlsx")], "skip"))
      .rejects.toThrow("正式提交响应无效");
  });
});
