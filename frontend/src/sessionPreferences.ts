/** 清理旧版本遗留的个人税口径。新版本只使用管理员服务端统一策略。 */
export function clearSessionScopedPreferences() {
  try {
    localStorage.removeItem("maintenance_project_profit_basis");
    localStorage.removeItem("tax_basis");
  } catch {
    // localStorage 被禁用时没有遗留偏好需要清理。
  }
}
