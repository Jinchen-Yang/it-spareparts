/** 只在当前登录会话内沿用的展示偏好；切换账号时必须清除。 */
export const MAINTENANCE_PROFIT_BASIS_KEY = "maintenance_project_profit_basis";

export function clearSessionScopedPreferences() {
  try {
    localStorage.removeItem(MAINTENANCE_PROFIT_BASIS_KEY);
  } catch {
    // localStorage 被禁用时，会话本来也不会持久化偏好。
  }
}
