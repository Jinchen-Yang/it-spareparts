import { api } from "../api";

export interface MaintenanceProject {
  project_id: string;
  project_code: string;
  display_name: string;
  salesperson: string | null;
  project_manager_id: string | null;
  /** 维保期限主数据（#39/#51）：可显示、可在面板编辑。 */
  period_from: string | null;
  period_to: string | null;
  lifecycle_status: string;
  is_active: boolean;
  version: number;
  /** 项目级可见账号（2026-08-25）：overview 载荷回显用。 */
  visible_usernames?: string[];
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
  salesperson?: string | null;
  project_manager_id?: string | null;
  /** 维保期限（#39/#51）：YYYY-MM-DD；起止整组提交。 */
  period_from?: string | null;
  period_to?: string | null;
  /** 项目级可见账号（2026-08-25）：整组同步；null=不调整。 */
  visible_usernames?: string[];
  reason: string;
}

export interface MaintenanceProjectLifecycleInput {
  version: number;
  reason: string;
}

export const listMaintenanceProjects = (params: {
  page?: number;
  page_size?: number;
  include_inactive?: boolean;
} = {}) =>
  api.get<MaintenanceProjectDirectory>("/maintenance/projects/stable", {
    params: { include_inactive: true, ...params },
  });

export const searchMaintenanceProjects = (body: {
  q: string;
  page?: number;
  page_size?: number;
  include_inactive?: boolean;
}) => api.post<MaintenanceProjectDirectory>(
  "/maintenance/projects/stable/search",
  { include_inactive: true, ...body },
);

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
