import { presales, type Inquiry } from "../../api/presales";
import { listWorkItems, type WorkItem, type WorkTab } from "../../api/workItems";
import { flattenOffers } from "./shared";

export async function findOwnTask(inquiryId: number): Promise<WorkItem | undefined> {
  // 待办接口的这三个页签均按服务端当前账号过滤，不以显示名称推断身份。
  for (const tab of ["assigned", "claimed", "done"] as WorkTab[]) {
    for (let page = 1; ; page += 1) {
      const result = await listWorkItems({ tab, subject_type: "presales_inquiry", page, page_size: 100 });
      const found = result.items.find((item) => item.subject_id === inquiryId);
      if (found) return found;
      if (page * 100 >= result.total) break;
    }
  }
  return undefined;
}

export async function loadInquiry(inquiryId: number) {
  const inquiry = await presales.getInquiry(inquiryId);
  const references = (inquiry.lines ?? []).flatMap((line) => [...new Set([
    ...(line.ref_offer_id ? [line.ref_offer_id] : []), ...(line.linked_offer_ids ?? []), ...(line.linked_offers ?? []).map((offer) => offer.id),
    ...(inquiry.line_responses ?? []).filter((row) => row.line_id === line.id && row.mode === "reuse" && row.ref_offer_id)
      .map((row) => row.ref_offer_id!),
  ])].map((ref_offer_id) => ({ ref_offer_id, part_id: line.part_id })));
  if (inquiry.kind !== "proactive_offer" || !references.length) return inquiry;
  const library = await presales.library([...new Set(references.flatMap((row) => row.part_id ? [row.part_id] : []))]);
  // 处理页编辑当前报价；原单关联中的钉版本只在销售详情显示，不能当作并发更新版本。
  const offers = new Map((inquiry.offers ?? []).filter((offer) => !references.some((row) => row.ref_offer_id === offer.id))
    .map((offer) => [offer.id, offer]));
  for (const offer of Object.values(library).flatMap(flattenOffers)) {
    if (references.some((line) => line.ref_offer_id === offer.id)) offers.set(offer.id, offer);
  }
  // 过期报价已从报价库移除。历史只用于定位原单，再读当前记录获取最新并发版本。
  await Promise.allSettled(references.filter((line) => !offers.has(line.ref_offer_id!)).map(async (line) => {
    const revisions = await presales.revisions(line.ref_offer_id!);
    const originId = revisions[revisions.length - 1]?.snapshot.inquiry_id;
    if (!originId) return;
    const origin = await presales.getInquiry(originId);
    const current = origin.offers?.find((offer) => offer.id === line.ref_offer_id);
    if (current) offers.set(current.id, current);
  }));
  return { ...inquiry, offers: Array.from(offers.values()) };
}

