import type { CatalogClient, Entry, Envelope, Gallery } from "../../api/pnCatalog";

export interface Job { job_id: string; kind: string; status: "pending" | "running" | "succeeded" | "failed" | "cancelled" | "stale"; part_id: number | null; resource_id: string; input_revision: number; attempt: number; retry_count: number; revision: number; error_code: string | null; result: Record<string, unknown> | null; created_at: string }
export interface Recognition { status: Job["status"]; result: { candidates: { pn: string; confidence: number }[]; item_category: string | null; provider: string; model: string } | null }
export interface Inbox { inbox_id: string; upload_id: string; asset_id: string | null; group_id: string | null; position: number; client_order: number | null; filename: string; size_bytes: number; status: "receiving" | "ready" | "assigned" | "failed" | "deleted"; revision: number; generation: number; claimed_by: string | null; claim_revision: number; claim_expires_at: string | null; uploaded_by: { principal_id: string; client_id: string }; uploaded_by_me: boolean; claimed_by_me: boolean; created_at: string; purpose: "common" | "defect"; part_id: number | null; assignment_key: string | null; recognition: Recognition | null }
export const JOB_LABELS: Record<string, string> = { "enrichment.recognize": "图片识别", "enrichment.research": "PN 研究", "enrichment.bundle": "图片打包", "media.verify_upload": "图片校验", "media.thumbnail": "缩略图" };
export const STATUS_LABELS: Record<string, string> = { pending: "等待执行", running: "执行中", succeeded: "已完成", failed: "失败", cancelled: "已取消", stale: "资料已变化，请重新发起", receiving: "接收中", ready: "待归档", assigned: "已归档", deleted: "已删除" };
export const jobGuard = (job: Job) => ({ expected_revision: job.revision, expected_attempt: job.attempt, expected_retry_count: job.retry_count });

/** A complete, single-revision selection is required before a human confirms a bundle. */
export async function completeGallery(client: CatalogClient, partId: number): Promise<{ gallery: Gallery; identityRevision: number }> {
  let first: Gallery | undefined; let cursor: string | null = null; let identityRevision: number | undefined;
  const entries: Entry[] = []; const ids = new Set<string>(); const cursors = new Set<string>();
  do {
    const result: Envelope<Gallery> = await client.get<Gallery>(`/parts/${partId}/gallery`, { status: "gallery", limit: 200, ...(cursor ? { cursor } : {}) });
    if (result.meta.identity_revision === undefined) throw new Error("图库缺少身份版本，请刷新后重试");
    if (first && (result.data.group_id !== first.group_id || result.data.revision !== first.revision || result.meta.identity_revision !== identityRevision)) throw new Error("读取期间图库发生变化，请重新核对");
    first ??= result.data; identityRevision ??= result.meta.identity_revision;
    for (const entry of result.data.entries) {
      if (ids.has(entry.entry_id)) throw new Error("图库分页重复，请重新核对");
      ids.add(entry.entry_id); entries.push(entry);
    }
    if (entries.length > 1000) throw new Error("图片超过单次打包的 1000 张上限，请减少图库范围后重试");
    cursor = result.meta.next_cursor || null;
    if (cursor && cursors.has(cursor)) throw new Error("图库分页无效，请刷新后重试");
    if (cursor) cursors.add(cursor);
  } while (cursor);
  return { gallery: { ...first!, entries, ordered_entry_ids: entries.map(e => e.entry_id) }, identityRevision: identityRevision! };
}
