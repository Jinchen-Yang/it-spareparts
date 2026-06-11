import { useState } from "react";
import { Layout, Menu, Button } from "antd";
import LoginPage from "./pages/LoginPage";
import PartSearchPage from "./pages/PartSearchPage";
import ProfitPage from "./pages/ProfitPage";
import InventoryPage from "./pages/InventoryPage";
import ImportPage from "./pages/ImportPage";
import GovernancePage from "./pages/GovernancePage";
import ChatPage from "./pages/ChatPage";

const { Header, Content } = Layout;

export default function App() {
  const [token, setToken] = useState<string | null>(localStorage.getItem("token"));
  const [page, setPage] = useState("parts");

  if (!token) return <LoginPage onLogin={(t) => setToken(t)} />;

  const logout = () => {
    localStorage.removeItem("token");
    localStorage.removeItem("role");
    localStorage.removeItem("name");
    setToken(null);
  };

  const roleMap: Record<string, string> = {
    admin: "管理员", boss: "老板", sales: "销售", purchaser: "采购", readonly: "只读",
  };
  const role = localStorage.getItem("role") || "";
  const name = localStorage.getItem("name") || "";

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header style={{ display: "flex", alignItems: "center" }}>
        <div style={{ color: "#fff", fontWeight: 600, marginRight: 32 }}>
          IT 备件智能管理系统
        </div>
        <Menu
          theme="dark"
          mode="horizontal"
          selectedKeys={[page]}
          onClick={(e) => setPage(e.key)}
          items={[
            { key: "import", label: "数据导入" },
            { key: "parts", label: "型号查询" },
            { key: "chat", label: "AI 助手" },
            { key: "profit", label: "利润分析" },
            { key: "inventory", label: "库存查询" },
            { key: "governance", label: "数据治理" },
          ]}
          style={{ flex: 1, minWidth: 0 }}
        />
        <span style={{ color: "#cbd5e1", marginRight: 12, fontSize: 13 }}>
          {name}{role ? `（${roleMap[role] || role}）` : ""}
        </span>
        <Button onClick={logout}>退出</Button>
      </Header>
      <Content style={{ padding: 24, background: "#f0f2f5" }}>
        {page === "import" && <ImportPage />}
        {page === "parts" && <PartSearchPage />}
        {page === "chat" && <ChatPage />}
        {page === "profit" && <ProfitPage />}
        {page === "inventory" && <InventoryPage />}
        {page === "governance" && <GovernancePage />}
      </Content>
    </Layout>
  );
}
