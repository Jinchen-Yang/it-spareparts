import { useEffect, useMemo, useRef, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Alert, Button, Result } from "antd";
import type { AxiosError } from "axios";
import LoginPage from "./pages/LoginPage";
import AppShell from "./AppShell";
import { DETAIL_ROUTES, NAV_ITEMS, NAV_REDIRECTS, defaultPath, isNavItemAllowed } from "./nav";
import { clearSessionScopedPreferences } from "./sessionPreferences";
import { TaxBasisProvider } from "./context/TaxBasis";
import { getBetaFeatures, getSessionPermissions } from "./api";

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
  const [betaFeatures, setBetaFeatures] = useState<Record<string, boolean>>(readBetaFeatures);
  const [inventoryAccess, setInventoryAccess] = useState<{ token: string; granted?: boolean; error?: boolean }>();
  const retryInventoryAccess = useRef<(() => void) | null>(null);
  const inventoryReady = inventoryAccess?.token === token && inventoryAccess.granted !== undefined;
  const inventoryError = inventoryAccess?.token === token && inventoryAccess.error;

  useEffect(() => {
    if (!token) return;
    let active = true;
    let latestRequest = 0;
    let lastDecision = 0;
    const recordDecision = (request: number, granted: boolean) => {
      if (!active || localStorage.getItem("token") !== token || request < lastDecision) return;
      // 已发出更新的请求时只允许收紧权限。临时错误不参与许可或撤权的排序。
      if (granted && request !== latestRequest) return;
      lastDecision = request;
      const superseded = request < latestRequest;
      setInventoryAccess((prior) => ({
        token,
        granted,
        error: superseded && prior?.token === token && prior.error,
      }));
    };
    const refresh = () => {
      const currentToken = localStorage.getItem("token");
      if (currentToken !== token) {
        setToken(currentToken);
        return;
      }
      const request = ++latestRequest;
      // 保留同一会话最近一次服务端确认的结果。首次核验和切换身份时等待新结果。
      setInventoryAccess((prior) => ({ token, granted: prior?.token === token ? prior.granted : undefined }));
      void getSessionPermissions().then(({ data }) => {
        recordDecision(request, data?.page_inventory_analytics === true);
      }).catch((error: AxiosError | null) => {
        if (!active) return;
        const currentToken = localStorage.getItem("token");
        if (currentToken !== token) {
          setToken(currentToken);
          return;
        }
        const status = error?.response?.status;
        if (status === 401 || status === 403) {
          recordDecision(request, false);
          return;
        }
        if (request !== latestRequest) return;
        const networkFailure = !error?.response && (
          error?.code === "ERR_NETWORK" || error?.code === "ECONNABORTED" || error?.code === "ETIMEDOUT"
        );
        const transient = networkFailure || (status !== undefined && status >= 500 && status < 600);
        setInventoryAccess((prior) => ({
          token,
          granted: transient && prior?.token === token ? prior.granted : undefined,
          error: true,
        }));
      });
    };
    const visible = () => { if (document.visibilityState === "visible") refresh(); };
    retryInventoryAccess.current = refresh;
    refresh();
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", visible);
    return () => {
      active = false;
      retryInventoryAccess.current = null;
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [token]);

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

  // 常规权限沿用登录快照；库存入口叠加本会话服务端确认值。
  // 两者与 Beta 能力共同决定菜单和直达路由。
  const inventoryGranted = inventoryReady && inventoryAccess?.granted === true;
  const { allowed, allowedDetails } = useMemo(() => {
    if (!token) return { allowed: [], allowedDetails: [] };
    const role = localStorage.getItem("role") || "";
    const perms = { ...readPerms(), page_inventory_analytics: inventoryGranted };
    return {
      allowed: NAV_ITEMS.filter((item) => isNavItemAllowed(item, role, perms, betaFeatures)),
      allowedDetails: DETAIL_ROUTES.filter((item) => isNavItemAllowed(item, role, perms, betaFeatures)),
    };
  }, [token, betaFeatures, inventoryGranted]);

  // 未登录时任何路径都先登录；登录后停留在原地址（支持深链接直达）
  if (!token) return <LoginPage onLogin={setToken} />;

  const logout = () => {
    clearSessionScopedPreferences();
    localStorage.removeItem("token");
    localStorage.removeItem("role");
    localStorage.removeItem("name");
    localStorage.removeItem("permissions");
    localStorage.removeItem("beta_features");
    setBetaFeatures({});
    setToken(null);
  };

  const permissionNotice = inventoryError && (
    <Alert
      type="warning"
      showIcon
      style={{ marginBottom: 12 }}
      message={inventoryReady ? "库存权限暂时未能刷新" : "库存分析权限读取失败"}
      action={
        <Button size="small" aria-label={inventoryReady ? "重试库存权限" : "重试"} onClick={() => retryInventoryAccess.current?.()}>
          重试
        </Button>
      }
    />
  );

  // 一个页面权限都没有的账号：给明确提示而不是空壳白屏
  if (allowed.length === 0 && inventoryReady) {
    return (
      <>
        {permissionNotice}
        <Result
          style={{ paddingTop: 120 }}
          status="403"
          title="当前账号未分配任何页面权限"
          subTitle="请联系管理员在「账号管理」中为你开通所需页面后重新登录。"
          extra={<Button type="primary" onClick={logout}>退出登录</Button>}
        />
      </>
    );
  }

  const home = allowed.length ? defaultPath(allowed) : "/inventory/analytics";
  const allowedKeys = new Set(allowed.map((it) => it.perm).filter(Boolean));
  // 兼容重定向只在对应权限具备时注册；无权限则不建，交给 * 回 home（不越权暴露目标页存在）
  const redirects = NAV_REDIRECTS.filter((r) => !r.perm || allowedKeys.has(r.perm));

  return (
    <TaxBasisProvider key={token}>
      <BrowserRouter key={token}>
        <Routes>
          <Route element={<AppShell allowed={allowed} onLogout={logout} onToken={setToken} notice={permissionNotice} />}>
            {!inventoryReady && <Route path="/inventory/analytics" element={
              inventoryError ? null : <div role="status">正在核对库存分析权限…</div>
            } />}
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
    </TaxBasisProvider>
  );
}
