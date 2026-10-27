import { beforeEach, describe, expect, it, vi } from "vitest";
import { presalesWorkbooks } from "../presalesWorkbooks";

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("../../api", () => ({ api: http }));

const workbook = new Blob(["xlsx"], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });

beforeEach(() => {
  Object.values(http).forEach((fn) => fn.mockReset().mockResolvedValue({ data: workbook }));
});

describe("售前工作簿 API", () => {
  it.each([
    ["getInquirySchema", "presales_inquiry_lines"],
    ["getQuoteSheetSchema", "presales_quote_sheet"],
  ] as const)("%s 复用通用 schema 接口", async (method, name) => {
    const controller = new AbortController();
    const schema = { name, version: 1, sheets: [], instructions: [] };
    http.get.mockResolvedValue({ data: schema });
    expect(await presalesWorkbooks[method](controller.signal)).toBe(schema);
    expect(http.get).toHaveBeenCalledWith(`/workbooks/${name}/schema`, { signal: controller.signal });
  });

  it("销售汇总的列选择器固定 PN 身份列并保持 reference 模式", async () => {
    const controller = new AbortController();
    const schema = { name: "presales_sales_summary", version: 1, instructions: [], sheets: [{
      key: "summary", title: "销售询价汇总", allow_insert: false, allow_delete: false,
      custom_columns: false, columns: [
        { key: "pn", type: "code", edit: "reference", mandatory: false },
        { key: "qty", type: "decimal", edit: "reference", mandatory: false },
      ],
    }] };
    http.get.mockResolvedValue({ data: schema });
    const result = await presalesWorkbooks.getSalesSummarySchema(controller.signal);
    expect(http.get).toHaveBeenCalledWith("/workbooks/presales_sales_summary/schema", { signal: controller.signal });
    expect(result.sheets[0].columns[0]).toMatchObject({ key: "pn", edit: "reference", mandatory: true });
    expect(result.sheets[0].columns[1].mandatory).toBe(false);
    expect(schema.sheets[0].columns[0].mandatory).toBe(false);
  });

  it("下载现有询价并将选择列编码为 JSON 查询参数", async () => {
    const controller = new AbortController();
    const columns = { lines: ["pn", "qty", "+特殊要求"] };
    expect(await presalesWorkbooks.exportInquiry(7, columns, controller.signal)).toBe(workbook);
    expect(http.get).toHaveBeenCalledWith("/presales/workbooks/inquiry/7", {
      params: { visible_columns: JSON.stringify(columns) }, responseType: "blob", signal: controller.signal,
    });
  });

  it("下载空模板时可省略列选择", async () => {
    expect(await presalesWorkbooks.exportInquiryTemplate()).toBe(workbook);
    expect(http.get).toHaveBeenCalledWith("/presales/workbooks/inquiry-template", {
      params: { visible_columns: undefined }, responseType: "blob", signal: undefined,
    });
  });

  it("空模板传递空列选择与取消信号", async () => {
    const controller = new AbortController();
    await presalesWorkbooks.exportInquiryTemplate({ lines: [] }, controller.signal);
    expect(http.get).toHaveBeenCalledWith("/presales/workbooks/inquiry-template", {
      params: { visible_columns: '{"lines":[]}' }, responseType: "blob", signal: controller.signal,
    });
  });

  it("合并采购报价表通过请求体传递来源单和选择列", async () => {
    const controller = new AbortController();
    const columns = { lines: ["pn", "unit_price", "mode"] };
    expect(await presalesWorkbooks.exportQuoteSheet([7, 8], columns, controller.signal)).toBe(workbook);
    expect(http.post).toHaveBeenCalledWith("/presales/workbooks/quote-sheet", {
      inquiry_ids: [7, 8], visible_columns: columns,
    }, { responseType: "blob", signal: controller.signal });
  });

  it("销售汇总保留 summary 工作表的选择列与下载响应", async () => {
    const controller = new AbortController();
    const columns = { summary: ["pn", "qty", "inquiry_nos"] };
    expect(await presalesWorkbooks.exportSalesSummary([7, 8], columns, controller.signal)).toBe(workbook);
    expect(http.post).toHaveBeenCalledWith("/presales/workbooks/sales-summary", {
      inquiry_ids: [7, 8], visible_columns: columns,
    }, { responseType: "blob", signal: controller.signal });
  });

  it("导出被拒时保留原始错误供调用方展示", async () => {
    const error = new Error("无权导出该询价单");
    http.post.mockRejectedValue(error);
    await expect(presalesWorkbooks.exportQuoteSheet([7])).rejects.toBe(error);
  });

  it("现有询价预览绑定单据，空模板预览不带单据", async () => {
    const file = new File(["xlsx"], "inquiry.xlsx");
    const preview = { operation_id: 5, plan_digest: "digest", plan: { rows: [] } };
    http.post.mockResolvedValue({ data: preview });
    const controller = new AbortController();
    expect(await presalesWorkbooks.previewInquiry(file, 7, controller.signal)).toBe(preview);
    expect(http.post).toHaveBeenLastCalledWith("/workbooks/presales_inquiry_lines/preview", expect.any(FormData), {
      params: { subject_type: "presales_inquiry", subject_id: 7 }, signal: controller.signal,
    });
    expect((http.post.mock.calls[http.post.mock.calls.length - 1][1] as FormData).get("file")).toBe(file);
    await presalesWorkbooks.previewInquiry(file);
    expect(http.post).toHaveBeenLastCalledWith("/workbooks/presales_inquiry_lines/preview", expect.any(FormData), {
      params: undefined, signal: undefined,
    });
  });

  it("报价预览交给通用两步接口", async () => {
    const file = new File(["xlsx"], "quote.xlsx");
    const controller = new AbortController();
    await presalesWorkbooks.previewQuoteSheet(file, controller.signal);
    expect(http.post).toHaveBeenCalledWith("/workbooks/presales_quote_sheet/preview", expect.any(FormData), {
      signal: controller.signal,
    });
    expect((http.post.mock.calls[0][1] as FormData).get("file")).toBe(file);
  });
});
