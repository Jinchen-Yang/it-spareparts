import type { AxiosInstance, AxiosRequestConfig, AxiosResponse } from "axios";
import type { AuthorizationContext } from "./contextTypes";

export interface ContextRequestTransport {
  readonly signal?: AbortSignal;
  request<T>(config: AxiosRequestConfig): Promise<AxiosResponse<T>>;
  isCurrent(): boolean;
}
export interface ContextualPayload { authorization_context?: AuthorizationContext }

/** Legacy callers retain their original transport until their resource is explicitly migrated. */
export function createDomainApi(api: AxiosInstance, capture?: ContextRequestTransport) {
  return {
    get: <T = unknown,>(url: string, config?: AxiosRequestConfig): Promise<AxiosResponse<T & ContextualPayload>> => capture
      ? capture.request<T & ContextualPayload>({ ...config, method: "GET", url }) : config === undefined ? api.get<T & ContextualPayload>(url) : api.get<T & ContextualPayload>(url, config),
    post: <T = unknown,>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<AxiosResponse<T & ContextualPayload>> => capture
      ? capture.request<T & ContextualPayload>({ ...config, method: "POST", url, data }) : config === undefined ? (data === undefined ? api.post<T & ContextualPayload>(url) : api.post<T & ContextualPayload>(url, data)) : api.post<T & ContextualPayload>(url, data, config),
    put: <T = unknown,>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<AxiosResponse<T & ContextualPayload>> => capture
      ? capture.request<T & ContextualPayload>({ ...config, method: "PUT", url, data }) : config === undefined ? api.put<T & ContextualPayload>(url, data) : api.put<T & ContextualPayload>(url, data, config),
    patch: <T = unknown,>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<AxiosResponse<T & ContextualPayload>> => capture
      ? capture.request<T & ContextualPayload>({ ...config, method: "PATCH", url, data }) : config === undefined ? api.patch<T & ContextualPayload>(url, data) : api.patch<T & ContextualPayload>(url, data, config),
    delete: <T = unknown,>(url: string, config?: AxiosRequestConfig): Promise<AxiosResponse<T & ContextualPayload>> => capture
      ? capture.request<T & ContextualPayload>({ ...config, method: "DELETE", url }) : config === undefined ? api.delete<T & ContextualPayload>(url) : api.delete<T & ContextualPayload>(url, config),
  };
}
