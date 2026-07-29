import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  Input, Card, Descriptions, Tag, Row, Col, Statistic, Empty, message, Space, Button,
  Select, InputNumber, Alert, Table, Popconfirm, Tooltip,
} from "antd";
import { EditOutlined, RightOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import ResizableTable from "../components/ResizableTable";
import PageHeader from "../components/PageHeader";
import PoolReferencePanel from "../components/pools/PoolReferencePanel";
import InlinePartEditModal from "../components/parts/InlinePartEditModal";
import api from "../api";
import type { Overview, PurchaseRow, SalesRow, InventoryRow } from "../api";
import { unifiedSearch, fetchOverview } from "../api/search";
import type { UnifiedSearchItem, UnifiedSearchResp } from "../api/search";
import { COLORS } from "../theme";
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

/**
 * 型号查询：URL 驱动（与采购三页同范式）——
 *   /parts?q=<查询>            搜索
 *   /parts?part_id=<ID>        稳定深链：直开该型号全景（刷新/前进/后退/分享均保持）
 *   /parts?pn=<PN>             兼容入口：按 PN 解析后自动改写成 part_id 深链
 * 精确命中（exact）唯一主结果自动打开全景，相似候选降级到"相似型号"独立区域。
 */
export default function PartSearchPage() {
  const [sp, setSp] = useSearchParams();
  const urlQ = sp.get("q") || "";
  const urlPartId = sp.get("part_id");
  const urlPn = sp.get("pn");
  const returnToPool = poolAnalysisReturnPath(sp);
  // 权限快照绑定当前登录周期；App 在 token 变化时重挂 Router/页面。
  // 组件存活期间不重读可变 localStorage，避免 DevTools 改值后借无关重渲染点亮入口。
  const [{ canEditSubstitutes, canEditPartDetails }] = useState(() => {
    const role = localStorage.getItem("role") || "";
    return {
      canEditSubstitutes: ["admin", "purchaser"].includes(role),
      // 详情页轻量编辑与「备件主数据」共用权限；后端 /parts/master 仍做最终准入。
      canEditPartDetails: canSee("page_master_data"),
    };
  });

  const [qInput, setQInput] = useState(urlQ);
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
  // 结构化规格过滤（整改 P2：硬盘容量/接口等条件查询）——本地态，属"筛选浏览"不属深链
  const [partType, setPartType] = useState<string | undefined>(undefined);
  const [iface, setIface] = useState<string | undefined>(undefined);
  const [capMin, setCapMin] = useState<number | null>(null);
  const [capMax, setCapMax] = useState<number | null>(null);
  // 品类筛选（宋总 2026-07-05：全品类按分类查，不只硬盘）—— 一级 → 二级级联
  const [cats, setCats] = useState<{ major: string; minors: string[] }[]>([]);
  const [catMajor, setCatMajor] = useState<string | undefined>(undefined);
  const [catMinor, setCatMinor] = useState<string | undefined>(undefined);
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

  const runSearch = async (q: string, filters?: Record<string, unknown>) => {
    const seq = ++searchSeq.current;
    setSearching(true);
    setError(null);
    try {
      const data = await unifiedSearch(q, { pageSize: 20, filters: filters as any });
      if (seq !== searchSeq.current) return;   // 已有更新的搜索在跑
      setResp(data);
      if (data.items.length === 0) message.info("没有匹配的型号");
      // 精确即唯一：自动打开唯一主结果的全景（replace 不额外堆历史，后退直接回搜索前）
      if (data.exact && data.items[0]?.part_id && !sp.get("part_id")) {
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

  // URL q → 搜索（刷新/前进/后退都从这里重放）
  useEffect(() => {
    setQInput(urlQ);
    if (urlQ.trim()) runSearch(urlQ);
    else setResp(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlQ]);

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

  const onSearch = (q: string) => {
    if (!q.trim()) return;
    // 新搜索：q 入 URL（push，一次搜索一条历史），清掉旧选中
    patchUrl({ q: q.trim(), part_id: undefined, pn: undefined });
  };

  // 按品类/规格筛选：结构化浏览（后端 structured 分支），结果进同一张结果表
  const doFilterSearch = () => {
    runSearch(qInput, {
      part_type: partType, interface: iface,
      capacity_min: capMin ?? undefined, capacity_max: capMax ?? undefined,
      category_major: catMajor, category_minor: catMinor,
    });
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
        <a onClick={() => openPart(r)}>
          {v} {r.needs_review && <Tag color="orange">待复核</Tag>}
          {r.is_excluded && <Tag color="red">已排除</Tag>}
        </a>
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

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title="型号查询"
        subtitle="按型号 / 品牌 / 描述近似搜索，点击型号查看完整全景（采购 · 销售 · 库存 · 成本 · 毛利）"
        extra={returnToPool ? <Link to={returnToPool}>返回互通池分析</Link> : undefined}
      />
      <Card>
        <Input.Search
          placeholder="输入型号 (PN) 或描述关键词，如 ST8000NM000A；规格条件可单独使用"
          enterButton="搜索"
          size="large"
          loading={searching}
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
          onSearch={onSearch}
          allowClear
        />
        <Space style={{ marginTop: 12 }} wrap>
          <Select allowClear showSearch placeholder="一级品类（全品类）" value={catMajor} style={{ width: 160 }}
            onChange={(v) => { setCatMajor(v); setCatMinor(undefined); }}
            options={cats.map((c) => ({ value: c.major }))} />
          <Select allowClear showSearch placeholder="二级品类" value={catMinor} onChange={setCatMinor}
            style={{ width: 160 }} disabled={!catMajor}
            options={minorOpts.map((m) => ({ value: m }))} />
          <Select allowClear placeholder="部件类型" value={partType} onChange={setPartType} style={{ width: 110 }}
            options={[{ value: "HDD" }, { value: "SSD" }, { value: "RAM", label: "内存" }]} />
          <Select allowClear placeholder="接口" value={iface} onChange={setIface} style={{ width: 100 }}
            options={["SAS", "SATA", "NVME", "FC", "SCSI"].map((v) => ({ value: v }))} />
          <InputNumber placeholder="容量≥(GB)" value={capMin} onChange={setCapMin} style={{ width: 120 }} min={0} />
          <InputNumber placeholder="容量≤(GB)" value={capMax} onChange={setCapMax} style={{ width: 120 }} min={0} />
          <Button type="primary" ghost onClick={doFilterSearch} loading={searching}>按品类/规格筛选</Button>
        </Space>
      </Card>

      {resp?.ambiguous && (
        <Alert
          type="warning" showIcon message="该写法命中多个型号（歧义）"
          description="同一写法对应多个未合并型号或多个别名指向不同目标，请人工从下表选择正确型号；如属重复数据，请在主数据管理中合并治理。"
        />
      )}

      {exactItem ? (
        <Card title="精确匹配" size="small">
          <Space wrap size={12} style={{ fontSize: 14 }}>
            <a onClick={() => openPart(exactItem)} style={{ fontFamily: "monospace", fontWeight: 600 }}>
              {exactItem.pn_std}
            </a>
            <Tag color="green">{exactItem.match_type === "exact_alias"
              ? `别名精确：${exactItem.matched_text}` : "PN 精确匹配"}</Tag>
            {exactItem.pool_name && <Tag color="geekblue">互通池：{exactItem.pool_name}</Tag>}
            <span style={{ color: "var(--mb-text-3)" }}>{exactItem.description || ""}</span>
          </Space>
        </Card>
      ) : (resp && resp.items.length > 0 && (
        <Card title="搜索结果" size="small">
          <ResizableTable storageKey="search-hits" rowKey="pn_std" size="small" columns={hitCols}
            dataSource={resp.items} pagination={{ pageSize: 10 }} scroll={{ x: 900 }} />
        </Card>
      ))}

      {ov ? (
        <Card loading={loadingOv} title={<>型号全景：<b>{ov.part.pn_std}</b>
          {ov.part.needs_review && <Tag color="orange" style={{ marginLeft: 8 }}>PN 待复核</Tag>}
          {ov.part.redirected_from && <Tag color="blue" style={{ marginLeft: 8 }}>由 {ov.part.redirected_from} 合并而来</Tag>}</>}>
          <div
            role="group"
            aria-label="型号订单历史"
            style={{ display: "flex", flexDirection: "column", gap: 16, width: "100%", marginBottom: 16 }}
          >
            <section aria-label="采购订单历史" style={{ width: "100%" }}>
              <Card title="采购历史(近20)" size="small">
                <ResizableTable storageKey="search-ov-pur" rowKey={(r) => r.order_no + r.order_date} size="small" columns={purCols} dataSource={ov.purchases_recent} pagination={false} scroll={{ x: 600, y: 280 }} />
              </Card>
            </section>
            <section aria-label="销售订单历史" style={{ width: "100%" }}>
              <Card title="销售历史(近20)" size="small">
                {ov.sales_recent_restricted ? (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description="按权限，销售逐单成交明细不可见；可参考下方平均售价与近期成交参考价"
                  />
                ) : (
                  <ResizableTable storageKey="search-ov-sal" rowKey={(r) => r.order_no + r.order_date} size="small" columns={salCols} dataSource={ov.sales_recent} pagination={false} scroll={{ x: 600, y: 280 }} />
                )}
              </Card>
            </section>
          </div>

          <section aria-label="型号基本信息">
            <Descriptions bordered size="small" column={3} style={{ marginBottom: 16 }}>
              <Descriptions.Item label="描述" span={3}>
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

          <div style={{ marginBottom: 16 }}>
            <PoolReferencePanel partId={ov.part.id} />
          </div>

          {ov.sale_price_ref?.ref_sale_price != null && (
            <div style={{
              display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap",
              background: COLORS.accentSoft, border: `1px solid ${COLORS.accentSoftBorder}`, borderRadius: 10,
              padding: "12px 16px", marginBottom: 16,
            }}>
              <span style={{ color: COLORS.text2, fontSize: 13 }}>近期成交参考价（销售出价用）</span>
              <span style={{ fontSize: 22, fontWeight: 500, color: COLORS.accentStrong }}>
                {(() => { const s = splitFixed(ov.sale_price_ref.ref_sale_price, "inc"); return <TaxMoney scope="sales" inc={s.inc} ex={s.ex} />; })()}
              </span>
              <span style={{ color: COLORS.text3, fontSize: 13 }}>
                近 {ov.sale_price_ref.ref_sale_samples} 单 / {ov.sale_price_ref.ref_window_days} 天加权
              </span>
              {ov.sale_price_ref.ref_sale_samples < 3 && (
                <Tag color="orange">成交少，仅供参考</Tag>
              )}
            </div>
          )}

          <Row gutter={16} style={{ marginBottom: 16 }}>
            {canCost && <Col xs={24} sm={12} lg={6}><Card size="small">
              {/* 移动加权单位成本：原值为未税，含税侧按 13% 换算。 */}
              <Statistic title="移动加权 · 单位成本" valueStyle={{ color: COLORS.accentStrong }}
                valueRender={() => { const s = splitFixed(ov.profit_summary.avg_cost_moving, "ex"); return <TaxMoney scope="purchase" inc={s.inc} ex={s.ex} />; }} />
              {canProfit && <span style={{ color: COLORS.text3 }}>毛利率 {pct(ov.profit_summary.avg_margin_moving)}</span>}
            </Card></Col>}
            {canCost && <Col xs={24} sm={12} lg={6}><Card size="small">
              {/* FIFO 单位成本：不含税口径 */}
              <Statistic title="FIFO · 单位成本"
                valueRender={() => { const s = splitFixed(ov.profit_summary.avg_cost_fifo, "ex"); return <TaxMoney scope="purchase" inc={s.inc} ex={s.ex} />; }} />
              {canProfit && <span style={{ color: COLORS.text3 }}>毛利率 {pct(ov.profit_summary.avg_margin_fifo)}</span>}
            </Card></Col>}
            <Col xs={24} sm={12} lg={6}><Card size="small">
              {/* 平均销售价：原值为含税，未税侧按 13% 换算。 */}
              <Statistic title="平均销售价"
                valueRender={() => { const s = splitFixed(ov.profit_summary.avg_sale_price, "inc"); return <TaxMoney scope="sales" inc={s.inc} ex={s.ex} />; }} />
              <span style={{ color: COLORS.text3 }}>累计售 {ov.profit_summary.total_qty_sold}</span>
            </Card></Col>
            <Col xs={24} sm={12} lg={6}><Card size="small">
              {/* 询价区间：含税口径，min~max 各自双值 */}
              <Statistic title="询价区间" valueRender={() => {
                if (!ov.inquiry_ref.count) return <>无</>;
                const lo = splitFixed(ov.inquiry_ref.min_money, "inc");
                const hi = splitFixed(ov.inquiry_ref.max_money, "inc");
                return (<span style={{ whiteSpace: "nowrap" }}>
                  <TaxMoney scope="sales" inc={lo.inc} ex={lo.ex} /> ~ <TaxMoney scope="sales" inc={hi.inc} ex={hi.ex} />
                </span>);
              }} />
            </Card></Col>
          </Row>

          <section aria-label="型号库存">
            <Card size="small" style={{ marginBottom: 16 }}
                  title={<Space size={10}>库存
                    {ov.stock_dynamic && (
                      <span style={{ fontWeight: 400, fontSize: 13 }}>
                        动态可用 <b style={{ color: (ov.stock_dynamic.dynamic_qty ?? 0) < 0 ? "var(--mb-danger)" : "var(--mb-accent, #2f5b7c)" }}>
                          {ov.stock_dynamic.dynamic_qty ?? 0}</b>
                        <span style={{ color: "var(--mb-text-3)", fontSize: 12 }}>
                          （期初{ov.stock_dynamic.anchor_qty ?? 0}{ov.stock_dynamic.anchor_date ? `@${ov.stock_dynamic.anchor_date}` : ""} +入{ov.stock_dynamic.in_qty ?? 0} −出{(ov.stock_dynamic.out_sales ?? 0) + (ov.stock_dynamic.out_maint ?? 0)}；下表分仓为快照参考）
                        </span>
                      </span>
                    )}
                  </Space>}>
              {/* 合并后同仓可有多行（不同源 pn），rowKey 不能用 warehouse */}
              <ResizableTable storageKey="search-ov-inv" rowKey={(_, i) => String(i)} size="small" columns={invCols} dataSource={ov.inventory} pagination={false}
                locale={{ emptyText: "无库存" }} />
            </Card>
          </section>

          <Card size="small" style={{ marginBottom: 16 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center",
                          gap: 12, flexWrap: "wrap", marginBottom: subsOpen ? 12 : 0 }}>
              <span onClick={toggleSubs}
                    style={{ fontWeight: 500, cursor: "pointer", userSelect: "none",
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
              </span>
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
                          <a style={{ color: "var(--mb-danger)", fontSize: 12.5 }}>解除</a>
                        </Popconfirm>
                      ) },
                  ]}
                />
              )
            )}
          </Card>

        </Card>
      ) : error ? (
        <Alert
          type="error" showIcon message="加载失败" description={error}
          action={
            <Button size="small" onClick={() => {
              if (urlPartId) loadOverview({ part_id: Number(urlPartId) });
              else if (urlPn) loadOverview({ pn_std: urlPn });
              else if (urlQ) runSearch(urlQ);
            }}>
              重试
            </Button>
          }
        />
      ) : (
        <Empty description={searching || loadingOv ? "加载中…" : "搜索并点击型号查看全景"} />
      )}

      {exactItem && similar.length > 0 && (
        <Card size="small" title={<span>相似型号 <span style={{ fontWeight: 400, fontSize: 12.5, color: "var(--mb-text-3)" }}>
          非精确命中，仅供参考（如要找的不是 {exactItem.pn_std}）</span></span>}>
          <ResizableTable storageKey="search-similar" rowKey="pn_std" size="small" columns={hitCols}
            dataSource={similar} pagination={false} scroll={{ x: 900 }} />
        </Card>
      )}

      <InlinePartEditModal
        open={inlineEditPn !== null}
        canEdit={canEditPartDetails}
        contextKey={`${urlPartId ?? ""}:${urlPn ?? ""}`}
        pn={inlineEditPn}
        onClose={() => setInlineEditPn(null)}
        onSaved={() => ov ? loadOverview({ part_id: ov.part.id }) : undefined}
      />
    </Space>
  );
}
