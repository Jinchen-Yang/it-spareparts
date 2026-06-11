import { useState } from "react";
import { Layout, Menu, Button, Tooltip, Avatar } from "antd";
import {
  ImportOutlined, SearchOutlined, RobotOutlined, BarChartOutlined,
  InboxOutlined, SafetyOutlined, LogoutOutlined, UserOutlined,
} from "@ant-design/icons";
import LoginPage from "./pages/LoginPage";
import PartSearchPage from "./pages/PartSearchPage";
import ProfitPage from "./pages/ProfitPage";
import InventoryPage from "./pages/InventoryPage";
import ImportPage from "./pages/ImportPage";
import GovernancePage from "./pages/GovernancePage";
import ChatPage from "./pages/ChatPage";

const { Header, Content } = Layout;

const NAV_ITEMS = [
  { key: "import",     label: "数据导入", icon: <ImportOutlined /> },
  { key: "parts",      label: "型号查询", icon: <SearchOutlined /> },
  { key: "chat",       label: "AI 助手",  icon: <RobotOutlined /> },
  { key: "profit",     label: "利润分析", icon: <BarChartOutlined /> },
  { key: "inventory",  label: "库存查询", icon: <InboxOutlined /> },
  { key: "governance", label: "数据治理", icon: <SafetyOutlined /> },
];

const ROLE_LABEL: Record<string, string> = {
  admin: "管理员", boss: "老板", sales: "销售", purchaser: "采购", readonly: "只读",
};
const ROLE_COLOR: Record<string, string> = {
  admin: "#f43f5e", boss: "#8b5cf6", sales: "#0ea5e9", purchaser: "#10b981", readonly: "#94a3b8",
};

export default function App() {
  const [token, setToken] = useState<string | null>(localStorage.getItem("token"));
  const [page, setPage] = useState("chat");

  if (!token) return <LoginPage onLogin={(t) => setToken(t)} />;

  const logout = () => {
    localStorage.removeItem("token");
    localStorage.removeItem("role");
    localStorage.removeItem("name");
    setToken(null);
  };

  const role = localStorage.getItem("role") || "";
  const name = localStorage.getItem("name") || "用户";
  const roleColor = ROLE_COLOR[role] || "#6b7280";

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header
        className="app-header"
        style={{
          display: "flex",
          alignItems: "center",
          padding: "0 24px",
          position: "sticky",
          top: 0,
          zIndex: 100,
          background: "linear-gradient(135deg, #0f0f23 0%, #1a1035 100%)",
          boxShadow: "0 1px 0 rgba(255,255,255,0.06), 0 4px 24px rgba(0,0,0,0.3)",
          height: 60,
        }}
      >
        {/* Logo */}
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginRight: 36, flexShrink: 0 }}>
          <div style={{
            width: 32, height: 32, borderRadius: 9,
            background: "linear-gradient(135deg, #4f46e5, #9333ea)",
            display: "flex", alignItems: "center", justifyContent: "center",
            boxShadow: "0 0 14px rgba(99,102,241,0.45)",
          }}>
            <RobotOutlined style={{ color: "#fff", fontSize: 16 }} />
          </div>
          <span style={{
            fontWeight: 700, fontSize: 15,
            background: "linear-gradient(90deg, #e0e7ff, #c7d2fe)",
            WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent",
          }}>
            IT 备件智能管理
          </span>
        </div>

        {/* Nav */}
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[page]}
          onClick={(e) => setPage(e.key)}
          items={NAV_ITEMS.map((it) => ({
            key: it.key,
            icon: it.icon,
            label: it.label,
            style: { fontSize: 13.5 },
          }))}
          style={{
            flex: 1,
            minWidth: 0,
            background: "transparent",
            borderBottom: "none",
            lineHeight: "60px",
          }}
        />

        {/* 右侧用户区 */}
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
          <Avatar
            size={28}
            style={{ background: roleColor, fontSize: 12, flexShrink: 0, cursor: "default" }}
            icon={<UserOutlined />}
          />
          <div style={{ lineHeight: 1.25 }}>
            <div style={{ color: "#e2e8f0", fontSize: 13, fontWeight: 500 }}>{name}</div>
            {role && (
              <div style={{ fontSize: 11, color: roleColor, fontWeight: 600 }}>
                {ROLE_LABEL[role] || role}
              </div>
            )}
          </div>
          <Tooltip title="退出登录">
            <Button
              type="text"
              icon={<LogoutOutlined />}
              onClick={logout}
              style={{ color: "rgba(255,255,255,0.45)", marginLeft: 2 }}
            />
          </Tooltip>
        </div>
      </Header>

      <Content
        style={{
          padding: page === "chat" ? 0 : 24,
          background: "#f4f5f9",
          flex: 1,
          minHeight: "calc(100vh - 60px)",
        }}
      >
        {page === "import"     && <ImportPage />}
        {page === "parts"      && <PartSearchPage />}
        {page === "chat"       && <ChatPage />}
        {page === "profit"     && <ProfitPage />}
        {page === "inventory"  && <InventoryPage />}
        {page === "governance" && <GovernancePage />}
      </Content>
    </Layout>
  );
}
