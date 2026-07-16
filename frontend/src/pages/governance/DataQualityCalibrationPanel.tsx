import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert, Button, Card, Col, Descriptions, Empty, Grid, Input, List, Row, Select,
  Space, Spin, Statistic, Table, Tag, Typography, message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  getPurchasePriceCalibration,
  type PurchasePriceCalibration,
  type PurchasePriceCalibrationParams,
  type PurchasePriceCalibrationSample,
  type PurchasePriceCalibrationThreshold,
  type PurchasePriceCalibrationTypeDistribution,
} from "../../api/dataQuality";

const { Text, Title } = Typography;
const FIXED_THRESHOLDS = [2, 3, 5, 10] as const;
const DEFAULT_SAMPLE_LIMIT = 6;

function readPermissions(): Record<string, boolean> {
  try { return JSON.parse(localStorage.getItem("permissions") || "{}"); } catch { return {}; }
}

function permissionState() {
  if (localStorage.getItem("role") === "admin") return { hasPage: true, hasCost: true };
  const permissions = readPermissions();
  return {
    hasPage: permissions.page_governance === true,
    hasCost: permissions.data_purchase_cost === true,
  };
}

function formatMoney(value: number) {
  return `¥${value.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatRate(value: number) {
  return `${(value * 100).toFixed(2)}%`;
}

function formatDateTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).format(date);
}

function directionTag(direction: PurchasePriceCalibrationSample["direction"]) {
  return direction === "increase"
    ? <Tag color="orange">本次变贵</Tag>
    : <Tag color="blue">本次变便宜</Tag>;
}

function taxBasisLabel(value: string) {
  if (value === "ex_tax" || value === "ex_tax_original") return "原值不含税";
  if (value === "inc_tax") return "含税按 13% 换算";
  if (value === "unknown_as_inc_tax") return "税口径未知，按含税换算";
  if (value === "inc_tax_or_unknown_div_1_13") return "含税或未知，已÷1.13换算";
  return value || "—";
}

export default function DataQualityCalibrationPanel() {
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false;
  const permission = permissionState();
  const [preview, setPreview] = useState<PurchasePriceCalibration | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [purchaseType, setPurchaseType] = useState<string | undefined>();
  const [sampleLimit, setSampleLimit] = useState(DEFAULT_SAMPLE_LIMIT);
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    if (!permission.hasPage || !permission.hasCost) return;
    const seq = ++requestSeq.current;
    const params: PurchasePriceCalibrationParams = {
      ...(dateFrom ? { date_from: dateFrom } : {}),
      ...(dateTo ? { date_to: dateTo } : {}),
      ...(purchaseType ? { purchase_type: purchaseType } : {}),
      sample_limit: sampleLimit,
    };
    setLoading(true);
    setLoadError("");
    try {
      const result = await getPurchasePriceCalibration(params);
      if (seq !== requestSeq.current) return;
      setPreview(result);
    } catch {
      if (seq !== requestSeq.current) return;
      setPreview(null);
      setLoadError("校准预览加载失败，旧结果已清空。");
      message.error("校准预览加载失败，请稍后重试");
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [dateFrom, dateTo, permission.hasCost, permission.hasPage, purchaseType, sampleLimit]);

  useEffect(() => {
    void load();
    return () => { requestSeq.current += 1; };
  }, [load]);

  const purchaseTypeOptions = useMemo(() => (preview?.purchase_types ?? []).map((row) => ({
    value: row.purchase_type,
    label: row.purchase_type,
  })), [preview]);

  if (!permission.hasPage) {
    return <Alert type="warning" showIcon message="无数据治理页面权限" description="当前账号不能进入规则校准预览。" />;
  }
  if (!permission.hasCost) {
    return (
      <Alert
        type="warning"
        showIcon
        message="无采购成本查看权限"
        description="倍率、候选排序和样本价格都可能反推采购成本，因此本页面不会发送预览请求。"
      />
    );
  }

  return (
    <div data-testid="data-quality-calibration-panel" style={{ maxWidth: "100%", overflowX: "hidden" }}>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="仅为模拟预览，不会生成数据疑点"
        description="固定展示 2/3/5/10 倍四档的候选规模，帮助甲方校准未来规则；不会修改采购、利润、库存、池均价或员工排名。"
      />

      <Space direction={isMobile ? "vertical" : "horizontal"} wrap style={{ width: "100%", marginBottom: 16 }}>
        <label style={{ width: isMobile ? "100%" : 160 }}>
          <Text type="secondary">本次采购起始日</Text>
          <Input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} />
        </label>
        <label style={{ width: isMobile ? "100%" : 160 }}>
          <Text type="secondary">本次采购截止日</Text>
          <Input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} />
        </label>
        <label style={{ width: isMobile ? "100%" : 180 }}>
          <Text type="secondary">采购类型</Text>
          <Select
            allowClear
            value={purchaseType}
            placeholder="全部采购类型"
            style={{ width: "100%" }}
            options={purchaseTypeOptions}
            onChange={setPurchaseType}
          />
        </label>
        <label style={{ width: isMobile ? "100%" : 140 }}>
          <Text type="secondary">每档方向样本数</Text>
          <Select
            value={sampleLimit}
            style={{ width: "100%" }}
            options={[3, 6, 10, 20].map((value) => ({ value, label: `${value} 条` }))}
            onChange={setSampleLimit}
          />
        </label>
        <Button type="primary" loading={loading} onClick={() => void load()}>刷新模拟预览</Button>
      </Space>

      {loadError && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message={loadError}
          action={<Button size="small" onClick={() => void load()}>重试</Button>}
        />
      )}

      <Spin spinning={loading}>
        {preview ? <PreviewContent preview={preview} isMobile={isMobile} />
          : !loading && !loadError ? <Empty description="当前筛选没有可比采购记录" /> : null}
      </Spin>
    </div>
  );
}

function PreviewContent({ preview, isMobile }: { preview: PurchasePriceCalibration; isMobile: boolean }) {
  const thresholdByValue = new Map(preview.thresholds.map((item) => [item.threshold, item]));
  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Row gutter={[12, 12]}>
        <Col xs={12} sm={8} md={6}>
          <Card size="small"><Statistic title="可比相邻对" value={preview.eligible_pairs} /></Card>
        </Col>
        <Col xs={12} sm={8} md={6}>
          <Card size="small"><Statistic title="涉及 PN" value={preview.distinct_parts} /></Card>
        </Col>
        <Col xs={24} sm={8} md={12}>
          <Card size="small">
            <Descriptions column={1} size="small">
              <Descriptions.Item label="规则版本">{preview.rule_version}</Descriptions.Item>
              <Descriptions.Item label="数据截止">{preview.data_through || "暂无有效数据"}</Descriptions.Item>
              <Descriptions.Item label="预览生成">{formatDateTime(preview.generated_at)}</Descriptions.Item>
              <Descriptions.Item label="抽样边界">每档每方向最多 {preview.sample_boundary.limit_per_threshold_direction} 条</Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>
      </Row>

      <section>
        <Title level={5}>固定倍率四档</Title>
        <div
          data-testid="calibration-threshold-grid"
          style={{
            display: "grid",
            gridTemplateColumns: isMobile ? "repeat(2, minmax(0, 1fr))" : "repeat(4, minmax(0, 1fr))",
            gap: 12,
          }}
        >
          {FIXED_THRESHOLDS.map((threshold) => (
            <ThresholdCard key={threshold} threshold={thresholdByValue.get(threshold)} value={threshold} />
          ))}
        </div>
      </section>

      <section>
        <Title level={5}>采购类型分布</Title>
        {preview.purchase_types.length
          ? <PurchaseTypeDistribution rows={preview.purchase_types} isMobile={isMobile} />
          : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前筛选没有采购类型分布" />}
      </section>

      <section aria-label="确定性抽样样本">
        <Title level={5}>确定性抽样样本</Title>
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="候选仅供业务校准，不代表采购记录有错"
          description="相同数据快照和筛选参数会返回相同样本顺序，请结合采购类型、单位、包装和紧急采购背景核实。"
        />
        {preview.samples.length
          ? <CalibrationSamples samples={preview.samples} isMobile={isMobile} />
          : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前档位没有抽样候选" />}
      </section>
    </Space>
  );
}

function ThresholdCard({ threshold, value }: {
  threshold: PurchasePriceCalibrationThreshold | undefined;
  value: number;
}) {
  return (
    <Card size="small" title={`${value} 倍档`} style={{ minWidth: 0 }}>
      <Space direction="vertical" size={2}>
        <Text strong>{threshold?.candidate_count ?? 0} 条候选</Text>
        <Text>{formatRate(threshold?.candidate_rate ?? 0)}</Text>
        <Text type="secondary">
          变贵 {threshold?.increase_count ?? 0} · 变便宜 {threshold?.decrease_count ?? 0}
        </Text>
      </Space>
    </Card>
  );
}

function PurchaseTypeDistribution({ rows, isMobile }: {
  rows: PurchasePriceCalibrationTypeDistribution[];
  isMobile: boolean;
}) {
  const flattened = rows.flatMap((row) => row.thresholds.map((threshold) => ({
    key: `${row.purchase_type}-${threshold.threshold}`,
    purchase_type: row.purchase_type,
    ...threshold,
  })));
  if (isMobile) {
    return (
      <List
        dataSource={flattened}
        renderItem={(row) => (
          <List.Item style={{ paddingInline: 0 }}>
            <Card size="small" style={{ width: "100%" }}>
              <Space direction="vertical" size={3}>
                <Space wrap><Text strong>{row.purchase_type}</Text><Tag>{row.threshold} 倍</Tag></Space>
                <Text>可比 {row.eligible_pairs.toLocaleString()} · 候选 {row.candidate_count.toLocaleString()}（{formatRate(row.candidate_rate)}）</Text>
                <Text type="secondary">变贵 {row.increase_count} · 变便宜 {row.decrease_count}</Text>
              </Space>
            </Card>
          </List.Item>
        )}
      />
    );
  }
  const columns: ColumnsType<(typeof flattened)[number]> = [
    { title: "采购类型", dataIndex: "purchase_type" },
    { title: "倍率档", dataIndex: "threshold", render: (value) => `${value} 倍` },
    { title: "可比对", dataIndex: "eligible_pairs", align: "right" },
    { title: "候选数", dataIndex: "candidate_count", align: "right" },
    { title: "候选率", dataIndex: "candidate_rate", align: "right", render: formatRate },
    { title: "本次变贵", dataIndex: "increase_count", align: "right" },
    { title: "本次变便宜", dataIndex: "decrease_count", align: "right" },
  ];
  return <Table rowKey="key" size="small" pagination={false} columns={columns} dataSource={flattened} />;
}

function CalibrationSamples({ samples, isMobile }: {
  samples: PurchasePriceCalibrationSample[];
  isMobile: boolean;
}) {
  if (isMobile) {
    return (
      <List
        data-testid="calibration-mobile-samples"
        dataSource={samples}
        renderItem={(sample) => (
          <List.Item style={{ paddingInline: 0 }}>
            <Card size="small" style={{ width: "100%" }}>
              <Space direction="vertical" size={6} style={{ width: "100%" }}>
                <Space wrap><Text strong>{sample.pn_std || "无型号"}</Text>{directionTag(sample.direction)}<Tag>{sample.ratio.toFixed(2)} 倍</Tag></Space>
                <Text type="secondary">{sample.purchase_type} · {sample.threshold} 倍档</Text>
                <SampleLine title="前一笔" line={sample.previous} />
                <SampleLine title="本次" line={sample.current} />
              </Space>
            </Card>
          </List.Item>
        )}
      />
    );
  }
  const columns: ColumnsType<PurchasePriceCalibrationSample> = [
    { title: "PN", dataIndex: "pn_std", width: 150, render: (value) => value || "无型号" },
    { title: "类型", dataIndex: "purchase_type", width: 110 },
    { title: "档位", dataIndex: "threshold", width: 76, render: (value) => `${value} 倍` },
    { title: "方向", dataIndex: "direction", width: 110, render: directionTag },
    { title: "倍率", dataIndex: "ratio", width: 90, align: "right", render: (value) => `${value.toFixed(2)} 倍` },
    {
      title: "前一笔采购", key: "previous", width: 280,
      render: (_, row) => <SampleLine title="前一笔" line={row.previous} hideTitle />,
    },
    {
      title: "本次采购", key: "current", width: 280,
      render: (_, row) => <SampleLine title="本次" line={row.current} hideTitle />,
    },
  ];
  return (
    <Table
      rowKey={(row) => `${row.threshold}-${row.direction}-${row.current.line_id}-${row.previous.line_id}`}
      size="small"
      pagination={false}
      columns={columns}
      dataSource={samples}
      scroll={{ x: 1100 }}
    />
  );
}

function SampleLine({ title, line, hideTitle = false }: {
  title: string;
  line: PurchasePriceCalibrationSample["current"];
  hideTitle?: boolean;
}) {
  return (
    <div>
      {!hideTitle && <Text type="secondary">{title}</Text>}
      <div>{line.order_no || "无单号"} · {line.order_date}</div>
      <div>{formatMoney(line.unit_price_ex_tax)} · {line.quantity.toLocaleString()}{line.unit ? ` ${line.unit}` : ""} · {taxBasisLabel(line.tax_basis)}</div>
    </div>
  );
}
