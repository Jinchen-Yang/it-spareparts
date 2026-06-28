import { useState } from "react";
import { Layout, Menu, Button, Tag } from "antd";
import { COLORS } from "./theme";
import LoginPage from "./pages/LoginPage";
import PartSearchPage from "./pages/PartSearchPage";
import ProfitPage from "./pages/ProfitPage";
import InventoryPage from "./pages/InventoryPage";
import ImportPage from "./pages/ImportPage";
import GovernancePage from "./pages/GovernancePage";
import ChatPage from "./pages/ChatPage";
import PurchasesPage from "./pages/PurchasesPage";
import AccountsPage from "./pages/AccountsPage";

const { Header, Content } = Layout;

/** 安全读取本地权限快照：localStorage 被写坏时回退为空而非抛错致整页白屏（审计 U-1）。 */
function readPerms(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem("permissions") || "{}");
  } catch {
    return {};
  }
}

export default function App() {
  const [token, setToken] = useState<string | null>(localStorage.getItem("token"));
  const [page, setPage] = useState("parts");

  if (!token) return <LoginPage onLogin={(t) => setToken(t)} />;

  const logout = () => {
    localStorage.removeItem("token");
    localStorage.removeItem("role");
    localStorage.removeItem("name");
    localStorage.removeItem("permissions");
    setToken(null);
  };

  const roleMap: Record<string, string> = {
    admin: "管理员", boss: "老板", sales: "销售", purchaser: "采购", readonly: "只读",
  };
  const role = localStorage.getItem("role") || "";
  const name = localStorage.getItem("name") || "";
  const perms: Record<string, boolean> = readPerms();
  const isAdmin = role === "admin";
  const can = (p: string) => isAdmin || !!perms[p];
  const menu = [
    { key: "import", label: "数据导入", perm: "page_import" },
    { key: "parts", label: "型号查询", perm: "page_parts" },
    { key: "purchases", label: "采购记录", perm: "page_purchases" },
    { key: "chat", label: "AI 助手", perm: "page_chat" },
    { key: "profit", label: "利润分析", perm: "page_profit" },
    { key: "inventory", label: "库存查询", perm: "page_inventory" },
    { key: "governance", label: "数据治理", perm: "page_governance" },
  ].filter((m) => can(m.perm)).map(({ key, label }) => ({ key, label }));
  if (isAdmin) menu.push({ key: "accounts", label: "账号管理" });
  const activePage = menu.some((m) => m.key === page) ? page : (menu[0]?.key || "parts");

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Header style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span
          style={{
            width: 22, height: 22, borderRadius: 7, marginRight: 6,
            background: COLORS.accent, display: "inline-flex",
            alignItems: "center", justifyContent: "center",
            color: "#F3F8FA", fontSize: 13, fontWeight: 500,
          }}
        >
          IT
        </span>
        <div style={{ color: COLORS.text, fontWeight: 600, marginRight: 28, fontSize: 15,
                      letterSpacing: "-0.2px", whiteSpace: "nowrap" }}>
          备件智能管理系统
        </div>
        <Menu
          mode="horizontal"
          selectedKeys={[activePage]}
          onClick={(e) => setPage(e.key)}
          items={menu}
          style={{ flex: 1, minWidth: 0, background: "transparent", borderBottom: "none" }}
        />
        <a href="/manual.html" target="_blank" rel="noopener"
           style={{ color: COLORS.text2, fontSize: 13, marginRight: 16, whiteSpace: "nowrap" }}>
          使用说明
        </a>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8, marginRight: 14,
                       whiteSpace: "nowrap" }}>
          <span style={{ color: COLORS.text, fontSize: 13, fontWeight: 500 }}>{name || "未命名"}</span>
          {role && <Tag style={{ marginInlineEnd: 0 }} color={role === "admin" ? "blue" : "default"}>
            {roleMap[role] || role}
          </Tag>}
        </span>
        <Button onClick={logout}>退出</Button>
      </Header>
      <Content style={{ padding: 24, background: COLORS.page }}>
        {activePage === "import" && <ImportPage />}
        {activePage === "parts" && <PartSearchPage />}
        {activePage === "purchases" && <PurchasesPage />}
        {activePage === "chat" && <ChatPage />}
        {activePage === "profit" && <ProfitPage />}
        {activePage === "inventory" && <InventoryPage />}
        {activePage === "governance" && <GovernancePage />}
        {activePage === "accounts" && <AccountsPage />}
      </Content>
    </Layout>
  );
}
