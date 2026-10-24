import { urlPageField, useUrlState } from "../hooks/useUrlState";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { App, Alert, Button, Form, Input, Modal, Select, Space, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import PageHeader from "../components/PageHeader";
import BatchNormalizeModal from "../components/BatchNormalizeModal";
import api, {
  SPEC_SOURCE_LABEL,
  type CategoryNode, type ClassifySuggestion, type MasterFields, type NearDup, type PartHit,
} from "../api";
import {
  fetchMasterEditState, masterEditConflictLines, masterEditErrorMessage, rebaseMasterEditDraft, saveMasterEdit,
  type MasterEditPayload, type MasterEditSnapshot,
} from "../api/search";

type FormState = MasterFields & { pn_std: string };
const EMPTY: FormState = {
  pn_std: "", description: "", brand: "", manufacturer: "", summary: "",
  category_major: null, category_minor: null,
  machine_or_part: "备件", unit: "",
};
const EDITABLE = ["description", "brand", "manufacturer", "summary",
  "category_major", "category_minor", "machine_or_part", "unit"] as const;
const PAGE_SIZE = 20;
const normalized = (value: string | null | undefined) => value?.trim() || null;

/** 备件主数据：采购可搜全部型号 → 编辑描述/品类/品牌，或新建 PN。
 * 人工改过的字段后端 locked_fields 标记，重导（氚云）不覆盖。 */
export default function MasterDataPage() {
  const { message } = App.useApp();
  const [{ q, page: pageParam }, patchUrl] = useUrlState({ q: { defaultValue: "" }, page: urlPageField });
  const page = Number(pageParam);
  const [searchInput, setSearchInput] = useState(q);
  useEffect(() => { setSearchInput(q); }, [q]);
  const [rows, setRows] = useState<PartHit[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [cats, setCats] = useState<CategoryNode[]>([]);

  const [mode, setMode] = useState<null | "create" | "edit">(null);
  const [pnTouched, setPnTouched] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [init, setInit] = useState<FormState>(EMPTY);
  const [near, setNear] = useState<NearDup[]>([]);
  const [suggest, setSuggest] = useState<ClassifySuggestion | null>(null);
  const [saving, setSaving] = useState(false);
  const [batchOpen, setBatchOpen] = useState(false);
  const [baseline, setBaseline] = useState<MasterEditSnapshot | null>(null);
  const [editLoading, setEditLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [conflictLines, setConflictLines] = useState<string[]>([]);
  const [canRebase, setCanRebase] = useState(false);
  const [rebasing, setRebasing] = useState(false);
  const token = useRef(localStorage.getItem("token"));
  const mounted = useRef(true);
  const editSeq = useRef(0);
  const searchSeq = useRef(0);
  const draftSeq = useRef(0);
  const editTarget = useRef<PartHit | null>(null);
  const sessionIsCurrent = () => mounted.current && Boolean(token.current)
    && localStorage.getItem("token") === token.current;
  const sessionConfig = () => ({ headers: { Authorization: `Bearer ${token.current}` } });

  useLayoutEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      ++editSeq.current;
      ++searchSeq.current;
    };
  }, []);

  useEffect(() => {
    if (!sessionIsCurrent()) return;
    api.get<{ categories: CategoryNode[] }>("/parts/master/categories", sessionConfig())
      .then((r) => { if (sessionIsCurrent()) setCats(r.data.categories); }).catch(() => undefined);
  }, []);

  // 服务端分页浏览：browse=true 走 search_parts（跨 PN+描述分词模糊 + 真实 total + 可翻到底），
  // 不再是「只取前 50 条、前端切 3 页」。宋总 2026-07-06 反馈：主数据池子大，模糊搜索每次只有 3 页。
  const doSearch = async (query: string, p = 1) => {
    if (!sessionIsCurrent()) return;
    const seq = ++searchSeq.current;
    const isCurrent = () => seq === searchSeq.current && sessionIsCurrent();
    setLoading(true);
    try {
      const { data } = await api.get<{ items: PartHit[]; total: number }>("/parts/search", {
        ...sessionConfig(), params: { q: query, page: p, page_size: PAGE_SIZE, browse: true },
      });
      if (!isCurrent()) return;
      setRows(data.items || []);
      setTotal(data.total || 0);
    } catch (e: any) {
      if (isCurrent()) message.error(masterEditErrorMessage(e, "搜索失败"));
    } finally {
      if (isCurrent()) setLoading(false);
    }
  };

  useEffect(() => {
    void doSearch(q, page);
    return () => { ++searchSeq.current; };
    // 已提交状态来自 URL；账号与请求代次保护仍由 doSearch 负责。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, page]);

  const search = (raw: string) => {
    // URL 只保存规范化后的关键词，避免首尾空白产生不同链接。
    const query = raw.trim();
    setSearchInput(query);
    if (query === q && page === 1) void doSearch(q, page);
    else patchUrl({ q: query, page: "1" });
  };

  const l2opts = (l1?: string | null) =>
    (cats.find((c) => c.name === l1)?.children || []).map((ch) => ({ label: ch.name, value: ch.name }));

  const openCreate = () => {
    if (!sessionIsCurrent()) return;
    ++editSeq.current; ++draftSeq.current;
    editTarget.current = null;
    setBaseline(null); setEditLoading(false); setLoadError(null); setSaveError(null); setSaving(false);
    setConflictLines([]);
    setCanRebase(false); setRebasing(false);
    setPnTouched(false);
    setForm(EMPTY); setInit(EMPTY); setNear([]); setSuggest(null); setMode("create");
  };
  const closeEditor = () => {
    ++editSeq.current; ++draftSeq.current;
    setMode(null); setSaving(false); setEditLoading(false);
    setCanRebase(false); setRebasing(false);
  };
  const openEdit = async (part: PartHit) => {
    if (!sessionIsCurrent()) return;
    const seq = ++editSeq.current;
    ++draftSeq.current;
    const isCurrent = () => seq === editSeq.current && sessionIsCurrent();
    editTarget.current = part;
    setMode("edit"); setForm({ ...EMPTY, pn_std: part.pn_std });
    setBaseline(null); setNear([]); setSuggest(null); setLoadError(null); setSaveError(null);
    setConflictLines([]);
    setCanRebase(false); setRebasing(false);
    setEditLoading(true); setSaving(false);
    try {
      if (!part.id) throw new Error("缺少稳定型号 ID，请刷新列表后重试");
      const snapshot = await fetchMasterEditState(part.id, token.current!);
      if (!isCurrent()) return;
      if (snapshot.part_id !== part.id || snapshot.identity.status !== "active") {
        throw new Error("型号身份已变化，请刷新列表后重新打开编辑");
      }
      const p = snapshot.fields;
      const f: FormState = {
        pn_std: snapshot.pn_std, description: p.description ?? "", brand: p.brand ?? "",
        manufacturer: p.manufacturer ?? "", summary: p.summary ?? "",
        category_major: p.category_major ?? null, category_minor: p.category_minor ?? null,
        machine_or_part: p.machine_or_part, unit: p.unit ?? "",
      };
      setBaseline(snapshot); setForm(f); setInit(f);
    } catch (error: any) {
      if (isCurrent()) setLoadError(masterEditErrorMessage(error, error?.message || "加载型号失败"));
    } finally {
      if (isCurrent()) setEditLoading(false);
    }
  };

  const set = (patch: Partial<FormState>) => {
    if (saving || rebasing || !sessionIsCurrent()) return;
    ++draftSeq.current;
    setForm((f) => ({ ...f, ...patch }));
    setSuggest(null);
  };

  const onSuggest = async () => {
    if (!form.description) { setSuggest(null); return; }
    if (!sessionIsCurrent()) return;
    const seq = editSeq.current;
    const draft = draftSeq.current;
    try {
      const { data } = await api.get<{ suggestion: ClassifySuggestion | null }>("/parts/master/suggest", {
        ...sessionConfig(), params: { description: form.description, pn: form.pn_std, brand: form.brand || "" },
      });
      if (seq !== editSeq.current || draft !== draftSeq.current || !sessionIsCurrent()) return;
      setSuggest(data.suggestion);
    } catch { /* 建议失败不打断录入 */ }
  };
  const applyNormalize = () => {
    if (saving || rebasing || !sessionIsCurrent() || !suggest || suggest.whole_system) return;
    set({
      description: suggest.canonical_description ?? form.description,
      category_major: suggest.category_l1 ?? form.category_major,
      category_minor: suggest.category_l2 ?? form.category_minor,
      brand: suggest.brand_norm ?? form.brand,
      machine_or_part: "备件",
    });
  };

  const onCheckDup = async () => {
    if (mode !== "create" || !form.pn_std.trim()) { setNear([]); return; }
    if (!sessionIsCurrent()) return;
    const seq = editSeq.current;
    const draft = draftSeq.current;
    try {
      const { data } = await api.get<{ near_duplicates: NearDup[] }>("/parts/master/check", {
        ...sessionConfig(), params: { pn_std: form.pn_std.trim() },
      });
      if (seq !== editSeq.current || draft !== draftSeq.current || !sessionIsCurrent()) return;
      setNear(data.near_duplicates || []);
    } catch { /* ignore */ }
  };

  const save = async (force = false) => {
    if (!sessionIsCurrent() || !mode || saving || rebasing || editLoading || loadError) return;
    const seq = editSeq.current;
    const isCurrent = () => seq === editSeq.current && sessionIsCurrent();
    setSaving(true);
    setSaveError(null);
    setConflictLines([]);
    setCanRebase(false);
    try {
      if (mode === "create") {
        const { data } = await api.post<{
          created: boolean; pn_std?: string; near_duplicates?: NearDup[]; message?: string;
        }>("/parts/master", { ...form, pn_std: form.pn_std.trim(), force }, sessionConfig());
        if (!isCurrent()) return;
        if (!data.created) {
          setNear(data.near_duplicates || []);
          message.warning(data.message || "存在近似型号，请确认");
          return;
        }
        message.success(`已新建 ${data.pn_std}`);
      } else {
        if (!baseline) return;
        const diff: MasterEditPayload = {
          part_id: baseline.part_id, pn_std: baseline.pn_std, base_snapshot: baseline,
        };
        EDITABLE.forEach((k) => {
          if (normalized(form[k]) !== normalized(init[k])) diff[k] = normalized(form[k]);
        });
        if (Object.keys(diff).length === 3) { message.info("没有改动"); closeEditor(); return; }
        const { data } = await saveMasterEdit(diff, token.current!);
        if (!isCurrent()) return;
        message.success(`已保存（重导不再覆盖：${data.updated.join("、")}）`);
      }
      closeEditor();
      doSearch(q, page);
    } catch (e: any) {
      if (isCurrent()) {
        setSaveError(masterEditErrorMessage(e, "保存失败，请重试"));
        setConflictLines(masterEditConflictLines(e));
        setCanRebase(e?.response?.status === 409
          && e?.response?.data?.detail?.code === "field_conflict");
      }
    } finally {
      if (isCurrent()) setSaving(false);
    }
  };

  const rebaseDraft = async () => {
    if (!canRebase || mode !== "edit" || !baseline || saving || rebasing || !sessionIsCurrent()) return;
    const seq = ++editSeq.current;
    ++draftSeq.current;
    const isCurrent = () => seq === editSeq.current && sessionIsCurrent();
    setRebasing(true);
    try {
      const latest = await fetchMasterEditState(baseline.part_id, token.current!);
      if (!isCurrent()) return;
      const fields = rebaseMasterEditDraft(baseline, latest, form);
      setBaseline(latest);
      setInit({ pn_std: latest.pn_std, ...latest.fields });
      setForm({ pn_std: latest.pn_std, ...fields });
      setSuggest(null); setSaveError(null); setConflictLines([]); setCanRebase(false);
      message.info("已更新编辑基线并保留草稿，请核对后再次保存");
    } catch (error: any) {
      if (isCurrent()) {
        setSaveError(masterEditErrorMessage(error, error?.message || "基线加载失败，请重试"));
      }
    } finally {
      if (isCurrent()) setRebasing(false);
    }
  };

  const columns: ColumnsType<PartHit> = [
    {
      title: "PN", dataIndex: "pn_std", width: 230,
      render: (v, r) => (
        <Space size={4}>
          <span style={{ fontWeight: 500 }}>{v}</span>
          {r.needs_review && <Tag color="orange">待复核</Tag>}
        </Space>
      ),
    },
    { title: "描述", dataIndex: "description", ellipsis: true },
    { title: "品牌", dataIndex: "brand", width: 130 },
    {
      title: "品类", dataIndex: "category_major", width: 150,
      render: (v) => v || <span style={{ color: "#bbb" }}>未分类</span>,
    },
    {
      title: "操作", width: 90, fixed: "right",
      render: (_, r) => <Button size="small" onClick={() => openEdit(r)}>编辑</Button>,
    },
  ];

  const fields = (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {mode === "create" ? (
        <Form component={false} layout="vertical" fields={[{
          name: "master-edit-pn", value: form.pn_std,
          errors: pnTouched && !form.pn_std.trim() ? ["请输入 PN"] : [],
        }]}>
          <Form.Item name="master-edit-pn" label="PN（产品唯一标识）" required>
            <Input placeholder="如 ST8000NM000A" disabled={saving || rebasing}
              onChange={(e) => set({ pn_std: e.target.value })}
              onBlur={() => { setPnTouched(true); void onCheckDup(); }} />
          </Form.Item>
        </Form>
      ) : (
        <div><label htmlFor="master-edit-pn" style={{ display: "block", marginBottom: 4 }}>PN</label>
          <Input id="master-edit-pn" value={form.pn_std} disabled /></div>
      )}

      {near.length > 0 && (
        <Alert type="warning" showIcon
          message="存在近似型号，确认不是重复再继续"
          description={<ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {near.map((n) => <li key={n.pn_std}><b>{n.pn_std}</b> — {n.description || "无描述"}（{n.reason}）</li>)}
          </ul>} />
      )}

      <div>
        <label htmlFor="master-edit-description" style={{ display: "block", marginBottom: 4 }}>描述</label>
        <Input.TextArea id="master-edit-description" value={form.description ?? ""} rows={2} onBlur={onSuggest} disabled={saving || rebasing}
          onChange={(e) => set({ description: e.target.value })}
          placeholder="如 希捷 8TB 7.2K 3.5 SATA 企业级硬盘" />
        {suggest && !suggest.whole_system && (suggest.canonical_description || suggest.category_l1) && (
          <div style={{ marginTop: 8, fontSize: 13, background: "#f6f3ee",
                        padding: "8px 10px", borderRadius: 6 }}>
            {suggest.canonical_description && (
              <div>标准描述：<span style={{ fontWeight: 500 }}>{suggest.canonical_description}</span>
                {suggest.review_status === "AUTO_OK"
                  ? <Tag color="green" style={{ marginLeft: 6 }}>自动通过</Tag>
                  : <Tag color="orange" style={{ marginLeft: 6 }}>需复核</Tag>}
              </div>
            )}
            <div style={{ marginTop: 4, display: "flex", alignItems: "center", gap: 4, flexWrap: "wrap" }}>
              {suggest.category_l1 && (
                <span>分类 <Tag color="blue">{suggest.category_l1}{suggest.category_l2 ? ` / ${suggest.category_l2}` : ""}</Tag></span>
              )}
              {suggest.brand_norm && (
                <span>品牌 <Tag>{suggest.brand_norm}{suggest.brand_zh ? `（${suggest.brand_zh}）` : ""}</Tag></span>
              )}
              <Button size="small" type="primary" ghost disabled={saving || rebasing} onClick={applyNormalize}>一键规范化</Button>
            </div>
            {suggest.structured_specs && Object.keys(suggest.structured_specs).length > 0 && (
              <div style={{ marginTop: 6, display: "flex", alignItems: "center", gap: 4, flexWrap: "wrap" }}>
                <span style={{ color: "var(--mb-text-3)", fontSize: 12 }}>字段依据</span>
                {Object.entries(suggest.structured_specs).map(([k, f]) => {
                  const safe = f.source === "DESCRIPTION_EXPLICIT" || f.source === "MODEL_DICTIONARY";
                  return (
                    <Tooltip key={k} title={`${SPEC_SOURCE_LABEL[f.source] || f.source} · 证据：${f.evidence}`}>
                      <Tag color={safe ? "blue" : "gold"} style={{ marginInlineEnd: 0, fontSize: 12 }}>
                        {k}={f.value}
                      </Tag>
                    </Tooltip>
                  );
                })}
              </div>
            )}
            {suggest.validation_errors && suggest.validation_errors.length > 0 && (
              <div style={{ marginTop: 4, fontSize: 12, color: "#c0392b" }}>校验：{suggest.validation_errors.join("；")}</div>
            )}
          </div>
        )}
        {suggest?.whole_system && (
          <div style={{ marginTop: 6, fontSize: 13, color: "#d46b08" }}>
            识别为整机/扩展柜——通常不作为备件主数据（可改类型为"整机"）。
          </div>
        )}
      </div>

      <Space size={12} style={{ width: "100%" }} wrap>
        <div style={{ flex: 1, minWidth: 220 }}>
          <label htmlFor="master-edit-manufacturer" style={{ display: "block", marginBottom: 4 }}>制造商</label>
          <Input
            id="master-edit-manufacturer"
            value={form.manufacturer ?? ""}
            placeholder="如 希捷 / 惠普"
            disabled={saving || rebasing}
            onChange={(e) => set({ manufacturer: e.target.value })}
          />
        </div>
      </Space>

      <div>
        <label htmlFor="master-edit-summary" style={{ display: "block", marginBottom: 4 }}>简介</label>
        <Input.TextArea
          id="master-edit-summary"
          value={form.summary ?? ""}
          rows={2}
          placeholder="面向商城展示的中文简介"
          disabled={saving || rebasing}
          onChange={(e) => set({ summary: e.target.value })}
        />
      </div>

      <Space size={12} style={{ width: "100%" }} wrap>
        <div style={{ minWidth: 180 }}>
          <label htmlFor="master-edit-major" style={{ display: "block", marginBottom: 4 }}>品类（大类）</label>
          <Select id="master-edit-major" style={{ width: 180 }} allowClear showSearch value={form.category_major ?? undefined} disabled={saving || rebasing}
            placeholder="选择大类" options={cats.map((c) => ({ label: c.name, value: c.name }))}
            onChange={(v) => set({ category_major: v ?? null, category_minor: null })} />
        </div>
        <div style={{ minWidth: 180 }}>
          <label htmlFor="master-edit-minor" style={{ display: "block", marginBottom: 4 }}>品类（二级）</label>
          <Select id="master-edit-minor" style={{ width: 180 }} allowClear showSearch value={form.category_minor ?? undefined}
            placeholder="选择二级" options={l2opts(form.category_major)}
            onChange={(v) => set({ category_minor: v ?? null })} disabled={saving || rebasing || !form.category_major} />
        </div>
      </Space>

      <Space size={12} wrap>
        <div><label htmlFor="master-edit-brand" style={{ display: "block", marginBottom: 4 }}>品牌</label>
          <Input id="master-edit-brand" style={{ width: 180 }} value={form.brand ?? ""} placeholder="如 Seagate / HPE" disabled={saving || rebasing}
            onChange={(e) => set({ brand: e.target.value })} /></div>
        <div><label htmlFor="master-edit-type" style={{ display: "block", marginBottom: 4 }}>类型</label>
          <Select id="master-edit-type" style={{ width: 120 }} value={form.machine_or_part ?? undefined} placeholder="未填" disabled={saving || rebasing}
            options={[{ value: "备件" }, { value: "整机" }]}
            onChange={(v) => set({ machine_or_part: v })} /></div>
        <div><label htmlFor="master-edit-unit" style={{ display: "block", marginBottom: 4 }}>单位</label>
          <Input id="master-edit-unit" style={{ width: 100 }} value={form.unit ?? ""} placeholder="个" disabled={saving || rebasing}
            onChange={(e) => set({ unit: e.target.value })} /></div>
      </Space>
    </Space>
  );

  return (
    <div>
      <PageHeader
        title="备件主数据"
        subtitle="采购可编辑型号的描述/品类/品牌，或新建 PN；改过的内容氚云重导不会覆盖。"
        extra={
          <Space>
            <Button onClick={() => setBatchOpen(true)}>批量规范化</Button>
            <Button type="primary" onClick={openCreate}>新建 PN</Button>
          </Space>
        }
      />
      <Input.Search
        placeholder="搜 PN / 描述 / 品牌（留空浏览全部）" allowClear enterButton
        style={{ maxWidth: 460, marginBottom: 16 }}
        value={searchInput} onChange={(e) => setSearchInput(e.target.value)} onSearch={search}
      />
      <Table
        rowKey="pn_std" size="small" columns={columns} dataSource={rows} loading={loading}
        scroll={{ x: 760 }}
        pagination={{
          current: page, pageSize: PAGE_SIZE, total, showSizeChanger: false,
          showTotal: (t) => `共 ${t} 个匹配`,
          onChange: (p) => patchUrl({ page: String(p) }),
        }}
      />

      <Modal
        open={mode !== null}
        title={mode === "create" ? "新建 PN" : `编辑 ${form.pn_std}`}
        onCancel={closeEditor}
        confirmLoading={saving}
        width={560}
        footer={
          mode === "create" && near.length > 0
            ? [
              <Button key="c" onClick={closeEditor}>取消</Button>,
              <Button key="f" danger loading={saving} onClick={() => save(true)}>确认不重复，强制新建</Button>,
            ]
            : [
              <Button key="c" onClick={closeEditor}>取消</Button>,
              <Button key="s" type="primary" loading={saving}
                disabled={editLoading || rebasing || Boolean(loadError) || (mode === "edit" && !baseline)
                  || (mode === "create" && !form.pn_std.trim())}
                onClick={() => save(false)}>保存</Button>,
            ]
        }
      >
        {editLoading ? <div role="status">正在加载型号资料…</div> : loadError ? (
          <Alert type="error" showIcon message={loadError}
            action={<Button onClick={() => editTarget.current && openEdit(editTarget.current)}>重试</Button>} />
        ) : fields}
        {saveError && <Alert type="error" showIcon style={{ marginTop: 12 }} message={saveError}
          description={<div style={{ overflowWrap: "anywhere" }}>
            {conflictLines.map((line, index) => <div key={index} style={{ whiteSpace: "pre-wrap" }}>{line}</div>)}
            <div>草稿已保留；请核对当前值与本次修改。</div>
            {canRebase && <Button loading={rebasing} disabled={saving} onClick={rebaseDraft}
              style={{ height: "auto", whiteSpace: "normal", marginTop: 8, maxWidth: "100%" }}>
              以当前值为新基线并保留我的草稿
            </Button>}
          </div>} />}
      </Modal>

      <BatchNormalizeModal
        open={batchOpen}
        onClose={() => setBatchOpen(false)}
        onApplied={() => doSearch(q, page)}
      />
    </div>
  );
}
