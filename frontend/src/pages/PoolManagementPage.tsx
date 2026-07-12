import { useEffect, useMemo, useRef, useState } from "react";
import {
  Button, Drawer, Grid, Input, InputNumber, Popconfirm, Segmented, Select, Space, Spin, Table,
  Tag, message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import PageHeader from "../components/PageHeader";
import { searchParts } from "../api";
import {
  archivePnPool, createPnPool, getPnPool, listPnPools, restorePnPool, setPnPoolPolicy,
  updatePnPool, updatePnPoolMembers,
  type PnPoolDetail, type PnPoolRow, type PriceBasis,
} from "../api/pools";

const PAGE_SIZE = 20;
type StatusFilter = "active" | "archived" | "all";

const SOURCE_LABEL: Record<string, string> = { manual: "人工", legacy_generated: "历史自动池" };
const BASIS_OPTIONS = [{ label: "未税", value: "ex_tax" }, { label: "含税", value: "inc_tax" }];
const basisLabel = (b?: string | null) => (b === "inc_tax" ? "含税" : "未税");

// 约束价（未税）：null = 未设置或按权限脱敏，列表统一显示 "--"
const fmtMoney = (v: number | null | undefined) =>
  v == null ? <span style={{ color: "var(--mb-text-3, #bbb)" }}>--</span>
    : Number(v).toFixed(2);
const fmtTime = (s: string | null | undefined) => (s ? new Date(s).toLocaleString("zh-CN") : "—");

interface MemberOption { value: number; label: string }

const memberLabel = (pn: string | null, desc: string | null) =>
  `${pn ?? "?"}${desc ? `｜${desc}` : ""}`;

/** 互通PN池管理（Slice 1 最小管理页）：人工池是唯一真值，保存即生效。
 * 列表（搜索/状态筛选/分页）+ 右侧抽屉编辑（名称/说明/成员/约束价/备注）+ 新建/归档/恢复。
 * 写操作全部携带 version（乐观锁）；连续保存串行，用上一步返回的新 version。 */
export default function PoolManagementPage() {
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false;

  // ---- 列表 ----
  const [q, setQ] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active");
  const [rows, setRows] = useState<PnPoolRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);

  const load = async (query = q, status = statusFilter, p = page) => {
    setLoading(true);
    try {
      const { data } = await listPnPools({
        q: query.trim() || undefined, status, page: p, page_size: PAGE_SIZE,
      });
      setRows(data.items || []);
      setTotal(data.total || 0);
      setPage(data.page || p);
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "池列表加载失败");
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load("", "active", 1); }, []);

  // ---- 抽屉（create / edit 共用同一套表单态）----
  const [mode, setMode] = useState<null | "create" | "edit">(null);
  const [detail, setDetail] = useState<PnPoolDetail | null>(null);   // edit 基线（diff 与 version 来源）
  const [detailLoading, setDetailLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [memberIds, setMemberIds] = useState<number[]>([]);
  const [memberOptions, setMemberOptions] = useState<MemberOption[]>([]);   // 已选成员的标签
  const [searchOptions, setSearchOptions] = useState<MemberOption[]>([]);   // 远程搜索结果
  const [purchaseValue, setPurchaseValue] = useState<number | null>(null);
  const [purchaseBasis, setPurchaseBasis] = useState<PriceBasis>("ex_tax");
  const [salesValue, setSalesValue] = useState<number | null>(null);
  const [salesBasis, setSalesBasis] = useState<PriceBasis>("ex_tax");
  const [note, setNote] = useState("");

  // 表单填充：edit 打开 / 409 后重拉详情时同步 version 与字段
  const hydrate = (d: PnPoolDetail) => {
    setDetail(d);
    setName(d.name);
    setDescription(d.description ?? "");
    const opts = (d.members || []).map((m) => ({
      value: m.part_id, label: memberLabel(m.pn_std, m.description),
    }));
    setMemberOptions(opts);
    setMemberIds(opts.map((o) => o.value));
    const p = d.price_policy;
    setPurchaseValue(p?.purchase_input_value != null ? Number(p.purchase_input_value) : null);
    setPurchaseBasis(p?.purchase_input_basis ?? "ex_tax");
    setSalesValue(p?.sales_input_value != null ? Number(p.sales_input_value) : null);
    setSalesBasis(p?.sales_input_basis ?? "ex_tax");
    setNote("");
    setSearchOptions([]);
  };

  const openCreate = () => {
    setDetail(null);
    setName(""); setDescription(""); setMemberIds([]); setMemberOptions([]); setSearchOptions([]);
    setPurchaseValue(null); setPurchaseBasis("ex_tax");
    setSalesValue(null); setSalesBasis("ex_tax");
    setNote("");
    setMode("create");
  };

  const openEdit = async (groupId: number) => {
    setMode("edit");
    setDetail(null);
    setDetailLoading(true);
    try {
      const { data } = await getPnPool(groupId);
      hydrate(data);
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "池详情加载失败");
      setMode(null);
    } finally {
      setDetailLoading(false);
    }
  };

  const closeDrawer = () => { setMode(null); setDetail(null); };

  // ---- 成员远程搜索（防抖 + 代次守卫；browse=true 分支返回 part id）----
  const [fetching, setFetching] = useState(false);
  const searchTimer = useRef<number>();
  const searchGen = useRef(0);
  const onMemberSearch = (kw: string) => {
    window.clearTimeout(searchTimer.current);
    const key = kw.trim();
    if (!key) { setSearchOptions([]); return; }
    searchTimer.current = window.setTimeout(async () => {
      const gen = ++searchGen.current;
      setFetching(true);
      try {
        const { data } = await searchParts(key, 1, 20, true);
        if (gen !== searchGen.current) return;
        setSearchOptions((data.items || [])
          .filter((it) => it.id != null)
          .map((it) => ({ value: it.id!, label: memberLabel(it.pn_std, it.description) })));
      } catch { /* 搜索失败不打断编辑 */ } finally {
        if (gen === searchGen.current) setFetching(false);
      }
    }, 300);
  };
  useEffect(() => () => window.clearTimeout(searchTimer.current), []);

  // 下拉选项 = 已选成员标签 + 搜索结果（去重，保证已选项标签不因搜索词变化而丢失）
  const mergedOptions = useMemo(() => {
    const seen = new Set(memberOptions.map((o) => o.value));
    return [...memberOptions, ...searchOptions.filter((o) => !seen.has(o.value))];
  }, [memberOptions, searchOptions]);

  const onMembersChange = (ids: number[]) => {
    const lookup = new Map(mergedOptions.map((o) => [o.value, o]));
    setMemberOptions(ids.map((id) => lookup.get(id) ?? { value: id, label: `#${id}` }));
    setMemberIds(ids);
  };

  // ---- 保存（保存即生效）----
  // 约束价是否被用户改动（与详情里的原始录入值/口径比对；没动就不调 PUT）
  const policyDirty = (d: PnPoolDetail | null) => {
    const p = d?.price_policy;
    const initPV = p?.purchase_input_value != null ? Number(p.purchase_input_value) : null;
    const initSV = p?.sales_input_value != null ? Number(p.sales_input_value) : null;
    const initPB = p?.purchase_input_basis ?? "ex_tax";
    const initSB = p?.sales_input_basis ?? "ex_tax";
    return purchaseValue !== initPV || salesValue !== initSV
      || (purchaseValue != null && purchaseBasis !== initPB)
      || (salesValue != null && salesBasis !== initSB);
  };

  // 保存失败后的详情重拉：409（他人已改）→ 整表单回填最新值；其它错误（如 400 成员冲突、
  // 前序步骤已生效）→ 只刷新基线 version/成员，保留用户正在编辑的内容供改正后重存
  const refreshAfterError = async (groupId: number, rehydrateForm: boolean) => {
    try {
      const { data } = await getPnPool(groupId);
      if (rehydrateForm) hydrate(data); else setDetail(data);
    } catch { /* 拉不到就保持现状，用户可关闭抽屉重开 */ }
  };

  const policyBody = (version: number) => ({
    version,
    purchase_value: purchaseValue, purchase_basis: purchaseBasis,
    sales_value: salesValue, sales_basis: salesBasis,
    note: note.trim() || null,
  });

  const save = async () => {
    if (!name.trim()) { message.warning("请输入池名称"); return; }
    setSaving(true);
    try {
      if (mode === "create") {
        const { data: created } = await createPnPool({
          name: name.trim(), description: description.trim() || null,
          member_part_ids: memberIds, note: note.trim() || null,
        });
        // 填了约束价才追加 PUT；失败时池已建成 → 转入编辑模式重试，避免重复建池
        if (purchaseValue != null || salesValue != null) {
          try {
            await setPnPoolPolicy(created.group_id, policyBody(created.version));
          } catch (e: any) {
            message.error(e?.response?.data?.detail || "池已创建，但约束价保存失败，请重试");
            setMode("edit");
            await refreshAfterError(created.group_id, false);
            load();
            return;
          }
        }
        message.success("已保存并生效");
        closeDrawer();
        load(q, statusFilter, 1);
        return;
      }

      // edit：按 diff 串行 PATCH 基本信息 → PATCH 成员 → PUT 约束价，每步用上一步返回的新 version
      const d = detail;
      if (!d) return;
      let version = d.version;
      let touched = false;
      const cleanNote = note.trim() || null;

      const nameChanged = name.trim() !== d.name;
      const descChanged = (description.trim() || null) !== (d.description ?? null);
      if (nameChanged || descChanged) {
        const { data } = await updatePnPool(d.group_id, {
          version,
          ...(nameChanged ? { name: name.trim() } : {}),
          ...(descChanged ? { description: description.trim() || null } : {}),
          note: cleanNote,
        });
        version = data.version;
        touched = true;
      }

      const initIds = new Set(d.members.map((m) => m.part_id));
      const nowIds = new Set(memberIds);
      const add = memberIds.filter((id) => !initIds.has(id));
      const remove = d.members.map((m) => m.part_id).filter((id) => !nowIds.has(id));
      if (add.length || remove.length) {
        const { data } = await updatePnPoolMembers(d.group_id, {
          version, add_part_ids: add, remove_part_ids: remove, note: cleanNote,
        });
        version = data.version;
        touched = true;
      }

      if (policyDirty(d)) {
        await setPnPoolPolicy(d.group_id, policyBody(version));
        touched = true;
      }

      if (!touched) { message.info("没有改动"); return; }
      message.success("已保存并生效");
      closeDrawer();
      load(q, statusFilter, page);
    } catch (e: any) {
      const isConflict = e?.response?.status === 409;
      message.error(e?.response?.data?.detail
        || (isConflict ? "保存冲突：该池刚被他人修改，已重新加载最新数据" : "保存失败"));
      if (mode === "edit" && detail) await refreshAfterError(detail.group_id, isConflict);
      load(q, statusFilter, page);   // 前序步骤可能已生效，列表同步刷新
    } finally {
      setSaving(false);
    }
  };

  // ---- 归档 / 恢复（携带当前行 version）----
  const doLifecycle = async (r: PnPoolRow, action: "archive" | "restore") => {
    try {
      if (action === "archive") await archivePnPool(r.group_id, { version: r.version });
      else await restorePnPool(r.group_id, { version: r.version });
      message.success(action === "archive" ? "已归档" : "已恢复");
    } catch (e: any) {
      message.error(e?.response?.data?.detail || (action === "archive" ? "归档失败" : "恢复失败"));
    }
    load(q, statusFilter, page);   // 失败（含 409 版本冲突）也刷新，拿到最新 version
  };

  // ---- 列表列 ----
  const columns: ColumnsType<PnPoolRow> = [
    {
      title: "池名称", dataIndex: "name", width: 220, ellipsis: true,
      render: (v, r) => (
        <span>
          <span style={{ fontWeight: 500 }}>{v}</span>
          {r.description && (
            <div style={{ fontSize: 12, color: "var(--mb-text-3, #999)" }}>{r.description}</div>
          )}
        </span>
      ),
    },
    { title: "成员数", dataIndex: "member_count", width: 80, align: "right" },
    { title: "采购上限(未税)", dataIndex: "purchase_ceiling_ex_tax", width: 130, align: "right", render: fmtMoney },
    { title: "销售下限(未税)", dataIndex: "sales_floor_ex_tax", width: 130, align: "right", render: fmtMoney },
    { title: "来源", dataIndex: "source", width: 110, render: (v) => SOURCE_LABEL[v] || v },
    {
      title: "状态", dataIndex: "status", width: 90,
      render: (v) => (v === "active" ? <Tag color="green">有效</Tag> : <Tag>已归档</Tag>),
    },
    { title: "更新人", dataIndex: "updated_by", width: 100, render: (v) => v || "—" },
    { title: "更新时间", dataIndex: "updated_at", width: 160, render: fmtTime },
    {
      title: "操作", key: "op", width: 110, fixed: isMobile ? undefined : "right",
      render: (_, r) => (
        <Space size="small">
          <a onClick={() => openEdit(r.group_id)}>编辑</a>
          {r.status === "active" ? (
            <Popconfirm title="归档该池？" description="归档后可随时恢复。"
              onConfirm={() => doLifecycle(r, "archive")}>
              <a style={{ color: "#cf1322" }}>归档</a>
            </Popconfirm>
          ) : (
            <Popconfirm title="恢复该池为有效？" onConfirm={() => doLifecycle(r, "restore")}>
              <a style={{ color: "#389e0d" }}>恢复</a>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  // ---- 抽屉表单 ----
  const policy = detail?.price_policy;
  const drawerForm = (
    <Space direction="vertical" size={14} style={{ width: "100%" }}>
      <div>
        <label style={{ display: "block", marginBottom: 4 }}>
          池名称<span style={{ color: "#cf1322" }}> *</span>
        </label>
        <Input value={name} maxLength={128} placeholder="如 8TB 7.2K SATA 企业盘互通池"
          onChange={(e) => setName(e.target.value)} />
      </div>

      <div>
        <label style={{ display: "block", marginBottom: 4 }}>用途 / 说明</label>
        <Input.TextArea value={description} rows={2} placeholder="这个池覆盖哪些场景、为什么互通（选填）"
          onChange={(e) => setDescription(e.target.value)} />
      </div>

      <div>
        <label style={{ display: "block", marginBottom: 4 }}>PN 成员</label>
        <Select
          mode="multiple"
          showSearch
          filterOption={false}
          style={{ width: "100%" }}
          placeholder="输入 PN / 描述搜索后选择；点已选项的 × 可移出"
          value={memberIds}
          options={mergedOptions}
          onSearch={onMemberSearch}
          onChange={onMembersChange}
          notFoundContent={fetching ? <Spin size="small" /> : "输入关键词搜索型号"}
          maxTagCount={isMobile ? 6 : undefined}
        />
        <div style={{ marginTop: 4, fontSize: 12, color: "var(--mb-text-3, #999)" }}>
          一个有效 PN 只能属于一个有效池；保存时按增删差异生效。
        </div>
      </div>

      <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
        <div style={{ flex: "1 1 220px", minWidth: 200 }}>
          <label style={{ display: "block", marginBottom: 4 }}>采购最高价（上限）</label>
          <div style={{ display: "flex", gap: 8 }}>
            <InputNumber<number> style={{ flex: 1 }} min={0} precision={2}
              placeholder="留空 = 不设" value={purchaseValue}
              onChange={(v) => setPurchaseValue(v ?? null)} />
            <Select style={{ width: 88 }} value={purchaseBasis} options={BASIS_OPTIONS}
              onChange={(v) => setPurchaseBasis(v as PriceBasis)} />
          </div>
        </div>
        <div style={{ flex: "1 1 220px", minWidth: 200 }}>
          <label style={{ display: "block", marginBottom: 4 }}>销售最低价（下限）</label>
          <div style={{ display: "flex", gap: 8 }}>
            <InputNumber<number> style={{ flex: 1 }} min={0} precision={2}
              placeholder="留空 = 不设" value={salesValue}
              onChange={(v) => setSalesValue(v ?? null)} />
            <Select style={{ width: 88 }} value={salesBasis} options={BASIS_OPTIONS}
              onChange={(v) => setSalesBasis(v as PriceBasis)} />
          </div>
        </div>
      </div>
      <div style={{ marginTop: -8, fontSize: 12, color: "var(--mb-text-3, #999)" }}>
        约束价统一按未税入库：选「含税」将 ÷1.13 自动换算为未税，原始录入值与口径保留可查。
      </div>

      <div>
        <label style={{ display: "block", marginBottom: 4 }}>变更备注</label>
        <Input value={note} maxLength={200} placeholder="本次改动的原因（选填，随本次变更留痕）"
          onChange={(e) => setNote(e.target.value)} />
      </div>

      {mode === "edit" && detail && (
        <div style={{
          paddingTop: 12, borderTop: "1px solid var(--mb-border, #eee)",
          fontSize: 12.5, color: "var(--mb-text-3, #999)", lineHeight: 1.8,
        }}>
          <div>最近变更：{detail.updated_by || "—"} · {fmtTime(detail.updated_at)}</div>
          {policy ? (
            <div>
              当前约束（原始录入）：采购上限 {policy.purchase_input_value != null
                ? `${Number(policy.purchase_input_value).toFixed(2)}（${basisLabel(policy.purchase_input_basis)}）`
                : "未设置"} · 销售下限 {policy.sales_input_value != null
                ? `${Number(policy.sales_input_value).toFixed(2)}（${basisLabel(policy.sales_input_basis)}）`
                : "未设置"}
              <div>约束设置人：{policy.changed_by || "—"} · {fmtTime(policy.valid_from)}</div>
            </div>
          ) : (
            <div>当前未设置约束价</div>
          )}
        </div>
      )}
    </Space>
  );

  return (
    <div>
      <PageHeader
        title="互通PN池管理"
        subtitle="人工维护的互通池是唯一真值：建池、调成员、设约束价（采购上限/销售下限，统一未税口径），保存即生效。"
        extra={<Button type="primary" onClick={openCreate}>新建池</Button>}
      />

      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 16 }}>
        <Input.Search
          placeholder="搜索池名/成员PN/描述/品牌" allowClear enterButton
          style={{ maxWidth: 380, flex: "1 1 260px" }}
          value={q} onChange={(e) => setQ(e.target.value)}
          onSearch={(v) => load(v, statusFilter, 1)}
        />
        <Segmented
          value={statusFilter}
          onChange={(v) => { setStatusFilter(v as StatusFilter); load(q, v as StatusFilter, 1); }}
          options={[
            { label: "有效", value: "active" },
            { label: "已归档", value: "archived" },
            { label: "全部", value: "all" },
          ]}
        />
      </div>

      <Table<PnPoolRow>
        rowKey="group_id" size="small" columns={columns} dataSource={rows} loading={loading}
        scroll={{ x: 1080 }}
        pagination={{
          current: page, pageSize: PAGE_SIZE, total, showSizeChanger: false,
          showTotal: (t) => `共 ${t} 个池`,
          onChange: (p) => load(q, statusFilter, p),
        }}
      />

      <Drawer
        width={isMobile ? "100%" : 720}
        open={mode !== null}
        onClose={closeDrawer}
        title={mode === "create" ? "新建互通PN池" : detail ? `编辑池 · ${detail.name}` : "编辑池"}
        extra={
          <Button type="primary" loading={saving}
            disabled={mode === "edit" && !detail}
            onClick={save}>
            保存
          </Button>
        }
      >
        {detailLoading ? (
          <div style={{ textAlign: "center", padding: 48 }}><Spin /></div>
        ) : drawerForm}
      </Drawer>
    </div>
  );
}
