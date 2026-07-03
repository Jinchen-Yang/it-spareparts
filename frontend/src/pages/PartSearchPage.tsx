import { useState } from "react";
import {
  Input, Card, Descriptions, Tag, Row, Col, Statistic, Empty, message, Space, Button,
  Select, InputNumber, Alert, Table,
} from "antd";
import { RightOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import ResizableTable from "../components/ResizableTable";
import PageHeader from "../components/PageHeader";
import api from "../api";
import type { Overview, PartHit, PurchaseRow, SalesRow, InventoryRow } from "../api";
import { COLORS } from "../theme";
import { money, pct, splitByFlag, splitFixed } from "../utils/format";
import { useTaxBasis, TaxMoney } from "../context/TaxBasis";
const errMsg = (e: any) =>
  !e?.response ? "无法连接服务器，请检查网络后重试"
  : e?.response?.data?.detail || `加载失败（${e?.response?.status ?? "?"}），请稍后重试`;

// 按登录用户权限决定是否展示成本/利润卡片（后端已把值脱敏成 null，前端再把空卡片整张藏掉）
const canSee = (key: string) => {
  if ((localStorage.getItem("role") || "") === "admin") return true;
  try { return JSON.parse(localStorage.getItem("permissions") || "{}")[key] === true; }
  catch { return false; }
};

export default function PartSearchPage() {
  const [hits, setHits] = useState<PartHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [ov, setOv] = useState<Overview | null>(null);
  const [loadingOv, setLoadingOv] = useState(false);
  const [subPn, setSubPn] = useState("");
  // 替代料卡片折叠（记忆本机）
  const [subsOpen, setSubsOpen] = useState(() => localStorage.getItem("ps_subs_open") !== "0");
  const toggleSubs = () => setSubsOpen((o) => { localStorage.setItem("ps_subs_open", o ? "0" : "1"); return !o; });
  const [lastQ, setLastQ] = useState("");
  const [lastPn, setLastPn] = useState("");   // 用于全景加载失败时的"重试"
  const [error, setError] = useState<string | null>(null);
  // 结构化规格过滤（整改 P2：硬盘容量/接口等条件查询）
  const [partType, setPartType] = useState<string | undefined>(undefined);
  const [iface, setIface] = useState<string | undefined>(undefined);
  const [capMin, setCapMin] = useState<number | null>(null);
  const [capMax, setCapMax] = useState<number | null>(null);

  const canCost = canSee("data_purchase_cost");   // 销售看不到 → 隐藏成本卡片
  const canProfit = canSee("data_profit");
  const { basis } = useTaxBasis();

  const doSearch = async (q: string, override?: Record<string, unknown>) => {
    const hasSpec = partType || iface || capMin != null || capMax != null || override;
    if (!q.trim() && !hasSpec) return;
    setSearching(true);
    setLastQ(q);
    setError(null);
    try {
      const { data } = await api.get("/parts/search", {
        params: {
          q: q.trim() || undefined, page_size: 20,
          part_type: partType, interface: iface,
          capacity_min: capMin ?? undefined, capacity_max: capMax ?? undefined,
          ...(override || {}),
        },
      });
      setHits(data.items);
      if (data.items.length === 0) message.info("没有匹配的型号");
    } catch (e) {
      setHits([]);
      const msg = errMsg(e);
      setError(msg);
      message.error(msg);
    } finally {
      setSearching(false);
    }
  };

  const openOverview = async (pn: string) => {
    setLoadingOv(true);
    setLastPn(pn);
    setError(null);
    try {
      const { data } = await api.get("/parts/overview", { params: { pn_std: pn } });
      setOv(data);
    } catch (e) {
      setOv(null);
      const msg = errMsg(e);
      setError(msg);
      message.error(msg);
    } finally {
      setLoadingOv(false);
    }
  };

  const addSubstitute = async () => {
    if (!ov || !subPn.trim()) return;
    try {
      const { data } = await api.post("/substitutes", { pn_a: ov.part.pn_std, pn_b: subPn.trim() });
      message.success(data.created ? "已添加替代料" : "该替代关系已存在");
      setSubPn("");
      openOverview(ov.part.pn_std);
    } catch (e: any) {
      message.error(e?.response?.data?.detail || "添加失败（需管理员权限）");
    }
  };

  const hitCols: ColumnsType<PartHit> = [
    {
      title: "型号 (PN)", dataIndex: "pn_std",
      render: (v, r) => (
        <a onClick={() => openOverview(v)}>
          {v} {r.needs_review && <Tag color="orange">待复核</Tag>}
          {r.is_excluded && <Tag color="red">已排除</Tag>}
        </a>
      ),
    },
    {
      title: "匹配度", dataIndex: "score", width: 150,
      render: (v: number | undefined, r) =>
        v == null ? "-" : (
          <span title={r.match_reason}>
            <Tag color={v >= 0.6 ? "green" : v >= 0.35 ? "blue" : "default"}>
              {(v * 100).toFixed(0)}%
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
    // 单价按订单税口径归列（含税单→含税列、不含单→不含税列），另一侧 "-"；跟随全局开关。零计算。
    ...(basis !== "ex" ? [{ title: "单价(含税)", key: "up_inc", width: 90, align: "right" as const,
      render: (_: unknown, r: PurchaseRow) => money(splitByFlag(r.unit_price, r.is_tax_inclusive).inc) }] as ColumnsType<PurchaseRow> : []),
    ...(basis !== "inc" ? [{ title: "单价(不含税)", key: "up_ex", width: 90, align: "right" as const,
      render: (_: unknown, r: PurchaseRow) => money(splitByFlag(r.unit_price, r.is_tax_inclusive).ex) }] as ColumnsType<PurchaseRow> : []),
  ];
  const salCols: ColumnsType<SalesRow> = [
    { title: "销售单号", dataIndex: "order_no", width: 170, ellipsis: true },
    { title: "日期", dataIndex: "order_date", width: 110 },
    { title: "客户", dataIndex: "customer", width: 160, ellipsis: true },
    { title: "数量", dataIndex: "qty", width: 70 },
    // 销售约定含税：真实值只落含税侧，不含税侧 "-"（不做税率换算）。
    ...(basis !== "ex" ? [{ title: "单价(含税)", key: "sp_inc", width: 100, align: "right" as const,
      render: (_: unknown, r: SalesRow) => money(splitFixed(r.unit_price, "inc").inc) }] as ColumnsType<SalesRow> : []),
    ...(basis !== "inc" ? [{ title: "单价(不含税)", key: "sp_ex", width: 100, align: "right" as const,
      render: (_: unknown, r: SalesRow) => money(splitFixed(r.unit_price, "inc").ex) }] as ColumnsType<SalesRow> : []),
  ];
  const invCols: ColumnsType<InventoryRow> = [
    { title: "仓库", dataIndex: "warehouse" },
    { title: "可用数量", dataIndex: "display_qty", width: 100 },
    { title: "源系统数量", dataIndex: "source_qty", width: 110 },
    // 成本/库存估值固定不含税：真实值只落不含税侧，含税侧 "-"。
    ...(basis !== "ex" ? [{ title: "单位成本(含税)", key: "uc_inc", width: 110, align: "right" as const,
      render: (_: unknown, r: InventoryRow) => money(splitFixed(r.unit_cost, "ex").inc) }] as ColumnsType<InventoryRow> : []),
    ...(basis !== "inc" ? [{ title: "单位成本(不含税)", key: "uc_ex", width: 110, align: "right" as const,
      render: (_: unknown, r: InventoryRow) => money(splitFixed(r.unit_cost, "ex").ex) }] as ColumnsType<InventoryRow> : []),
    ...(basis !== "ex" ? [{ title: "库存金额(含税)", key: "iv_inc", width: 120, align: "right" as const,
      render: (_: unknown, r: InventoryRow) => money(splitFixed(r.inventory_value, "ex").inc) }] as ColumnsType<InventoryRow> : []),
    ...(basis !== "inc" ? [{ title: "库存金额(不含税)", key: "iv_ex", width: 120, align: "right" as const,
      render: (_: unknown, r: InventoryRow) => money(splitFixed(r.inventory_value, "ex").ex) }] as ColumnsType<InventoryRow> : []),
  ];

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title="型号查询"
        subtitle="按型号 / 品牌 / 描述近似搜索，点击型号查看完整全景（采购 · 销售 · 库存 · 成本 · 毛利）"
      />
      <Card>
        <Input.Search
          placeholder="输入型号 (PN) 或描述关键词，如 ST8000NM000A；规格条件可单独使用"
          enterButton="搜索"
          size="large"
          loading={searching}
          onSearch={(q) => doSearch(q)}
          allowClear
        />
        <Space style={{ marginTop: 12 }} wrap>
          <Select allowClear placeholder="部件类型" value={partType} onChange={setPartType} style={{ width: 120 }}
            options={[{ value: "HDD" }, { value: "SSD" }, { value: "RAM", label: "内存" }]} />
          <Select allowClear placeholder="接口" value={iface} onChange={setIface} style={{ width: 120 }}
            options={["SAS", "SATA", "NVME", "FC", "SCSI"].map((v) => ({ value: v }))} />
          <InputNumber placeholder="容量≥(GB)" value={capMin} onChange={setCapMin} style={{ width: 130 }} min={0} />
          <InputNumber placeholder="容量≤(GB)" value={capMax} onChange={setCapMax} style={{ width: 130 }} min={0} />
          <Button onClick={() => doSearch(lastQ, {})} loading={searching}>按规格筛选</Button>
        </Space>
      </Card>

      {hits.length > 0 && (
        <Card title="搜索结果" size="small">
          <ResizableTable storageKey="search-hits" rowKey="pn_std" size="small" columns={hitCols} dataSource={hits} pagination={{ pageSize: 10 }} />
        </Card>
      )}

      {ov ? (
        <Card loading={loadingOv} title={<>型号全景：<b>{ov.part.pn_std}</b>
          {ov.part.needs_review && <Tag color="orange" style={{ marginLeft: 8 }}>PN 待复核</Tag>}
          {ov.part.redirected_from && <Tag color="blue" style={{ marginLeft: 8 }}>由 {ov.part.redirected_from} 合并而来</Tag>}</>}>
          <Descriptions bordered size="small" column={3} style={{ marginBottom: 16 }}>
            <Descriptions.Item label="描述" span={3}>{ov.part.description || "-"}</Descriptions.Item>
            <Descriptions.Item label="品牌">{ov.part.brand || "-"}</Descriptions.Item>
            <Descriptions.Item label="品类">{ov.part.category_major || "-"}</Descriptions.Item>
            <Descriptions.Item label="规格">{ov.part.category_minor || "-"}</Descriptions.Item>
          </Descriptions>

          {ov.sale_price_ref?.ref_sale_price != null && (
            <div style={{
              display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap",
              background: COLORS.accentSoft, border: `1px solid ${COLORS.accentSoftBorder}`, borderRadius: 10,
              padding: "12px 16px", marginBottom: 16,
            }}>
              <span style={{ color: COLORS.text2, fontSize: 13 }}>近期成交参考价（销售出价用）</span>
              <span style={{ fontSize: 22, fontWeight: 500, color: COLORS.accentStrong }}>
                {(() => { const s = splitFixed(ov.sale_price_ref.ref_sale_price, "inc"); return <TaxMoney inc={s.inc} ex={s.ex} />; })()}
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
            {canCost && <Col span={6}><Card size="small">
              {/* 移动加权单位成本：不含税口径（真实值只落不含税侧） */}
              <Statistic title="移动加权 · 单位成本" valueStyle={{ color: COLORS.accentStrong }}
                valueRender={() => { const s = splitFixed(ov.profit_summary.avg_cost_moving, "ex"); return <TaxMoney inc={s.inc} ex={s.ex} />; }} />
              {canProfit && <span style={{ color: COLORS.text3 }}>毛利率 {pct(ov.profit_summary.avg_margin_moving)}</span>}
            </Card></Col>}
            {canCost && <Col span={6}><Card size="small">
              {/* FIFO 单位成本：不含税口径 */}
              <Statistic title="FIFO · 单位成本"
                valueRender={() => { const s = splitFixed(ov.profit_summary.avg_cost_fifo, "ex"); return <TaxMoney inc={s.inc} ex={s.ex} />; }} />
              {canProfit && <span style={{ color: COLORS.text3 }}>毛利率 {pct(ov.profit_summary.avg_margin_fifo)}</span>}
            </Card></Col>}
            <Col span={6}><Card size="small">
              {/* 平均销售价：含税口径（真实值只落含税侧） */}
              <Statistic title="平均销售价"
                valueRender={() => { const s = splitFixed(ov.profit_summary.avg_sale_price, "inc"); return <TaxMoney inc={s.inc} ex={s.ex} />; }} />
              <span style={{ color: COLORS.text3 }}>累计售 {ov.profit_summary.total_qty_sold}</span>
            </Card></Col>
            <Col span={6}><Card size="small">
              {/* 询价区间：含税口径，min~max 各自双值 */}
              <Statistic title="询价区间" valueRender={() => {
                if (!ov.inquiry_ref.count) return <>无</>;
                const lo = splitFixed(ov.inquiry_ref.min_money, "inc");
                const hi = splitFixed(ov.inquiry_ref.max_money, "inc");
                return (<span style={{ whiteSpace: "nowrap" }}>
                  <TaxMoney inc={lo.inc} ex={lo.ex} /> ~ <TaxMoney inc={hi.inc} ex={hi.ex} />
                </span>);
              }} />
            </Card></Col>
          </Row>

          <Card title="库存" size="small" style={{ marginBottom: 16 }}>
            {/* 合并后同仓可有多行（不同源 pn），rowKey 不能用 warehouse */}
            <ResizableTable storageKey="search-ov-inv" rowKey={(_, i) => String(i)} size="small" columns={invCols} dataSource={ov.inventory} pagination={false}
              locale={{ emptyText: "无库存" }} />
          </Card>

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
              {subsOpen && (
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
                <span style={{ color: "var(--mb-text-3)" }}>暂无替代料，可在右上方添加</span>
              ) : (
                <Table
                  size="small" rowKey={(s) => s.pn_std}
                  dataSource={ov.substitutes} pagination={false}
                  columns={[
                    { title: "通用号 (PN)", dataIndex: "pn_std", width: 200,
                      render: (v: string) => (
                        <a onClick={() => openOverview(v)} style={{ fontFamily: "monospace", fontSize: 12.5 }}>{v}</a>
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
                  ]}
                />
              )
            )}
          </Card>

          <Row gutter={16}>
            <Col span={12}>
              <Card title="采购历史(近20)" size="small">
                <ResizableTable storageKey="search-ov-pur" rowKey={(r) => r.order_no + r.order_date} size="small" columns={purCols} dataSource={ov.purchases_recent} pagination={false} scroll={{ x: 600, y: 280 }} />
              </Card>
            </Col>
            <Col span={12}>
              <Card title="销售历史(近20)" size="small">
                {ov.sales_recent_restricted ? (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description="按权限，销售逐单成交明细不可见；可参考上方平均售价与近期成交参考价"
                  />
                ) : (
                  <ResizableTable storageKey="search-ov-sal" rowKey={(r) => r.order_no + r.order_date} size="small" columns={salCols} dataSource={ov.sales_recent} pagination={false} scroll={{ x: 600, y: 280 }} />
                )}
              </Card>
            </Col>
          </Row>
        </Card>
      ) : error ? (
        <Alert
          type="error" showIcon message="加载失败" description={error}
          action={
            <Button size="small" onClick={() => (lastPn ? openOverview(lastPn) : doSearch(lastQ))}>
              重试
            </Button>
          }
        />
      ) : (
        <Empty description={searching ? "搜索中…" : "搜索并点击型号查看全景"} />
      )}
    </Space>
  );
}
