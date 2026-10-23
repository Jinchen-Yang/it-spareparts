import CatalogPartPanel from "../components/pnCatalog/CatalogPartPanel";
import PageSkeleton from "../components/PageSkeleton";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { App, Input, Card, Descriptions, Tag, Statistic, Tabs, Empty, Space, Button, Select, InputNumber, Alert, Table, Popconfirm, Tooltip, Modal } from "antd";
import { EditOutlined, RightOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import ResizableTable from "../components/ResizableTable";
import PageHeader from "../components/PageHeader";
import PoolReferencePanel from "../components/pools/PoolReferencePanel";
import InlinePartEditModal from "../components/parts/InlinePartEditModal";
import api from "../api";
import type { Overview, PurchaseRow, SalesRow, InventoryRow } from "../api";
import { unifiedSearch, fetchOverview } from "../api/search";
import type { SearchFilters, UnifiedSearchItem, UnifiedSearchResp } from "../api/search";
import "./PartSearchPage.css";
import { completeTaxPair, money, pct, splitByFlag, splitFixed } from "../utils/format";
import type { TaxSplit } from "../utils/format";
import { useTaxBasis, TaxMoney } from "../context/TaxBasis";
import { poolAnalysisReturnPath } from "../utils/poolAnalysisNavigation";
const errMsg = (e: any) =>
  !e?.response ? "无法连接服务器，请检查网络后重试"
  : e?.response?.data?.detail || `加载失败（${e?.response?.status ?? "?"}），请稍后重试`;

// 按登录用户权限决定是否展示成本/利润卡片（后端已把值脱敏成 null，前端再把空卡片整张藏掉）
const canSee = (key: string) => {
  if ((localStorage.getItem("role") || "") === "admin") return true;
  try { return JSON.parse(localStorage.getItem("permissions") || "{}")[key] === true; }
  catch { return false; }
};

function purchasePricePair(row: PurchaseRow): TaxSplit {
  // 甲方明确：未标注税口径统一按未税。旧/新后端若错误地把 None 归到含税，
  // 这里仍以该业务规则为准；已明确口径时优先使用后端双值。
  if (row.is_tax_inclusive == null) return splitByFlag(row.unit_price, null);
  const explicit = completeTaxPair(row.price_inc, row.price_ex);
  return explicit.inc != null || explicit.ex != null
    ? explicit
    : splitByFlag(row.unit_price, row.is_tax_inclusive);
}

function salesPricePair(row: SalesRow): TaxSplit {
  const explicit = completeTaxPair(row.price_inc, row.price_ex);
  return explicit.inc != null || explicit.ex != null
    ? explicit
    : splitFixed(row.unit_price, "inc");
}

const filterKeys = ["part_type", "interface", "capacity_min", "capacity_max", "category_major", "category_minor"] as const;
function readFilters(params: URLSearchParams): SearchFilters {
  const filters: SearchFilters = {};
  for (const key of filterKeys) {
    const value = params.get(key);
    if (!value) continue;
    if (key === "capacity_min" || key === "capacity_max") {
      const number = Number(value);
      if (Number.isFinite(number) && number >= 0) filters[key] = number;
    } else filters[key] = value;
  }
  return filters;
}

/**
 * 型号查询：URL 驱动（与采购三页同范式）——
 *   /parts?q=<查询>            搜索
 *   /parts?part_id=<ID>        稳定深链：直开该型号全景（刷新/前进/后退/分享均保持）
 *   /parts?pn=<PN>             兼容入口：按 PN 解析后自动改写成 part_id 深链
 * 精确命中（exact）唯一主结果自动打开全景，相似候选降级到"相似型号"独立区域。
 */
export default function PartSearchPage({ catalog }: { catalog?: { mode: "legacy" | "shared"; canEdit: boolean } } = {}) {
  const { message } = App.useApp();
  const [sp, setSp] = useSearchParams();
  const urlQ = sp.get("q") || "";
  const urlFilters = readFilters(sp);
  const filterQuery = JSON.stringify(urlFilters);
  const urlPartId = sp.get("part_id");
  const urlPn = sp.get("pn");
  const returnToPool = poolAnalysisReturnPath(sp);
  // 权限快照绑定当前登录周期；App 在 token 变化时重挂 Router/页面。
  // 组件存活期间不重读可变 localStorage，避免 DevTools 改值后借无关重渲染点亮入口。
  const [{ canEditSubstitutes, canEditPartDetails: legacyCanEditPartDetails }] = useState(() => {
    const role = localStorage.getItem("role") || "";
    return {
      canEditSubstitutes: ["admin", "purchaser"].includes(role),
      // 详情页轻量编辑与「备件主数据」共用权限；后端 /parts/master 仍做最终准入。
      canEditPartDetails: canSee("page_master_data"),
    };
  });

  const canEditPartDetails = catalog?.mode === "shared" ? catalog.canEdit : legacyCanEditPartDetails;

  const [qInput, setQInput] = useState(urlQ);
  const [queryError, setQueryError] = useState(false);
  const [resp, setResp] = useState<UnifiedSearchResp | null>(null);
  const [searching, setSearching] = useState(false);
  const [ov, setOv] = useState<Overview | null>(null);
  const [loadingOv, setLoadingOv] = useState(false);
  const [subPn, setSubPn] = useState("");
  const [inlineEditPn, setInlineEditPn] = useState<string | null>(null);
  useEffect(() => { setInlineEditPn(null); }, [urlPartId, urlPn]);
  // 替代料卡片折叠（记忆本机）
  const [subsOpen, setSubsOpen] = useState(() => localStorage.getItem("ps_subs_open") !== "0");
  const toggleSubs = () => setSubsOpen((o) => { localStorage.setItem("ps_subs_open", o ? "0" : "1"); return !o; });
  const [error, setError] = useState<string | null>(null);
  // 代次守卫：慢响应不得覆盖新请求的结果（与采购页 loadSeqRef 同范式）
  const searchSeq = useRef(0);
  const ovSeq = useRef(0);
  // 编辑中的条件只在提交时写入 URL；刷新和前进/后退可完整重放组合查询
  const [partType, setPartType] = useState<string | undefined>(urlFilters.part_type);
  const [iface, setIface] = useState<string | undefined>(urlFilters.interface);
  const [capMin, setCapMin] = useState<number | null>(urlFilters.capacity_min ?? null);
  const [capMax, setCapMax] = useState<number | null>(urlFilters.capacity_max ?? null);
  // 品类筛选（宋总 2026-07-05：全品类按分类查，不只硬盘）—— 一级 → 二级级联
  const [cats, setCats] = useState<{ major: string; minors: string[] }[]>([]);
  const [catMajor, setCatMajor] = useState<string | undefined>(urlFilters.category_major);
  const [catMinor, setCatMinor] = useState<string | undefined>(urlFilters.category_minor);
  useEffect(() => {
    api.get("/parts/categories").then(({ data }) => setCats(data.categories || [])).catch(() => {});
  }, []);
  const minorOpts = cats.find((c) => c.major === catMajor)?.minors || [];

  const canCost = canSee("data_purchase_cost");   // 销售看不到 → 隐藏成本卡片
  const canProfit = canSee("data_profit");
  const purchaseBasis = useTaxBasis("purchase");
  const salesBasis = useTaxBasis("sales");

  /** URL 增量更新：push 进历史（深链/后退语义），replace 用于精确命中自动选中等改写 */
  const patchUrl = (next: Record<string, string | undefined>, replace = false) => {
    setSp((prev) => {
      const merged = new URLSearchParams(prev);
      for (const [k, v] of Object.entries(next)) {
        if (v === undefined || v === "") merged.delete(k);
        else merged.set(k, v);
      }
      return merged;
    }, { replace });
  };

  const runSearch = async (q: string, filters?: SearchFilters, selectExact = !sp.get("part_id")) => {
    const seq = ++searchSeq.current;
    setSearching(true);
    setError(null);
    try {
      const data = await unifiedSearch(q, { pageSize: 20, filters });
      if (seq !== searchSeq.current) return;   // 已有更新的搜索在跑
      setResp(data);
      if (data.items.length === 0) message.info("没有匹配的型号");
      // 精确即唯一：自动打开唯一主结果的全景（replace 不额外堆历史，后退直接回搜索前）
      if (data.exact && data.items[0]?.part_id && selectExact) {
        patchUrl({ part_id: String(data.items[0].part_id), pn: undefined }, true);
      }
    } catch (e) {
      if (seq !== searchSeq.current) return;
      setResp(null);
      const msg = errMsg(e);
      setError(msg);
      message.error(msg);
    } finally {
      if (seq === searchSeq.current) setSearching(false);
    }
  };

  // 只订阅查询条件；精确命中补写 part_id 不会再发一次搜索。
  useEffect(() => {
    const filters: SearchFilters = JSON.parse(filterQuery);
    setQInput(urlQ);
    setQueryError(false);
    setPartType(filters.part_type);
    setIface(filters.interface);
    setCapMin(filters.capacity_min ?? null);
    setCapMax(filters.capacity_max ?? null);
    setCatMajor(filters.category_major);
    setCatMinor(filters.category_minor);
    if (urlQ.trim() || Object.keys(filters).length) runSearch(urlQ, filters);
    else {
      setResp(null);
      setSearching(false);
    }
    return () => { searchSeq.current += 1; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlQ, filterQuery]);

  const loadOverview = async (key: { part_id: number } | { pn_std: string }) => {
    const seq = ++ovSeq.current;
    setLoadingOv(true);
    setError(null);
    try {
      const data = await fetchOverview(key);
      if (seq !== ovSeq.current) return;
      setOv(data);
      // pn 兼容入口 / 旧深链：统一改写成稳定 part_id 深链（replace 不堆历史）
      if (data.part?.id && String(data.part.id) !== sp.get("part_id")) {
        patchUrl({ part_id: String(data.part.id), pn: undefined }, true);
      }
    } catch (e) {
      if (seq !== ovSeq.current) return;
      setOv(null);
      const msg = errMsg(e);
      setError(msg);
      message.error(msg);
    } finally {
      if (seq === ovSeq.current) setLoadingOv(false);
    }
  };

  // URL part_id / pn → 型号全景（稳定深链；后退/前进保持选中型号）
  useEffect(() => {
    if (urlPartId) {
      const idNum = Number(urlPartId);
      if (!Number.isFinite(idNum) || idNum <= 0) {
        ovSeq.current += 1;
        setLoadingOv(false);
        setOv(null);
        return;
      }
      if (ov?.part?.id === idNum) {
        // 返回已缓存型号时仍要废弃离开期间启动的请求，避免 A→慢B→A 后 B 覆盖 A。
        ovSeq.current += 1;
        setLoadingOv(false);
        return;
      }
      loadOverview({ part_id: idNum });
    } else if (urlPn) {
      loadOverview({ pn_std: urlPn });
    } else {
      ovSeq.current += 1;
      setLoadingOv(false);
      setOv(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlPartId, urlPn]);

  const onSearch = () => {
    const nextFilters = {
      part_type: partType, interface: iface,
      capacity_min: capMin == null ? undefined : String(capMin),
      capacity_max: capMax == null ? undefined : String(capMax),
      category_major: catMajor, category_minor: catMinor,
    };
    const filters = readFilters(new URLSearchParams(
      Object.entries(nextFilters).filter((entry): entry is [string, string] => entry[1] !== undefined),
    ));
    const query = qInput.trim();
    if (!query && !Object.keys(filters).length) {
      setQueryError(true);
      return;
    }
    setQueryError(false);
    // 新条件产生一条历史；同条件重查用 replace 刷新，不堆历史，也不重复发请求。
    const unchanged = query === urlQ && JSON.stringify(filters) === filterQuery;
    patchUrl({ ...nextFilters, q: query, part_id: undefined, pn: undefined }, unchanged && !urlPartId && !urlPn);
    if (unchanged) runSearch(query, filters, true);
  };

  /** 点击结果行：part_id 深链（push 进历史，后退回到列表） */
  const openPart = (it: UnifiedSearchItem) => {
    if (it.part_id) patchUrl({ part_id: String(it.part_id), pn: undefined });
    else patchUrl({ pn: it.pn_std, part_id: undefined });   // 极端兜底：无 id 走 pn 兼容
  };
  /** 替代料等只有 PN 的入口：pn 兼容深链（加载后自动改写成 part_id） */
  const openByPn = (pn: string) => patchUrl({ pn, part_id: undefined });

  const addSubstitute = async () => {
    if (!ov || !subPn.trim()) return;
    try {
      const { data } = await api.post("/substitutes", { pn_a: ov.part.pn_std, pn_b: subPn.trim() });
      message.success(data.created ? "已添加替代料" : "该替代关系已存在");
      setSubPn("");
      loadOverview({ part_id: ov.part.id });
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "添加失败（需管理员或采购权限）");
    }
  };

  const removeSubstitute = async (pnB: string) => {
    if (!ov) return;
    try {
      const { data } = await api.delete("/substitutes", { params: { pn_a: ov.part.pn_std, pn_b: pnB } });
      message.success(data.deleted ? "已解除替代关系" : "未找到该直连替代关系");
      loadOverview({ part_id: ov.part.id });
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "解除失败（需管理员或采购权限）");
    }
  };

  const hitCols: ColumnsType<UnifiedSearchItem> = [
    {
      title: "型号 (PN)", dataIndex: "pn_std", width: 220,
      render: (v, r) => (
        <Button type="link" size="small" onClick={() => openPart(r)}>
          {v} {r.needs_review && <Tag color="orange">待复核</Tag>}
          {r.is_excluded && <Tag color="red">已排除</Tag>}
        </Button>
      ),
    },
    {
      title: "匹配度", dataIndex: "score", width: 150,
      render: (v: number | undefined, r) =>
        v == null ? "-" : (
          <span title={`${r.match_reason || ""}${r.matched_text ? `｜命中：${r.matched_text}` : ""}`}>
            <Tag color={r.match_type === "exact_pn" || r.match_type === "exact_alias"
              ? "green" : v >= 0.6 ? "cyan" : v >= 0.35 ? "blue" : "default"}>
              {r.match_type === "exact_pn" ? "精确"
                : r.match_type === "exact_alias" ? "别名精确"
                : `${(v * 100).toFixed(0)}%`}
            </Tag>
            <span style={{ color: "#999", fontSize: 12 }}>
              {(r.match_reason || "").split("；")[0]}
            </span>
          </span>
        ),
    },
    { title: "描述", dataIndex: "description", ellipsis: true },
    { title: "品牌", dataIndex: "brand", width: 140 },
    { title: "品类", dataIndex: "category_major", width: 140 },
    { title: "互通池", dataIndex: "pool_name", width: 150, ellipsis: true,
      render: (v: string | null, r) => v
        ? <Tag color="geekblue" title={`池 ID ${r.pool_group_id}`}>{v}</Tag>
        : <span style={{ color: "#bbb" }}>-</span> },
    { title: "规格", key: "specs", width: 240,
      render: (_, r: any) => {
        const s = r.specs || {};
        const order = ["part_type", "capacity", "interface", "rpm", "generation", "frequency", "form_factor"];
        const tags = order.filter((k) => s[k]).map((k) => <Tag key={k}>{s[k]}</Tag>);
        return tags.length ? tags : <span style={{ color: "#bbb" }}>-</span>;
      } },
  ];

  const purCols: ColumnsType<PurchaseRow> = [
    { title: "采购单号", dataIndex: "order_no", width: 170, ellipsis: true },
    { title: "日期", dataIndex: "order_date", width: 110 },
    { title: "类型", dataIndex: "source_type", width: 90,
      render: (v: string | null) => v === "维保需求"
        ? <Tag color="orange" title="不计入成本">维保</Tag>
        : v === "回收" ? <Tag>回收</Tag>
        : v },
    { title: "供应商", dataIndex: "supplier", width: 160, ellipsis: true },
    { title: "数量", dataIndex: "qty", width: 70 },
    // 单价按订单税口径解释原值，缺失侧统一按 13% 补齐；未标注按未税原值。
    ...(purchaseBasis !== "ex" ? [{ title: "单价(含税)", key: "up_inc", width: 90, align: "right" as const,
      render: (_: unknown, r: PurchaseRow) => money(purchasePricePair(r).inc) }] as ColumnsType<PurchaseRow> : []),
    ...(purchaseBasis !== "inc" ? [{ title: "单价(不含税)", key: "up_ex", width: 90, align: "right" as const,
      render: (_: unknown, r: PurchaseRow) => money(purchasePricePair(r).ex) }] as ColumnsType<PurchaseRow> : []),
  ];
  const salCols: ColumnsType<SalesRow> = [
    { title: "销售单号", dataIndex: "order_no", width: 170, ellipsis: true },
    { title: "日期", dataIndex: "order_date", width: 110 },
    { title: "客户", dataIndex: "customer", width: 160, ellipsis: true },
    { title: "数量", dataIndex: "qty", width: 70 },
    // 销售价原值为含税，未税侧统一按 13% 换算。
    ...(salesBasis !== "ex" ? [{ title: "单价(含税)", key: "sp_inc", width: 100, align: "right" as const,
      render: (_: unknown, r: SalesRow) => money(salesPricePair(r).inc) }] as ColumnsType<SalesRow> : []),
    ...(salesBasis !== "inc" ? [{ title: "单价(不含税)", key: "sp_ex", width: 100, align: "right" as const,
      render: (_: unknown, r: SalesRow) => money(salesPricePair(r).ex) }] as ColumnsType<SalesRow> : []),
  ];
  const invCols: ColumnsType<InventoryRow> = [
    { title: "仓库", dataIndex: "warehouse" },
    { title: "可用数量", dataIndex: "display_qty", width: 100 },
    { title: "源系统数量", dataIndex: "source_qty", width: 110 },
    // 成本/库存估值原值为未税，含税侧统一按 13% 换算。
    ...(purchaseBasis !== "ex" ? [{ title: "单位成本(含税)", key: "uc_inc", width: 110, align: "right" as const,
      render: (_: unknown, r: InventoryRow) => money(splitFixed(r.unit_cost, "ex").inc) }] as ColumnsType<InventoryRow> : []),
    ...(purchaseBasis !== "inc" ? [{ title: "单位成本(不含税)", key: "uc_ex", width: 110, align: "right" as const,
      render: (_: unknown, r: InventoryRow) => money(splitFixed(r.unit_cost, "ex").ex) }] as ColumnsType<InventoryRow> : []),
    ...(purchaseBasis !== "ex" ? [{ title: "库存金额(含税)", key: "iv_inc", width: 120, align: "right" as const,
      render: (_: unknown, r: InventoryRow) => money(splitFixed(r.inventory_value, "ex").inc) }] as ColumnsType<InventoryRow> : []),
    ...(purchaseBasis !== "inc" ? [{ title: "库存金额(不含税)", key: "iv_ex", width: 120, align: "right" as const,
      render: (_: unknown, r: InventoryRow) => money(splitFixed(r.inventory_value, "ex").ex) }] as ColumnsType<InventoryRow> : []),
  ];

  const exactItem = resp?.exact ? resp.items[0] : undefined;
  const similar = resp?.similar_items || [];
  const sharedMaster = ov?.shared_master || resp?.shared_master;

  return (
    <div className="part-search-page">
      <PageHeader
        title="型号查询"
        subtitle="按型号 / 品牌 / 描述近似搜索，点击型号查看完整全景（采购 · 销售 · 库存 · 成本 · 毛利）"
        extra={returnToPool ? <Link to={returnToPool}>返回互通池分析</Link> : undefined}
      />
      <Card className="part-search-form-card">
        <form aria-label="型号查询条件" onSubmit={(event) => { event.preventDefault(); onSearch(); }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.nativeEvent.isComposing || event.keyCode === 229)) event.preventDefault();
          }}>
          <div className="part-search-keyword">
            <Input
              aria-label="型号或关键词"
              aria-invalid={queryError || undefined}
              aria-describedby={queryError ? "part-query-error" : undefined}
              status={queryError ? "error" : undefined}
              placeholder="输入型号 (PN) 或描述关键词；规格条件可单独使用"
              size="large" value={qInput}
              onChange={(e) => { setQInput(e.target.value); setQueryError(false); }} allowClear
            />
            <Button type="primary" htmlType="submit" size="large" loading={searching}>搜索</Button>
          </div>
          {queryError && <div id="part-query-error" role="alert" className="part-query-error">请输入型号或关键词</div>}
          <div className="part-search-filters">
            <Select aria-label="一级品类" allowClear showSearch placeholder="一级品类（全品类）" value={catMajor}
              onChange={(v) => { setCatMajor(v); setCatMinor(undefined); }}
              options={cats.map((c) => ({ value: c.major }))} />
            <Select aria-label="二级品类" allowClear showSearch placeholder="二级品类" value={catMinor} onChange={setCatMinor}
              disabled={!catMajor} options={minorOpts.map((m) => ({ value: m }))} />
            <Select aria-label="部件类型" allowClear placeholder="部件类型" value={partType} onChange={setPartType}
              options={[{ value: "HDD" }, { value: "SSD" }, { value: "RAM", label: "内存" }]} />
            <Select aria-label="接口" allowClear placeholder="接口" value={iface} onChange={setIface}
              options={["SAS", "SATA", "NVME", "FC", "SCSI"].map((v) => ({ value: v }))} />
            <InputNumber aria-label="最小容量" placeholder="容量≥" suffix="GB" value={capMin} onChange={setCapMin} min={0} />
            <InputNumber aria-label="最大容量" placeholder="容量≤" suffix="GB" value={capMax} onChange={setCapMax} min={0} />
          </div>
        </form>
      </Card>

      {sharedMaster?.status === "unavailable" && <Alert type="warning" showIcon message="共享主资料暂不可用" description="型号身份和经营记录仍可查询，当前描述、品牌、品类与规格已隐藏。" />}
      {sharedMaster?.status === "restricted" && <Alert type="info" showIcon message="当前账号未获得共享主资料读取权限，型号身份和经营记录仍可查询。" />}

      {resp?.ambiguous && (
        <Alert
          type="warning" showIcon message="该写法命中多个型号（歧义）"
          description="同一写法对应多个未合并型号或多个别名指向不同目标，请人工从下表选择正确型号；如属重复数据，请在主数据管理中合并治理。"
        />
      )}

      {exactItem ? (
        <div className="part-match-results">
          <Card title="精确匹配" size="small">
            <Button type="link" size="small" onClick={() => openPart(exactItem)} style={{ fontFamily: "monospace", fontWeight: 600 }}>
              {exactItem.pn_std}
            </Button>
            <Tag color="green">{exactItem.match_type === "exact_alias"
              ? `别名精确：${exactItem.matched_text}` : "PN 精确匹配"}</Tag>
            {exactItem.pool_name && <Tag color="geekblue">互通池：{exactItem.pool_name}</Tag>}
            <div className="part-match-description">{exactItem.description || ""}</div>
          </Card>
          {similar.length > 0 && <Card title="相似型号" size="small">
            <div className="part-similar-list" aria-label="相似型号候选">
              {similar.map((it) => <div key={it.part_id}>
                <Button type="link" size="small" onClick={() => openPart(it)} title={it.description || undefined}>
                  {it.pn_std}
                </Button>
                <small>{it.match_reason || "非精确命中，仅供参考"}</small>
                {it.needs_review && <Tag color="orange">待复核</Tag>}
                {it.is_excluded && <Tag color="red">已排除</Tag>}
              </div>)}
            </div>
          </Card>}
        </div>
      ) : (resp && resp.items.length > 0 && (
        <Card title="搜索结果" size="small">
          <ResizableTable storageKey="search-hits" rowKey="pn_std" size="small" columns={hitCols}
            dataSource={resp.items} pagination={{ pageSize: 10 }} scroll={{ x: 900 }} />
        </Card>
      ))}

      {ov ? (
        <Card className="part-overview" loading={loadingOv} title={<>型号全景：<b>{ov.part.pn_std}</b>
          {ov.part.needs_review && <Tag color="orange" style={{ marginLeft: 8 }}>PN 待复核</Tag>}
          {ov.part.redirected_from && <Tag color="blue" style={{ marginLeft: 8 }}>由 {ov.part.redirected_from} 合并而来</Tag>}</>}>
          <section aria-label="型号摘要" className="part-summary">
            <div aria-label="近期成交参考价" className="part-summary-metric">
              <span>近期成交参考价</span>
              <strong>{(() => { const s = splitFixed(ov.sale_price_ref?.ref_sale_price, "inc");
                return <TaxMoney scope="sales" inc={s.inc} ex={s.ex} stack />; })()}</strong>
              {ov.sale_price_ref?.ref_sale_price != null ? <>
                <small>近 {ov.sale_price_ref.ref_sale_samples} 单 / {ov.sale_price_ref.ref_window_days} 天加权</small>
                {ov.sale_price_ref.ref_sale_samples < 3 && <Tag color="orange">成交少，仅供参考</Tag>}
              </> : <small>暂无成交参考价</small>}
            </div>
            <div aria-label="动态可用库存" className="part-summary-metric">
              <span>动态可用库存</span>
              <strong className={(ov.stock_dynamic?.dynamic_qty ?? 0) < 0 ? "is-negative" : undefined}>
                {ov.stock_dynamic?.dynamic_qty ?? "—"}
              </strong>
              <small>期初 + 入库 − 出库</small>
            </div>
            {canCost && <div aria-label="移动加权单位成本" className="part-summary-metric">
              <span>移动加权 · 单位成本</span>
              <strong>{(() => { const s = splitFixed(ov.profit_summary.avg_cost_moving, "ex");
                return <TaxMoney scope="purchase" inc={s.inc} ex={s.ex} stack />; })()}</strong>
            </div>}
            {canCost && canProfit && <div aria-label="移动加权毛利率" className="part-summary-metric">
              <span>毛利率</span>
              <strong>{pct(ov.profit_summary.avg_margin_moving)}</strong>
              <small>移动加权</small>
            </div>}
            <div className="part-summary-metric">
              <span>互通池</span>
              <PoolReferencePanel key={ov.part.id} partId={ov.part.id} summary />
            </div>
          </section>

          <section aria-label="型号基本信息" className="part-section">
            <h3>基本信息</h3>
            <Descriptions bordered size="small" column={{ xs: 1, sm: 3 }} style={{ marginBottom: 16 }}>
              <Descriptions.Item label="描述" span="filled">
                <Space size={8} wrap>
                  <span>{ov.part.description || "-"}</span>
                  {canEditPartDetails && (
                    <Tooltip title="修改描述和品类">
                      <Button
                        type="link"
                        size="small"
                        icon={<EditOutlined />}
                        aria-label={`修改型号 ${ov.part.pn_std} 的描述和品类`}
                        onClick={() => setInlineEditPn(ov.part.pn_std)}
                        style={{ paddingInline: 0 }}
                      >
                        修改
                      </Button>
                    </Tooltip>
                  )}
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="品牌">{ov.part.brand || "-"}</Descriptions.Item>
              <Descriptions.Item label="品类">{ov.part.category_major || "-"}</Descriptions.Item>
              <Descriptions.Item label="规格">{ov.part.category_minor || "-"}</Descriptions.Item>
            </Descriptions>
          </section>

          <section aria-label="型号库存" className="part-section">
            <h3>库存</h3>
            <div className="part-stock-note">
                    {ov.stock_dynamic && (
                      <span style={{ fontWeight: 400, fontSize: 13 }}>
                        动态可用 <b style={{ color: (ov.stock_dynamic.dynamic_qty ?? 0) < 0 ? "var(--mb-danger)" : "var(--mb-accent, #2f5b7c)" }}>
                          {ov.stock_dynamic.dynamic_qty ?? 0}</b>
                        <span style={{ color: "var(--mb-text-3)", fontSize: 12 }}>
                          （期初{ov.stock_dynamic.anchor_qty ?? 0}{ov.stock_dynamic.anchor_date ? `@${ov.stock_dynamic.anchor_date}` : ""} +入{ov.stock_dynamic.in_qty ?? 0} −出{(ov.stock_dynamic.out_sales ?? 0) + (ov.stock_dynamic.out_maint ?? 0)}；下表分仓为快照参考）
                        </span>
                      </span>
                    )}
            </div>
              {/* 合并后同仓可有多行（不同源 pn），rowKey 不能用 warehouse */}
              <ResizableTable storageKey="search-ov-inv" rowKey={(_, i) => String(i)} size="small" columns={invCols} dataSource={ov.inventory} pagination={false}
                locale={{ emptyText: "无库存" }} scroll={{ x: 650 }} />
          </section>

          <section aria-label="更多价格统计" className="part-section">
            <h3>更多价格统计</h3>
            <div className="part-price-stats">
              {canCost && <div>
                <Statistic title="FIFO · 单位成本"
                  valueRender={() => { const s = splitFixed(ov.profit_summary.avg_cost_fifo, "ex"); return <TaxMoney scope="purchase" inc={s.inc} ex={s.ex} stack />; }} />
                {canProfit && <span>毛利率 {pct(ov.profit_summary.avg_margin_fifo)}</span>}
              </div>}
              <div>
                <Statistic title="平均销售价"
                  valueRender={() => { const s = splitFixed(ov.profit_summary.avg_sale_price, "inc"); return <TaxMoney scope="sales" inc={s.inc} ex={s.ex} stack />; }} />
                <span>累计售 {ov.profit_summary.total_qty_sold}</span>
              </div>
              <Statistic title="询价区间" valueRender={() => {
                if (!ov.inquiry_ref.count) return <>无</>;
                const lo = splitFixed(ov.inquiry_ref.min_money, "inc");
                const hi = splitFixed(ov.inquiry_ref.max_money, "inc");
                return <><TaxMoney scope="sales" inc={lo.inc} ex={lo.ex} stack /> ~ <TaxMoney scope="sales" inc={hi.inc} ex={hi.ex} stack /></>;
              }} />
            </div>
          </section>

          <div role="group" aria-label="型号订单历史" className="part-section">
            <h3>订单历史</h3>
            <Tabs items={[
              { key: "purchase", label: "采购历史(近20)", children: (
                <section aria-label="采购订单历史">
                  <ResizableTable storageKey="search-ov-pur" rowKey={(r) => r.order_no + r.order_date} size="small" columns={purCols} dataSource={ov.purchases_recent} pagination={false} scroll={{ x: 600, y: 280 }} />
                </section>
              ) },
              { key: "sales", label: "销售历史(近20)", children: (
                <section aria-label="销售订单历史">
                  {ov.sales_recent_restricted ? (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
                      description="按权限，销售逐单成交明细不可见；可参考上方平均售价与近期成交参考价" />
                  ) : (
                    <ResizableTable storageKey="search-ov-sal" rowKey={(r) => r.order_no + r.order_date} size="small" columns={salCols} dataSource={ov.sales_recent} pagination={false} scroll={{ x: 600, y: 280 }} />
                  )}
                </section>
              ) },
            ]} />
          </div>

          <section aria-label="替代料" className="part-section">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
                          gap: 12, flexWrap: "wrap", marginBottom: subsOpen ? 12 : 0 }}>
              <button type="button" aria-expanded={subsOpen} onClick={toggleSubs}
                    style={{ border: 0, background: "none", padding: 0, font: "inherit", color: "inherit",
                             fontWeight: 500, cursor: "pointer", userSelect: "none",
                             display: "flex", alignItems: "center", gap: 8 }}>
                <RightOutlined style={{ fontSize: 12, color: "var(--mb-text-3)", transition: "transform .15s",
                                        transform: subsOpen ? "rotate(90deg)" : "none" }} />
                替代料 · 通用号
                {!subsOpen && (
                  <span style={{ fontSize: 12.5, fontWeight: 400, color: "var(--mb-text-3)" }}>
                    {ov.substitutes.length
                      ? `${ov.substitutes.length} 个通用号 · 库存合计 ${ov.substitutes.reduce((t, s) => t + (s.stock_qty ?? 0), 0)}`
                      : "暂无"}
                  </span>
                )}
              </button>
              {subsOpen && canEditSubstitutes && (
                <Space wrap>
                  <Input
                    placeholder="输入可替代的型号 (PN)" style={{ width: 240 }} size="small"
                    value={subPn} onChange={(e) => setSubPn(e.target.value)} onPressEnter={addSubstitute}
                  />
                  <Button size="small" onClick={addSubstitute} disabled={!subPn.trim()}>添加替代料</Button>
                </Space>
              )}
            </div>
            {subsOpen && (
              ov.substitutes.length === 0 ? (
                <span style={{ color: "var(--mb-text-3)" }}>
                  {canEditSubstitutes ? "暂无替代料，可在右上方添加" : "暂无替代料"}
                </span>
              ) : (
                <Table
                  size="small" rowKey={(s) => s.pn_std}
                  dataSource={ov.substitutes} pagination={false} scroll={{ x: 700 }}
                  columns={[
                    { title: "通用号 (PN)", dataIndex: "pn_std", width: 200,
                      render: (v: string) => (
                        <Button
                          type="link" size="small"
                          aria-label={`查看型号 ${v}`}
                          onClick={() => openByPn(v)}
                          style={{ fontFamily: "monospace", fontSize: 12.5, padding: 0, height: "auto" }}
                        >
                          {v}
                        </Button>
                      ) },
                    { title: "关系", key: "rel", width: 170,
                      render: (_, s) => s.via
                        ? <Tag color="cyan">互替 · 经 {s.via}</Tag>
                        : <Tag color={s.relation === "互替" ? "geekblue" : "orange"}>{s.relation || "互替"}</Tag> },
                    { title: "库存", dataIndex: "stock_qty", width: 80, align: "right" as const,
                      render: (v: number | null) => (
                        <span style={{ fontWeight: 500, color: (v ?? 0) > 0 ? undefined : "var(--mb-text-3)" }}>
                          {v ?? 0}
                        </span>
                      ) },
                    { title: "描述", dataIndex: "description", ellipsis: true,
                      render: (v: string | null) => v || <span style={{ color: "var(--mb-text-3)" }}>—</span> },
                    { title: "", key: "act", width: 64, align: "center" as const,
                      // 只在直连关系上给「解除」（间接「经 X」无直连边，删不了）；仅管理员/采购可见
                      render: (_, s) => (s.via || !canEditSubstitutes) ? null : (
                        <Popconfirm
                          title="解除替代关系" description={`确定解除与 ${s.pn_std} 的直连替代关系？`}
                          okText="解除" cancelText="取消" okButtonProps={{ danger: true }}
                          onConfirm={() => removeSubstitute(s.pn_std)}
                        >
                          <Button type="link" size="small" danger style={{ fontSize: 12.5 }}>解除</Button>
                        </Popconfirm>
                      ) },
                  ]}
                />
              )
            )}
          </section>

        </Card>
      ) : error ? (
        <Alert
          type="error" showIcon message="加载失败" description={error}
          action={
            <Button size="small" onClick={() => {
              if (urlPartId) loadOverview({ part_id: Number(urlPartId) });
              else if (urlPn) loadOverview({ pn_std: urlPn });
              else if (urlQ || Object.keys(urlFilters).length) runSearch(urlQ, urlFilters);
            }}>
              重试
            </Button>
          }
        />
      ) : (
        searching || loadingOv ? <PageSkeleton label="加载中…" /> : <Empty description="搜索并点击型号查看全景" />
      )}

      {catalog?.mode === "shared" ? <Modal open={inlineEditPn !== null && canEditPartDetails} onCancel={() => setInlineEditPn(null)} title="共享 PN 维护" width={1000} footer={null} destroyOnClose>{inlineEditPn && ov && canEditPartDetails && <CatalogPartPanel key={ov.part.id} partId={ov.part.id} onChanged={() => loadOverview({ part_id: ov.part.id })} />}</Modal> : <InlinePartEditModal
        open={inlineEditPn !== null}
        canEdit={canEditPartDetails}
        contextKey={`${urlPartId ?? ""}:${urlPn ?? ""}`}
        partId={ov?.part.id ?? null}
        pn={inlineEditPn}
        onClose={() => setInlineEditPn(null)}
        onSaved={() => ov ? loadOverview({ part_id: ov.part.id }) : undefined}
      />}
    </div>
  );
}
