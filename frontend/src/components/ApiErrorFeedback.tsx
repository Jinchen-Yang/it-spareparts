import { useCallback, useEffect, useMemo, useState } from "react";
import { App, Button, Space, Typography } from "antd";
import { apiErrorDiagnostic, parseApiError, type ApiErrorInfo } from "../utils/apiError";

export function ApiErrorFeedback({ info, refreshError, afterSuccess }: {
  info: ApiErrorInfo; refreshError?: ApiErrorInfo; afterSuccess?: string;
}) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const diagnostic = apiErrorDiagnostic(info) + (refreshError ? `\n\n后续刷新失败\n${apiErrorDiagnostic(refreshError)}` : "");
  useEffect(() => { setCopyState("idle"); }, [diagnostic]);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(diagnostic);
      setCopyState("copied");
    } catch { setCopyState("failed"); }
  };
  return <Space direction="vertical" size={8} style={{ width: "100%", textAlign: "left" }}>
    {afterSuccess && <Typography.Text strong>{afterSuccess}已成功，但页面刷新失败，请勿重复提交。</Typography.Text>}
    <Typography.Text>{info.message}</Typography.Text>
    <Typography.Text type="secondary">{afterSuccess ? "请手动刷新查看最新结果；当前填写内容会保留。" : info.suggestion}</Typography.Text>
    {refreshError && <Typography.Text type="warning">自动刷新也未成功，请稍后手动重试；原操作错误信息已保留。</Typography.Text>}
    <details style={{ width: "100%" }}><summary style={{ cursor: "pointer" }}>查看诊断信息</summary>
      <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: 12, userSelect: "text" }}>{diagnostic}</pre>
      <Button size="small" onClick={() => void copy()}>{copyState === "copied" ? "已复制诊断信息" : "复制诊断信息"}</Button>
      {copyState === "failed" && <div role="status">浏览器不允许自动复制，请选中上方诊断信息手动复制。</div>}
    </details>
  </Space>;
}

type Refresh = () => void | Promise<unknown>;
export type ApiErrorHandler = ((error: unknown, refresh?: Refresh) => Promise<void>) & {
  afterSuccess: (error: unknown, action?: string) => void;
};

/** 无论刷新是否失败都消化异常；原始错误不会被第二次请求覆盖。 */
export function useApiError(): ApiErrorHandler {
  const { notification } = App.useApp();
  const show = useCallback((info: ApiErrorInfo, extra?: { refreshError?: ApiErrorInfo; afterSuccess?: string }) => {
    notification.open({
      key: `api-error:${info.code}:${info.method}:${info.path}`,
      type: extra?.afterSuccess || info.kind === "version_conflict" || info.kind === "conflict" || info.kind === "duplicate" ? "warning" : "error",
      message: extra?.afterSuccess ? "操作成功，页面待刷新" : "操作未完成",
      description: <ApiErrorFeedback info={info} {...extra} />, duration: 0,
    });
  }, [notification]);
  return useMemo(() => Object.assign(async (error: unknown, refresh?: Refresh) => {
    const info = parseApiError(error);
    show(info);
    if (info.kind !== "version_conflict" || !refresh) return;
    try { await refresh(); }
    catch (cause) { show(info, { refreshError: parseApiError(cause, "刷新失败") }); }
  }, { afterSuccess: (error: unknown, action = "操作") => show(parseApiError(error, "刷新失败"), { afterSuccess: action }) }), [show]);
}
