import { api } from "../api";

export interface MaintenanceProject {
  project_id: string;
  project_code: string;
  display_name: string;
  project_manager_id: string | null;
  lifecycle_status: string;
  is_active: boolean;
  version: number;
}

export interface MaintenanceProjectDirectory {
  rows: MaintenanceProject[];
  total: number;
  page: number;
  page_size: number;
  as_of: string;
  data_version: string;
}

export interface MaintenanceProjectOverview {
  project: MaintenanceProject;
}

export interface MaintenanceProjectCreateInput {
  project_code: string;
  display_name: string;
  project_manager_id?: string | null;
  reason: string;
}

export interface MaintenanceProjectUpdateInput {
  version: number;
  display_name?: string;
  project_manager_id?: string | null;
  reason: string;
}

export interface MaintenanceProjectLifecycleInput {
  version: number;
  reason: string;
}

export const listMaintenanceProjects = (params: {
  q?: string;
  page?: number;
  page_size?: number;
  include_inactive?: boolean;
} = {}) =>
  api.get<MaintenanceProjectDirectory>("/maintenance/projects/stable", {
    params: { include_inactive: true, ...params },
  });

export const getMaintenanceProject = (projectId: string) =>
  api.get<MaintenanceProjectOverview>(`/maintenance/projects/stable/${projectId}`);

export const createMaintenanceProject = (body: MaintenanceProjectCreateInput) =>
  api.post<MaintenanceProject>("/maintenance/projects/stable", body);

export const updateMaintenanceProject = (
  projectId: string,
  body: MaintenanceProjectUpdateInput,
) => api.patch<MaintenanceProject>(`/maintenance/projects/stable/${projectId}`, body);

export const archiveMaintenanceProject = (
  projectId: string,
  body: MaintenanceProjectLifecycleInput,
) => api.post<MaintenanceProject>(
  `/maintenance/projects/stable/${projectId}/archive`,
  body,
);

export const restoreMaintenanceProject = (
  projectId: string,
  body: MaintenanceProjectLifecycleInput,
) => api.post<MaintenanceProject>(
  `/maintenance/projects/stable/${projectId}/restore`,
  body,
);
