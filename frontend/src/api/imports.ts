import api from "../api";

export type ImportSeverity = "info" | "warning" | "error";
export type ImportSheetAction = "selected" | "ignored_recognized" | "ignored_unrecognized";
export type ImportMode = "skip" | "upsert";

export interface ImportPrecheckIssue {
  severity: ImportSeverity;
  code: string;
  message: string;
}

export interface ImportPrecheckSheet {
  sheet_name: string;
  detected_type: string | null;
  action: ImportSheetAction;
  header_row: number | null;
  data_rows: number;
  duplicate_headers: string[];
  issues: ImportPrecheckIssue[];
}

export interface ImportPrecheckFile {
  filename: string;
  file_type: string | null;
  warning: string | null;
  severity: ImportSeverity | "unknown";
  can_import: boolean | null;
  issues: ImportPrecheckIssue[];
  sheets: ImportPrecheckSheet[];
}

export interface ImportPrecheckResult {
  contract: "v2" | "legacy" | "invalid";
  decision: "clean" | "warning" | "blocked" | "unknown";
  blocked: boolean;
  files: ImportPrecheckFile[];
}

const SEVERITIES = new Set<ImportSeverity>(["info", "warning", "error"]);
const ACTIONS = new Set<ImportSheetAction>([
  "selected", "ignored_recognized", "ignored_unrecognized",
]);
const SEVERITY_RANK: Record<ImportSeverity, number> = { info: 0, warning: 1, error: 2 };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNullableString(value: unknown): value is string | null {
  return typeof value === "string" || value === null;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isSeverity(value: unknown): value is ImportSeverity {
  return typeof value === "string" && SEVERITIES.has(value as ImportSeverity);
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}

function normalizeIssue(value: unknown): ImportPrecheckIssue | null {
  if (!isRecord(value) || !isSeverity(value.severity)
    || typeof value.code !== "string" || typeof value.message !== "string") return null;
  return { severity: value.severity, code: value.code, message: value.message };
}

function normalizeIssues(value: unknown): ImportPrecheckIssue[] | null {
  if (!Array.isArray(value)) return null;
  const issues = value.map(normalizeIssue);
  return issues.every((issue): issue is ImportPrecheckIssue => issue !== null) ? issues : null;
}

function normalizeSheet(value: unknown): ImportPrecheckSheet | null {
  if (!isRecord(value) || typeof value.sheet_name !== "string"
    || !isNullableString(value.detected_type)
    || typeof value.action !== "string" || !ACTIONS.has(value.action as ImportSheetAction)
    || !(isPositiveInteger(value.header_row) || value.header_row === null)
    || !isNonNegativeInteger(value.data_rows)
    || !Array.isArray(value.duplicate_headers)
    || !value.duplicate_headers.every((header) => typeof header === "string")) return null;
  const issues = normalizeIssues(value.issues);
  if (!issues) return null;
  const recognized = value.action === "selected" || value.action === "ignored_recognized";
  if (recognized
    ? !isNonEmptyString(value.detected_type) || !isPositiveInteger(value.header_row)
    : value.detected_type !== null || value.header_row !== null) return null;
  return {
    sheet_name: value.sheet_name,
    detected_type: value.detected_type,
    action: value.action as ImportSheetAction,
    header_row: value.header_row,
    data_rows: value.data_rows,
    duplicate_headers: value.duplicate_headers,
    issues,
  };
}

function normalizeV2File(value: unknown): ImportPrecheckFile | null {
  if (!isRecord(value) || typeof value.filename !== "string"
    || !isNullableString(value.file_type) || typeof value.ok !== "boolean"
    || typeof value.missing_price !== "boolean" || !isNullableString(value.warning)
    || typeof value.can_import !== "boolean" || !isSeverity(value.severity)
    || !Array.isArray(value.selected_sheets)
    || !value.selected_sheets.every((sheet) => typeof sheet === "string")
    || !Array.isArray(value.sheets)) return null;
  const issues = normalizeIssues(value.issues);
  const sheets = value.sheets.map(normalizeSheet);
  if (!issues || !sheets.every((sheet): sheet is ImportPrecheckSheet => sheet !== null)) return null;
  const selectedSheets = sheets
    .filter((sheet) => sheet.action === "selected")
    .map((sheet) => sheet.sheet_name);
  const selected = sheets.filter((sheet) => sheet.action === "selected");
  const recognized = sheets.filter((sheet) => sheet.action !== "ignored_unrecognized");
  if (new Set(sheets.map((sheet) => sheet.sheet_name)).size !== sheets.length
    || value.selected_sheets.length !== selectedSheets.length
    || value.selected_sheets.some((sheet, index) => sheet !== selectedSheets[index])
    || (value.can_import && selectedSheets.length === 0)
    || ((selected.length > 0 || value.can_import) && !isNonEmptyString(value.file_type))) return null;
  const expenseSheets = recognized.filter((sheet) => sheet.detected_type === "expense");
  if (expenseSheets.length > 0) {
    const expectedFileType = recognized.length > 1 ? "workbook" : "expense";
    if (value.file_type !== expectedFileType
      || recognized.some((sheet) => sheet.action !== (
        sheet.detected_type === "expense" ? "selected" : "ignored_recognized"
      ))) return null;
  } else if (recognized.length > 0) {
    if (value.file_type !== recognized[0].detected_type
      || recognized.some((sheet, index) => sheet.action !== (
        index === 0 ? "selected" : "ignored_recognized"
      ))) return null;
  } else if (value.file_type !== null) return null;
  if (sheets.some((sheet) => sheet.action === "ignored_recognized"
    && !sheet.issues.some((issue) => issue.severity === "warning"
      && issue.code === "sheet_ignored_recognized"))) return null;
  if (sheets.some((sheet) => sheet.duplicate_headers.length > 0
    && !sheet.issues.some((issue) => sheet.action === "selected"
      ? issue.severity === "error" && issue.code === "duplicate_headers"
      : issue.severity === "warning" && issue.code === "duplicate_headers_ignored"))) return null;
  const allIssues = [...issues, ...sheets.flatMap((sheet) => sheet.issues)];
  const severity = allIssues.reduce<ImportSeverity>(
    (highest, issue) => SEVERITY_RANK[issue.severity] > SEVERITY_RANK[highest]
      ? issue.severity : highest,
    "info",
  );
  const warning = severity === "info"
    ? null
    : allIssues.find((issue) => issue.severity === severity)?.message ?? null;
  const missingPrice = allIssues.some((issue) => issue.code === "missing_price_columns");
  if (value.severity !== severity
    || value.can_import !== (selectedSheets.length > 0 && severity !== "error")
    || value.warning !== warning
    || value.ok !== (warning === null)
    || value.missing_price !== missingPrice) return null;
  return {
    filename: value.filename,
    file_type: value.file_type,
    warning: value.warning,
    severity: value.severity,
    can_import: value.can_import,
    issues,
    sheets,
  };
}

function fallbackFiles(value: unknown): ImportPrecheckFile[] {
  if (!isRecord(value) || !Array.isArray(value.files)) return [];
  return value.files.map((file) => {
    const record = isRecord(file) ? file : {};
    return {
      filename: typeof record.filename === "string" ? record.filename : "未知文件",
      file_type: isNullableString(record.file_type) ? record.file_type : null,
      warning: isNullableString(record.warning) ? record.warning : null,
      severity: "unknown" as const,
      can_import: null,
      issues: [],
      sheets: [],
    };
  });
}

export function normalizeImportPrecheck(value: unknown): ImportPrecheckResult {
  const record = isRecord(value) ? value : null;
  const hasV2Markers = !!record && ["has_errors", "can_import_all"].some((key) => key in record);
  const wireFiles = record && Array.isArray(record.files) ? record.files : null;
  const topLevelValid = !!record && wireFiles !== null
    && typeof record.any_warning === "boolean"
    && typeof record.missing_price_any === "boolean"
    && typeof record.has_errors === "boolean"
    && typeof record.can_import_all === "boolean";
  const normalizedFiles = topLevelValid ? wireFiles.map(normalizeV2File) : [];
  if (!topLevelValid || normalizedFiles.length === 0
    || !normalizedFiles.every((file): file is ImportPrecheckFile => file !== null)) {
    return {
      contract: hasV2Markers ? "invalid" : "legacy",
      decision: "unknown",
      blocked: true,
      files: fallbackFiles(value),
    };
  }
  const files = normalizedFiles.filter((file): file is ImportPrecheckFile => file !== null);

  const hasNestedError = files.some((file) => file.severity === "error"
    || file.can_import === false
    || file.issues.some((issue) => issue.severity === "error")
    || file.sheets.some((sheet) => sheet.issues.some((issue) => issue.severity === "error")));
  const anyWarning = wireFiles.some((file) => isRecord(file) && file.ok === false);
  const missingPriceAny = wireFiles.some((file) => isRecord(file) && file.missing_price === true);
  if (record.has_errors !== hasNestedError
    || record.can_import_all !== files.every((file) => file.can_import === true)
    || record.any_warning !== anyWarning
    || record.missing_price_any !== missingPriceAny) {
    return {
      contract: "invalid",
      decision: "unknown",
      blocked: true,
      files: fallbackFiles(value),
    };
  }
  const blocked = record.has_errors === true || record.can_import_all === false || hasNestedError;
  const warning = files.some((file) => file.severity === "warning"
    || file.issues.some((issue) => issue.severity === "warning")
    || file.sheets.some((sheet) => sheet.action === "ignored_recognized"
      || sheet.issues.some((issue) => issue.severity === "warning")))
    || record.any_warning === true
    || record.missing_price_any === true
    || wireFiles.some((file) => isRecord(file)
      && (file.ok === false || file.missing_price === true || typeof file.warning === "string"));
  return {
    contract: "v2",
    decision: blocked ? "blocked" : warning ? "warning" : "clean",
    blocked,
    files,
  };
}

function filesFormData(files: readonly File[]) {
  const form = new FormData();
  files.forEach((file) => form.append("files", file));
  return form;
}

export async function precheckImportFiles(files: readonly File[], signal?: AbortSignal) {
  const { data } = await api.post("/import/precheck", filesFormData(files), {
    signal,
    timeout: 30_000,
  });
  const result = normalizeImportPrecheck(data);
  if (result.contract === "v2"
    && (result.files.length !== files.length
      || result.files.some((file, index) => file.filename !== files[index].name))) {
    return { ...result, contract: "invalid", decision: "unknown", blocked: true } as const;
  }
  return result;
}

export interface UploadImportBatchResult {
  job_id: number;
  total_files: number;
}

export async function uploadImportBatch(files: readonly File[], mode: ImportMode) {
  const { data } = await api.post<UploadImportBatchResult>(
    "/import/upload-batch",
    filesFormData(files),
    { params: { mode } },
  );
  if (!isRecord(data) || !isPositiveInteger(data.job_id)
    || !isNonNegativeInteger(data.total_files) || data.total_files !== files.length) {
    throw new Error("正式提交响应无效");
  }
  return data;
}

export async function downloadImportErrors(batchId: number) {
  const response = await api.get<Blob>(`/import/batches/${batchId}/errors.csv`, {
    responseType: "blob",
  });
  const disposition = response.headers["content-disposition"] || "";
  const match = /filename="?([^";]+)"?/.exec(disposition);
  const filename = match?.[1] || `import-batch-${batchId}-issues.csv`;
  const url = URL.createObjectURL(response.data);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    try {
      anchor.click();
    } finally {
      anchor.remove();
    }
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}
