import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  Alert, Breadcrumb, Button, Drawer, Dropdown, Grid, Layout, Menu, Modal,
  Spin, Tag,
} from "antd";
import { MenuFoldOutlined, MenuOutlined, MenuUnfoldOutlined, UserOutlined } from "@ant-design/icons";
import type { MenuProps } from "antd";
import { COLORS } from "./theme";
import { APP_VERSION, CHANGELOG, LATEST } from "./version";
import { NAV_GROUPS, matchNavItem } from "./nav";
import type { NavItem } from "./nav";
import ChangePasswordModal from "./components/ChangePasswordModal";
import { TaxBasisToggle } from "./context/TaxBasis";

const { Header, Content, Sider } = Layout;

const SIDER_WIDTH = 224;
const SIDER_COLLAPSED = 72;

function Brand({ collapsed }: { collapsed?: boolean }) {
  return (
    <div
      style={{
        height: 60, display: "flex", alignItems: "center",
        justifyContent: collapsed ? "center" : "flex-start",
        gap: 8, padding: collapsed ? 0 : "0 20px",
        borderBottom: `1px solid ${COLORS.borderSoft}`,
        overflow: "hidden", whiteSpace: "nowrap",
      }}
    >
      <span
        style={{
          width: 22, height: 22, borderRadius: 7, flex: "none",
          background: COLORS.accent, display: "inline-flex",
          alignItems: "center", justifyContent: "center",
          color: "#F3F8FA", fontSize: 13, fontWeight: 500,
        }}
      >
        IT
      </span>
      {!collapsed && (
        <span style={{ color: COLORS.text, fontWeight: 600, fontSize: 15, letterSpacing: "-0.2px" }}>
          备件智能管理系统
        </span>
      )}
    </div>
  );
}

export default function AppShell({
  allowed,
  onLogout,
  onToken,
}: {
  allowed: NavItem[];
  onLogout: () => void;
  onToken: (token: string) => void;
}) {
  const location = useLocation();
  const navigate = useNavigate();
  const screens = Grid.useBreakpoint();
  // md 未计算完成(首帧 undefined)按桌面处理，避免闪抽屉
  const isMobile = screens.md === false;

  const [collapsed, setCollapsed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [changelogOpen, setChangelogOpen] = useState(false);
  const [changePwOpen, setChangePwOpen] = useState(false);
  const [showWhatsNew, setShowWhatsNew] = useState(
    () => localStorage.getItem("seen_version") !== APP_VERSION,
  );
  const dismissWhatsNew = () => {
    localStorage.setItem("seen_version", APP_VERSION);
    setShowWhatsNew(false);
  };

  const role = localStorage.getItem("role") || "";
  const name = localStorage.getItem("name") || "";
  const roleMap: Record<string, string> = {
    admin: "管理员", boss: "老板", sales: "销售", purchaser: "采购", readonly: "只读",
  };

  const active = matchNavItem(location.pathname);

  // 页面标题跟随路由：浏览器标签/收藏夹可辨识
  useEffect(() => {
    document.title = active ? `${active.label} · 备件智能管理系统` : "备件智能管理系统";
  }, [active]);

  // 视口跨过断点回桌面时收起抽屉，避免转回竖屏时抽屉无操作自动弹开
  useEffect(() => {
    if (!isMobile) setDrawerOpen(false);
  }, [isMobile]);

  // 切页滚动置顶（跳过首帧，保留浏览器后退的原生滚动恢复）
  const firstNav = useRef(true);
  useEffect(() => {
    if (firstNav.current) { firstNav.current = false; return; }
    window.scrollTo(0, 0);
  }, [location.pathname]);

  // 空闲预取全部可见页面 chunk：点菜单即秒开，弱网下也不再"点了没反应"
  // （react-router v7 的导航在 transition 里，chunk 未就绪时不会显示 fallback）
  useEffect(() => {
    const idle: (cb: () => void) => number =
      window.requestIdleCallback || ((cb) => window.setTimeout(cb, 2000));
    const id = idle(() => allowed.forEach((it) => { it.load().catch(() => {}); }));
    return () => (window.cancelIdleCallback || window.clearTimeout)(id);
  }, [allowed]);

  // 分组菜单：只渲染当前用户可见的项；空组整组隐藏
  const allowedKeys = useMemo(() => new Set(allowed.map((it) => it.key)), [allowed]);
  const menuItems: MenuProps["items"] = useMemo(() => {
    const out: NonNullable<MenuProps["items"]> = [];
    for (const g of NAV_GROUPS) {
      const items = g.items
        .filter((it) => allowedKeys.has(it.key))
        .map((it) => ({ key: it.key, icon: it.icon, label: it.label }));
      if (!items.length) continue;
      if (g.label === null) out.push(...items);
      else out.push({ type: "group" as const, key: g.key, label: g.label, children: items });
    }
    return out;
  }, [allowedKeys]);

  const onMenuClick: MenuProps["onClick"] = (e) => {
    const target = allowed.find((it) => it.key === e.key);
    if (target) navigate(target.path);
    setDrawerOpen(false);
  };

  const menu = (
    <Menu
      mode="inline"
      selectedKeys={active ? [active.key] : []}
      onClick={onMenuClick}
      items={menuItems}
      style={{ borderInlineEnd: "none", background: "transparent", padding: "8px 8px 16px" }}
    />
  );

  const group = NAV_GROUPS.find((g) => active && g.items.some((it) => it.key === active.key));
  const breadcrumbItems = active
    ? [
        ...(group?.label ? [{ title: group.label }] : []),
        { title: active.label },
      ]
    : [];

  // 移动端把次级入口收进用户菜单，顶栏只留 菜单/标题/用户 三件事
  const userMenuItems: MenuProps["items"] = [
    {
      key: "who",
      disabled: true,
      label: (
        <span>
          {name || "未命名"}
          {role && (
            <Tag style={{ marginInlineStart: 8 }} color={role === "admin" ? "blue" : "default"}>
              {roleMap[role] || role}
            </Tag>
          )}
        </span>
      ),
    },
    { type: "divider" },
    { key: "changePw", label: "修改密码" },
    { key: "manual", label: "使用说明" },
    { key: "changelog", label: `更新日志 · v${APP_VERSION}` },
    { type: "divider" },
    { key: "logout", label: "退出登录" },
  ];
  const onUserMenu: MenuProps["onClick"] = ({ key }) => {
    if (key === "changePw") setChangePwOpen(true);
    else if (key === "manual") window.open("/manual.html", "_blank", "noopener");
    else if (key === "changelog") setChangelogOpen(true);
    else if (key === "logout") onLogout();
  };

  return (
    <Layout style={{ minHeight: "100vh" }}>
      {!isMobile && (
        <Sider
          theme="light"
          width={SIDER_WIDTH}
          collapsedWidth={SIDER_COLLAPSED}
          collapsible
          collapsed={collapsed}
          trigger={null}
          style={{ borderRight: `1px solid ${COLORS.border}`, background: COLORS.surface }}
        >
          <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
            <Brand collapsed={collapsed} />
            <div style={{ flex: 1, overflowY: "auto", minHeight: 0 }}>{menu}</div>
            {/* 可访问的收起控件：真实 button（Tab 可聚焦、Enter/Space 生效、aria-* 齐全），
                取代 antd 默认的 <div> trigger（无语义、键盘不可达） */}
            <Button
              type="text"
              aria-label={collapsed ? "展开侧边栏" : "收起侧边栏"}
              aria-expanded={!collapsed}
              onClick={() => setCollapsed((c) => !c)}
              icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
              style={{
                height: 44, borderRadius: 0, flex: "none",
                borderTop: `1px solid ${COLORS.borderSoft}`,
                display: "flex", alignItems: "center",
                justifyContent: collapsed ? "center" : "flex-start",
                paddingInline: collapsed ? 0 : 20, color: COLORS.text2,
              }}
            >
              {!collapsed && "收起"}
            </Button>
          </div>
        </Sider>
      )}

      <Layout style={{ minWidth: 0 }}>
        <Header
          style={{
            display: "flex", alignItems: "center", gap: 8,
            paddingInline: isMobile ? 12 : 24,
          }}
        >
          {isMobile ? (
            <>
              <Button
                type="text"
                icon={<MenuOutlined />}
                aria-label="打开菜单"
                onClick={() => setDrawerOpen(true)}
              />
              <span
                style={{
                  flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis",
                  whiteSpace: "nowrap", fontWeight: 600, fontSize: 15, color: COLORS.text,
                }}
              >
                {active?.label || "备件智能管理系统"}
              </span>
              <Dropdown menu={{ items: userMenuItems, onClick: onUserMenu }} trigger={["click"]}>
                <Button type="text" icon={<UserOutlined />} aria-label="用户菜单" />
              </Dropdown>
            </>
          ) : (
            <>
              <Breadcrumb items={breadcrumbItems} />
              <span style={{ flex: 1 }} />
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6, marginRight: 16, whiteSpace: "nowrap" }}>
                <span style={{ color: COLORS.text2, fontSize: 12.5 }}>价格</span>
                <TaxBasisToggle />
              </span>
              <span
                onClick={() => setChangelogOpen(true)}
                title="查看更新日志"
                style={{ color: COLORS.text2, fontSize: 12.5, marginRight: 16, cursor: "pointer", whiteSpace: "nowrap" }}
              >
                v{APP_VERSION}
              </span>
              <a
                href="/manual.html" target="_blank" rel="noopener"
                style={{ color: COLORS.text2, fontSize: 13, marginRight: 16, whiteSpace: "nowrap" }}
              >
                使用说明
              </a>
              <span style={{ display: "inline-flex", alignItems: "center", gap: 8, marginRight: 14, whiteSpace: "nowrap" }}>
                <span style={{ color: COLORS.text, fontSize: 13, fontWeight: 500 }}>{name || "未命名"}</span>
                {role && (
                  <Tag style={{ marginInlineEnd: 0 }} color={role === "admin" ? "blue" : "default"}>
                    {roleMap[role] || role}
                  </Tag>
                )}
              </span>
              <Button onClick={() => setChangePwOpen(true)} style={{ marginRight: 8 }}>修改密码</Button>
              <Button onClick={onLogout}>退出</Button>
            </>
          )}
        </Header>

        <Content style={{ padding: isMobile ? 12 : 24, background: COLORS.page }}>
          {showWhatsNew && (
            <Alert
              type="success"
              showIcon
              closable
              onClose={dismissWhatsNew}
              style={{ marginBottom: 16 }}
              message={`本次更新 · v${APP_VERSION}（${LATEST.date}）`}
              description={
                <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                  {LATEST.items.map((it, i) => <li key={i}>{it}</li>)}
                </ul>
              }
              action={
                <Button size="small" type="link" onClick={() => setChangelogOpen(true)}>
                  完整日志
                </Button>
              }
            />
          )}
          <Suspense
            fallback={
              <div style={{ display: "flex", justifyContent: "center", paddingTop: 120 }}>
                <Spin size="large" />
              </div>
            }
          >
            <Outlet />
          </Suspense>
        </Content>
      </Layout>

      {isMobile && (
        <Drawer
          placement="left"
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          width={Math.min(280, window.innerWidth - 56)}
          styles={{ body: { padding: 0, display: "flex", flexDirection: "column" } }}
          title={null}
          closable={false}
        >
          <Brand />
          <div style={{ flex: 1, overflowY: "auto" }}>{menu}</div>
          <div
            style={{
              padding: "12px 20px", borderTop: `1px solid ${COLORS.borderSoft}`,
              display: "flex", alignItems: "center", gap: 8,
            }}
          >
            <span style={{ color: COLORS.text2, fontSize: 12.5 }}>价格</span>
            <TaxBasisToggle />
          </div>
        </Drawer>
      )}

      <ChangePasswordModal
        open={changePwOpen}
        onClose={() => setChangePwOpen(false)}
        onChanged={onToken}
      />
      <Modal
        open={changelogOpen}
        onCancel={() => setChangelogOpen(false)}
        footer={null}
        title="更新日志"
      >
        {CHANGELOG.map((e) => (
          <div key={e.version} style={{ marginBottom: 14 }}>
            <div style={{ fontWeight: 600 }}>
              v{e.version}
              <span style={{ color: COLORS.text2, fontWeight: 400, fontSize: 12.5, marginLeft: 8 }}>
                {e.date}
              </span>
            </div>
            <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
              {e.items.map((it, i) => <li key={i}>{it}</li>)}
            </ul>
          </div>
        ))}
      </Modal>
    </Layout>
  );
}
