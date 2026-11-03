import { useEffect, useRef, useState } from "react";
import { AutoComplete, Button, InputNumber, Select, Space } from "antd";
import { presales, type Offer } from "../../api/presales";
import { suggestParts, type PartSuggestion } from "../../api/search";
import CreatePartModal from "../../components/parts/CreatePartModal";
import { readPermissionMap } from "../../nav";
import { existingOfferConflict } from "./ExistingOfferDialog";
import { priceText } from "./answers";
import { usePresalesError } from "./shared";
import "./presales.css";

/** 不经询价直接给型号报价；不生成单据（D-0046）。 */
export function DirectQuote({ onDone, onClose }: { onDone: (text: string) => void; onClose: () => void }) {
  const handleError = usePresalesError();
  const [text, setText] = useState("");
  const [options, setOptions] = useState<PartSuggestion[]>([]);
  const [part, setPart] = useState<PartSuggestion>();
  const [price, setPrice] = useState<number | null>(null);
  const [tax, setTax] = useState<boolean | null>(true);
  const [basis, setBasis] = useState<"sale" | "cost">("sale");
  const taxChanged = useRef(false);
  const basisChanged = useRef(false);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const canCreatePart = localStorage.getItem("role") === "admin" || readPermissionMap().page_master_data === true;
  useEffect(() => {
    const query = text.trim();
    if (!query || part?.pn_std === text) { setOptions([]); return; }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      suggestParts(query, 10, controller.signal).then((rows) => { if (!controller.signal.aborted) setOptions(rows); }).catch(() => undefined);
    }, 200);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [text, part]);
  const save = async () => {
    if (!part || price == null) return;
    setBusy(true);
    try {
      const input = { part_id: part.part_id, unit_price: price, is_tax_inclusive: tax, price_basis: basis, no_stock: false };
      let published: Offer;
      try { published = await presales.directOffer(input); }
      catch (error) {
        const existing = existingOfferConflict(error)?.existing_offers[0];
        if (!existing) throw error;
        // 首次读取既有报价的版本后更新一次；版本再次冲突则保留输入交由错误恢复处理。
        published = await presales.directOffer({ ...input,
          is_tax_inclusive: taxChanged.current ? tax : existing.is_tax_inclusive ?? null,
          price_basis: basisChanged.current ? basis : existing.price_basis ?? "sale",
          on_existing: "update", existing_offer_id: existing.id, expected_version: existing.version });
      }
      setTax(published.is_tax_inclusive ?? null); setBasis(published.price_basis ?? "sale");
      onDone(`${part.pn_std} 已更新为 ${priceText(published)}，保存成功。`);
    } catch (error) { await handleError(error); }
    finally { setBusy(false); }
  };
  return <div className="presales-direct">
    <p className="presales-wide presales-muted">不用等询价，直接给这个型号报价，保存后所有人可见。</p>
    <label>PN<AutoComplete aria-label="主动报价 PN" value={text} placeholder="输入 PN 选择型号" style={{ width: "100%" }}
      onChange={(value) => { setText(value); if (part && value !== part.pn_std) { setPart(undefined); taxChanged.current = false; basisChanged.current = false; setTax(true); setBasis("sale"); } }}
      onSelect={(_, option) => { const picked = options.find((row) => String(row.part_id) === option.key); if (picked) { setPart(picked); setText(picked.pn_std); } }}
      options={options.map((row) => ({ key: String(row.part_id), value: row.pn_std, label: <span>{row.pn_std} <span className="presales-muted">{row.summary || row.description}</span></span> }))} /></label>
    <label>单价（元）<InputNumber aria-label="主动报价单价" min={0} precision={2} style={{ width: "100%" }} value={price} onChange={(value) => setPrice(value)} /></label>
    <label>税<Select aria-label="主动报价含税" value={tax == null ? "unknown" : tax ? "inc" : "ex"} onSelect={(value) => { taxChanged.current = true; setTax(value === "unknown" ? null : value === "inc"); }} options={[{ value: "inc", label: "含税" }, { value: "ex", label: "不含税" }, ...(tax == null ? [{ value: "unknown", label: "未声明" }] : [])]} /></label>
    <label>价格性质<Select aria-label="主动报价价格性质" value={basis} onSelect={(value) => { basisChanged.current = true; setBasis(value); }} options={[{ value: "sale", label: "售价" }, { value: "cost", label: "成本价" }]} /></label>
    <Space><Button type="primary" loading={busy} disabled={!part || price == null} onClick={() => void save()}>保存</Button><Button onClick={onClose}>收起</Button></Space>
    {text && !part && <span className="presales-wide presales-muted">请从下拉中选择已建档的型号{canCreatePart && <Button type="link" disabled={busy} onClick={() => setCreating(true)}>新建 PN 档案</Button>}</span>}
    {creating && <CreatePartModal compact pn={text.trim()} onClose={() => setCreating(false)} onSelected={(selected) => { setPart(selected); setText(selected.pn_std); }} />}
  </div>;
}
