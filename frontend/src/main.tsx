import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider, message } from "antd";
import zhCN from "antd/locale/zh_CN";
import App from "./App";
import ErrorBoundary from "./components/ErrorBoundary";
import { themeConfig } from "./theme";
import "antd/dist/reset.css";
import "./index.css";

// 全局提示下移到顶栏(60px)下方，避免短暂遮挡导航/退出
message.config({ top: 72 });

// 发版后旧标签页动态加载已被删除的旧 chunk 会触发 vite:preloadError：
// 自动整页刷新一次拿新 index.html；30 秒窗口内不重复刷新，防止真离线时无限循环
// （二次失败会正常抛错，由 ErrorBoundary 显示"刷新页面"）。
window.addEventListener("vite:preloadError", (e) => {
  const KEY = "chunk_reload_at";
  const last = Number(sessionStorage.getItem(KEY) || 0);
  if (Date.now() - last > 30_000) {
    e.preventDefault();
    sessionStorage.setItem(KEY, String(Date.now()));
    window.location.reload();
  }
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={themeConfig}>
      <ErrorBoundary>
        <App />
      </ErrorBoundary>
    </ConfigProvider>
  </React.StrictMode>
);
