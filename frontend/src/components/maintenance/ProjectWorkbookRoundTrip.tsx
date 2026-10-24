import WorkbookRoundTrip from "./WorkbookRoundTrip";
import { applyProjectMaster, downloadProjectMaster, validateProjectMaster } from "../../api/maintenanceWorkbooks";
import { useContextAdmission } from "../../authz/access";
import { useContextTransport } from "../../authz/domainTransport";
import { actionRule, allRule, anyRule, surfaceRule } from "../../authz/rules";

/** Each alternative stays a complete grant contract for the same object. No cross-module field/scope union. */
export default function ProjectWorkbookRoundTrip({ projectId, surface, sheets, filename, title, hint, canUpload, onAfterApply }: {
  projectId: string;
  surface: "project_master" | "parts_roundtrip" | "expense_roundtrip" | "collection_roundtrip" | "site_roundtrip";
  sheets?: string[];
  filename: string;
  title: string;
  hint?: string;
  canUpload: boolean;
  onAfterApply?: () => Promise<boolean | void>;
}) {
  const admission = useContextAdmission();
  const capture = useContextTransport(JSON.stringify([projectId, surface, sheets]), true);
  const rule = (action: "download" | "validate" | "apply") => anyRule(
    ...["maintenance_managed_workbook", "maintenance_general_workbook", "maintenance_general_workbook_write"]
      .map(id => allRule(surfaceRule(`${id}.${surface}`), actionRule(id, action))),
  );
  const download = admission.state(rule("download"), true) !== "deny";
  const validate = admission.state(rule("validate"), canUpload) !== "deny";
  const apply = admission.state(rule("apply"), canUpload) !== "deny";
  if (!download && !validate) return null;
  return <WorkbookRoundTrip size="small" title={title} filename={filename} hint={hint}
    capture={capture} canDownload={download} canUpload={validate} canApply={apply}
    onDownload={() => downloadProjectMaster(projectId, sheets, capture)}
    onValidate={file => validateProjectMaster(projectId, file, capture)}
    onApply={(file, opts) => applyProjectMaster(projectId, file, opts, capture)}
    onAfterApply={onAfterApply} />;
}
