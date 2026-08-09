import type { AgentToolAudit } from "./api";
import { parseArtifactId } from "./artifactIds";

/**
 * Human-readable, content-free tool summary.
 *
 * Never render arg_keys or arbitrary properties: the server intentionally sends only
 * counts and canonical artifact IDs so customer queries/cells/rows cannot reappear in UI.
 */
export function summarizeToolAudit(a?: AgentToolAudit): string {
  if (!a) return "";
  const parts: string[] = [];
  const counts: Array<[keyof AgentToolAudit, string]> = [
    ["query_count", "个查询"],
    ["row_count", "行"],
    ["cell_count", "个单元格"],
    ["column_count", "列"],
  ];
  for (const [key, unit] of counts) {
    const count = a[key];
    if (typeof count === "number" && Number.isInteger(count) && count >= 0) {
      parts.push(`${count} ${unit}`);
    }
  }
  if (Array.isArray(a.artifact_ids)) {
    for (const artifactId of a.artifact_ids) {
      const canonical = parseArtifactId(artifactId);
      if (canonical) parts.push(`文件 ${canonical.slice(0, 8)}…`);
    }
  }
  if (!parts.length && Number.isInteger(a.arg_count) && a.arg_count > 0) {
    parts.push(`${a.arg_count} 项参数`);
  }
  return parts.join(" · ");
}
