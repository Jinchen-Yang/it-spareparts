import { useEffect, useRef, useState } from "react";
import { Alert, App, AutoComplete, Button, Collapse, Form, Input, Select, Space, Typography } from "antd";
import { masterCategories, type CategoryNode, type MasterCreateInput, type MasterSpecField } from "../../api";
import type { PartSuggestion } from "../../api/search";
import LivePartSearch from "./LivePartSearch";
import { buildSpecUpdates, type SpecDraft } from "./partCreate";
import { useApiError } from "../ApiErrorFeedback";
import { parseApiError } from "../../utils/apiError";

type Duplicate = { pn_std: string; description?: string | null; reason?: string };
interface Props {
  initialPn: string;
  initialDescription?: string | null;
  submitLabel: string;
  disabled?: boolean;
  compact?: boolean;
  onCheck: (pn: string) => Promise<Duplicate[]>;
  onCreate: (input: MasterCreateInput, specFields: MasterSpecField[]) => Promise<void>;
  onSelect: (part: PartSuggestion) => Promise<void>;
  onBusyChange?: (busy: boolean) => void;
  onRefresh?: () => Promise<void>;
}

/** 两种建档入口共用资料表单，各自保留原有提交、权限与事务边界。 */
export default function PartRegistrationForm({ initialPn, initialDescription, submitLabel, compact = false, disabled = false, onCheck, onCreate, onSelect, onBusyChange, onRefresh }: Props) {
  const handleError = useApiError();
  const { modal } = App.useApp();
  const [draft, setDraft] = useState<MasterCreateInput>({ pn_std: initialPn, description: initialDescription ?? "", brand: "", machine_or_part: "备件" });
  const [categories, setCategories] = useState<CategoryNode[]>([]);
  const [specFields, setSpecFields] = useState<MasterSpecField[]>([]);
  const [rows, setRows] = useState<SpecDraft[]>([]);
  const nextRow = useRef(0);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [reload, setReload] = useState(0);
  const [duplicates, setDuplicates] = useState<Duplicate[]>();
  const [selected, setSelected] = useState<PartSuggestion[]>([]);
  const [busy, setBusy] = useState(false);
  const [validation, setValidation] = useState("");
  const [versionConflict, setVersionConflict] = useState(false);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    let disposed = false;
    setLoading(true); setLoadFailed(false);
    void masterCategories().then(({ data }) => {
      if (disposed) return;
      setCategories(data.categories); setSpecFields(data.spec_fields ?? []);
    }).catch((error: unknown) => {
      if (!disposed) { setLoadFailed(true); void handleError(error); }
    }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [reload, handleError]);

  const change = (fields: Partial<MasterCreateInput>) => {
    setDraft((previous) => ({ ...previous, ...fields })); setValidation("");
    if (fields.pn_std !== undefined) { setDuplicates(undefined); setSelected([]); }
  };
  const perform = async (action: () => Promise<void>) => {
    if (disabled || inFlight.current) return;
    inFlight.current = true; setBusy(true); onBusyChange?.(true); setValidation("");
    try { await action(); }
    catch (error) {
      if (!mounted.current) return;
      const detail = (error as { response?: { data?: { detail?: { near_duplicates?: Duplicate[] } } } })?.response?.data?.detail;
      if (detail?.near_duplicates) setDuplicates(detail.near_duplicates);
      if (parseApiError(error).kind === "version_conflict") setVersionConflict(true);
      await handleError(error);
    } finally {
      inFlight.current = false;
      if (mounted.current) { setBusy(false); onBusyChange?.(false); }
    }
  };
  const check = () => perform(async () => {
    const found = await onCheck(draft.pn_std.trim());
    if (mounted.current) { setDuplicates(found); setSelected([]); }
  });
  const create = () => {
    if (loading || loadFailed || duplicates?.length !== 0) return;
    let specs: ReturnType<typeof buildSpecUpdates>;
    try { specs = buildSpecUpdates(rows, specFields); }
    catch (error) { setValidation((error as Error).message); return; }
    const input: MasterCreateInput = { ...draft, pn_std: draft.pn_std.trim(),
      description: draft.description?.trim() || null, brand: draft.brand?.trim() || null,
      ...(Object.keys(specs).length ? { spec_updates: specs } : {}) };
    void perform(() => onCreate(input, specFields));
  };
  const changeRow = (id: number, values: Partial<SpecDraft>) => {
    setRows((previous) => previous.map((row) => row.id === id ? { ...row, ...values } : row)); setValidation("");
  };
  return <Form layout="vertical" disabled={disabled || busy}>
    <Alert type="info" showIcon message="先查重，再绑定已有 PN 或新建" style={{ marginBottom: 16 }} />
    {versionConflict && onRefresh && <Alert type="warning" showIcon message="该行已变化，建档填写内容仍保留。"
      description="请先复制需要保留的资料，再重新加载该行。"
      action={<Button onClick={() => modal.confirm({ title: "放弃当前建档资料并重新加载？", okText: "重新加载", cancelText: "继续保留",
        onOk: () => perform(async () => { await onRefresh(); if (mounted.current) setVersionConflict(false); }),
      })}>重新加载该行</Button>} style={{ marginBottom: 16 }} />}
    <Form.Item label="PN" required>
      <Space.Compact style={{ width: "100%" }}>
        <Input aria-label={`${initialPn}建档 PN`} value={draft.pn_std} maxLength={128}
          onChange={(event) => change({ pn_std: event.target.value })} />
        <Button loading={busy} disabled={disabled || !draft.pn_std.trim()} onClick={() => void check()}>查重</Button>
      </Space.Compact>
    </Form.Item>
    {duplicates && (duplicates.length ? <div style={{ marginBottom: 16 }}>
      <Alert type="warning" showIcon message="发现相同或近似型号，请核对后选用已有档案" />
      {duplicates.map((item) => <div key={item.pn_std}><Typography.Text>{item.pn_std} · {item.description} · {item.reason}</Typography.Text></div>)}
      <LivePartSearch inputProps={{ "aria-label": `${initialPn}绑定已有 PN`, disabled: disabled || busy }} value={selected} onChange={setSelected} />
      <Button disabled={disabled || !selected.length || busy} onClick={() => void perform(() => onSelect(selected[0]))}>绑定已有 PN</Button>
    </div> : <Alert type="success" showIcon message="查重无命中，可新建主数据" style={{ marginBottom: 16 }} />)}
    <Form.Item label="描述"><Input.TextArea aria-label={`${initialPn}建档描述`} value={draft.description ?? ""} rows={2}
      placeholder="完整型号描述、用途或适配信息" onChange={(event) => change({ description: event.target.value })} /></Form.Item>
    <Collapse ghost defaultActiveKey={compact ? [] : ["more"]} items={[{ key: "more", label: "更多资料（品牌、类别、参数）", children: <>
    <Form.Item label="简介"><Input.TextArea aria-label={`${initialPn}建档简介`} value={draft.summary ?? ""} rows={2}
      placeholder="便于快速识别的一段简介" onChange={(event) => change({ summary: event.target.value })} /></Form.Item>
    <div className="presales-form">
      <Form.Item label="品牌"><Input aria-label={`${initialPn}建档品牌`} maxLength={128} value={draft.brand ?? ""}
        onChange={(event) => change({ brand: event.target.value })} /></Form.Item>
      <Form.Item label="制造商"><Input aria-label={`${initialPn}建档制造商`} maxLength={128} value={draft.manufacturer ?? ""}
        onChange={(event) => change({ manufacturer: event.target.value })} /></Form.Item>
      <Form.Item label="一级类别"><Select aria-label={`${initialPn}建档一级类别`} loading={loading} disabled={disabled || busy || loading || loadFailed}
        allowClear showSearch optionFilterProp="label" placeholder="选择主数据类别" value={draft.category_major ?? undefined}
        options={categories.map((category) => ({ value: category.name, label: category.name }))}
        onChange={(value) => change({ category_major: value ?? null, category_minor: null })} /></Form.Item>
      <Form.Item label="二级类别"><Select aria-label={`${initialPn}建档二级类别`} disabled={disabled || busy || loading || loadFailed || !draft.category_major}
        allowClear showSearch optionFilterProp="label" placeholder="选择二级类别" value={draft.category_minor ?? undefined}
        options={(categories.find((category) => category.name === draft.category_major)?.children ?? []).map((child) => ({ value: child.name, label: child.name }))}
        onChange={(value) => change({ category_minor: value ?? null })} /></Form.Item>
      <Form.Item label="整机 / 备件"><Select aria-label={`${initialPn}建档类型`} value={draft.machine_or_part ?? "备件"}
        options={[{ value: "备件", label: "备件" }, { value: "整机", label: "整机" }]}
        onChange={(value) => change({ machine_or_part: value })} /></Form.Item>
      <Form.Item label="单位"><Input aria-label={`${initialPn}建档单位`} maxLength={16} placeholder="如：个、块、台" value={draft.unit ?? ""}
        onChange={(event) => change({ unit: event.target.value })} /></Form.Item>
    </div>
    {loadFailed && <Alert type="error" showIcon message="主数据类别与参数选项加载失败，填写内容已保留。"
      action={<Button onClick={() => setReload((value) => value + 1)}>重试加载</Button>} />}
    <Form.Item label="参数（名称 / 值）">
      <Space direction="vertical" style={{ width: "100%" }}>
        {rows.map((row, index) => <Space.Compact key={row.id} style={{ width: "100%" }}>
          <AutoComplete aria-label={`参数 ${index + 1} 名称`} style={{ width: "40%" }} value={row.name}
            options={specFields.map((field) => ({ value: field.label }))}
            filterOption={(value, option) => String(option?.value ?? "").toLowerCase().includes(value.toLowerCase())}
            onChange={(value) => changeRow(row.id, { name: value })} placeholder="如：核心数、容量" />
          <Input aria-label={`参数 ${index + 1} 值`} style={{ width: "50%" }} maxLength={128} value={row.value}
            placeholder="如：32、80GB" onChange={(event) => changeRow(row.id, { value: event.target.value })} />
          <Button aria-label={`删除参数 ${index + 1}`} onClick={() => setRows((previous) => previous.filter((item) => item.id !== row.id))}>删除</Button>
        </Space.Compact>)}
        <Button type="dashed" disabled={disabled || busy || loading || loadFailed || rows.length >= 50}
          onClick={() => setRows((previous) => [...previous, { id: ++nextRow.current, name: "", value: "" }])}>添加参数</Button>
      </Space>
    </Form.Item>
    </> }]} />
    {loadFailed && compact && <Alert type="error" message="建档资料加载失败，请重试" action={<Button onClick={() => setReload((value) => value + 1)}>重试加载</Button>} />}
    {validation && <Alert type="warning" showIcon message={validation} style={{ marginBottom: 16 }} />}
    {duplicates?.length === 0 && <Button type="primary" loading={busy} disabled={disabled || loading || loadFailed} onClick={create}>{submitLabel}</Button>}
  </Form>;
}
