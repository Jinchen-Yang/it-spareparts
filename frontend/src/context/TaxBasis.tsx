// 管理员统一配置的三域税口径。普通页面只读，不提供本地 setter；
// 本上下文只决定展示哪一侧，固定 13% 的缺失侧补齐集中在 utils/format.ts。
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Alert, Button, Spin } from "antd";
import { getSystemSettings } from "../api/systemSettings";
import type { SystemSettings, TaxDisplayBasis } from "../api/systemSettings";
import { money, moneyExact } from "../utils/format";

export type TaxBasis = TaxDisplayBasis;
export type TaxScope = "purchase" | "sales" | "maintenance";
export type TaxSide = Exclude<TaxBasis, "both">;

export const TAX_BASIS_OPTIONS = [
  { label: "含税", value: "inc" },
  { label: "不含税", value: "ex" },
  { label: "两列", value: "both" },
];

export interface TaxDisplayPolicy {
  purchase: TaxBasis;
  sales: TaxBasis;
  maintenance: TaxBasis;
}

export const DEFAULT_TAX_DISPLAY_POLICY: TaxDisplayPolicy = {
  purchase: "both",
  sales: "ex",
  maintenance: "both",
};

const TAX_POLICY_CHANNEL = "itdata-tax-display-policy";
export const TAX_POLICY_REFRESH_INTERVAL_MS = 60_000;

function openTaxPolicyChannel(): BroadcastChannel | null {
  if (typeof BroadcastChannel === "undefined") return null;
  try {
    return new BroadcastChannel(TAX_POLICY_CHANNEL);
  } catch {
    // 跨标签通知只是加速路径；focus/visibility/定时刷新仍会收敛到服务端事实。
    return null;
  }
}

export function announceTaxDisplayPolicyChanged(version: number): void {
  if (!Number.isInteger(version) || version < 1) return;
  const channel = openTaxPolicyChannel();
  if (!channel) return;
  try {
    channel.postMessage({ type: "tax-display-policy-changed", version });
  } catch {
    // 非关键通知失败不能把已经提交成功的管理员设置伪装成保存失败。
  } finally {
    try {
      channel.close();
    } catch {
      // 关闭失败同样不能改变已经提交的业务结果。
    }
  }
}

function validBasis(value: unknown): value is TaxBasis {
  return value === "inc" || value === "ex" || value === "both";
}

export function policyFromSettings(settings: SystemSettings): TaxDisplayPolicy {
  if (
    !validBasis(settings.purchase_display_basis)
    || !validBasis(settings.sales_display_basis)
    || !validBasis(settings.maintenance_display_basis)
    || !Number.isInteger(settings.version)
    || settings.version < 1
  ) {
    throw new Error("invalid tax display policy response");
  }
  return {
    purchase: settings.purchase_display_basis,
    sales: settings.sales_display_basis,
    maintenance: settings.maintenance_display_basis,
  };
}

interface Ctx {
  policy: TaxDisplayPolicy;
  basisFor: (scope: TaxScope) => TaxBasis;
  loading: boolean;
  loadFailed: boolean;
  refresh: (minimumVersion?: number) => Promise<void>;
}

const defaultContext: Ctx = {
  policy: DEFAULT_TAX_DISPLAY_POLICY,
  basisFor: (scope) => DEFAULT_TAX_DISPLAY_POLICY[scope],
  loading: false,
  loadFailed: false,
  refresh: async () => undefined,
};

const TaxBasisContext = createContext<Ctx>(defaultContext);

export function TaxBasisProvider({ children }: { children: ReactNode }) {
  const [policy, setPolicy] = useState<TaxDisplayPolicy>(DEFAULT_TAX_DISPLAY_POLICY);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const mountedRef = useRef(true);
  const inFlightRef = useRef<Promise<SystemSettings> | null>(null);
  const requiredVersionRef = useRef(0);
  const currentVersionRef = useRef(0);

  const loadPolicy = useCallback(async (
    blocking: boolean,
    minimumVersion = 0,
  ): Promise<void> => {
    if (Number.isInteger(minimumVersion) && minimumVersion > 0) {
      requiredVersionRef.current = Math.max(
        requiredVersionRef.current,
        minimumVersion,
      );
    }
    if (blocking && mountedRef.current) setLoading(true);

    let staleRetries = 0;
    try {
      while (mountedRef.current) {
        let request = inFlightRef.current;
        if (!request) {
          request = getSystemSettings()
            .then(({ data }) => data)
            .finally(() => {
              inFlightRef.current = null;
            });
          inFlightRef.current = request;
        }

        const settings = await request;
        const nextPolicy = policyFromSettings(settings);
        if (settings.version < requiredVersionRef.current) {
          if (staleRetries >= 1) {
            throw new Error("tax display policy response is stale");
          }
          staleRetries += 1;
          continue;
        }
        if (!mountedRef.current) return;
        if (settings.version >= currentVersionRef.current) {
          currentVersionRef.current = settings.version;
          setPolicy(nextPolicy);
          setLoadFailed(false);
        }
        return;
      }
    } catch {
      if (!mountedRef.current) return;
      // 读取失败、响应非法或已知版本尚未可见时都失败关闭；不使用本地旧口径。
      setPolicy(DEFAULT_TAX_DISPLAY_POLICY);
      setLoadFailed(true);
    } finally {
      if (blocking && mountedRef.current) setLoading(false);
    }
  }, []);

  const refresh = useCallback(
    (minimumVersion = 0) => loadPolicy(true, minimumVersion),
    [loadPolicy],
  );
  const refreshSilently = useCallback(
    () => loadPolicy(false),
    [loadPolicy],
  );

  useEffect(() => {
    mountedRef.current = true;
    void refresh();
    return () => {
      mountedRef.current = false;
    };
  }, [refresh]);

  useEffect(() => {
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") {
        void refreshSilently();
      }
    };
    const intervalId = window.setInterval(
      refreshWhenVisible,
      TAX_POLICY_REFRESH_INTERVAL_MS,
    );
    window.addEventListener("focus", refreshWhenVisible);
    document.addEventListener("visibilitychange", refreshWhenVisible);

    const channel = openTaxPolicyChannel();
    const onPolicyChanged = (event: MessageEvent<unknown>) => {
      const payload = event.data;
      if (
        typeof payload !== "object"
        || payload === null
        || !("type" in payload)
        || !("version" in payload)
        || payload.type !== "tax-display-policy-changed"
        || !Number.isInteger(payload.version)
        || (payload.version as number) < 1
      ) {
        return;
      }
      // 已知管理员版本变化后，旧口径不再可信；阻塞金额直到至少读到该版本。
      void loadPolicy(true, payload.version as number);
    };
    channel?.addEventListener("message", onPolicyChanged);

    return () => {
      window.clearInterval(intervalId);
      window.removeEventListener("focus", refreshWhenVisible);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
      channel?.removeEventListener("message", onPolicyChanged);
      channel?.close();
    };
  }, [loadPolicy, refreshSilently]);

  const value = useMemo<Ctx>(() => ({
    policy,
    basisFor: (scope) => policy[scope],
    loading,
    loadFailed,
    refresh,
  }), [policy, loading, loadFailed, refresh]);

  return (
    <TaxBasisContext.Provider value={value}>
      {children}
    </TaxBasisContext.Provider>
  );
}

export const useTaxDisplayPolicy = () => useContext(TaxBasisContext);

export function useTaxBasis(scope: TaxScope): TaxBasis {
  return useTaxDisplayPolicy().basisFor(scope);
}

export function taxSidesForBasis(basis: TaxBasis): TaxSide[] {
  return basis === "both" ? ["inc", "ex"] : [basis];
}

export function taxBasisCaption(basis: TaxBasis): string {
  if (basis === "inc") return "含税";
  if (basis === "ex") return "不含税";
  return "含税 / 不含税";
}

/**
 * 财务金额页面的统一安全边界。
 *
 * 管理员策略是解释金额列的必要证据；策略未加载或读取失败时，不允许页面先按
 * 客户端默认值展示一套看似有效的金额。失败状态保留明确的重试入口。
 */
export function TaxPolicyBoundary({ children }: { children: ReactNode }) {
  const { loading, loadFailed, refresh } = useTaxDisplayPolicy();
  if (loading) {
    return (
      <div
        aria-label="正在加载金额展示口径"
        style={{ display: "flex", justifyContent: "center", alignItems: "center", gap: 10, padding: "72px 0" }}
      >
        <Spin />
        <span>正在加载金额展示口径…</span>
      </div>
    );
  }
  if (loadFailed) {
    return (
      <Alert
        type="error"
        showIcon
        message="金额展示口径加载失败"
        description="为避免把含税与未税金额按错误口径展示，当前已暂停显示业务页面。请重试；若持续失败，请联系管理员检查系统设置服务。"
        action={<Button onClick={() => void refresh()}>重新加载</Button>}
      />
    );
  }
  return <>{children}</>;
}

// 内联双值（卡片/Statistic 用；表格请改用两列）。跟随全局开关：
// inc→只含税、ex→只不含税、both→「含 X / 不含 Y」。确实无原始金额时由 money() 显示 "-"。
// stack=true：both 口径下把含/不含拆成两行堆叠（大字号 KPI 卡用，避免一行放不下横向溢出）。
export function TaxMoneyByBasis({
  basis,
  inc,
  ex,
  stack,
  exact,
}: {
  basis: TaxBasis;
  inc: number | null;
  ex: number | null;
  stack?: boolean;
  exact?: boolean;
}) {
  const format = exact ? moneyExact : money;
  if (basis === "inc") return <>{format(inc)}</>;
  if (basis === "ex") return <>{format(ex)}</>;
  if (stack)
    return (
      <span style={{ display: "inline-flex", flexDirection: "column", lineHeight: 1.25 }}>
        <span><span style={{ color: "var(--mb-text-3)" }}>含 </span>{format(inc)}</span>
        <span><span style={{ color: "var(--mb-text-3)" }}>不含 </span>{format(ex)}</span>
      </span>
    );
  return (
    <span style={{ whiteSpace: "nowrap" }}>
      <span style={{ color: "var(--mb-text-3)" }}>含 </span>{format(inc)}
      <span style={{ color: "var(--mb-text-3)" }}> · 不含 </span>{format(ex)}
    </span>
  );
}

export function TaxMoney({
  inc,
  ex,
  scope,
  stack,
  exact,
}: {
  inc: number | null;
  ex: number | null;
  scope: TaxScope;
  stack?: boolean;
  exact?: boolean;
}) {
  const { basisFor, loading, loadFailed } = useTaxDisplayPolicy();
  if (loading || loadFailed) return <>—</>;
  return (
    <TaxMoneyByBasis
      basis={basisFor(scope)}
      inc={inc}
      ex={ex}
      stack={stack}
      exact={exact}
    />
  );
}
