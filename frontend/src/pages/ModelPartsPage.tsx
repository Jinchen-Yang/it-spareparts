import { useMemo } from "react";
import type { AxiosRequestConfig } from "axios";
import EmbeddedPage, { type PoolHost } from "@spareparts/model-parts-workbench";
import { api } from "../api";
import { parseApiError } from "../utils/apiError";

function forSession(token: string | null): PoolHost {
  function checkSession() {
    if (!token || token !== localStorage.getItem("token")) throw new DOMException("登录账号已变化", "AbortError");
  }

  async function request<T>(path: string, options: AxiosRequestConfig = {}): Promise<T> {
    checkSession();
    try {
      const response = await api.request<T>({
        ...options, url: "/model-parts/" + path, headers: { Authorization: "Bearer " + token }, timeout: 15000,
      });
      checkSession();
      return response.data;
    } catch (error) {
      checkSession();
      if (options.signal?.aborted) throw new DOMException("请求已取消", "AbortError");
      const info = parseApiError(error, "暂时无法读取或保存机型备件，请重试。");
      throw Object.assign(new Error(info.message), { status: info.status });
    }
  }
  return {
    read: signal => request("catalog", { signal }),
    candidates: (input, signal) => request("candidates", { method: "POST", data: input, signal }),
    command: input => request("commands", { method: "POST", data: input }),
  };
}

export default function ModelPartsPage() {
  const token = localStorage.getItem("token");
  const host = useMemo(() => forSession(token), [token]);
  return <EmbeddedPage key={token} host={host} />;
}
