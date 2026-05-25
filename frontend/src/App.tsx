import { useState } from "react";
import { Layout, Menu, Button } from "antd";
import LoginPage from "./pages/LoginPage";
import PartSearchPage from "./pages/PartSearchPage";
import ProfitPage from "./pages/ProfitPage";
import InventoryPage from "./pages/InventoryPage";
import ImportPage from "./pages/ImportPage";

const { Header, Content } = Layout;

export default function App() {
  const [token, setToken] = useState<string | null>(localStorage.getItem("token"));
  const [page, setPage] = useState("parts");

  if (!token) return <LoginPage onLogin={(t) => setToken(t)} />;

  const logout = () => {
    localStorage.removeItem("token");
    setToken(null);
  };

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
            { key: "profit", label: "利润分析" },
            { key: "inventory", label: "库存查询" },
          ]}
          style={{ flex: 1, minWidth: 0 }}
        />
        <Button onClick={logout}>退出</Button>
      </Header>
      <Content style={{ padding: 24, background: "#f0f2f5" }}>
        {page === "import" && <ImportPage />}
        {page === "parts" && <PartSearchPage />}
        {page === "profit" && <ProfitPage />}
        {page === "inventory" && <InventoryPage />}
      </Content>
    </Layout>
  );
}
