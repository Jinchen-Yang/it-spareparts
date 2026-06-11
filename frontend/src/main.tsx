import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import App from "./App";
import "antd/dist/reset.css";
import "./global.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: "#4f46e5",
          colorPrimaryHover: "#6366f1",
          colorPrimaryActive: "#3730a3",
          borderRadius: 10,
          borderRadiusLG: 14,
          borderRadiusSM: 6,
          fontFamily:
            '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
          fontSize: 14,
          colorBgContainer: "#ffffff",
          colorBorder: "#e8eaed",
          colorBorderSecondary: "#f0f0f0",
          boxShadow: "0 1px 3px 0 rgba(0,0,0,0.08), 0 1px 2px -1px rgba(0,0,0,0.06)",
          boxShadowSecondary: "0 4px 16px -4px rgba(0,0,0,0.10), 0 2px 8px -2px rgba(0,0,0,0.06)",
          colorTextSecondary: "#6b7280",
          colorTextTertiary: "#9ca3af",
          lineHeight: 1.6,
        },
        components: {
          Layout: { headerBg: "#0f0f23" },
          Menu: {
            darkItemBg: "transparent",
            darkItemSelectedBg: "rgba(99,102,241,0.18)",
            darkItemHoverBg: "rgba(255,255,255,0.06)",
            darkItemSelectedColor: "#a5b4fc",
            darkItemColor: "rgba(255,255,255,0.72)",
          },
          Card: {
            borderRadiusLG: 14,
            paddingLG: 20,
          },
          Button: {
            borderRadius: 8,
            controlHeight: 36,
            controlHeightLG: 42,
          },
          Input: {
            borderRadius: 8,
            controlHeight: 36,
          },
          Table: {
            borderRadius: 10,
            headerBg: "#f8f9fc",
            headerColor: "#374151",
            rowHoverBg: "#f5f7ff",
          },
          Tag: {
            borderRadius: 6,
          },
          Modal: {
            borderRadiusLG: 16,
          },
        },
      }}
    >
      <App />
    </ConfigProvider>
  </React.StrictMode>
);
