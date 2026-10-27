import PageSkeleton from "./components/PageSkeleton";
import { Suspense, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  Alert, Breadcrumb, Button, Drawer, Dropdown, Grid, Layout, Menu, Modal,
  Tag,
} from "antd";
import { ArrowLeftOutlined, MenuFoldOutlined, MenuOutlined, MenuUnfoldOutlined, UserOutlined } from "@ant-design/icons";
import type { MenuProps } from "antd";
import { COLORS, SHELL_LAYOUT } from "./theme";
import { APP_VERSION, CHANGELOG, LATEST } from "./version";
import { NAV_GROUPS, matchDetailRoute, matchNavItem } from "./nav";
import type { NavItem } from "./nav";
import ChangePasswordModal from "./components/ChangePasswordModal";
import { TaxPolicyBoundary } from "./context/TaxBasis";

const { Header, Content, Sider } = Layout;

const SIDER_WIDTH = 224;
const SIDER_COLLAPSED = 72;

export type ShellHeaderMode = "mobile" | "tablet" | "desktop";

/** Ant Design md starts at exactly 768px; lg starts at 992px.  The old desktop
 * action row overflowed the 544px content area at 768px (after the sider).
 * Tablet mode keeps the desktop sider but uses the compact user-menu header. */
export function shellHeaderMode(screens: { md?: boolean; lg?: boolean }): ShellHeaderMode {
  if (screens.md === false) return "mobile";
  if (screens.lg === false) return "tablet";
  return "desktop";
}

function Brand({ collapsed }: { collapsed?: boolean }) {
  return (
    <div
      style={{
        height: `var(--mb-shell-header-height, ${SHELL_LAYOUT.headerHeight}px)`, flexShrink: 0, display: "flex", alignItems: "center",
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
  const headerMode = shellHeaderMode(screens);
  const isMobile = headerMode === "mobile";
  const compactHeader = headerMode !== "desktop";

  const [collapsed, setCollapsed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [changelogOpen, setChangelogOpen] = useState(false);
  const [changePwOpen, setChangePwOpen] = useState(false);
  const [showWhatsNew, setShowWhatsNew] = useState(
    () => localStorage.getItem("seen_version") !== APP_VERSION,
  );
  const [openKeys, setOpenKeys] = useState<string[]>(() => {
    try {
      const stored = localStorage.getItem("sidebar_open_keys");
      return stored ? JSON.parse(stored) : [];
    } catch { return []; }
  });
  const dismissWhatsNew = () => {
    localStorage.setItem("seen_version", APP_VERSION);
    setShowWhatsNew(false);
  };

  const role = localStorage.getItem("role") || "";
  const name = localStorage.getItem("name") || "";
  const roleMap: Record<string, string> = {
    admin: "管理员", boss: "老板", sales: "销售", purchaser: "采购", readonly: "只读",
  };

  // 详情路由（如 /pool-analysis/:id）挂在母页之下：菜单仍高亮母页、面包屑/标题追加详情段
  const detail = matchDetailRoute(location.pathname);
  const active = matchNavItem(location.pathname)
    ?? (detail ? allowed.find((it) => it.key === detail.menuKey) : undefined);

  // 页面标题跟随路由：浏览器标签/收藏夹可辨识
  useEffect(() => {
    const label = detail?.label ?? active?.label;
    document.title = label ? `${label} · 备件智能管理系统` : "备件智能管理系统";
  }, [active, detail]);

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

  // 当前路由代码与金额口径请求并行下载；业务内容仍由 TaxPolicyBoundary 把关。
  // 不在 idle 时预取整站，避免图表/导入等无关代码与首屏争抢带宽和主线程。
  const allowedActive = allowed.find((item) => item.key === active?.key);
  const currentLoad = allowedActive
    ? (detail?.load ?? allowedActive.load)
    : undefined;
  useEffect(() => {
    void currentLoad?.().catch(() => {});
  }, [currentLoad]);

  // 分组菜单：group 自动变为可折叠 SubMenu；空组整组隐藏
  const allowedByKey = useMemo(() => new Map(allowed.map((it) => [it.key, it])), [allowed]);
  const menuItems: MenuProps["items"] = useMemo(() => {
    const out: NonNullable<MenuProps["items"]> = [];
    for (const g of NAV_GROUPS) {
      const items = g.items
        .map((it) => allowedByKey.get(it.key))
        .filter((it): it is NavItem => it !== undefined)
        .map((it) => ({
          key: it.key, icon: it.icon, label: it.menuLabel ?? it.label,
          // 只预取鼠标/键盘正在指向的目标，失败交给实际导航的错误边界处理。
          onMouseEnter: () => { void it.load().catch(() => {}); },
          onFocus: () => { void it.load().catch(() => {}); },
        }));
      if (!items.length) continue;
      if (g.label === null) out.push(...items);
      else out.push({
        key: g.key,
        label: g.label,
        icon: null,
        children: items,
      });
    }
    return out;
  }, [allowedByKey]);

  // 当前路由所属组自动展开；手动展开/折叠写入 localStorage
  const activeGroupKeys = useMemo(() => {
    if (!active) return [];
    const group = NAV_GROUPS.find((g) => g.items.some((it) => it.key === active.key));
    return group && group.label !== null ? [group.key] : [];
  }, [active]);

  const resolvedOpenKeys = useMemo(() => {
    const merged = new Set(openKeys);
    for (const k of activeGroupKeys) merged.add(k);
    return Array.from(merged);
  }, [openKeys, activeGroupKeys]);

  const onOpenChange = (keys: string[]) => {
    setOpenKeys(keys);
    try { localStorage.setItem("sidebar_open_keys", JSON.stringify(keys)); } catch {}
  };

  const onMenuClick: MenuProps["onClick"] = (e) => {
    const target = allowed.find((it) => it.key === e.key);
    if (target) navigate(target.path);
    setDrawerOpen(false);
  };

  const menu = (
    <Menu
      mode="inline"
      selectedKeys={active ? [active.key] : []}
      openKeys={resolvedOpenKeys}
      onOpenChange={onOpenChange}
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
        ...(detail ? [{ title: detail.label }] : []),
      ]
    : detail ? [{ title: detail.label }] : [];

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
    <Layout style={{
      minHeight: "100vh",
      "--mb-shell-header-height": `${SHELL_LAYOUT.headerHeight}px`,
      "--mb-shell-content-padding": `${isMobile ? SHELL_LAYOUT.mobileContentPadding : SHELL_LAYOUT.contentPadding}px`,
    } as CSSProperties}>
      {!isMobile && (
        <Sider
          theme="light"
          width={SIDER_WIDTH}
          collapsedWidth={SIDER_COLLAPSED}
          collapsible
          collapsed={collapsed}
          trigger={null}
          style={{
            position: "sticky", top: 0, height: "100vh",
            borderRight: `1px solid ${COLORS.border}`, background: COLORS.surface,
          }}
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
          data-shell-header-mode={headerMode}
          style={{
            display: "flex", alignItems: "center", gap: 8,
            paddingInline: "var(--mb-shell-content-padding)",
            height: "var(--mb-shell-header-height)",
            lineHeight: "var(--mb-shell-header-height)",
            minWidth: 0,
          }}
        >
          {compactHeader ? (
            <>
              {isMobile && (
                <Button
                  type="text"
                  icon={<MenuOutlined />}
                  aria-label="打开菜单"
                  onClick={() => setDrawerOpen(true)}
                />
              )}
              {isMobile && detail && active && (
                <Button type="text" icon={<ArrowLeftOutlined />} aria-label="返回"
                  onClick={() => {
                    const index = (window.history.state as { idx?: number } | null)?.idx;
                    if (typeof index === "number" && index > 0) navigate(-1);
                    else navigate(active.path);
                  }}>
                  返回
                </Button>
              )}
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
              <Button
                type="text"
                onClick={() => setChangelogOpen(true)}
                title="查看更新日志"
                style={{ color: COLORS.text2, fontSize: 12.5, marginRight: 16, paddingInline: 0, whiteSpace: "nowrap" }}
              >
                v{APP_VERSION}
              </Button>
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

        <Content style={{ padding: "var(--mb-shell-content-padding)", background: COLORS.page }}>
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
          <TaxPolicyBoundary>
            <Suspense
              key={location.pathname}
              fallback={<PageSkeleton />}
            >
              <Outlet />
            </Suspense>
          </TaxPolicyBoundary>
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
