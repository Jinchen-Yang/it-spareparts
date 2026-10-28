import { presales, type Inquiry, type InquiryLine } from "../../api/presales";
import PartRegistrationForm from "../../components/parts/PartRegistrationForm";

export default function PartBinding({ line, onBound, refresh, disabled }: {
  line: InquiryLine; onBound: (inquiry: Inquiry) => void; refresh: () => Promise<void>; disabled?: boolean;
}) {
  return <PartRegistrationForm key={line.id} initialPn={line.pn_raw} initialDescription={line.description}
    submitLabel="新建并绑定 PN"
    disabled={disabled}
    onRefresh={refresh}
    onCheck={async (pn) => (await presales.partCheck(line.id, pn)).near_duplicates}
    onSelect={async (part) => onBound(await presales.bindPart(line.id, { expected_version: line.version, part_id: part.part_id }))}
    onCreate={async (input) => {
      const { force: _force, ...fields } = input;
      onBound(await presales.createPart(line.id, { ...fields, expected_version: line.version }));
    }} />;
}
