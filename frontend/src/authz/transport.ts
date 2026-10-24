import { AxiosHeaders, type AxiosRequestConfig } from "axios";

/** Explicit captured identity for asynchronous requests; never re-read localStorage. */
export interface CapturedTransport { token: string; signal: AbortSignal }
export function transportConfig(context?: CapturedTransport, headers?: AxiosRequestConfig["headers"]) {
  if (!context) return headers ? { headers } : {};
  // Axios normalizes header names case-insensitively. Clone before replacing identity,
  // so a caller's business headers (including idempotency and multipart settings)
  // survive without lending a stale Authorization.
  const capturedHeaders = new AxiosHeaders();
  for (const [name, value] of Object.entries(headers ?? {})) {
    if (value !== undefined) capturedHeaders.set(name, value);
  }
  capturedHeaders.delete("Authorization");
  capturedHeaders.set("Authorization", `Bearer ${context.token}`);
  return { headers: Object.fromEntries(Object.entries(capturedHeaders)), signal: context.signal };
}
