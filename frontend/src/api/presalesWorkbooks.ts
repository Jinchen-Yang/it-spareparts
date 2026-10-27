import { api } from "../api";
import { getWorkbookSchema } from "./workbooks";
import type { WorkbookPreviewResult, WorkbookSchema, WorkbookVisibleColumns } from "./workbooks";

const root = "/presales/workbooks";

export const presalesWorkbooks = {
  getInquirySchema(signal?: AbortSignal): Promise<WorkbookSchema> {
    return getWorkbookSchema("presales_inquiry_lines", signal);
  },

  getQuoteSheetSchema(signal?: AbortSignal): Promise<WorkbookSchema> {
    return getWorkbookSchema("presales_quote_sheet", signal);
  },

  async getSalesSummarySchema(signal?: AbortSignal): Promise<WorkbookSchema> {
    const schema = await getWorkbookSchema("presales_sales_summary", signal);
    return { ...schema, sheets: schema.sheets.map((sheet) => ({
      ...sheet, columns: sheet.columns.map((column) => column.key === "pn"
        ? { ...column, mandatory: true } : column),
    })) };
  },

  async exportInquiry(id: number, visibleColumns?: WorkbookVisibleColumns, signal?: AbortSignal): Promise<Blob> {
    const { data } = await api.get<Blob>(`${root}/inquiry/${id}`, {
      params: { visible_columns: visibleColumns === undefined ? undefined : JSON.stringify(visibleColumns) },
      responseType: "blob", signal,
    });
    return data;
  },

  async exportInquiryTemplate(visibleColumns?: WorkbookVisibleColumns, signal?: AbortSignal): Promise<Blob> {
    const { data } = await api.get<Blob>(`${root}/inquiry-template`, {
      params: { visible_columns: visibleColumns === undefined ? undefined : JSON.stringify(visibleColumns) },
      responseType: "blob", signal,
    });
    return data;
  },

  async exportQuoteSheet(inquiryIds: number[], visibleColumns?: WorkbookVisibleColumns, signal?: AbortSignal): Promise<Blob> {
    const { data } = await api.post<Blob>(`${root}/quote-sheet`, {
      inquiry_ids: inquiryIds, visible_columns: visibleColumns,
    }, { responseType: "blob", signal });
    return data;
  },

  async exportSalesSummary(inquiryIds: number[], visibleColumns?: WorkbookVisibleColumns, signal?: AbortSignal): Promise<Blob> {
    const { data } = await api.post<Blob>(`${root}/sales-summary`, {
      inquiry_ids: inquiryIds, visible_columns: visibleColumns,
    }, { responseType: "blob", signal });
    return data;
  },

  async previewInquiry(file: File, inquiryId?: number, signal?: AbortSignal): Promise<WorkbookPreviewResult> {
    const body = new FormData();
    body.append("file", file);
    const { data } = await api.post<WorkbookPreviewResult>("/workbooks/presales_inquiry_lines/preview", body, {
      params: inquiryId === undefined ? undefined : { subject_type: "presales_inquiry", subject_id: inquiryId },
      signal,
    });
    return data;
  },

  async previewQuoteSheet(file: File, signal?: AbortSignal): Promise<WorkbookPreviewResult> {
    const body = new FormData();
    body.append("file", file);
    const { data } = await api.post<WorkbookPreviewResult>("/workbooks/presales_quote_sheet/preview", body, { signal });
    return data;
  },
};
