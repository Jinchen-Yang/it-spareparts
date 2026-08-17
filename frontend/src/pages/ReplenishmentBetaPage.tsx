import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Collapse,
  Dropdown,
  Empty,
  Input,
  InputNumber,
  List,
  Modal,
  Pagination,
  Result,
  Row,
  Space,
  Spin,
  Steps,
  Tag,
  Typography,
  message,
} from "antd";
import {
  ArrowLeftOutlined,
  CheckCircleOutlined,
  DownloadOutlined,
  ReloadOutlined,
  SearchOutlined,
  ShoppingCartOutlined,
} from "@ant-design/icons";
import { useNavigate } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import {
  addReplenishmentLine,
  createReplenishmentApplication,
  downloadManualReviewWorkbook,
  downloadPurchaseListWorkbook,
  downloadWbddSubsetWorkbook,
  getReplenishmentApplication,
  getReplenishmentCapabilities,
  listReplenishmentApplications,
  removeReplenishmentLine,
  searchReplenishmentCatalog,
  startReplenishmentRevision,
  submitReplenishmentApplication,
  updateReplenishmentDraft,
  updateReplenishmentLine,
  type ApplicationSummary,
  type CatalogPart,
  type PriceStats,
  type ReplenishmentApplication,
  type ReplenishmentCapabilities,
  type ReplenishmentLine,
  type ReplenishmentVersion,
} from "../api/replenishment";
import "./ReplenishmentBetaPage.css";

// 补库申请自 2026-08-17 归入维保项目组：前置态返回按钮指向维保主页
const MAINTENANCE_HOME_PATH = "/maintenance";

const { Text, Title } = Typography;

const STATUS: Record<ReplenishmentApplication["status"], { label: string; color: string }> = {
  draft: { label: "购物车草稿", color: "blue" },
  submitted: { label: "等待审核", color: "gold" },
  needs_revision: { label: "有条目被打回", color: "red" },
  approved: { label: "审核通过", color: "green" },
};

function errorText(error: unknown): string {
  const payload = error as { response?: { data?: { detail?: string | { message?: string } } } };
  const detail = payload.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  return "操作失败，请刷新后重试";
}

function priceText(stats: PriceStats | null, unit: string | null): string {
  if (!stats || stats.weighted_avg == null) return "半年内无有效样本";
  const quantity = unit
    ? `${stats.total_qty ?? 0} ${unit}`
    : `数量 ${stats.total_qty ?? 0}`;
  return `¥${stats.weighted_avg.toFixed(2)} · ${quantity} · ${stats.order_count} 单`;
}

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function PriceFacts({ part }: { part: Pick<CatalogPart, "purchase" | "sales" | "price_window" | "unit"> }) {
  return (
    <div className="replenishment-price-facts">
      <div><span>近半年采购</span><b>{priceText(part.purchase, part.unit)}</b></div>
      <div><span>近半年销售</span><b>{priceText(part.sales, part.unit)}</b></div>
      <Text type="secondary" style={{ fontSize: 11 }}>
        {part.price_window.date_from} 至 {part.price_window.date_to} · 未税数量加权
      </Text>
    </div>
  );
}

function CatalogCard({
  part,
  disabled,
  replacing,
  onAdd,
}: {
  part: CatalogPart;
  disabled: boolean;
  replacing: boolean;
  onAdd: (quantity: number) => void;
}) {
  const [quantity, setQuantity] = useState(1);
  return (
    <Card className="replenishment-part-card" size="small">
      <div className="replenishment-part-heading">
        <div>
          <Text strong copyable>{part.pn_std}</Text>
          <div className="replenishment-part-desc">{part.description || "暂无产品描述"}</div>
        </div>
        <Space size={4} wrap>
          <Tag color={part.pool.group_id ? "geekblue" : "default"}>
            {part.pool.name || "未加入互通池"}
          </Tag>
          {part.needs_review && <Tag color="orange">主数据待复核</Tag>}
        </Space>
      </div>
      <PriceFacts part={part} />
      <Space.Compact block>
        <InputNumber
          min={0.001}
          max={999999.999}
          precision={3}
          value={quantity}
          onChange={(value) => setQuantity(Number(value || 1))}
          style={{ flex: 1 }}
        />
        <Button disabled>{part.unit || "件"}</Button>
        <Button
          type={replacing ? "primary" : "default"}
          icon={<ShoppingCartOutlined />}
          disabled={disabled}
          onClick={() => onAdd(quantity)}
        >
          {replacing ? "替换为此 PN" : "加入补库单"}
        </Button>
      </Space.Compact>
    </Card>
  );
}

function CartLine({
  line,
  appVersion,
  disabled,
  replacing,
  feedbackReason,
  onReplace,
  onSave,
  onRemove,
}: {
  line: ReplenishmentLine;
  appVersion: number;
  disabled: boolean;
  replacing: boolean;
  feedbackReason?: string | null;
  onReplace: () => void;
  onSave: (line: ReplenishmentLine, quantity: number, note: string) => void;
  onRemove: () => void;
}) {
  const [quantity, setQuantity] = useState(line.quantity);
  const [note, setNote] = useState(line.special_note || "");
  useEffect(() => {
    setQuantity(line.quantity);
    setNote(line.special_note || "");
  }, [line.line_id, line.quantity, line.special_note, appVersion]);
  return (
    <div className={`replenishment-cart-line ${line.source_line_id ? "is-revision" : ""}`}>
      <div className="replenishment-cart-line-top">
        <div>
          <Text strong>{line.line_no}. {line.pn_std}</Text>
          <div className="replenishment-part-desc">{line.description || "暂无产品描述"}</div>
        </div>
        <Tag>{line.pool.name || "未加入互通池"}</Tag>
      </div>
      {line.source_line_id && !disabled && (
        <Alert
          showIcon
          type="warning"
          message="这是上一版本的打回项"
          description={feedbackReason
            ? `审核原因：${feedbackReason}。请重选 PN，或说明继续使用原 PN 的特殊情况。`
            : "请重选 PN，或在特殊情况说明中写明继续使用原 PN 的原因。"}
        />
      )}
      <PriceFacts part={line} />
      <Row gutter={[8, 8]}>
        <Col xs={24} sm={7}>
          <Space.Compact block>
            <InputNumber
              min={0.001}
              max={999999.999}
              precision={3}
              value={quantity}
              disabled={disabled}
              onChange={(value) => setQuantity(Number(value || 0))}
              style={{ flex: 1 }}
            />
            <Button disabled>{line.unit || "件"}</Button>
          </Space.Compact>
        </Col>
        <Col xs={24} sm={17}>
          <Input
            value={note}
            maxLength={4000}
            disabled={disabled}
            placeholder={line.source_line_id ? "特殊情况继续原 PN 时必填" : "备注（选填）"}
            onChange={(event) => setNote(event.target.value)}
          />
        </Col>
      </Row>
      {!disabled && (
        <Space wrap>
          <Button size="small" type="primary" onClick={() => onSave(line, quantity, note)}>保存</Button>
          {line.source_line_id && (
            <Button size="small" type={replacing ? "primary" : "default"} onClick={onReplace}>
              {replacing ? "正在重选：请从左侧选择" : "重选 PN"}
            </Button>
          )}
          {!line.source_line_id && <Button size="small" danger onClick={onRemove}>移除</Button>}
        </Space>
      )}
    </div>
  );
}

function VersionHistory({ versions }: { versions: ReplenishmentVersion[] }) {
  return (
    <div className="replenishment-version-history">
      <Text strong>版本留存（{versions.length}）</Text>
      <Text type="secondary" style={{ fontSize: 12 }}>每次已提交内容和审核结果只读保留</Text>
      <Collapse
        size="small"
        items={versions.map((version) => ({
          key: version.version_id,
          label: (
            <Space wrap>
              <Text strong>v{version.version_no}</Text>
              <Tag color={version.status === "draft" ? "blue" : "default"}>
                {version.status === "draft" ? "草稿" : "已提交"}
              </Tag>
              {version.review && (
                <Text type="secondary">
                  通过 {version.review.approved_count} / 打回 {version.review.rejected_count}
                </Text>
              )}
            </Space>
          ),
          children: (
            <div className="replenishment-history-version">
              <Text type="secondary">
                {version.submitted_at
                  ? `提交时间：${new Date(version.submitted_at).toLocaleString()}`
                  : "尚未提交"}
              </Text>
              {version.content_digest && (
                <Text type="secondary" copyable={{ text: version.content_digest }}>
                  版本摘要：{version.content_digest.slice(0, 12)}…
                </Text>
              )}
              {version.request_note && <Text>整单备注：{version.request_note}</Text>}
              {version.lines.map((line) => (
                <div className="replenishment-history-line" key={line.line_id}>
                  <Space wrap>
                    <Text strong>{line.line_no}. {line.pn_std}</Text>
                    <Text>数量 {line.quantity} {line.unit || "件"}</Text>
                    <Tag>{line.pool.name || "未加入互通池"}</Tag>
                    {line.review && (
                      <Tag color={line.review.decision === "approved" ? "green" : "red"}>
                        {line.review.decision === "approved" ? "通过" : "打回"}
                      </Tag>
                    )}
                  </Space>
                  <div className="replenishment-part-desc">{line.description || "暂无产品描述"}</div>
                  <PriceFacts part={line} />
                  {line.special_note && <Text>特殊情况：{line.special_note}</Text>}
                  {line.review?.reason && <Text type="danger">审核原因：{line.review.reason}</Text>}
                </div>
              ))}
            </div>
          ),
        }))}
      />
    </div>
  );
}

export default function ReplenishmentBetaPage() {
  const navigate = useNavigate();
  const [capabilities, setCapabilities] = useState<ReplenishmentCapabilities | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [query, setQuery] = useState("");
  const [catalog, setCatalog] = useState<CatalogPart[]>([]);
  const [catalogTotal, setCatalogTotal] = useState(0);
  const [catalogPage, setCatalogPage] = useState(1);
  const [applications, setApplications] = useState<ApplicationSummary[]>([]);
  const [applicationPage, setApplicationPage] = useState(1);
  const [applicationTotal, setApplicationTotal] = useState(0);
  const [current, setCurrent] = useState<ReplenishmentApplication | null>(null);
  const [warehouse, setWarehouse] = useState("");
  const [requestNote, setRequestNote] = useState("");
  const [replaceLineId, setReplaceLineId] = useState<string | null>(null);

  const latest = current?.versions[0] || null;
  const editable = !!current && current.status === "draft" && latest?.status === "draft";

  const refreshList = async (preferredId?: string, page = applicationPage) => {
    const { data } = await listReplenishmentApplications(page, 20);
    setApplications(data.items);
    setApplicationPage(data.page);
    setApplicationTotal(data.total);
    const targetId = preferredId || current?.application_id || data.items[0]?.application_id;
    if (targetId) {
      const detail = await getReplenishmentApplication(targetId);
      setCurrent(detail.data);
    } else {
      setCurrent(null);
    }
  };

  const loadCatalog = async (page = 1, q = query) => {
    const { data } = await searchReplenishmentCatalog(q, page);
    setCatalog(data.items);
    setCatalogTotal(data.total);
    setCatalogPage(data.page);
  };

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const { data } = await getReplenishmentCapabilities();
        if (!active) return;
        setCapabilities(data);
        if (data.enabled && data.can_view_price) {
          await Promise.all([loadCatalog(1, ""), refreshList()]);
        }
      } catch (error) {
        if (active) message.error(errorText(error));
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => { active = false; };
    // Initial capability and data load only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    setWarehouse(latest?.warehouse || "");
    setRequestNote(latest?.request_note || "");
    setReplaceLineId(null);
  }, [latest?.version_id, latest?.warehouse, latest?.request_note]);

  const run = async (task: () => Promise<ReplenishmentApplication>, success: string) => {
    setWorking(true);
    try {
      const next = await task();
      setCurrent(next);
      await refreshList(next.application_id);
      message.success(success);
    } catch (error) {
      message.error(errorText(error));
    } finally {
      setWorking(false);
    }
  };

  const createCart = () => run(
    async () => (await createReplenishmentApplication()).data,
    "已创建新的补库购物车",
  );

  const addOrReplace = (part: CatalogPart, quantity: number) => {
    if (!current) {
      message.info("请先创建补库购物车");
      return;
    }
    if (replaceLineId) {
      const source = latest?.lines.find((line) => line.line_id === replaceLineId);
      if (!source) return;
      void run(
        async () => {
          const response = await updateReplenishmentLine(current.application_id, source.line_id, {
            expected_version: current.version,
            part_id: part.part_id,
            quantity,
            special_note: source.special_note,
          });
          setReplaceLineId(null);
          return response.data;
        },
        `已替换为 ${part.pn_std}`,
      );
      return;
    }
    void run(
      async () => (await addReplenishmentLine(current.application_id, {
        expected_version: current.version,
        part_id: part.part_id,
        quantity,
      })).data,
      `已将 ${part.pn_std} 加入补库单`,
    );
  };

  const saveHeader = () => {
    if (!current) return;
    void run(
      async () => (await updateReplenishmentDraft(current.application_id, {
        expected_version: current.version,
        warehouse,
        request_note: requestNote,
      })).data,
      "申请信息已保存",
    );
  };

  const saveLine = (line: ReplenishmentLine, quantity: number, note: string) => {
    if (!current) return;
    void run(
      async () => (await updateReplenishmentLine(current.application_id, line.line_id, {
        expected_version: current.version,
        part_id: line.part_id,
        quantity,
        special_note: note || null,
      })).data,
      "条目已保存",
    );
  };

  const removeLine = (line: ReplenishmentLine) => {
    if (!current) return;
    Modal.confirm({
      title: `移除 ${line.pn_std}？`,
      content: "只会从当前补库草稿移除，不影响任何库存。",
      okText: "确认移除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: () => run(
        async () => (await removeReplenishmentLine(current.application_id, line.line_id, current.version)).data,
        "已从补库单移除",
      ),
    });
  };

  const submitCart = () => {
    if (!current) return;
    Modal.confirm({
      title: `提交 ${current.application_no} v${latest?.version_no}？`,
      content: "提交后本版本及半年价格快照将不可修改，审核方会针对这个精确版本反馈。",
      okText: "确认提交",
      cancelText: "继续检查",
      onOk: () => run(
        async () => (await submitReplenishmentApplication(current.application_id, current.version)).data,
        "补库申请已提交",
      ),
    });
  };

  const exportWorkbook = async (kind: "manual" | "wbdd" | "purchase") => {
    if (!current) return;
    setWorking(true);
    try {
      const response = kind === "manual"
        ? await downloadManualReviewWorkbook(current.application_id)
        : kind === "wbdd"
          ? await downloadWbddSubsetWorkbook(current.application_id)
          : await downloadPurchaseListWorkbook(current.application_id);
      saveBlob(
        response.data,
        kind === "manual"
          ? `${current.application_no}-人工审核.xlsx`
          : kind === "wbdd"
            ? `${current.application_no}-WBDD字段子集.xlsx`
            : `${current.application_no}-采购清单.xlsx`,
      );
      message.success("文件已生成；导出行为已留痕");
    } catch (error) {
      message.error(errorText(error));
    } finally {
      setWorking(false);
    }
  };

  const rejectedFeedback = useMemo(
    () => latest?.status === "submitted" && latest.review
      ? latest.lines
        .filter((line) => line.review?.decision === "rejected")
        .map((line) => ({ version: latest.version_no, line }))
      : [],
    [latest],
  );

  if (loading) return <Spin fullscreen tip="正在打开补库申请…" />;
  if (!capabilities) return <Result status="error" title="无法读取补库申请状态" />;
  if (!capabilities.enabled) {
    return (
      <Result
        status="info"
        title="补库申请当前未开放"
        subTitle="服务端功能开关已关闭，历史申请仍安全保留，库存与原业务流程不受影响。"
        extra={<Button type="primary" icon={<ArrowLeftOutlined />} onClick={() => navigate(MAINTENANCE_HOME_PATH)}>返回维保主页</Button>}
      />
    );
  }
  if (!capabilities.can_view_price) {
    return (
      <Result
        status="403"
        title="补库申请暂不可用"
        subTitle="当前账号没有半年采购/销售价格事实的查看权限，请联系管理员授权。"
        extra={<Button type="primary" icon={<ArrowLeftOutlined />} onClick={() => navigate(MAINTENANCE_HOME_PATH)}>返回维保主页</Button>}
      />
    );
  }

  // 流程可视（附录 A：状态语义显式化）：草稿 → 提交待审 → 审核结果
  const step = current === null
    ? { current: 0, status: "wait" as const }
    : current.status === "draft"
      ? { current: 0, status: "process" as const }
      : current.status === "submitted"
        ? { current: 1, status: "process" as const }
        : current.status === "needs_revision"
          ? { current: 2, status: "error" as const }
          : { current: 2, status: "finish" as const };

  // 依据层：次要导出收进「更多导出」，主操作每个状态只有一个（附录 A：一页一条决断层）
  const manualExportAllowed = current
    && ["submitted", "needs_revision", "approved"].includes(current.status)
    && latest?.status === "submitted";
  const moreExports = [
    ...(manualExportAllowed
      ? [{ key: "manual", label: "导出老板人工审核表", onClick: () => void exportWorkbook("manual") }]
      : []),
    ...(current?.status === "approved"
      ? [{
          key: "wbdd",
          label: "导出 WBDD 字段子集",
          disabled: !current.salesperson_name_snapshot,
          onClick: () => void exportWorkbook("wbdd"),
        }]
      : []),
  ];

  return (
    <div className="replenishment-beta-page">
      <PageHeader
        title="维保补库申请"
        subtitle="搜索 PN、查看近半年采购/销售事实并加入补库单；提交和审核只记录申请，不会自动定价或改变库存。"
        extra={(
          <Space wrap>
            <Button icon={<ReloadOutlined />} onClick={() => void refreshList()}>刷新审核结果</Button>
            {capabilities.can_create && (
              <Button type="primary" icon={<ShoppingCartOutlined />} onClick={() => void createCart()}>新建补库单</Button>
            )}
          </Space>
        )}
      />

      {/* ① 当前补库单：决断层（状态一句话 + 唯一主操作）→ 表单 → 行列表 → 依据层 */}
      <Card
        className="replenishment-cart-card"
        title={(
          <Space wrap>
            <ShoppingCartOutlined />
            {current ? (
              <>
                <Title level={5} copyable style={{ margin: 0 }}>{current.application_no}</Title>
                <Tag color={STATUS[current.status].color}>{STATUS[current.status].label}</Tag>
                <Text type="secondary">v{latest?.version_no} · {current.owner_display_name}</Text>
              </>
            ) : "当前补库单"}
          </Space>
        )}
      >
        {!current ? (
          <Empty description="还没有补库单——新建后从下方选购 PN 加入" image={Empty.PRESENTED_IMAGE_SIMPLE}>
            {capabilities.can_create && (
              <Button type="primary" icon={<ShoppingCartOutlined />} onClick={() => void createCart()}>新建补库单</Button>
            )}
          </Empty>
        ) : (
          <Spin spinning={working}>
            <Steps
              size="small"
              current={step.current}
              status={step.status}
              style={{ marginBottom: 16, maxWidth: 640 }}
              items={[
                { title: "编辑草稿" },
                { title: "提交待审" },
                { title: current.status === "needs_revision" ? "已打回" : "审核完成" },
              ]}
            />

            {/* 决断层：每个状态一句话 + 唯一主操作按钮 */}
            {editable && (
              <Alert
                showIcon
                type="info"
                message="草稿编辑中"
                description="从下方选购 PN 加入补库单，填好出库仓库后提交审核。"
                action={capabilities.can_create && (
                  <Button
                    type="primary"
                    disabled={!latest?.lines.length || !warehouse.trim()}
                    title={!latest?.lines.length ? "先加入至少一个 PN"
                      : !warehouse.trim() ? "先填写出库仓库" : undefined}
                    onClick={submitCart}
                  >
                    提交审核
                  </Button>
                )}
                style={{ marginBottom: 16 }}
              />
            )}
            {current.status === "submitted" && (
              <Alert
                showIcon
                type="info"
                message={`已提交 v${latest?.version_no}，等待审核方回传逐条结果`}
                action={<Button onClick={() => void refreshList()}>刷新审核结果</Button>}
                style={{ marginBottom: 16 }}
              />
            )}
            {current.status === "needs_revision" && (
              <Alert
                showIcon
                type="warning"
                message={`审核打回 ${latest?.review?.rejected_count ?? 0} 条——处理后可重新提交`}
                description={(
                  <div>
                    {rejectedFeedback.map(({ version, line }) => (
                      <div key={line.line_id}>v{version} · {line.pn_std}：{line.review?.reason}</div>
                    ))}
                  </div>
                )}
                action={capabilities.can_create && (
                  <Button
                    type="primary"
                    danger
                    onClick={() => void run(
                      async () => (await startReplenishmentRevision(current.application_id, current.version)).data,
                      "已创建复提草稿，仅带入打回条目",
                    )}
                  >
                    处理打回条目
                  </Button>
                )}
                style={{ marginBottom: 16 }}
              />
            )}
            {current.status === "approved" && (
              <Alert
                showIcon
                type="success"
                icon={<CheckCircleOutlined />}
                message="全部条目已通过审核"
                description="采购清单为四列导出（PN / 数量 / 采购金额参考 / 销售金额参考），仅作采购执行参考。"
                action={(
                  <Button type="primary" icon={<DownloadOutlined />} onClick={() => void exportWorkbook("purchase")}>
                    导出采购清单
                  </Button>
                )}
                style={{ marginBottom: 16 }}
              />
            )}

            {/* 表单区（仅草稿态可编辑；置顶不藏，提交条件一目了然） */}
            <Space direction="vertical" size={8} style={{ width: "100%", marginBottom: 16 }}>
              <Space wrap style={{ width: "100%" }}>
                <Input
                  value={warehouse}
                  disabled={!editable}
                  maxLength={64}
                  style={{ width: 260 }}
                  placeholder="出库仓库（提交前必填）"
                  onChange={(event) => setWarehouse(event.target.value)}
                />
                <Input.TextArea
                  value={requestNote}
                  disabled={!editable}
                  maxLength={4000}
                  autoSize={{ minRows: 1, maxRows: 5 }}
                  style={{ width: 360 }}
                  placeholder="整单备注（选填）"
                  onChange={(event) => setRequestNote(event.target.value)}
                />
                {editable && <Button onClick={saveHeader}>保存仓库和备注</Button>}
              </Space>
            </Space>

            {/* 事实层：条目列表 */}
            <div className="replenishment-cart-lines">
              {latest?.lines.map((line) => (
                <CartLine
                  key={line.line_id}
                  line={line}
                  appVersion={current.version}
                  disabled={!editable || !capabilities.can_create || working}
                  replacing={replaceLineId === line.line_id}
                  feedbackReason={line.source_line_id
                    ? current.versions
                      .flatMap((version) => version.lines)
                      .find((historyLine) => historyLine.line_id === line.source_line_id)
                      ?.review?.reason
                    : null}
                  onReplace={() => setReplaceLineId((value) => value === line.line_id ? null : line.line_id)}
                  onSave={saveLine}
                  onRemove={() => removeLine(line)}
                />
              ))}
              {!latest?.lines.length && (
                <Empty description="还没有条目——从下方选购 PN 加入" image={Empty.PRESENTED_IMAGE_SIMPLE} />
              )}
            </div>

            {/* 依据层：次要导出收纳 + 边界说明 + 版本留存（默认收起） */}
            <Space wrap style={{ marginTop: 16 }}>
              {moreExports.length > 0 && (
                <Dropdown
                  menu={{
                    items: moreExports.map(({ key, label, disabled }) => ({
                      key, label, disabled,
                    })),
                    onClick: ({ key }) => moreExports.find((item) => item.key === key)?.onClick(),
                  }}
                >
                  <Button icon={<DownloadOutlined />}>更多导出</Button>
                </Dropdown>
              )}
            </Space>
            {current.status === "approved" && (
              <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
                WBDD 字段子集只是录入辅助，不含源数据 ID 和 F 字段码，不能直接回灌氚云。
              </Text>
            )}
            {!current.salesperson_name_snapshot && (
              <Alert
                showIcon
                type="warning"
                message="销售人员业务映射缺失"
                description="当前账号只有页面显示名，没有氚云/销售事实使用的销售人员映射；可继续留存申请，但不能导出 WBDD 字段子集。"
                style={{ marginTop: 12 }}
              />
            )}
            <VersionHistory versions={current.versions} />
          </Spin>
        )}
      </Card>

      {/* ② 选购 PN（全宽，不再与购物单挤压） */}
      <Card
        title={<Space><SearchOutlined />选购 PN</Space>}
        extra={<Text type="secondary">没有所属池或半年价格的 PN 仍会完整显示</Text>}
        style={{ marginTop: 16 }}
      >
        {replaceLineId && (
          <Alert
            closable
            type="warning"
            showIcon
            message="重选模式：点击任一商品卡的「替换为此 PN」"
            onClose={() => setReplaceLineId(null)}
            style={{ marginBottom: 12 }}
          />
        )}
        <Input.Search
          allowClear
          enterButton="搜索"
          size="large"
          value={query}
          placeholder="搜索 PN、产品描述或品牌"
          onChange={(event) => setQuery(event.target.value)}
          onSearch={() => void loadCatalog(1)}
          style={{ marginBottom: 14, maxWidth: 560 }}
        />
        <div className="replenishment-catalog-grid">
          {catalog.map((part) => (
            <CatalogCard
              key={part.part_id}
              part={part}
              replacing={!!replaceLineId}
              disabled={!capabilities.can_create || !editable || working}
              onAdd={(quantity) => addOrReplace(part, quantity)}
            />
          ))}
        </div>
        {!catalog.length && <Empty description="没有匹配的 PN" />}
        {catalogTotal > 20 && (
          <Pagination
            current={catalogPage}
            pageSize={20}
            total={catalogTotal}
            showSizeChanger={false}
            onChange={(page) => void loadCatalog(page)}
            style={{ marginTop: 16 }}
          />
        )}
      </Card>

      {/* ③ 历史申请（折叠，不与当前单抢注意力） */}
      <Card style={{ marginTop: 16 }} styles={{ body: { paddingTop: 8 } }}>
        <Collapse
          ghost
          items={[{
            key: "history",
            label: <Space>历史申请<Badge count={applicationTotal} color="#d9d9d9" /></Space>,
            children: (
              <>
                <List
                  dataSource={applications}
                  locale={{ emptyText: "暂无申请" }}
                  renderItem={(item) => (
                    <List.Item
                      className={item.application_id === current?.application_id ? "is-current-application" : ""}
                      onClick={() => void getReplenishmentApplication(item.application_id).then((response) => setCurrent(response.data))}
                    >
                      <List.Item.Meta
                        title={<Space><span>{item.application_no}</span><Badge color={STATUS[item.status].color} text={STATUS[item.status].label} /></Space>}
                        description={`v${item.latest_version_no} · ${new Date(item.updated_at).toLocaleString()}`}
                      />
                    </List.Item>
                  )}
                />
                {applicationTotal > 20 && (
                  <Pagination
                    current={applicationPage}
                    pageSize={20}
                    total={applicationTotal}
                    showSizeChanger={false}
                    onChange={(page) => {
                      setApplicationPage(page);
                      void refreshList(undefined, page);
                    }}
                    style={{ marginTop: 12, textAlign: "right" }}
                  />
                )}
              </>
            ),
          }]}
        />
      </Card>

      {/* 口径边界：依据层，页尾小字（原先一进页就横一大条） */}
      <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 12 }}>
        口径边界：{capabilities.data_contract}
      </Text>
    </div>
  );
}
