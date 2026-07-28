import { api } from "../api";

export type MaintenanceProfitDefaultBasis = "inc" | "ex" | "both";

export interface SystemSettings {
  maintenance_project_profit_default_basis: MaintenanceProfitDefaultBasis;
  version: number;
  updated_by?: string | null;
  updated_at?: string | null;
}

export interface SystemSettingsUpdate {
  maintenance_project_profit_default_basis: MaintenanceProfitDefaultBasis;
  expected_version: number;
}

export const getSystemSettings = () =>
  api.get<SystemSettings>("/system-settings");

export const updateSystemSettings = (body: SystemSettingsUpdate) =>
  api.put<SystemSettings>("/system-settings", body);
