import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider, message } from "antd";
import zhCN from "antd/locale/zh_CN";
import App from "./App";
import { themeConfig } from "./theme";
import "antd/dist/reset.css";
import "./index.css";

// 全局提示下移到顶栏(60px)下方，避免短暂遮挡导航/退出
message.config({ top: 72 });

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={themeConfig}>
      <App />
    </ConfigProvider>
  </React.StrictMode>
);
