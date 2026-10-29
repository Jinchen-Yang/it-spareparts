import { useEffect, useRef, useState } from "react";
import { App, Modal } from "antd";
import { masterCheck, masterCreate } from "../../api";
import type { PartSuggestion } from "../../api/search";
import PartRegistrationForm from "./PartRegistrationForm";
import { createdPartSuggestion } from "./partCreate";
import { useApiError } from "../ApiErrorFeedback";

export default function CreatePartModal({ pn, description, compact = false, onClose, onSelected }: {
  pn: string; description?: string | null; compact?: boolean; onClose: () => void; onSelected: (part: PartSuggestion) => void;
}) {
  const { message } = App.useApp();
  const handleError = useApiError();
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  const sessionToken = useRef(localStorage.getItem("token"));
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  return <Modal open title="新建 PN 档案" width={680} footer={null} onCancel={onClose}
    maskClosable={!busy} keyboard={!busy} closable={!busy} destroyOnHidden>
    <PartRegistrationForm compact={compact} initialPn={pn} initialDescription={description} submitLabel="新建并加入当前单据" onBusyChange={setBusy}
      onCheck={async (value) => (await masterCheck(value)).data.near_duplicates}
      onSelect={async (part) => { onSelected(part); onClose(); }}
      onCreate={async (input, fields) => {
        const response = await masterCreate(input);
        if (!mounted.current || localStorage.getItem("token") !== sessionToken.current) return;
        const { data } = response;
        if (!data.created) throw { response: { ...response, data: { detail: {
          code: "near_duplicates", message: "出现相同或近似型号，请核对并选用已有档案。", near_duplicates: data.near_duplicates,
        } } } };
        if (!data.id || !data.pn_std) {
          handleError.afterSuccess({ response }, "PN 建档");
          onClose();
          return;
        }
        onSelected(createdPartSuggestion(data.id, data.pn_std, input, fields));
        onClose();
        void message.success(`已新建 ${data.pn_std} 并加入当前单据`);
      }} />
  </Modal>;
}
