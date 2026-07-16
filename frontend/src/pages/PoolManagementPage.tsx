import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  Alert, Button, Drawer, Grid, Input, InputNumber, Popconfirm, Segmented, Select, Space, Spin,
  Table, Tag, message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useSearchParams } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import PoolPolicyCoverage from "../components/pools/PoolPolicyCoverage";
import { searchParts } from "../api";
import {
  archivePnPool, createPnPool, getPnPool, listPnPools, restorePnPool, setPnPoolPolicy,
  updatePnPool, updatePnPoolMembers,
  type PnPoolDetail, type PnPoolRow, type PoolPolicyCoverage as Coverage,
  type PoolPolicyMissing, type PriceBasis,
} from "../api/pools";

const PAGE_SIZE = 20;
const MIN_MEMBERS = 2;   // 《互通PN池》核心规则5：有效池至少两个 PN（后端同规则兜底）
type StatusFilter = "active" | "archived" | "all";
type DrawerMode = null | "create" | "edit";
interface DrawerRequestContext {
  generation: number;
  mode: DrawerMode;
  groupId: number | null;
}

const SOURCE_LABEL: Record<string, string> = { manual: "人工", legacy_generated: "历史自动池" };
const BASIS_OPTIONS = [{ label: "未税", value: "ex_tax" }, { label: "含税", value: "inc_tax" }];
const basisLabel = (b?: string | null) => (b === "inc_tax" ? "含税" : "未税");

const MUTED = { color: "var(--mb-text-3, #999)" };
const POLICY_MISSING_VALUES: PoolPolicyMissing[] = ["purchase", "sales", "either", "both"];
// 约束价（未税）三态展示：无权限（price_restricted）≠ 未设置（null）≠ 数值——
// 绝不把"看不见"和"没设"都画成 "--" 让人猜（复审非阻塞 1）
const fmtMoney = (v: number | null | undefined, restricted: boolean) =>
  restricted ? <span style={MUTED}>无价格权限</span>
    : v == null ? <span style={MUTED}>未设置</span>
      : Number(v).toFixed(2);
const fmtTime = (s: string | null | undefined) => (s ? new Date(s).toLocaleString("zh-CN") : "—");

/** 本地权限快照（登录时整份写入 localStorage；写坏时回退空对象，与 App.tsx 同口径）。 */
function readLocalPerms(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem("permissions") || "{}");
  } catch {
    return {};
  }
}

interface MemberOption { value: number; label: string }

const memberLabel = (pn: string | null, desc: string | null) =>
  `${pn ?? "?"}${desc ? `｜${desc}` : ""}`;

/** 互通PN池管理（Slice 1）：人工池是唯一真值，保存即生效。
 *
 * 权限分区（复审阻塞 3）——manage 与 set_policy 是两种独立授权，任一权限都可进页面：
 * - action_pool_manage：建池、改名称/说明、增删成员、归档/恢复；
 * - action_pool_set_policy：设置约束价（无权限时该区域明确显示文案，不给可编辑输入框）。
 * 编辑抽屉拆成三个独立保存动作（基本信息 / 成员 / 约束价），每个按钮 = 恰好一个
 * HTTP 请求 = 后端一个事务——不存在"一个保存按钮伪装原子操作、前半成功后半 403"。
 * 归档池只读（显示档案与"先恢复"提示，不渲染必然 400 的可保存表单）。 */
export default function PoolManagementPage() {
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false;
  const [searchParams, setSearchParams] = useSearchParams();
  const rawPolicyMissing = searchParams.get("policy_missing") as PoolPolicyMissing | null;
  const policyMissing = rawPolicyMissing && POLICY_MISSING_VALUES.includes(rawPolicyMissing)
    ? rawPolicyMissing : null;

  // 权限快照随登录周期固定（改权限会踢重登），组件生命周期内读一次即可
  const [{ canManage, canSetPolicy, localCoverageRestricted }] = useState(() => {
    const isAdmin = localStorage.getItem("role") === "admin";
    const perms = readLocalPerms();
    return {
      canManage: isAdmin || !!perms.action_pool_manage,
      canSetPolicy: isAdmin || !!perms.action_pool_set_policy,
      localCoverageRestricted: !isAdmin && perms.data_pool_price_governance === false,
    };
  });

  // ---- 列表 ----
  const [q, setQ] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active");
  const [rows, setRows] = useState<PnPoolRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [coverageRestricted, setCoverageRestricted] = useState(localCoverageRestricted);
  const listGeneration = useRef(0);
  const listView = useRef<{
    query: string; status: StatusFilter; page: number; policyMissing: PoolPolicyMissing | null;
  }>({
    query: "", status: "active", page: 1, policyMissing: null,
  });

  const load = async (
    query = q, status = statusFilter, p = page,
    missing: PoolPolicyMissing | null = localCoverageRestricted ? null : policyMissing,
  ) => {
    const generation = ++listGeneration.current;
    listView.current = { query, status, page: p, policyMissing: missing };
    setLoading(true);
    setListError(null);
    try {
      const { data } = await listPnPools({
        q: query.trim() || undefined, status, page: p, page_size: PAGE_SIZE,
        ...(missing ? { policy_missing: missing } : {}),
      });
      if (generation !== listGeneration.current) return;
      setRows(data.items || []);
      setTotal(data.total || 0);
      setPage(data.page || p);
      setCoverageRestricted(!!data.coverage_restricted);
      setCoverage(data.coverage_restricted ? null : (data.coverage ?? null));
    } catch (e: any) {
      if (generation !== listGeneration.current) return;
      // 失败时绝不保留上一筛选的行；否则 URL/高亮已切换却仍展示旧数据。
      setRows([]);
      setTotal(0);
      setPage(1);
      setListError(e?.response?.data?.detail || "池列表加载失败");
    } finally {
      if (generation === listGeneration.current) setLoading(false);
    }
  };
  const reloadCurrentList = () => {
    const current = listView.current;
    return load(current.query, current.status, current.page, current.policyMissing);
  };
  useEffect(() => {
    const current = listView.current;
    const missing = localCoverageRestricted ? null : policyMissing;
    // 缺失筛选只定义于有效池；深链/前进后退恢复筛选时，状态标签也必须同步回有效。
    const nextStatus: StatusFilter = missing ? "active" : current.status;
    if (current.status !== nextStatus) {
      current.status = nextStatus;
      setStatusFilter(nextStatus);
    }
    load(current.query, nextStatus, 1, missing);
    return () => { listGeneration.current += 1; };
    // URL 是缺失筛选的唯一真值；搜索词和状态由用户显式提交，不放进依赖避免逐键请求。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [policyMissing, localCoverageRestricted]);

  const setPolicyMissing = (next: "purchase" | "sales") => {
    // 覆盖率分母是有效池，点击覆盖卡必须回到有效池列表，避免用归档列表解释全局数字。
    listView.current.status = "active";
    setStatusFilter("active");
    const merged = new URLSearchParams(searchParams);
    merged.set("policy_missing", next);
    setSearchParams(merged, { replace: false });
  };
  const clearPolicyMissing = () => {
    const merged = new URLSearchParams(searchParams);
    merged.delete("policy_missing");
    setSearchParams(merged, { replace: false });
  };

  // ---- 抽屉（create / edit 共用同一套表单态）----
  const [mode, setMode] = useState<DrawerMode>(null);
  const [detail, setDetail] = useState<PnPoolDetail | null>(null);   // edit 基线（diff 与 version 来源）
  const [detailLoading, setDetailLoading] = useState(false);
  const drawerContext = useRef<DrawerRequestContext>({ generation: 0, mode: null, groupId: null });

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

  // 三个独立保存动作各自的 loading（互不阻塞、互不伪装）
  const [savingInfo, setSavingInfo] = useState(false);
  const [savingMembers, setSavingMembers] = useState(false);
  const [savingPolicy, setSavingPolicy] = useState(false);
  const [creating, setCreating] = useState(false);

  // 成员搜索也属于抽屉会话；切池、关闭或转新建时，旧搜索结果不得落入新表单。
  const [fetching, setFetching] = useState(false);
  const searchTimer = useRef<number>();
  const searchGen = useRef(0);

  const beginDrawerContext = (nextMode: DrawerMode, groupId: number | null) => {
    const next: DrawerRequestContext = {
      generation: drawerContext.current.generation + 1,
      mode: nextMode,
      groupId,
    };
    drawerContext.current = next;
    window.clearTimeout(searchTimer.current);
    searchGen.current += 1;
    setFetching(false);
    setDetailLoading(false);
    // 旧会话的写请求即使稍后完成，也不能控制新抽屉的 loading 状态。
    setSavingInfo(false);
    setSavingMembers(false);
    setSavingPolicy(false);
    setCreating(false);
    return next;
  };

  const isDrawerContextCurrent = (ctx: DrawerRequestContext) => {
    const current = drawerContext.current;
    return current.generation === ctx.generation
      && current.mode === ctx.mode
      && current.groupId === ctx.groupId;
  };

  const isPoolResponseCurrent = (ctx: DrawerRequestContext, responseGroupId: number) =>
    ctx.groupId === responseGroupId && isDrawerContextCurrent(ctx);

  const currentEditContext = (groupId: number) => {
    const current = drawerContext.current;
    return current.mode === "edit" && current.groupId === groupId ? { ...current } : null;
  };

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
    beginDrawerContext("create", null);
    setDetail(null);
    setName(""); setDescription(""); setMemberIds([]); setMemberOptions([]); setSearchOptions([]);
    setPurchaseValue(null); setPurchaseBasis("ex_tax");
    setSalesValue(null); setSalesBasis("ex_tax");
    setNote("");
    setMode("create");
  };

  const openEdit = async (groupId: number) => {
    const ctx = beginDrawerContext("edit", groupId);
    setMode("edit");
    setDetail(null);
    setDetailLoading(true);
    try {
      const { data } = await getPnPool(groupId);
      if (!isPoolResponseCurrent(ctx, data.group_id)) return;
      hydrate(data);
    } catch (e: any) {
      if (!isDrawerContextCurrent(ctx)) return;
      message.error(e?.response?.data?.detail || "池详情加载失败");
      beginDrawerContext(null, null);
      setMode(null);
      setDetail(null);
    } finally {
      if (isDrawerContextCurrent(ctx)) setDetailLoading(false);
    }
  };

  const closeDrawer = () => {
    beginDrawerContext(null, null);
    setMode(null);
    setDetail(null);
  };

  // ---- 成员远程搜索（防抖 + 代次守卫；browse=true 分支返回 part id）----
  const onMemberSearch = (kw: string) => {
    window.clearTimeout(searchTimer.current);
    const gen = ++searchGen.current;
    const key = kw.trim();
    if (!key) { setSearchOptions([]); setFetching(false); return; }
    searchTimer.current = window.setTimeout(async () => {
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
  useEffect(() => () => {
    window.clearTimeout(searchTimer.current);
    searchGen.current += 1;
    drawerContext.current = {
      generation: drawerContext.current.generation + 1, mode: null, groupId: null,
    };
  }, []);

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

  // ---- diff 判定（各保存按钮只在真有改动时可用）----
  const infoDirty = detail != null
    && (name.trim() !== detail.name
      || (description.trim() || null) !== (detail.description ?? null));

  const memberDiff = useMemo(() => {
    if (!detail) return { add: [] as number[], remove: [] as number[] };
    const initIds = new Set(detail.members.map((m) => m.part_id));
    const nowIds = new Set(memberIds);
    return {
      add: memberIds.filter((id) => !initIds.has(id)),
      remove: detail.members.map((m) => m.part_id).filter((id) => !nowIds.has(id)),
    };
  }, [detail, memberIds]);
  const membersDirty = memberDiff.add.length > 0 || memberDiff.remove.length > 0;

  // 约束价单侧 diff：每侧独立判定 set / unset / keep（null 永远不是"清空"）
  const policyBody = () => {
    const p = detail?.price_policy;
    const initPV = p?.purchase_input_value != null ? Number(p.purchase_input_value) : null;
    const initSV = p?.sales_input_value != null ? Number(p.sales_input_value) : null;
    const initPB = p?.purchase_input_basis ?? "ex_tax";
    const initSB = p?.sales_input_basis ?? "ex_tax";
    const body: Parameters<typeof setPnPoolPolicy>[1] = {
      version: detail?.version ?? 0, note: note.trim() || null,
    };
    if (purchaseValue == null) {
      if (initPV != null) body.purchase_unset = true;                    // 用户显式清空了已设值
    } else if (purchaseValue !== initPV || purchaseBasis !== initPB) {
      body.purchase_value = purchaseValue; body.purchase_basis = purchaseBasis;
    }
    if (salesValue == null) {
      if (initSV != null) body.sales_unset = true;
    } else if (salesValue !== initSV || salesBasis !== initSB) {
      body.sales_value = salesValue; body.sales_basis = salesBasis;
    }
    return body;
  };
  const policyDirty = useMemo(() => {
    if (!detail) return false;
    const b = policyBody();
    return b.purchase_unset || b.sales_unset
      || b.purchase_value !== undefined || b.sales_value !== undefined;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail, purchaseValue, purchaseBasis, salesValue, salesBasis]);

  // 基线刷新守卫：只有当拉回的 version 恰好是我们预期的值（自己刚保存产生的新版本，
  // 或失败时的原版本）才允许"只换基线、保留用户输入"；出现非预期的版本推进 = 他人
  // 并发修改——必须整表单回填并明确提示。否则"旧表单 × 新版本号"的分裂态会让下一次
  // 保存无冲突地滚回他人写入，击穿乐观锁"绝不静默覆盖"的承诺。
  const refreshBaseline = async (ctx: DrawerRequestContext, expectedVersion: number) => {
    if (ctx.groupId == null || !isDrawerContextCurrent(ctx)) return;
    try {
      const { data } = await getPnPool(ctx.groupId);
      if (!isPoolResponseCurrent(ctx, data.group_id)) return;
      if (data.version !== expectedVersion || data.status !== "active") {
        hydrate(data);
        message.warning("该池刚被他人同时修改，已重新加载最新数据，请确认后再继续编辑");
      } else {
        setDetail(data);
      }
    } catch { /* 拉不到就保持现状，用户可关闭抽屉重开 */ }
  };

  const handleSaveError = async (
    ctx: DrawerRequestContext, expectedVersion: number, e: any, fallback: string,
  ) => {
    if (ctx.groupId == null || !isDrawerContextCurrent(ctx)) {
      await reloadCurrentList();
      return;
    }
    const isConflict = e?.response?.status === 409;
    message.error(e?.response?.data?.detail
      || (isConflict ? "保存冲突：该池刚被他人修改，已重新加载最新数据" : fallback));
    if (isConflict) {
      // 409：他人已改，整表单回填最新值；跨池/已关闭时旧响应直接丢弃。
      try {
        const { data } = await getPnPool(ctx.groupId);
        if (isPoolResponseCurrent(ctx, data.group_id)) hydrate(data);
      } catch { /* 拉不到就保持现状 */ }
    } else {
      // 其它错误：本次保存未生效，预期版本不变；若版本仍被推进说明有并发修改
      await refreshBaseline(ctx, expectedVersion);
    }
    await reloadCurrentList();
  };

  // ---- 三个独立保存动作：每个按钮恰好一个请求，成功/失败独立呈现 ----
  const saveInfo = async () => {
    if (!detail) return;
    const ctx = currentEditContext(detail.group_id);
    if (!ctx) return;
    const expectedVersion = detail.version;
    if (!name.trim()) { message.warning("请输入池名称"); return; }
    setSavingInfo(true);
    try {
      const nameChanged = name.trim() !== detail.name;
      const descChanged = (description.trim() || null) !== (detail.description ?? null);
      const { data } = await updatePnPool(detail.group_id, {
        version: detail.version,
        ...(nameChanged ? { name: name.trim() } : {}),
        ...(descChanged ? { description: description.trim() || null } : {}),
        note: note.trim() || null,
      });
      if (!isPoolResponseCurrent(ctx, data.group_id)) {
        await reloadCurrentList();
        return;
      }
      message.success("基本信息已保存");
      await refreshBaseline(ctx, data.version);
      await reloadCurrentList();
    } catch (e: any) {
      await handleSaveError(ctx, expectedVersion, e, "基本信息保存失败");
    } finally {
      if (isDrawerContextCurrent(ctx)) setSavingInfo(false);
    }
  };

  const saveMembers = async () => {
    if (!detail) return;
    const ctx = currentEditContext(detail.group_id);
    if (!ctx) return;
    const expectedVersion = detail.version;
    if (memberIds.length < MIN_MEMBERS) {
      message.warning(`有效池至少包含 ${MIN_MEMBERS} 个 PN`);
      return;
    }
    setSavingMembers(true);
    try {
      const { data } = await updatePnPoolMembers(detail.group_id, {
        version: detail.version,
        add_part_ids: memberDiff.add, remove_part_ids: memberDiff.remove,
        note: note.trim() || null,
      });
      if (!isPoolResponseCurrent(ctx, data.group_id)) {
        await reloadCurrentList();
        return;
      }
      message.success("成员变更已保存");
      await refreshBaseline(ctx, data.version);
      await reloadCurrentList();
    } catch (e: any) {
      await handleSaveError(ctx, expectedVersion, e, "成员保存失败");
    } finally {
      if (isDrawerContextCurrent(ctx)) setSavingMembers(false);
    }
  };

  const savePolicy = async () => {
    if (!detail) return;
    const ctx = currentEditContext(detail.group_id);
    if (!ctx) return;
    const expectedVersion = detail.version;
    setSavingPolicy(true);
    try {
      const { data } = await setPnPoolPolicy(detail.group_id, policyBody());
      if (!isPoolResponseCurrent(ctx, data.group_id)) {
        await reloadCurrentList();
        return;
      }
      message.success("约束价已保存");
      await refreshBaseline(ctx, data.version);
      await reloadCurrentList();
    } catch (e: any) {
      await handleSaveError(ctx, expectedVersion, e, "约束价保存失败");
    } finally {
      if (isDrawerContextCurrent(ctx)) setSavingPolicy(false);
    }
  };

  // 新建 = 单个 POST（名称/说明/成员一个事务）；约束价创建后在编辑抽屉里单独设置
  const create = async () => {
    const ctx = { ...drawerContext.current };
    if (ctx.mode !== "create") return;
    if (!name.trim()) { message.warning("请输入池名称"); return; }
    if (memberIds.length < MIN_MEMBERS) {
      message.warning(`有效池至少包含 ${MIN_MEMBERS} 个 PN`);
      return;
    }
    setCreating(true);
    try {
      const { data: created } = await createPnPool({
        name: name.trim(), description: description.trim() || null,
        member_part_ids: memberIds, note: note.trim() || null,
      });
      await reloadCurrentList();
      if (!isDrawerContextCurrent(ctx)) return;
      message.success(canSetPolicy ? "池已创建；可继续设置约束价" : "池已创建");
      if (canSetPolicy) {
        await openEdit(created.group_id);   // 顺手设约束价：进入编辑抽屉（独立的保存动作）
      } else {
        closeDrawer();
      }
    } catch (e: any) {
      if (!isDrawerContextCurrent(ctx)) {
        await reloadCurrentList();
        return;
      }
      message.error(e?.response?.data?.detail || "建池失败");
      await reloadCurrentList();
    } finally {
      if (isDrawerContextCurrent(ctx)) setCreating(false);
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
    reloadCurrentList();   // 失败（含 409 版本冲突）也刷新，拿到最新 version
  };

  // ---- 列表列 ----
  const columns: ColumnsType<PnPoolRow> = [
    {
      title: "池名称", dataIndex: "name", width: 220, ellipsis: true,
      render: (v, r) => (
        <span>
          <span style={{ fontWeight: 500 }}>{v}</span>
          {r.description && (
            <div style={{ fontSize: 12, ...MUTED }}>{r.description}</div>
          )}
        </span>
      ),
    },
    { title: "成员数", dataIndex: "member_count", width: 80, align: "right" },
    {
      title: "采购上限(未税)", dataIndex: "purchase_ceiling_ex_tax", width: 130, align: "right",
      render: (v, r) => fmtMoney(v, r.price_restricted),
    },
    {
      title: "销售下限(未税)", dataIndex: "sales_floor_ex_tax", width: 130, align: "right",
      render: (v, r) => fmtMoney(v, r.price_restricted),
    },
    { title: "来源", dataIndex: "source", width: 110, render: (v) => SOURCE_LABEL[v] || v },
    {
      title: "状态", dataIndex: "status", width: 90,
      render: (v) => (v === "active" ? <Tag color="green">有效</Tag> : <Tag>已归档</Tag>),
    },
    { title: "更新人", dataIndex: "updated_by", width: 100, render: (v) => v || "—" },
    { title: "更新时间", dataIndex: "updated_at", width: 160, render: fmtTime },
    {
      // 操作全部用真实 Button（可 Tab 聚焦、Enter/Space 触发、有焦点环）——
      // 无 href 的 <a onClick> 键盘不可达（复审非阻塞 2）
      title: "操作", key: "op", width: 120, fixed: isMobile ? undefined : "right",
      render: (_, r) => (
        <Space size={0}>
          <Button type="link" size="small" style={{ paddingInline: 4 }}
            onClick={() => openEdit(r.group_id)}>
            {r.status === "active" && (canManage || canSetPolicy) ? "编辑" : "查看"}
          </Button>
          {canManage && (r.status === "active" ? (
            <Popconfirm title="归档该池？" description="归档后退出经营分析，可随时恢复。"
              onConfirm={() => doLifecycle(r, "archive")}>
              <Button type="link" size="small" danger style={{ paddingInline: 4 }}>归档</Button>
            </Popconfirm>
          ) : (
            <Popconfirm title="恢复该池为有效？" onConfirm={() => doLifecycle(r, "restore")}>
              <Button type="link" size="small" style={{ paddingInline: 4, color: "#389e0d" }}>
                恢复
              </Button>
            </Popconfirm>
          ))}
        </Space>
      ),
    },
  ];

  // ---- 抽屉内容 ----
  const policy = detail?.price_policy;
  const isArchived = mode === "edit" && detail?.status === "archived";
  const priceRestricted = detail?.price_restricted ?? false;
  const sectionTitle = (t: string) => (
    <div style={{ fontWeight: 600, marginBottom: 8 }}>{t}</div>
  );
  const sectionBox: CSSProperties = {
    padding: 12, border: "1px solid var(--mb-border, #eee)", borderRadius: 8,
  };

  // 归档池：只读档案 + 明确"先恢复"提示，绝不渲染必然 400 的可保存表单
  const archivedView = detail && (
    <Space direction="vertical" size={14} style={{ width: "100%" }}>
      <Alert type="warning" showIcon message="该池已归档，处于只读状态"
        description={canManage
          ? "归档池不参与经营分析，也不能编辑。如需修改，请先在列表中「恢复」该池。"
          : "归档池不参与经营分析，也不能编辑。恢复操作需要池维护权限。"} />
      <div>
        <div style={{ fontWeight: 600 }}>{detail.name}</div>
        {detail.description && <div style={{ marginTop: 4, ...MUTED }}>{detail.description}</div>}
      </div>
      <div>
        {sectionTitle(`PN 成员（${detail.members.length}）`)}
        <Space size={[6, 6]} wrap>
          {detail.members.map((m) => (
            <Tag key={m.part_id}>{memberLabel(m.pn_std, m.description)}</Tag>
          ))}
        </Space>
      </div>
      <div>
        {sectionTitle("约束价（归档时快照）")}
        {priceRestricted ? (
          <span style={MUTED}>无价格权限</span>
        ) : (
          <div style={{ lineHeight: 1.9 }}>
            <div>采购上限（未税）：{policy?.purchase_ceiling_ex_tax != null
              ? Number(policy.purchase_ceiling_ex_tax).toFixed(2) : "未设置"}</div>
            <div>销售下限（未税）：{policy?.sales_floor_ex_tax != null
              ? Number(policy.sales_floor_ex_tax).toFixed(2) : "未设置"}</div>
          </div>
        )}
      </div>
      <div style={{ fontSize: 12.5, ...MUTED }}>
        最近变更：{detail.updated_by || "—"} · {fmtTime(detail.updated_at)}
      </div>
    </Space>
  );

  const infoSection = (
    <div style={sectionBox}>
      {sectionTitle("基本信息")}
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <div>
          <label style={{ display: "block", marginBottom: 4 }}>
            池名称<span style={{ color: "#cf1322" }}> *</span>
          </label>
          <Input value={name} maxLength={128} placeholder="如 8TB 7.2K SATA 企业盘互通池"
            disabled={!canManage}
            onChange={(e) => setName(e.target.value)} />
        </div>
        <div>
          <label style={{ display: "block", marginBottom: 4 }}>用途 / 说明</label>
          <Input.TextArea value={description} rows={2}
            placeholder="这个池覆盖哪些场景、为什么互通（选填）"
            disabled={!canManage}
            onChange={(e) => setDescription(e.target.value)} />
        </div>
        {mode === "edit" && canManage && (
          <Button type="primary" loading={savingInfo} disabled={!infoDirty} onClick={saveInfo}>
            保存基本信息
          </Button>
        )}
        {mode === "edit" && !canManage && (
          <span style={{ fontSize: 12, ...MUTED }}>无池维护权限，名称与说明只读。</span>
        )}
      </Space>
    </div>
  );

  const membersSection = (
    <div style={sectionBox}>
      {sectionTitle("PN 成员")}
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
        disabled={!canManage}
        notFoundContent={fetching ? <Spin size="small" /> : "输入关键词搜索型号"}
        maxTagCount={isMobile ? 6 : undefined}
      />
      <div style={{ marginTop: 4, fontSize: 12, ...MUTED }}>
        有效池至少 {MIN_MEMBERS} 个 PN；一个有效 PN 只能属于一个有效池；保存时按增删差异生效。
      </div>
      {mode === "edit" && canManage && (
        <Button type="primary" style={{ marginTop: 8 }} loading={savingMembers}
          disabled={!membersDirty} onClick={saveMembers}>
          保存成员变更
        </Button>
      )}
      {mode === "edit" && !canManage && (
        <div style={{ marginTop: 4, fontSize: 12, ...MUTED }}>无池维护权限，成员只读。</div>
      )}
    </div>
  );

  const policyInputs = (
    <>
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
      <div style={{ marginTop: 6, fontSize: 12, ...MUTED }}>
        统一按未税入库：选「含税」将 ÷1.13 自动换算为未税，原始录入值与口径保留可查。
        把已设的一侧清空并保存 = 显式取消该侧约束；没动的一侧保持原值。
      </div>
      <Button type="primary" style={{ marginTop: 8 }} loading={savingPolicy}
        disabled={!policyDirty} onClick={savePolicy}>
        保存约束价
      </Button>
    </>
  );

  const policyReadonly = (
    <div style={{ lineHeight: 1.9 }}>
      {priceRestricted ? (
        <span style={MUTED}>无价格权限</span>
      ) : (
        <>
          <div>采购上限（未税）：{policy?.purchase_ceiling_ex_tax != null
            ? Number(policy.purchase_ceiling_ex_tax).toFixed(2) : "未设置"}</div>
          <div>销售下限（未税）：{policy?.sales_floor_ex_tax != null
            ? Number(policy.sales_floor_ex_tax).toFixed(2) : "未设置"}</div>
        </>
      )}
    </div>
  );

  const policySection = mode === "edit" && (
    <div style={sectionBox}>
      {sectionTitle("约束价")}
      {!canSetPolicy ? (
        <>
          <Alert type="info" showIcon style={{ marginBottom: 8 }}
            message="无约束价设置权限"
            description="约束价（采购上限/销售下限）需要「池约束价设置」权限，请联系管理员开通。" />
          {policyReadonly}
        </>
      ) : priceRestricted ? (
        // 理论上不可达（后端已禁止"可写不可读"组合），防御性兜底
        <Alert type="warning" showIcon message="无价格权限"
          description="当前账号看不到约束价现值，为避免误改已禁用编辑。" />
      ) : policyInputs}
      {policy && !priceRestricted && (
        <div style={{ marginTop: 8, fontSize: 12.5, ...MUTED }}>
          当前约束（原始录入）：采购上限 {policy.purchase_input_value != null
            ? `${Number(policy.purchase_input_value).toFixed(2)}（${basisLabel(policy.purchase_input_basis)}）`
            : "未设置"} · 销售下限 {policy.sales_input_value != null
            ? `${Number(policy.sales_input_value).toFixed(2)}（${basisLabel(policy.sales_input_basis)}）`
            : "未设置"}
          <div>约束设置人：{policy.changed_by || "—"} · {fmtTime(policy.valid_from)}</div>
        </div>
      )}
    </div>
  );

  const editableView = (
    <Space direction="vertical" size={14} style={{ width: "100%" }}>
      {infoSection}
      {membersSection}
      {policySection}
      <div>
        <label style={{ display: "block", marginBottom: 4 }}>变更备注</label>
        <Input value={note} maxLength={200}
          placeholder="本次改动的原因（选填，随各保存动作留痕）"
          onChange={(e) => setNote(e.target.value)} />
      </div>
      {mode === "create" && (
        <Button type="primary" loading={creating} onClick={create}>创建池</Button>
      )}
      {mode === "edit" && detail && (
        <div style={{ fontSize: 12.5, ...MUTED }}>
          最近变更：{detail.updated_by || "—"} · {fmtTime(detail.updated_at)}
        </div>
      )}
    </Space>
  );

  return (
    <div>
      <PageHeader
        title="互通PN池管理"
        subtitle="人工维护的互通池是唯一真值：建池、调成员、设约束价（采购上限/销售下限，统一未税口径），保存即生效。"
        extra={canManage
          ? <Button type="primary" onClick={openCreate}>新建池</Button>
          : undefined}
      />

      <PoolPolicyCoverage
        coverage={coverage}
        restricted={coverageRestricted || localCoverageRestricted}
        selected={localCoverageRestricted ? null : policyMissing}
        onSelect={setPolicyMissing}
        onClear={policyMissing ? clearPolicyMissing : undefined}
      />

      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 16 }}>
        <Input.Search
          placeholder="搜索池名/成员PN/描述/品牌" allowClear enterButton
          style={{ maxWidth: 380, flex: "1 1 260px" }}
          value={q} onChange={(e) => setQ(e.target.value)}
          onSearch={(v) => load(v, statusFilter, 1,
            localCoverageRestricted ? null : policyMissing)}
        />
        <Segmented
          value={statusFilter}
          onChange={(v) => {
            const nextStatus = v as StatusFilter;
            setStatusFilter(nextStatus);
            listView.current.status = nextStatus;
            if (nextStatus !== "active" && policyMissing && !localCoverageRestricted) {
              // 归档/全部与“有效池缺失约束”不能同时成立；先清 URL，随后由 effect 按新状态取数。
              clearPolicyMissing();
              return;
            }
            load(q, nextStatus, 1, localCoverageRestricted ? null : policyMissing);
          }}
          options={[
            { label: "有效", value: "active" },
            { label: "已归档", value: "archived" },
            { label: "全部", value: "all" },
          ]}
        />
      </div>

      {listError && (
        <Alert type="error" showIcon style={{ marginBottom: 12 }} message={listError}
          description="当前列表未展示任何旧筛选结果，请重试加载。"
          action={<Button size="small" onClick={reloadCurrentList}
            aria-label="重试加载池列表">重试</Button>} />
      )}

      <Table<PnPoolRow>
        rowKey="group_id" size="small" columns={columns} dataSource={rows} loading={loading}
        scroll={{ x: 1190 }}   // ≥列宽总和，防止固定操作列悬浮盖住更新时间列
        pagination={{
          current: page, pageSize: PAGE_SIZE, total, showSizeChanger: false,
          showTotal: (t) => `共 ${t} 个池`,
          onChange: (p) => load(q, statusFilter, p,
            localCoverageRestricted ? null : policyMissing),
        }}
      />

      <Drawer
        width={isMobile ? "100%" : 720}
        open={mode !== null}
        onClose={closeDrawer}
        title={mode === "create" ? "新建互通PN池"
          : detail ? `${isArchived ? "查看归档池" : "编辑池"} · ${detail.name}` : "编辑池"}
      >
        {detailLoading ? (
          <div style={{ textAlign: "center", padding: 48 }}><Spin /></div>
        ) : isArchived ? archivedView : editableView}
      </Drawer>
    </div>
  );
}
