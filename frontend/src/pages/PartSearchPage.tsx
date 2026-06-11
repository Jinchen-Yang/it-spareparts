import { useState } from "react";
import {
  Input, Table, Card, Descriptions, Tag, Row, Col, Statistic, Empty, message, Space, Button,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import api from "../api";
import type { Overview, PartHit, PurchaseRow, SalesRow, InventoryRow } from "../api";

const money = (v: number | null) => (v == null ? "-" : `¥${v.toLocaleString()}`);
const pct = (v: number | null | undefined) => (v == null ? "-" : `${(v * 100).toFixed(1)}%`);

export default function PartSearchPage() {
  const [hits, setHits] = useState<PartHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [ov, setOv] = useState<Overview | null>(null);
  const [loadingOv, setLoadingOv] = useState(false);
  const [subPn, setSubPn] = useState("");

  const doSearch = async (q: string) => {
    if (!q.trim()) return;
    setSearching(true);
    try {
      const { data } = await api.get("/parts/search", { params: { q, page_size: 20 } });
      setHits(data.items);
      if (data.items.length === 0) message.info("没有匹配的型号");
    } finally {
      setSearching(false);
    }
  };

  const openOverview = async (pn: string) => {
    setLoadingOv(true);
    try {
      const { data } = await api.get("/parts/overview", { params: { pn_std: pn } });
      setOv(data);
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
    { title: "单价", dataIndex: "unit_price", width: 90, render: money },
  ];
  const salCols: ColumnsType<SalesRow> = [
    { title: "销售单号", dataIndex: "order_no", width: 170, ellipsis: true },
    { title: "日期", dataIndex: "order_date", width: 110 },
    { title: "客户", dataIndex: "customer", width: 160, ellipsis: true },
    { title: "数量", dataIndex: "qty", width: 70 },
    { title: "单价(含税)", dataIndex: "unit_price", width: 100, render: money },
  ];
  const invCols: ColumnsType<InventoryRow> = [
    { title: "仓库", dataIndex: "warehouse" },
    { title: "可用数量", dataIndex: "display_qty", width: 100 },
    { title: "源系统数量", dataIndex: "source_qty", width: 110 },
    { title: "单位成本", dataIndex: "unit_cost", width: 110, render: money },
    { title: "库存金额", dataIndex: "inventory_value", width: 120, render: money },
  ];

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card>
        <Input.Search
          placeholder="输入型号 (PN) 或描述关键词，如 ST8000NM000A"
          enterButton="搜索"
          size="large"
          loading={searching}
          onSearch={doSearch}
          allowClear
        />
      </Card>

      {hits.length > 0 && (
        <Card title="搜索结果" size="small">
          <Table rowKey="pn_std" size="small" columns={hitCols} dataSource={hits} pagination={{ pageSize: 10 }} />
        </Card>
      )}

      {ov ? (
        <Card loading={loadingOv} title={<>型号全景：<b>{ov.part.pn_std}</b>{ov.part.needs_review && <Tag color="orange" style={{ marginLeft: 8 }}>PN 待复核</Tag>}</>}>
          <Descriptions bordered size="small" column={3} style={{ marginBottom: 16 }}>
            <Descriptions.Item label="描述" span={3}>{ov.part.description || "-"}</Descriptions.Item>
            <Descriptions.Item label="品牌">{ov.part.brand || "-"}</Descriptions.Item>
            <Descriptions.Item label="品类">{ov.part.category_major || "-"}</Descriptions.Item>
            <Descriptions.Item label="规格">{ov.part.category_minor || "-"}</Descriptions.Item>
          </Descriptions>

          <Row gutter={16} style={{ marginBottom: 16 }}>
            <Col span={6}><Card size="small">
              <Statistic title="移动加权 · 单位成本" value={ov.profit_summary.avg_cost_moving ?? "-"} prefix="¥" />
              <span style={{ color: "#888" }}>毛利率 {pct(ov.profit_summary.avg_margin_moving)}</span>
            </Card></Col>
            <Col span={6}><Card size="small">
              <Statistic title="FIFO · 单位成本" value={ov.profit_summary.avg_cost_fifo ?? "-"} prefix="¥" />
              <span style={{ color: "#888" }}>毛利率 {pct(ov.profit_summary.avg_margin_fifo)}</span>
            </Card></Col>
            <Col span={6}><Card size="small">
              <Statistic title="平均销售价(含税)" value={ov.profit_summary.avg_sale_price ?? "-"} prefix="¥" />
              <span style={{ color: "#888" }}>累计售 {ov.profit_summary.total_qty_sold}</span>
            </Card></Col>
            <Col span={6}><Card size="small"><Statistic title="询价区间" value={ov.inquiry_ref.count ? `${money(ov.inquiry_ref.min_money)}~${money(ov.inquiry_ref.max_money)}` : "无"} /></Card></Col>
          </Row>

          <Card title="库存" size="small" style={{ marginBottom: 16 }}>
            <Table rowKey="warehouse" size="small" columns={invCols} dataSource={ov.inventory} pagination={false}
              locale={{ emptyText: "无库存" }} />
          </Card>

          <Card title="替代料" size="small" style={{ marginBottom: 16 }}>
            <Space wrap style={{ marginBottom: ov.substitutes.length ? 12 : 0 }}>
              <Input
                placeholder="输入可替代的型号 (PN)" style={{ width: 260 }}
                value={subPn} onChange={(e) => setSubPn(e.target.value)} onPressEnter={addSubstitute}
              />
              <Button onClick={addSubstitute} disabled={!subPn.trim()}>添加替代料</Button>
            </Space>
            <div>
              {ov.substitutes.length === 0 ? (
                <span style={{ color: "#999" }}>暂无替代料，可在上方添加</span>
              ) : (
                ov.substitutes.map((s) => (
                  <Tag key={s.pn_std} color="geekblue" style={{ marginBottom: 4 }}>
                    {s.pn_std}
                    {s.description ? ` · ${s.description}` : ""}
                  </Tag>
                ))
              )}
            </div>
          </Card>

          <Row gutter={16}>
            <Col span={12}>
              <Card title="采购历史(近20)" size="small">
                <Table rowKey={(r) => r.order_no + r.order_date} size="small" columns={purCols} dataSource={ov.purchases_recent} pagination={false} scroll={{ x: 600, y: 280 }} />
              </Card>
            </Col>
            <Col span={12}>
              <Card title="销售历史(近20)" size="small">
                <Table rowKey={(r) => r.order_no + r.order_date} size="small" columns={salCols} dataSource={ov.sales_recent} pagination={false} scroll={{ x: 600, y: 280 }} />
              </Card>
            </Col>
          </Row>
        </Card>
      ) : (
        <Empty description="搜索并点击型号查看全景" />
      )}
    </Space>
  );
}
