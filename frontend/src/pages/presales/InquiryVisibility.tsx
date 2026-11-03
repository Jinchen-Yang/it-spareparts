import { useEffect, useRef, useState } from "react";
import { Alert, App, Button, Modal, Select, Space, Spin } from "antd";
import { listAudiencePeople, type AudiencePerson } from "../../api/audience";
import { presales, type InquiryVisibility } from "../../api/presales";
import { notifyPoolChanged } from "../../api/presalesPool";
import { parseApiError } from "../../utils/apiError";
import { usePresalesError } from "./shared";

export const INQUIRY_VISIBILITY_HELP = "默认本人、采购／报价人和管理员可见，可额外指定其他人员。不选则不增加查看者。草稿仅本人和管理员可见。";

export function InquiryPeopleSelect({ value, onChange, savedPeople = [], disabled = false }: {
  value: string[];
  onChange: (value: string[]) => void;
  savedPeople?: InquiryVisibility["visible_to"];
  disabled?: boolean;
}) {
  const [people, setPeople] = useState<AudiencePerson[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setFailed(false);
    const timer = window.setTimeout(() => {
      listAudiencePeople(query, controller.signal).then((rows) => {
        if (!controller.signal.aborted) setPeople((previous) => [...new Map([...previous, ...rows].map((person) => [person.id, person])).values()]);
      }).catch(() => {
        if (!controller.signal.aborted) setFailed(true);
      }).finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    }, query ? 200 : 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [query, retry]);
  const options = [...new Map([
    ...savedPeople.map((person) => ({ value: person.id, label: person.name })),
    ...people.map((person) => ({ value: person.id, label: person.display_name || person.id })),
  ].map((option) => [option.value, option])).values()];
  return <Space direction="vertical" style={{ width: "100%" }}>
    <Select aria-label="额外可见人员" mode="multiple" allowClear showSearch style={{ width: "100%" }}
      placeholder="可选，搜索姓名或账号" value={value} disabled={disabled} loading={loading}
      options={options} filterOption={(input, option) => `${option?.label ?? ""} ${option?.value ?? ""}`.toLocaleLowerCase().includes(input.toLocaleLowerCase())}
      onSearch={setQuery} onChange={onChange} />
    {failed && <Alert type="error" message="人员加载失败" action={<Button size="small" onClick={() => setRetry((previous) => previous + 1)}>重试</Button>} />}
  </Space>;
}

export default function InquiryVisibilityDialog({ inquiryId, inquiryNo, onClose, onSaved, onDenied }: {
  inquiryId: number;
  inquiryNo: string;
  onClose: () => void;
  onSaved: () => void;
  onDenied: () => void;
}) {
  const { message } = App.useApp();
  const handleError = usePresalesError();
  const [saved, setSaved] = useState<InquiryVisibility>();
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [retry, setRetry] = useState(0);
  const deniedCallback = useRef(onDenied);
  deniedCallback.current = onDenied;
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setFailed(false); setSaved(undefined); setSelected([]);
    presales.getInquiryVisibility(inquiryId, controller.signal).then((data) => {
      if (controller.signal.aborted) return;
      setSaved(data); setSelected(data.visible_to.map((person) => person.id)); setConflict(false);
    }).catch((error) => {
      if (controller.signal.aborted) return;
      setFailed(true);
      const info = parseApiError(error);
      if (info.kind === "permission" || info.status === 404) deniedCallback.current();
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [inquiryId, retry]);
  const save = async () => {
    if (!saved || loading || busy || conflict) return;
    setBusy(true);
    try {
      await presales.updateInquiryVisibility(inquiryId, { expected_version: saved.version, visible_to: selected });
      void message.success("可见人员已更新");
      notifyPoolChanged(); onSaved(); onClose();
    } catch (error) {
      const info = parseApiError(error);
      if (info.kind === "version_conflict" || info.status === 409) setConflict(true);
      if (info.kind === "permission" || info.status === 404) { onDenied(); onClose(); }
      await handleError(error);
    } finally { setBusy(false); }
  };
  return <Modal open title={`${inquiryNo} · 可见人员`} okText="保存可见人员" cancelText="取消"
    onCancel={() => { if (!busy) onClose(); }} onOk={() => void save()} confirmLoading={busy}
    okButtonProps={{ disabled: !saved || loading || failed || conflict }} cancelButtonProps={{ disabled: busy }} closable={!busy} maskClosable={!busy}>
    <p>{INQUIRY_VISIBILITY_HELP}</p>
    {loading ? <Spin aria-label="加载可见人员" /> : <InquiryPeopleSelect value={selected} onChange={setSelected} savedPeople={saved?.visible_to} disabled={busy || failed || conflict} />}
    {failed && <Alert type="error" message="可见人员加载失败，请确认当前权限后重试" action={<Button onClick={() => setRetry((previous) => previous + 1)}>重试</Button>} />}
    {conflict && <Alert type="warning" message="询价已被修改，请重新加载可见人员后再保存"
      description="重新加载将替换当前未保存的选择。" action={<Button onClick={() => setRetry((previous) => previous + 1)}>重新加载可见人员</Button>} />}
  </Modal>;
}
