import { useEffect, useMemo, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Button, Result } from "antd";
import LoginPage from "./pages/LoginPage";
import AppShell from "./AppShell";
import { DETAIL_ROUTES, NAV_ITEMS, NAV_REDIRECTS, defaultPath } from "./nav";

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

  useEffect(() => {
    const syncCrossTabSession = (event: StorageEvent) => {
      if (event.storageArea === localStorage && event.key === "token") {
        setToken(event.newValue);
      }
    };
    window.addEventListener("storage", syncCrossTabSession);
    return () => window.removeEventListener("storage", syncCrossTabSession);
  }, []);

  // 权限快照随登录周期固定：只在 token 变化（登录/登出/改密）时重算，
  // 让 AppShell 收到的 allowed 引用稳定、下游 useMemo 可依赖
  const allowed = useMemo(() => {
    if (!token) return [];
    const role = localStorage.getItem("role") || "";
    const isAdmin = role === "admin";
    const perms = readPerms();
    // 权限规则与旧版一致：admin 全通；有 perm 键的查权限快照；anyPerm 任一命中即可见
    // （互通PN池管理：manage / set_policy 两个动作权限共享入口）；两者都没有的仅 admin（账号管理）
    return NAV_ITEMS.filter((it) => isAdmin
      || (it.perm ? !!perms[it.perm]
        : it.anyPerm ? it.anyPerm.some((p) => !!perms[p])
          : false));
  }, [token]);

  // 带参详情路由（如 /pool-analysis/:groupId）：与母页共用同一权限门，无权限则不注册
  const allowedDetails = useMemo(() => {
    if (!token) return [];
    const isAdmin = (localStorage.getItem("role") || "") === "admin";
    const perms = readPerms();
    return DETAIL_ROUTES.filter((r) => isAdmin || !!perms[r.perm]);
  }, [token]);

  // 未登录时任何路径都先登录；登录后停留在原地址（支持深链接直达）
  if (!token) return <LoginPage onLogin={setToken} />;

  const logout = () => {
    localStorage.removeItem("token");
    localStorage.removeItem("role");
    localStorage.removeItem("name");
    localStorage.removeItem("permissions");
    setToken(null);
  };

  // 一个页面权限都没有的账号：给明确提示而不是空壳白屏
  if (allowed.length === 0) {
    return (
      <Result
        style={{ paddingTop: 120 }}
        status="403"
        title="当前账号未分配任何页面权限"
        subTitle="请联系管理员在「账号管理」中为你开通所需页面后重新登录。"
        extra={<Button type="primary" onClick={logout}>退出登录</Button>}
      />
    );
  }

  const home = defaultPath(allowed);
  const allowedKeys = new Set(allowed.map((it) => it.perm).filter(Boolean));
  // 兼容重定向只在对应权限具备时注册；无权限则不建，交给 * 回 home（不越权暴露目标页存在）
  const redirects = NAV_REDIRECTS.filter((r) => !r.perm || allowedKeys.has(r.perm));

  return (
    <BrowserRouter key={token}>
      <Routes>
        <Route element={<AppShell allowed={allowed} onLogout={logout} onToken={setToken} />}>
          {allowed.map((it) => (
            <Route key={it.key} path={it.path} element={<it.page />} />
          ))}
          {allowedDetails.map((r) => (
            <Route key={r.key} path={r.path} element={<r.page />} />
          ))}
          {redirects.map((r) => (
            <Route key={r.from} path={r.from} element={<Navigate to={r.to} replace />} />
          ))}
          {/* 根路径与无权限/不存在的地址一律回默认页 */}
          <Route path="*" element={<Navigate to={home} replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
