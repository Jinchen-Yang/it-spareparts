import { useEffect, useState } from "react";
import { Alert, Button, Input, Modal, Select, Space, Table, Tag, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import PageHeader from "../components/PageHeader";
import api, {
  masterCategories, masterCheck, masterCreate, masterEdit, masterSuggest, searchParts,
  type CategoryNode, type ClassifySuggestion, type MasterFields, type NearDup, type PartHit,
} from "../api";

type FormState = MasterFields & { pn_std: string };
const EMPTY: FormState = {
  pn_std: "", description: "", brand: "", category_major: null, category_minor: null,
  machine_or_part: "备件", unit: "",
};
const EDITABLE = ["description", "brand", "category_major", "category_minor",
  "machine_or_part", "unit"] as const;

/** 备件主数据：采购可搜全部型号 → 编辑描述/品类/品牌，或新建 PN。
 * 人工改过的字段后端 locked_fields 标记，重导（氚云）不覆盖。 */
export default function MasterDataPage() {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<PartHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [cats, setCats] = useState<CategoryNode[]>([]);

  const [mode, setMode] = useState<null | "create" | "edit">(null);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [init, setInit] = useState<FormState>(EMPTY);
  const [near, setNear] = useState<NearDup[]>([]);
  const [suggest, setSuggest] = useState<ClassifySuggestion | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    masterCategories().then((r) => setCats(r.data.categories)).catch(() => undefined);
    doSearch("");
  }, []);

  const doSearch = async (query: string) => {
    setLoading(true);
    try {
      const { data } = await searchParts(query, 1, 50);
      setRows(data.items || []);
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "搜索失败");
    } finally {
      setLoading(false);
    }
  };

  const l2opts = (l1?: string | null) =>
    (cats.find((c) => c.name === l1)?.children || []).map((ch) => ({ label: ch.name, value: ch.name }));

  const openCreate = () => {
    setForm(EMPTY); setInit(EMPTY); setNear([]); setSuggest(null); setMode("create");
  };
  const openEdit = async (pn: string) => {
    try {
      const { data } = await api.get("/parts/overview", { params: { pn_std: pn } });
      const p = data.part;
      const f: FormState = {
        pn_std: p.pn_std, description: p.description ?? "", brand: p.brand ?? "",
        category_major: p.category_major ?? null, category_minor: p.category_minor ?? null,
        machine_or_part: p.machine_or_part ?? "备件", unit: p.unit ?? "",
      };
      setForm(f); setInit(f); setNear([]); setSuggest(null); setMode("edit");
    } catch {
      message.error("加载型号失败");
    }
  };

  const set = (patch: Partial<FormState>) => setForm((f) => ({ ...f, ...patch }));

  const onSuggest = async () => {
    if (!form.description) { setSuggest(null); return; }
    try {
      const { data } = await masterSuggest(form.description, form.pn_std, form.brand || "");
      setSuggest(data.suggestion);
    } catch { /* 建议失败不打断录入 */ }
  };
  const applySuggest = () => {
    if (!suggest || suggest.whole_system) return;
    set({ category_major: suggest.category_l1 ?? form.category_major,
      category_minor: suggest.category_l2 ?? null, machine_or_part: "备件" });
  };

  const onCheckDup = async () => {
    if (mode !== "create" || !form.pn_std.trim()) { setNear([]); return; }
    try {
      const { data } = await masterCheck(form.pn_std.trim());
      setNear(data.near_duplicates || []);
    } catch { /* ignore */ }
  };

  const save = async (force = false) => {
    setSaving(true);
    try {
      if (mode === "create") {
        const { data } = await masterCreate({ ...form, pn_std: form.pn_std.trim(), force });
        if (!data.created) {
          setNear(data.near_duplicates || []);
          message.warning(data.message || "存在近似型号，请确认");
          return;
        }
        message.success(`已新建 ${data.pn_std}`);
      } else {
        const diff: any = { pn_std: form.pn_std };
        EDITABLE.forEach((k) => {
          if ((form[k] ?? null) !== (init[k] ?? null)) diff[k] = form[k] ?? null;
        });
        if (Object.keys(diff).length === 1) { message.info("没有改动"); setMode(null); return; }
        const { data } = await masterEdit(diff);
        message.success(`已保存（重导不再覆盖：${data.updated.join("、")}）`);
      }
      setMode(null);
      doSearch(q);
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "保存失败");
    } finally {
      setSaving(false);
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
      render: (_, r) => <Button size="small" onClick={() => openEdit(r.pn_std)}>编辑</Button>,
    },
  ];

  const fields = (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {mode === "create" ? (
        <div>
          <label style={{ display: "block", marginBottom: 4 }}>PN（产品唯一标识）<span style={{ color: "#cf1322" }}>*</span></label>
          <Input value={form.pn_std} placeholder="如 ST8000NM000A"
            onChange={(e) => set({ pn_std: e.target.value })} onBlur={onCheckDup} />
        </div>
      ) : (
        <div><label style={{ display: "block", marginBottom: 4 }}>PN</label>
          <Input value={form.pn_std} disabled /></div>
      )}

      {near.length > 0 && (
        <Alert type="warning" showIcon
          message="存在近似型号，确认不是重复再继续"
          description={<ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {near.map((n) => <li key={n.pn_std}><b>{n.pn_std}</b> — {n.description || "无描述"}（{n.reason}）</li>)}
          </ul>} />
      )}

      <div>
        <label style={{ display: "block", marginBottom: 4 }}>描述</label>
        <Input.TextArea value={form.description ?? ""} rows={2} onBlur={onSuggest}
          onChange={(e) => set({ description: e.target.value })}
          placeholder="如 希捷 8TB 7.2K 3.5 SATA 企业级硬盘" />
        {suggest && !suggest.whole_system && (suggest.category_l1 || suggest.category_l2) && (
          <div style={{ marginTop: 6, fontSize: 13 }}>
            建议品类：<Tag color="blue">{suggest.category_l1}{suggest.category_l2 ? ` / ${suggest.category_l2}` : ""}</Tag>
            <Button size="small" type="link" onClick={applySuggest}>采用</Button>
          </div>
        )}
        {suggest?.whole_system && (
          <div style={{ marginTop: 6, fontSize: 13, color: "#d46b08" }}>
            识别为整机/扩展柜——通常不作为备件主数据（可改类型为"整机"）。
          </div>
        )}
      </div>

      <Space size={12} style={{ width: "100%" }} wrap>
        <div style={{ minWidth: 180 }}>
          <label style={{ display: "block", marginBottom: 4 }}>品类（大类）</label>
          <Select style={{ width: 180 }} allowClear showSearch value={form.category_major ?? undefined}
            placeholder="选择大类" options={cats.map((c) => ({ label: c.name, value: c.name }))}
            onChange={(v) => set({ category_major: v ?? null, category_minor: null })} />
        </div>
        <div style={{ minWidth: 180 }}>
          <label style={{ display: "block", marginBottom: 4 }}>品类（二级）</label>
          <Select style={{ width: 180 }} allowClear showSearch value={form.category_minor ?? undefined}
            placeholder="选择二级" options={l2opts(form.category_major)}
            onChange={(v) => set({ category_minor: v ?? null })} disabled={!form.category_major} />
        </div>
      </Space>

      <Space size={12} wrap>
        <div><label style={{ display: "block", marginBottom: 4 }}>品牌</label>
          <Input style={{ width: 180 }} value={form.brand ?? ""} placeholder="如 Seagate / HPE"
            onChange={(e) => set({ brand: e.target.value })} /></div>
        <div><label style={{ display: "block", marginBottom: 4 }}>类型</label>
          <Select style={{ width: 120 }} value={form.machine_or_part ?? "备件"}
            options={[{ value: "备件" }, { value: "整机" }]}
            onChange={(v) => set({ machine_or_part: v })} /></div>
        <div><label style={{ display: "block", marginBottom: 4 }}>单位</label>
          <Input style={{ width: 100 }} value={form.unit ?? ""} placeholder="个"
            onChange={(e) => set({ unit: e.target.value })} /></div>
      </Space>
    </Space>
  );

  return (
    <div>
      <PageHeader
        title="备件主数据"
        subtitle="采购可编辑型号的描述/品类/品牌，或新建 PN；改过的内容氚云重导不会覆盖。"
        extra={<Button type="primary" onClick={openCreate}>新建 PN</Button>}
      />
      <Input.Search
        placeholder="搜 PN / 描述 / 品牌（留空浏览）" allowClear enterButton
        style={{ maxWidth: 460, marginBottom: 16 }}
        value={q} onChange={(e) => setQ(e.target.value)} onSearch={doSearch}
      />
      <Table
        rowKey="pn_std" size="small" columns={columns} dataSource={rows} loading={loading}
        scroll={{ x: 760 }} pagination={{ pageSize: 20, showSizeChanger: false }}
      />

      <Modal
        open={mode !== null}
        title={mode === "create" ? "新建 PN" : `编辑 ${form.pn_std}`}
        onCancel={() => setMode(null)}
        confirmLoading={saving}
        width={560}
        footer={
          mode === "create" && near.length > 0
            ? [
              <Button key="c" onClick={() => setMode(null)}>取消</Button>,
              <Button key="f" danger loading={saving} onClick={() => save(true)}>确认不重复，强制新建</Button>,
            ]
            : [
              <Button key="c" onClick={() => setMode(null)}>取消</Button>,
              <Button key="s" type="primary" loading={saving}
                disabled={mode === "create" && !form.pn_std.trim()}
                onClick={() => save(false)}>保存</Button>,
            ]
        }
      >
        {fields}
      </Modal>
    </div>
  );
}
