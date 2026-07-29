import { api } from "../api";

export type TaxDisplayBasis = "inc" | "ex" | "both";

export interface SystemSettings {
  purchase_display_basis: TaxDisplayBasis;
  sales_display_basis: TaxDisplayBasis;
  maintenance_display_basis: TaxDisplayBasis;
  version: number;
  updated_by?: string | null;
  updated_at?: string | null;
}

export interface SystemSettingsUpdate {
  purchase_display_basis: TaxDisplayBasis;
  sales_display_basis: TaxDisplayBasis;
  maintenance_display_basis: TaxDisplayBasis;
  expected_version: number;
}

export const getSystemSettings = () =>
  api.get<SystemSettings>("/system-settings");

export const updateSystemSettings = (body: SystemSettingsUpdate) =>
  api.put<SystemSettings>("/system-settings", body);
