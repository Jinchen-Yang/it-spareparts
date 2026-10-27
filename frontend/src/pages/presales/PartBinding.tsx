import { useState } from "react";
import { Alert, Button, Input, Space, Tag, Typography } from "antd";
import { presales, type Inquiry, type InquiryLine, type NearDuplicate } from "../../api/presales";
import type { PartSuggestion } from "../../api/search";
import LivePartSearch from "../../components/parts/LivePartSearch";
import PartThumb from "../../components/gallery/PartThumb";
import PartQuoteBadges from "./PartQuoteBadges";
import { usePresalesError } from "./shared";

export default function PartBinding({ line, onBound, refresh }: { line: InquiryLine; onBound: (inquiry: Inquiry) => void; refresh: () => Promise<void> }) {
  const handlePresalesError = usePresalesError();
  const [pn, setPn] = useState(line.pn_raw);
  const [description, setDescription] = useState(line.description ?? "");
  const [brand, setBrand] = useState("");
  const [duplicates, setDuplicates] = useState<NearDuplicate[]>();
  const [selected, setSelected] = useState<PartSuggestion[]>([]);
  const [busy, setBusy] = useState(false);
  const check = async () => {
    setBusy(true);
    try { setDuplicates((await presales.partCheck(line.id, pn)).near_duplicates); }
    catch (error) { handlePresalesError(error, refresh); }
    finally { setBusy(false); }
  };
  const bind = async (create: boolean) => {
    if (!duplicates || (!create && !selected[0])) return;
    setBusy(true);
    try {
      onBound(create
        ? await presales.createPart(line.id, { expected_version: line.version, pn_std: pn,
          description: description || null, brand: brand || null })
        : await presales.bindPart(line.id, { expected_version: line.version, part_id: selected[0].part_id }));
    } catch (error) { setDuplicates(undefined); handlePresalesError(error, refresh); }
    finally { setBusy(false); }
  };
  return <Space direction="vertical" style={{ width: "100%" }}>
    <Tag color="warning">待建档：先查重，再绑定已有 PN 或新建</Tag>
    <Space wrap>
      <Input aria-label={`${line.pn_raw}建档 PN`} value={pn} disabled={busy} maxLength={256}
        onChange={(event) => { setPn(event.target.value); setDuplicates(undefined); setSelected([]); }} />
      <Button loading={busy} disabled={!pn.trim()} onClick={() => void check()}>查重</Button>
    </Space>
    {duplicates && (duplicates.length ? <>
      <Alert type="info" message="发现已有或近似型号，请在实时检索中选定后绑定" />
      {duplicates.map((item) => <Typography.Text key={item.pn_std}>{item.pn_std} · {item.reason}</Typography.Text>)}
      <LivePartSearch inputProps={{ "aria-label": `${line.pn_raw}绑定已有 PN` }} value={selected} onChange={setSelected}
        renderThumb={(item) => <PartThumb partId={item.part_id} pn={item.pn_std} />}
        renderBadges={(item) => <PartQuoteBadges partId={item.part_id} />} />
      <Button disabled={!selected.length || busy} onClick={() => void bind(false)}>绑定已有 PN</Button>
    </> : <>
      <Typography.Text>查重无命中，可新建主数据。</Typography.Text>
      <Input aria-label={`${line.pn_raw}建档描述`} placeholder="产品描述" value={description} disabled={busy}
        onChange={(event) => setDescription(event.target.value)} />
      <Input aria-label={`${line.pn_raw}建档品牌`} placeholder="品牌（可选）" value={brand} disabled={busy}
        onChange={(event) => setBrand(event.target.value)} />
      <Button loading={busy} onClick={() => void bind(true)}>新建并绑定 PN</Button>
    </>)}
  </Space>;
}

