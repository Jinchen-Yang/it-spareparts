const LEGACY_ARTIFACT_ID = /^[0-9a-f]{12}$/i;
const UUID_ARTIFACT_ID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Strictly parse a complete Artifact v2 UUID or historical 12-hex file id. */
export function parseArtifactId(value: string): string | null {
  const candidate = String(value || "").trim();
  if (!LEGACY_ARTIFACT_ID.test(candidate) && !UUID_ARTIFACT_ID.test(candidate)) return null;
  return candidate.toLowerCase();
}

/** Extract only the canonical file-download route; suffixes and truncated ids fail closed. */
export function artifactIdFromFileUrl(url: string): string | null {
  const match = /^\/api\/agent\/files\/([^/?#]+)$/.exec(String(url || ""));
  return match ? parseArtifactId(match[1]) : null;
}

export interface UploadedAttachmentMessage {
  filename: string;
  fileId: string;
  body: string;
}

/** Parse the server-facing upload prefix used in persisted user chat messages. */
export function parseUploadedAttachmentMessage(text: string): UploadedAttachmentMessage | null {
  const match = /^\[已上传文件「(.+?)」 file_id=([^，\]\s]+)(?:，[^\]]*)?\]\n\n?([\s\S]*)$/.exec(text);
  if (!match) return null;
  const fileId = parseArtifactId(match[2]);
  if (!fileId) return null;
  return { filename: match[1], fileId, body: match[3] };
}
