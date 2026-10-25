const sessionCacheClearers = new Set<() => void>();

/** 注册需要随登录周期清理的内存缓存（如价格参考）；模块加载时调用一次。 */
export function registerSessionCacheClearer(clear: () => void) {
  sessionCacheClearers.add(clear);
}

/** 清理旧版本遗留的个人税口径与所有按账号缓存。新版本只使用服务端统一策略。 */
export function clearSessionScopedPreferences() {
  try {
    localStorage.removeItem("maintenance_project_profit_basis");
    localStorage.removeItem("tax_basis");
  } catch {
    // localStorage 被禁用时没有遗留偏好需要清理。
  }
  for (const clear of sessionCacheClearers) clear();
}
