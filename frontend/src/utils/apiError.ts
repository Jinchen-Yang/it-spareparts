export type ApiErrorKind = "version_conflict" | "duplicate" | "permission" | "validation" | "network" | "server" | "conflict" | "unknown";

export interface ApiErrorInfo {
  code: string;
  kind: ApiErrorKind;
  message: string;
  suggestion: string;
  status?: number;
  method?: string;
  path?: string;
  requestId?: string;
}

const record = (value: unknown): Record<string, unknown> => value && typeof value === "object" ? value as Record<string, unknown> : {};
const identifier = (value: unknown, limit = 100) => typeof value === "string" && value.length <= limit && /^[a-zA-Z0-9_.:-]+$/.test(value) ? value : undefined;

function headerValue(headers: unknown, name: string) {
  const values = record(headers);
  if (typeof values.get === "function") return values.get.call(headers, name);
  return Object.entries(values).find(([key]) => key.toLowerCase() === name)?.[1];
}

/** 只保留路径。凭据、主机、查询串、请求体和任意响应字段均不进入诊断。 */
function requestPath(config: Record<string, unknown>): string | undefined {
  if (typeof config.url !== "string") return undefined;
  try {
    const base = typeof config.baseURL === "string" ? config.baseURL.replace(/\/$/, "") : "";
    const url = /^(?:https?:)?\/\//i.test(config.url) ? config.url : `${base}/${config.url.replace(/^\//, "")}`;
    return new URL(url, "https://diagnostic.invalid").pathname;
  } catch { return undefined; }
}

const fieldLabels: Record<string, string> = {
  pn: "PN", pn_std: "PN", pn_raw: "PN", part_id: "PN 档案", description: "描述", summary: "简介",
  brand: "品牌", manufacturer: "制造商", category_major: "一级类别", category_minor: "二级类别",
  specs: "参数", spec_updates: "参数", key: "参数名", value: "参数值", unit: "单位", numeric_value: "参数数值", qty: "数量", lines: "明细", unit_price: "单价", machine_or_part: "整机或备件",
  stock_qty: "库存数量", qty_available: "可供数量", stock_location: "货物所在地", lead_time: "交期",
  supplier_id: "供应商", source_key: "货源标识", is_tax_inclusive: "含税口径", expected_version: "记录版本", reason: "原因",
  notes: "备注", field_values: "扩展字段", name: "名称", assignee_id: "处理人", customer_name: "客户名称",
};

function validationMessage(detail: unknown[]): string {
  const fields = detail.slice(0, 4).map((raw) => {
    const item = record(raw);
    const location = Array.isArray(item.loc) ? item.loc.filter((part) => part !== "body" && part !== "query" && part !== "path") : [];
    const name = location.map((part) => typeof part === "number" ? `第 ${part + 1} 项` : fieldLabels[String(part)] ?? "字段").join(" / ") || "字段";
    const type = typeof item.type === "string" ? item.type : "";
    const reason = type.includes("missing") ? "必填" : /greater_than|less_than|too_long|too_short/.test(type) ? "超出允许范围"
      : /int_|float_|decimal_/.test(type) ? "请填写有效数字" : "格式不正确";
    // Pydantic 的 msg、input 和 ctx 可能带原始业务输入，只使用字段标签与类型。
    return `${name}：${reason}`;
  });
  return `请检查填写内容${fields.length ? `（${fields.join("；")}${detail.length > fields.length ? "；另有字段待检查" : ""}）` : ""}`;
}

export function parseApiError(error: unknown, fallback = "操作未完成"): ApiErrorInfo {
  const source = record(error);
  const response = record(source.response);
  const data = record(response.data);
  const detail = record(data.detail);
  const config = record(source.config ?? response.config);
  const status = typeof response.status === "number" && response.status >= 100 && response.status <= 599 ? response.status : undefined;
  const serverCode = identifier(detail.code ?? data.code);
  const code = serverCode?.toLowerCase();
  const rawMessage = typeof data.detail === "string" ? data.detail : typeof detail.message === "string" ? detail.message : undefined;
  // 仅展示已预期的 4xx 业务原因；5xx 内部异常不能回显到页面或复制内容中。
  const businessMessage = status && status >= 400 && status < 500 && rawMessage ? rawMessage.replace(/[\u0000-\u001f]/g, " ").slice(0, 300) : undefined;
  const method = typeof config.method === "string" && /^(get|post|put|patch|delete|head|options)$/i.test(config.method) ? config.method.toUpperCase() : undefined;
  const info: ApiErrorInfo = {
    code: serverCode ?? (status ? `http_${status}` : "unknown_error"), kind: "unknown",
    message: businessMessage ?? fallback, suggestion: "请保留当前填写内容，重试前确认操作结果；持续失败时复制诊断信息联系管理员。",
    status, method, path: requestPath(config),
    requestId: identifier(headerValue(response.headers, "x-request-id") ?? detail.request_id ?? data.request_id, 128),
  };
  if (!status && (source.request || ["ERR_NETWORK", "ECONNABORTED", "ETIMEDOUT"].includes(String(source.code)))) {
    return { ...info, code: source.code === "ECONNABORTED" || source.code === "ETIMEDOUT" ? "request_timeout" : "network_error",
      kind: "network", message: "未收到服务器响应", suggestion: "请检查网络后刷新确认操作结果，避免重复提交；当前填写内容请继续保留。" };
  }
  if (status && status >= 500) return { ...info, kind: "server", message: "服务暂时不可用",
    suggestion: "请稍后刷新确认操作结果；若仍失败，复制诊断信息联系管理员，避免重复提交。" };
  if (status === 401 || status === 403) return { ...info, code: serverCode ?? (status === 401 ? "authentication_required" : "permission_denied"), kind: "permission",
    message: businessMessage ?? (status === 401 ? "登录已失效" : "当前账号没有此操作权限"),
    suggestion: status === 401 ? "请重新登录后确认操作结果。" : "请确认当前账号及操作权限，需要时联系管理员。" };
  const legacyVersion = !serverCode && status === 409 && (!!detail.version || /版本冲突|记录已更新|已被.*领取|待办已更新/.test(rawMessage ?? "") || !!detail.claimed_by_name);
  if (code === "version_conflict" || code === "work_item_version_conflict" || legacyVersion) return { ...info,
    code: serverCode ?? "version_conflict", kind: "version_conflict", message: typeof detail.claimed_by_name === "string" && detail.claimed_by_name
      ? `已被 ${detail.claimed_by_name.slice(0, 80)} 领取` : businessMessage ?? "记录版本已变化",
    suggestion: "请核对最新记录与当前填写内容后再提交，系统不会自动重试写入。" };
  if (["duplicate_pn", "near_duplicates"].includes(code ?? "")) return { ...info, kind: "duplicate", message: businessMessage ?? "已有相同或相近的 PN 档案",
    suggestion: "请核对候选档案，优先选择已有 PN；确认是不同型号后再调整填写内容。" };
  if (status === 422 || status === 400 || code === "validation_error") return { ...info, code: serverCode ?? "validation_error", kind: "validation",
    message: Array.isArray(data.detail) ? validationMessage(data.detail) : businessMessage ?? "填写内容未通过校验", suggestion: "请检查并修正对应字段后重试，已填写内容会保留。" };
  if (status === 409) return { ...info, code: serverCode ?? "conflict", kind: "conflict", message: businessMessage ?? "当前状态不允许此操作",
    suggestion: code === "existing_offer" || code === "existing_draft" ? "请查看已有报价或草稿，再选择后续操作。" : "请核对当前记录及操作条件后重试，已填写内容会保留。" };
  if (status === 404) return { ...info, message: businessMessage ?? "记录不存在或当前不可见", suggestion: "请返回列表核对记录；需要时联系管理员确认访问权限。" };
  return info;
}

/** 复制内容严格使用白名单元数据，不含 message、请求/响应正文及请求头。 */
export function apiErrorDiagnostic(info: ApiErrorInfo): string {
  return [`错误码: ${info.code}`, `HTTP 状态: ${info.status ?? "未收到响应"}`, `请求方法: ${info.method ?? "未知"}`,
    `请求路径: ${info.path ?? "未知"}`, `请求标识: ${info.requestId ?? "服务端未返回"}`].join("\n");
}
