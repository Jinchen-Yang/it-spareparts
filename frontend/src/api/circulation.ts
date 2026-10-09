// 备件循环客户端（/api/circulation/*，板块 D：回收清单/检测单/循环档案/SN 台账）。
// 档案挂在 PN 上（一 PN 一档）；SN 台账一 SN 一物（D-23/D-24 已确认口径）。
import { api } from "../api";

export interface CirculationAttachment {
  attachment_id: string;
  kind: "photo" | "report";
  original_filename: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  uploaded_by: string;
  uploaded_at: string;
}

export interface CirculationArchive {
  archive_id: string;
  pn_std: string;
  part_id: number | null;
  listing_status: "pending" | "listed" | "force_listed" | "delisted";
  requirements_met: boolean;
  photo_count: number;
  report_count: number;
  force_listed_by: string | null;
  force_listed_at: string | null;
  force_reason: string | null;
  attachments: CirculationAttachment[];
}

export interface SnLedgerRow {
  sn: string;
  pn_std: string;
  part_id: number | null;
  lifecycle_status: "pending_detection" | "in_stock" | "bad_stock" | "retired";
  source_batch_id: string | null;
  detection_item_id: string | null;
  created_at: string;
}

/** 查 PN 档案；404（尚无档案）返回 null，由调用方决定引导拍照。 */
export async function getArchive(pnStd: string): Promise<CirculationArchive | null> {
  try {
    const res = await api.get(`/circulation/recycle-imports/archives/${encodeURIComponent(pnStd)}`);
    return res.data as CirculationArchive;
  } catch (err) {
    const status = (err as { response?: { status?: number } }).response?.status;
    if (status === 404) return null;
    throw err;
  }
}

/** 上传照片/检测报告附件（PN 无档案时后端自动建档）。 */
export async function uploadArchiveAttachment(
  pnStd: string, kind: "photo" | "report", file: File,
): Promise<CirculationArchive> {
  const form = new FormData();
  form.append("kind", kind);
  form.append("file", file);
  const res = await api.post(
    `/circulation/recycle-imports/archives/${encodeURIComponent(pnStd)}/attachments`,
    form,
  );
  return res.data as CirculationArchive;
}

export interface SnLedgerQuery {
  pn_std?: string;
  lifecycle_status?: SnLedgerRow["lifecycle_status"];
}

export async function listSnItems(query: SnLedgerQuery = {}): Promise<SnLedgerRow[]> {
  const res = await api.get("/circulation/recycle-imports/sn-items", { params: query });
  return res.data.sn_items as SnLedgerRow[];
}
