const ANALYTICS_PATH = "/maintenance/analytics";

/** Only app-relative analysis links may restore a drilldown's original filters. */
export function maintenanceAnalyticsReturnPath(value: string | null): string | null {
  if (!value || /[\u0000-\u0020\\]/.test(value)) return null;
  if (value !== ANALYTICS_PATH && !value.startsWith(`${ANALYTICS_PATH}?`)) return null;
  const parsed = new URL(value, "https://maintenance.invalid");
  if (parsed.pathname !== ANALYTICS_PATH || parsed.hash) return null;
  return parsed.pathname + parsed.search;
}

const PROJECT_PANEL_TABS = [
  "overview", "parts-orders", "expense", "collection", "site", "acceptance", "analytics",
] as const;

export type ProjectPanelTab = typeof PROJECT_PANEL_TABS[number];

export function readProjectPanelTab(value: string | null): ProjectPanelTab {
  return PROJECT_PANEL_TABS.find((tab) => tab === value) ?? "overview";
}
