import { api } from "../api";

export interface ProjectImportPreview {
  import_id: number;
  status: "preview" | "error" | "applied";
  errors?: string[];
  row_count: number;
  new_count: number;
  updated_count: number;
  new_projects?: Array<{
    source_id: string;
    project_name: string;
    salesperson?: string | null;
    business_type?: string | null;
  }>;
  updated_projects?: Array<{
    source_id: string;
    project_id: string;
    project_name: string;
  }>;
}

export interface ProjectImportDetail {
  import_id: number;
  filename: string;
  file_hash: string;
  status: string;
  preview: ProjectImportPreview | null;
  applied_at: string | null;
  operated_by: string;
  created_at: string;
}

export interface ProjectImportApplyResult {
  created: number;
  updated: number;
}

export const previewProjectImport = (file: File) => {
  const form = new FormData();
  form.append("file", file);
  return api.post<ProjectImportPreview>(
    "/maintenance/project-imports/preview",
    form,
    { timeout: 60000 },
  );
};

export const getProjectImport = (importId: number) =>
  api.get<ProjectImportDetail>(`/maintenance/project-imports/${importId}`);

export const applyProjectImport = (importId: number) =>
  api.post<ProjectImportApplyResult>(
    `/maintenance/project-imports/${importId}/apply`,
  );
