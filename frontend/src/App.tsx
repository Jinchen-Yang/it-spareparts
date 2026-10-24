import { accountAccess } from "./authz/accountAccess";
import { hasControlledRoute, routeRule, ruleState, type Rule } from "./authz/rules";
import { AuthzProvider, AuthorizationBoundary, AuthzUnavailable, actorAllowed, resourceAllowed, resourceUsesLegacy, useAuthz } from "./authz/AuthzProvider";
import { AUTHZ_SESSION_INVALIDATED } from "./authz/events";
import { useEffect, useMemo, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Button, Result } from "antd";
import LoginPage from "./pages/LoginPage";
import AppShell from "./AppShell";
import { DETAIL_ROUTES, NAV_ITEMS, NAV_REDIRECTS, defaultPath } from "./nav";
import { clearSessionScopedPreferences } from "./sessionPreferences";
import { TaxBasisProvider } from "./context/TaxBasis";
import { getBetaFeatures } from "./api";

/** 安全读取本地权限快照：localStorage 被写坏时回退为空而非抛错致整页白屏（审计 U-1）。 */
function readPerms(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem("permissions") || "{}");
  } catch {
    return {};
  }
}

function readBetaFeatures(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem("beta_features") || "{}");
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

  useEffect(() => {
    const invalidated = () => setToken(localStorage.getItem("token"));
    window.addEventListener(AUTHZ_SESSION_INVALIDATED, invalidated);
    return () => window.removeEventListener(AUTHZ_SESSION_INVALIDATED, invalidated);
  }, []);
  return <AuthzProvider token={token}><AppContent token={token} setToken={setToken} /></AuthzProvider>;
}

function AppContent({ token, setToken }: { token: string | null; setToken: (token: string | null) => void }) {
  const authz = useAuthz();
  const [betaFeatures, setBetaFeatures] = useState<Record<string, boolean>>(readBetaFeatures);
  useEffect(() => {
    if (!token) {
      setBetaFeatures({});
      return;
    }
    let current = true;
    let latestRequest = 0;
    const expectedToken = token;
    const refreshBetaFeatures = () => {
      const requestId = ++latestRequest;
      void getBetaFeatures()
        .then(({ data }) => {
          if (
            !current
            || requestId !== latestRequest
            || localStorage.getItem("token") !== expectedToken
          ) return;
          // 逐键重建（不要写死键名）：漏掉任何一个键都会让对应导航组永久隐藏，
          // 且因为本函数在挂载/聚焦时覆盖 localStorage，登录时写入的完整快照会被抹掉。
          const refreshed = {
            maintenance: data.maintenance === true,
            replenishment: data.replenishment === true,
            maintenance_boss: data.maintenance_boss === true,
          };
          localStorage.setItem("beta_features", JSON.stringify(refreshed));
          setBetaFeatures(refreshed);
        })
        .catch(() => {
          if (
            !current
            || requestId !== latestRequest
            || localStorage.getItem("token") !== expectedToken
          ) return;
          // Capability refresh failures hide Beta only; stable pages remain usable.
          localStorage.setItem("beta_features", "{}");
          setBetaFeatures({});
        });
    };
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") refreshBetaFeatures();
    };

    refreshBetaFeatures();
    window.addEventListener("focus", refreshBetaFeatures);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      current = false;
      window.removeEventListener("focus", refreshBetaFeatures);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [token]);

  const allowedRoute = (rule: Rule, legacyPageAllowed: boolean) => {
    // Legacy is considered per resource in each complete clause, never as an OR bypass.
    const legacy = Object.fromEntries(rule.anyOf.flat().map(need => [need.resource_id, legacyPageAllowed]));
    return ruleState(authz.snapshot, rule, legacy) !== "deny";
  };

  // 旧模块继续使用登录权限；已迁移模块由服务端刷新后的能力快照决定入口。
  const allowed = useMemo(() => {
    if (!token) return [];
    const role = localStorage.getItem("role") || "";
    const isAdmin = role === "admin";
    const perms = readPerms();
    // admin 全通；组合能力入口优先按完整能力判断；其余沿用单权限/任一权限规则。
    return NAV_ITEMS.filter((it) => {
      if (it.betaFeature && betaFeatures[it.betaFeature] !== true) return false;
      if (it.key === "accounts") return authz.status === "ready" && accountAccess(authz.snapshot).enter;
      if (hasControlledRoute(it.path)) {
        const oldAllowed = isAdmin || (it.visibleWhen ? it.visibleWhen() : it.perm ? !!perms[it.perm] : it.anyPerm ? it.anyPerm.some(p => !!perms[p]) : false);
        return authz.status === "ready" && allowedRoute(routeRule(it.path), oldAllowed);
      }
      return (!it.betaFeature || betaFeatures[it.betaFeature] === true) && (isAdmin
        || (it.visibleWhen ? it.visibleWhen()
          : it.perm ? !!perms[it.perm]
          : it.anyPerm ? it.anyPerm.some((p) => !!perms[p])
            : false));
    });
  }, [token, betaFeatures, authz.status, authz.snapshot]);

  // 带参详情路由（如 /pool-analysis/:groupId）：与母页共用同一权限门，无权限则不注册
  const allowedDetails = useMemo(() => {
    if (!token) return [];
    const isAdmin = (localStorage.getItem("role") || "") === "admin";
    const perms = readPerms();
    return DETAIL_ROUTES.filter(r => {
      if (r.betaFeature && betaFeatures[r.betaFeature] !== true) return false;
      const legacy = isAdmin || (r.perm ? !!perms[r.perm] : r.anyPerm ? r.anyPerm.some(p => !!perms[p]) : false);
      return hasControlledRoute(r.path) ? authz.status === "ready" && allowedRoute(routeRule(r.path), legacy) : legacy;
    });
  }, [token, betaFeatures, authz.status, authz.snapshot]);

  // 未登录时任何路径都先登录；登录后停留在原地址（支持深链接直达）
  if (!token) return <LoginPage onLogin={setToken} />;

  const logout = () => {
    clearSessionScopedPreferences();
    localStorage.removeItem("token");
    localStorage.removeItem("role");
    localStorage.removeItem("name");
    localStorage.removeItem("username");
    localStorage.removeItem("permissions");
    localStorage.removeItem("beta_features");
    setBetaFeatures({});
    setToken(null);
  };

  // 一个页面权限都没有的账号：给明确提示而不是空壳白屏
  if (allowed.length === 0 && authz.status !== "ready") return <div style={{ padding: 24 }}>
    <AuthzUnavailable /><Button style={{ marginTop: 16 }} onClick={logout}>退出登录</Button>
  </div>;

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
    <TaxBasisProvider key={token}>
      <BrowserRouter key={token}>
        <Routes>
          <Route element={<AppShell allowed={allowed} onLogout={logout} onToken={setToken} />}>
            {[...NAV_ITEMS, ...DETAIL_ROUTES].filter(it => (hasControlledRoute(it.path) || it.key === "accounts") && ![...allowed, ...allowedDetails].includes(it as never)).map(it => (
              <Route key={it.key} path={it.path} element={authz.status !== "ready" ? <AuthzUnavailable />
                : <Result status="403" title="没有此页面访问权限" subTitle="请联系管理员检查模块授权" />} />
            ))}
            {allowed.map(it => <Route key={it.key} path={it.path} element={<it.page key={hasControlledRoute(it.path) ? `${token}:${authz.generation}:${authz.snapshot?.version}` : "legacy"} />} />)}
            {allowedDetails.map(r => <Route key={r.key} path={r.path} element={<r.page key={hasControlledRoute(r.path) ? `${token}:${authz.generation}:${authz.snapshot?.version}` : "legacy"} />} />)}
            {redirects.map((r) => (
              <Route key={r.from} path={r.from} element={<Navigate to={r.to} replace />} />
            ))}
            {/* 根路径与无权限/不存在的地址一律回默认页 */}
            <Route path="*" element={<Navigate to={home} replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </TaxBasisProvider>
  );
}
