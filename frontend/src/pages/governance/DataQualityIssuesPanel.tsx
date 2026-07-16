import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert, Button, Descriptions, Drawer, Empty, Grid, Input, List, Modal, Pagination,
  Select, Space, Spin, Table, Tag, Typography, message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  decideDataQualityIssue, getDataQualityIssue, listDataQualityIssues, reopenDataQualityIssue,
  type DataQualityDecision, type DataQualityIssueDetail, type DataQualityIssueListItem,
  type DataQualityIssueSide, type DataQualityIssueStatus, type DataQualityEvidenceValue,
} from "../../api/dataQuality";
import { activatableProps } from "../purchases/shared";

const { Text } = Typography;
const PAGE_SIZE = 20;

const STATUS_META: Record<DataQualityIssueStatus, { label: string; color: string }> = {
  open: { label: "待核实", color: "orange" },
  confirmed_valid: { label: "确认数据正确", color: "green" },
  confirmed_source_error: { label: "确认源数据错误", color: "magenta" },
  source_changed: { label: "数据已变化", color: "blue" },
};

const RULE_OPTIONS = [
  { value: "unit_price_outlier", label: "单价待核实" },
  { value: "purchase_price_neighbour_ratio", label: "采购价格待核实" },
  { value: "sales_price_neighbour_ratio", label: "销售价格待核实" },
  { value: "quantity_unit_review", label: "数量单位待核实" },
];

type PendingAction = DataQualityDecision | "reopen";

function readPermissions(): Record<string, boolean> {
  try { return JSON.parse(localStorage.getItem("permissions") || "{}"); } catch { return {}; }
}

function reviewPermissionState() {
  if (localStorage.getItem("role") === "admin") {
    return { hasAction: true, hasPurchaseCost: true, canReview: true };
  }
  const p = readPermissions();
  const hasAction = p.page_governance === true && p.action_data_quality_review === true;
  const hasPurchaseCost = p.data_purchase_cost === true;
  return { hasAction, hasPurchaseCost, canReview: hasAction && hasPurchaseCost };
}

function issueLabel(row: Pick<DataQualityIssueListItem, "order_no" | "pn_std">) {
  return `查看疑点 ${row.order_no || "无单号"} ${row.pn_std || "无型号"} 详情`;
}

function formatDateTime(value: string | null | undefined) {
  if (!value) return "—";
  return value.replace("T", " ").replace("Z", "").slice(0, 16);
}

function formatQuantity(quantity: number | null, unit: string | null) {
  if (quantity == null) return "—";
  return `${quantity.toLocaleString()}${unit ? ` ${unit}` : ""}`;
}

function formatPrice(value: number | null, restricted = false) {
  if (restricted) return "无价格权限";
  return value == null ? "—" : `¥${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function statusTag(status: DataQualityIssueStatus) {
  const meta = STATUS_META[status];
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

function sideTag(side: DataQualityIssueSide) {
  return <Tag color={side === "purchase" ? "geekblue" : "purple"}>{side === "purchase" ? "采购" : "销售"}</Tag>;
}

function ruleName(row: Pick<DataQualityIssueListItem, "rule_code" | "rule_label">) {
  return row.rule_label || RULE_OPTIONS.find((item) => item.value === row.rule_code)?.label || row.rule_code;
}

function evidenceValue(key: string, value: DataQualityEvidenceValue, priceRestricted: boolean) {
  if (priceRestricted && /price|amount|cost|金额|价格|单价/i.test(key)) return "无价格权限";
  if (value == null || value === "") return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export default function DataQualityIssuesPanel() {
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false;
  const reviewPermission = reviewPermissionState();
  const canReview = reviewPermission.canReview;
  const [status, setStatus] = useState<DataQualityIssueStatus | undefined>("open");
  const [side, setSide] = useState<DataQualityIssueSide | undefined>();
  const [ruleCode, setRuleCode] = useState<string | undefined>();
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [rows, setRows] = useState<DataQualityIssueListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [detail, setDetail] = useState<DataQualityIssueDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);
  const [note, setNote] = useState("");
  const [noteError, setNoteError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const ruleOptions = useMemo(() => {
    const byCode = new Map(RULE_OPTIONS.map((item) => [item.value, item]));
    for (const row of rows) {
      if (!byCode.has(row.rule_code)) {
        byCode.set(row.rule_code, { value: row.rule_code, label: ruleName(row) });
      }
    }
    return Array.from(byCode.values());
  }, [rows]);

  const load = useCallback(async (targetPage = page) => {
    setLoading(true);
    try {
      const data = await listDataQualityIssues({
        ...(status ? { status } : {}),
        ...(side ? { side } : {}),
        ...(ruleCode ? { rule_code: ruleCode } : {}),
        ...(query ? { q: query } : {}),
        page: targetPage,
        page_size: PAGE_SIZE,
      });
      setRows(data.items);
      setTotal(data.total);
      setPage(data.page || targetPage);
    } catch {
      message.error("数据疑点加载失败，请稍后重试");
    } finally {
      setLoading(false);
    }
  }, [page, query, ruleCode, side, status]);

  useEffect(() => { void load(1); }, [status, side, ruleCode, query]); // eslint-disable-line react-hooks/exhaustive-deps

  const openDetail = async (row: DataQualityIssueListItem) => {
    setDetailLoading(true);
    try {
      setDetail(await getDataQualityIssue(row.id));
    } catch {
      message.error("疑点详情加载失败，请稍后重试");
    } finally {
      setDetailLoading(false);
    }
  };

  const refreshDetail = async (id: number) => {
    const fresh = await getDataQualityIssue(id);
    setDetail(fresh);
  };

  const beginAction = (action: PendingAction) => {
    setPendingAction(action);
    setNote("");
    setNoteError("");
  };

  const submitAction = async () => {
    if (!detail || !pendingAction) return;
    const cleanNote = note.trim();
    if (!cleanNote) {
      setNoteError("请填写核实原因");
      return;
    }
    setSubmitting(true);
    try {
      const updated = pendingAction === "reopen"
        ? await reopenDataQualityIssue(detail.id, { version: detail.version, note: cleanNote })
        : await decideDataQualityIssue(detail.id, {
          decision: pendingAction, version: detail.version, note: cleanNote,
        });
      setDetail(updated);
      setPendingAction(null);
      setNote("");
      message.success(pendingAction === "reopen" ? "已重新打开，等待核实" : "核实结论已记录");
      await load(page);
    } catch (error: unknown) {
      const statusCode = (error as { response?: { status?: number } })?.response?.status;
      if (statusCode === 409) {
        setPendingAction(null);
        message.warning("数据已被其他人更新，已刷新，请重新核实");
        await Promise.all([load(page), refreshDetail(detail.id)]);
      } else if (statusCode === 422) {
        setNoteError("请填写核实原因");
      } else {
        message.error("核实操作失败，请稍后重试");
      }
    } finally {
      setSubmitting(false);
    }
  };

  const columns = useMemo<ColumnsType<DataQualityIssueListItem>>(() => [
    { title: "状态", dataIndex: "status", width: 116, render: statusTag },
    { title: "方向", dataIndex: "side", width: 76, render: sideTag },
    { title: "日期", dataIndex: "order_date", width: 108, render: (v) => v || "—" },
    { title: "单号", dataIndex: "order_no", width: 180, render: (v) => v || "—" },
    { title: "PN", dataIndex: "pn_std", width: 170, render: (v) => v || "—" },
    { title: "经办人", dataIndex: "handler", width: 100, render: (v) => v || "—" },
    { title: "数量/单位", key: "quantity", width: 110, align: "right", render: (_, row) => formatQuantity(row.quantity, row.unit) },
    { title: "单价", dataIndex: "unit_price", width: 120, align: "right", render: (value, row) => formatPrice(value, row.price_restricted) },
    { title: "规则", key: "rule", width: 190, render: (_, row) => ruleName(row) },
    { title: "导入批次", key: "batch", width: 180, render: (_, row) => row.import_batch_name || (row.import_batch_id ? `#${row.import_batch_id}` : "—") },
    { title: "更新时间", dataIndex: "updated_at", width: 150, render: formatDateTime },
    { title: "操作", key: "action", width: 76, fixed: "right", render: (_, row) => (
      <Button type="link" onClick={() => void openDetail(row)} aria-label={issueLabel(row)}>查看</Button>
    ) },
  ], []); // eslint-disable-line react-hooks/exhaustive-deps

  const actionTitle = pendingAction === "confirmed_valid" ? "确认数据正确"
    : pendingAction === "confirmed_source_error" ? "确认源数据错误" : "重新打开";
  const priceRestricted = detail?.price_restricted === true;

  return (
    <div data-testid="data-quality-issues-panel" style={{ maxWidth: "100%", overflowX: "hidden" }}>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="数据疑点仅表示需要人工核实"
        description="待核实记录仍参与现有价格、利润、库存和池分析；这里记录的结论不会在本阶段自动改变经营数字。"
      />

      <Space direction={isMobile ? "vertical" : "horizontal"} wrap style={{ width: "100%", marginBottom: 16 }}>
        <div aria-label="疑点状态" style={{ width: isMobile ? "100%" : 170 }}>
          <Select
            allowClear value={status} placeholder="全部状态" style={{ width: "100%" }}
            onChange={(value) => { setPage(1); setStatus(value); }}
            options={Object.entries(STATUS_META).map(([value, meta]) => ({ value, label: meta.label }))}
          />
        </div>
        <div aria-label="业务方向" style={{ width: isMobile ? "100%" : 130 }}>
          <Select
            allowClear value={side} placeholder="采购/销售" style={{ width: "100%" }}
            onChange={(value) => { setPage(1); setSide(value); }}
            options={[{ value: "purchase", label: "采购" }, { value: "sales", label: "销售" }]}
          />
        </div>
        <div aria-label="疑点规则" style={{ width: isMobile ? "100%" : 190 }}>
          <Select
            allowClear value={ruleCode} placeholder="全部规则" style={{ width: "100%" }}
            onChange={(value) => { setPage(1); setRuleCode(value); }} options={ruleOptions}
          />
        </div>
        <Input.Search
          value={queryInput} placeholder="搜索 PN 或单号" allowClear
          style={{ width: isMobile ? "100%" : 260 }}
          onChange={(event) => setQueryInput(event.target.value)}
          onSearch={(value) => { setPage(1); setQuery(value.trim()); }}
        />
      </Space>

      {isMobile ? (
        <Spin spinning={loading}>
          {rows.length ? (
            <List
              dataSource={rows}
              renderItem={(row) => (
                <List.Item
                  {...activatableProps(() => void openDetail(row), issueLabel(row))}
                  style={{ display: "block", padding: "14px 4px", cursor: "pointer" }}
                >
                  <Space direction="vertical" size={7} style={{ width: "100%" }}>
                    <Space wrap>{statusTag(row.status)}{sideTag(row.side)}<Text strong>{row.pn_std || "无型号"}</Text></Space>
                    <Text>{row.order_no || "无单号"} · {row.order_date || "无日期"}</Text>
                    <Space wrap><Text type="secondary">{row.handler || "无经办人"}</Text><Text>{formatQuantity(row.quantity, row.unit)}</Text><Text>{formatPrice(row.unit_price, row.price_restricted)}</Text></Space>
                    <Text type="secondary">{ruleName(row)}</Text>
                  </Space>
                </List.Item>
              )}
            />
          ) : !loading ? <QueueEmpty /> : null}
          {total > PAGE_SIZE && (
            <Pagination simple current={page} pageSize={PAGE_SIZE} total={total} onChange={(next) => void load(next)} />
          )}
        </Spin>
      ) : rows.length || loading ? (
        <Table
          rowKey="id" size="small" loading={loading} columns={columns} dataSource={rows}
          scroll={{ x: 1570 }}
          pagination={{ current: page, pageSize: PAGE_SIZE, total, showSizeChanger: false, onChange: (next) => void load(next) }}
        />
      ) : <QueueEmpty />}

      <Drawer
        open={!!detail || detailLoading}
        title={detail ? `疑点详情 · ${detail.order_no || `#${detail.id}`}` : "疑点详情"}
        onClose={() => setDetail(null)}
        placement={isMobile ? "bottom" : "right"}
        height={isMobile ? "100%" : undefined}
        width={isMobile ? undefined : 720}
        styles={{ body: { overflowY: "auto" } }}
        footer={detail && canReview ? (
          <Space wrap>
            {detail.status === "open" ? <>
              <Button type="primary" onClick={() => beginAction("confirmed_valid")}>确认数据正确</Button>
              <Button onClick={() => beginAction("confirmed_source_error")}>确认源数据错误</Button>
            </> : (
              <Button onClick={() => beginAction("reopen")}>重新打开</Button>
            )}
          </Space>
        ) : null}
      >
        <Spin spinning={detailLoading}>
          {detail && <>
            {reviewPermission.hasAction && !reviewPermission.hasPurchaseCost && (
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 16 }}
                message="无采购成本数据权限，不能确认"
                description="你仍可查看已授权的信息；如需提交核实结论，请联系管理员补充采购成本数据权限。"
              />
            )}
            <IssueDetail detail={detail} priceRestricted={priceRestricted} />
          </>}
        </Spin>
      </Drawer>

      <Modal
        open={!!pendingAction}
        title={actionTitle}
        okText="确认提交"
        cancelText="取消"
        confirmLoading={submitting}
        onCancel={() => setPendingAction(null)}
        onOk={() => void submitAction()}
      >
        <p>该结论会实名写入审计记录。请再次确认，并说明你核实过的依据。</p>
        <Input.TextArea
          rows={4} value={note} placeholder="必填：说明核实依据和结论"
          status={noteError ? "error" : undefined}
          onChange={(event) => { setNote(event.target.value); if (event.target.value.trim()) setNoteError(""); }}
        />
        {noteError && <div role="alert" style={{ color: "var(--mb-danger)", marginTop: 6 }}>{noteError}</div>}
      </Modal>
    </div>
  );
}

function QueueEmpty() {
  return (
    <Empty description={null}>
      <Space direction="vertical" size={4}>
        <Text strong>当前尚未启用自动阈值规则</Text>
        <Text type="secondary">这里没有记录不代表所有数据都已核实正确。</Text>
      </Space>
    </Empty>
  );
}

function IssueDetail({ detail, priceRestricted }: { detail: DataQualityIssueDetail; priceRestricted: boolean }) {
  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <section>
        <Typography.Title level={5}>原始事实</Typography.Title>
        <Descriptions bordered size="small" column={{ xs: 1, sm: 2 }}>
          <Descriptions.Item label="状态">{statusTag(detail.status)}</Descriptions.Item>
          <Descriptions.Item label="方向">{sideTag(detail.side)}</Descriptions.Item>
          <Descriptions.Item label="PN">{detail.pn_std || "—"}</Descriptions.Item>
          <Descriptions.Item label="描述">{detail.fact.description || "—"}</Descriptions.Item>
          <Descriptions.Item label="品牌">{detail.fact.brand || "—"}</Descriptions.Item>
          <Descriptions.Item label="经办人">{detail.handler || "—"}</Descriptions.Item>
          <Descriptions.Item label="数量/单位">{formatQuantity(detail.fact.quantity, detail.fact.unit)}</Descriptions.Item>
          <Descriptions.Item label="单价">{formatPrice(detail.fact.unit_price, priceRestricted)}</Descriptions.Item>
          <Descriptions.Item label="行金额">{formatPrice(detail.fact.line_amount, priceRestricted)}</Descriptions.Item>
        </Descriptions>
      </section>

      <section>
        <Typography.Title level={5}>规则证据</Typography.Title>
        {detail.evidence_restricted ? (
          <Alert type="warning" showIcon message="无价格权限，规则证据已隐藏" />
        ) : (
          <Descriptions bordered size="small" column={1}>
            <Descriptions.Item label="规则">{ruleName(detail)}</Descriptions.Item>
            {Object.entries(detail.evidence || {}).map(([key, value]) => (
              <Descriptions.Item key={key} label={key}>{evidenceValue(key, value, priceRestricted)}</Descriptions.Item>
            ))}
          </Descriptions>
        )}
      </section>

      <section>
        <Typography.Title level={5}>订单定位</Typography.Title>
        <Descriptions bordered size="small" column={{ xs: 1, sm: 2 }}>
          <Descriptions.Item label="单号">{detail.order.order_no || detail.order_no || "—"}</Descriptions.Item>
          <Descriptions.Item label="日期">{detail.order.order_date || detail.order_date || "—"}</Descriptions.Item>
          <Descriptions.Item label="经办人">{detail.order.handler || detail.handler || "—"}</Descriptions.Item>
          <Descriptions.Item label="往来单位">{detail.order.counterparty || "—"}</Descriptions.Item>
          <Descriptions.Item label="数据状态">{detail.order.data_status || "—"}</Descriptions.Item>
        </Descriptions>
      </section>

      <section>
        <Typography.Title level={5}>导入批次定位</Typography.Title>
        {detail.batch ? (
          <Descriptions bordered size="small" column={{ xs: 1, sm: 2 }}>
            <Descriptions.Item label="批次">{detail.batch.filename || `#${detail.batch.id}`}</Descriptions.Item>
            <Descriptions.Item label="导入人">{detail.batch.imported_by || "—"}</Descriptions.Item>
            <Descriptions.Item label="导入时间">{formatDateTime(detail.batch.imported_at)}</Descriptions.Item>
          </Descriptions>
        ) : <Text type="secondary">该记录没有可用的导入批次定位。</Text>}
      </section>

      <section>
        <Typography.Title level={5}>已有结论与审计摘要</Typography.Title>
        <Descriptions bordered size="small" column={{ xs: 1, sm: 2 }}>
          <Descriptions.Item label="发现人">{detail.detected_by || "—"}</Descriptions.Item>
          <Descriptions.Item label="发现时间">{formatDateTime(detail.detected_at)}</Descriptions.Item>
          <Descriptions.Item label="最近核实人">{detail.reviewed_by || "—"}</Descriptions.Item>
          <Descriptions.Item label="核实时间">{formatDateTime(detail.reviewed_at)}</Descriptions.Item>
          <Descriptions.Item label="核实原因" span={2}>{detail.review_note || "尚无人工结论"}</Descriptions.Item>
        </Descriptions>
        {!!detail.audits?.length && (
          <List
            size="small" style={{ marginTop: 8 }} dataSource={detail.audits}
            renderItem={(audit) => <List.Item>{formatDateTime(audit.created_at)} · {audit.username || "系统"} · {audit.note || audit.action}</List.Item>}
          />
        )}
      </section>
    </Space>
  );
}
