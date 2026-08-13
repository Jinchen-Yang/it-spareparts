import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Input,
  Modal,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType, TableRowSelection } from "antd/es/table/interface";
import axios from "axios";

import {
  armMaintenanceDemandDeleteIntent,
  cancelMaintenanceDemandDeleteIntent,
  createMaintenanceDemandDeleteIntent,
  executeMaintenanceDemandDeleteIntent,
  searchMaintenanceDemands,
  type MaintenanceDemandDeleteIntent,
  type MaintenanceDemandSummary,
} from "../../api/maintenanceDemands";
import PageHeader from "../../components/PageHeader";
import { readMaintenanceCapabilities } from "../../components/maintenance/maintenancePermissions";

const MAX_SELECTED_HEADERS = 1_000;
const MAX_SELECTED_LINES = 20_000;

function newIdempotencyKey(): string {
  const random = globalThis.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `wbdd-delete-${random}`;
}

function errorText(error: unknown, fallback: string): string {
  if (!axios.isAxiosError(error)) return fallback;
  const detail = error.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail.message === "string") return detail.message;
  return fallback;
}

function retryDelayFrom425(error: unknown): number | null {
  if (!axios.isAxiosError(error) || error.response?.status !== 425) return null;
  const detail = error.response.data?.detail;
  const notBefore = Date.parse(detail?.not_before || "");
  const serverNow = Date.parse(detail?.server_now || "");
  if (!Number.isFinite(notBefore) || !Number.isFinite(serverNow)) return 1_000;
  return Math.max(0, notBefore - serverNow);
}

function referenceText(row: MaintenanceDemandSummary): string {
  if (row.downstream_references.length === 0) return "暂无下游引用";
  return row.downstream_references.map((reference) => reference.label).join("；");
}

const columns: ColumnsType<MaintenanceDemandSummary> = [
  {
    title: "维保需求单",
    dataIndex: "order_no",
    width: 210,
    render: (value: string, row) => (
      <Space direction="vertical" size={1}>
        <Typography.Text strong>{value}</Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {row.source_order_id}
        </Typography.Text>
      </Space>
    ),
  },
  { title: "制单日期", dataIndex: "order_date", width: 120, render: (value) => value || "—" },
  { title: "项目", dataIndex: "project", ellipsis: true, render: (value) => value || "未填写项目" },
  {
    title: "备件行数",
    dataIndex: "line_count",
    width: 100,
    render: (value: number) => <Tag>{value} 行</Tag>,
  },
  {
    title: "下游引用提示",
    width: 260,
    render: (_, row) => (
      <Typography.Text type={row.downstream_references.length ? undefined : "secondary"}>
        {referenceText(row)}
      </Typography.Text>
    ),
  },
];

export default function MaintenanceDemandManagementPage({ pageSize = 50 }: { pageSize?: number }) {
  const [{ canDeleteDemand }] = useState(readMaintenanceCapabilities);
  const [query, setQuery] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [page, setPage] = useState(1);
  const [rows, setRows] = useState<MaintenanceDemandSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [selected, setSelected] = useState<Record<string, MaintenanceDemandSummary>>({});
  const [selectionError, setSelectionError] = useState("");
  const [reviewOpen, setReviewOpen] = useState(false);
  const [reviewFilter, setReviewFilter] = useState("");
  const [reason, setReason] = useState("");
  const [intent, setIntent] = useState<MaintenanceDemandDeleteIntent | null>(null);
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [working, setWorking] = useState(false);
  const [workflowError, setWorkflowError] = useState("");
  const [success, setSuccess] = useState("");
  const [waitDeadline, setWaitDeadline] = useState<number | null>(null);
  const [countdown, setCountdown] = useState(0);
  const loadGeneration = useRef(0);

  const load = useCallback(async () => {
    const generation = ++loadGeneration.current;
    setLoading(true);
    setLoadError("");
    try {
      const { data } = await searchMaintenanceDemands({
        q: submittedQuery || undefined,
        page,
        page_size: pageSize,
      });
      if (generation !== loadGeneration.current) return;
      setRows(data.items);
      setTotal(data.total);
    } catch (error) {
      if (generation !== loadGeneration.current) return;
      setRows([]);
      setTotal(0);
      setLoadError(errorText(error, "维保需求单加载失败，请重试。"));
    } finally {
      if (generation === loadGeneration.current) setLoading(false);
    }
  }, [page, pageSize, submittedQuery]);

  useEffect(() => {
    void load();
    return () => { loadGeneration.current += 1; };
  }, [load]);

  useEffect(() => {
    if (waitDeadline === null) {
      setCountdown(0);
      return undefined;
    }
    const update = () => setCountdown(Math.max(0, Math.ceil((waitDeadline - Date.now()) / 1_000)));
    update();
    const timer = window.setInterval(update, 100);
    return () => window.clearInterval(timer);
  }, [waitDeadline]);

  const selectedRows = useMemo(() => Object.values(selected), [selected]);
  const selectedLineCount = useMemo(
    () => selectedRows.reduce((sum, row) => sum + row.line_count, 0),
    [selectedRows],
  );
  const canonicalReviewRows = intent?.items || selectedRows;
  const filteredReviewRows = useMemo(() => {
    const term = reviewFilter.trim().toLocaleLowerCase();
    if (!term) return canonicalReviewRows;
    return canonicalReviewRows.filter((row) => [
      row.order_no,
      row.source_order_id,
      row.project,
      row.linked_sales_order_no,
    ].some((value) => String(value || "").toLocaleLowerCase().includes(term)));
  }, [canonicalReviewRows, reviewFilter]);

  const applySelection = (updates: MaintenanceDemandSummary[], checked: boolean) => {
    setSelectionError("");
    setSelected((current) => {
      const next = { ...current };
      for (const row of updates) {
        if (checked) next[row.source_order_id] = row;
        else delete next[row.source_order_id];
      }
      const nextRows = Object.values(next);
      const lines = nextRows.reduce((sum, row) => sum + row.line_count, 0);
      if (nextRows.length > MAX_SELECTED_HEADERS || lines > MAX_SELECTED_LINES) {
        setSelectionError(
          `单批最多 ${MAX_SELECTED_HEADERS} 张、${MAX_SELECTED_LINES} 行；本次勾选未加入。`,
        );
        return current;
      }
      return next;
    });
  };

  const rowSelection: TableRowSelection<MaintenanceDemandSummary> | undefined = canDeleteDemand
    ? {
      preserveSelectedRowKeys: true,
      selectedRowKeys: Object.keys(selected),
      onSelect: (row, checked) => applySelection([row], checked),
      onSelectAll: (checked, _selectedRows, changedRows) => applySelection(changedRows, checked),
    }
    : undefined;

  const beginReview = () => {
    setReviewFilter("");
    setReason("");
    setIntent(null);
    setIdempotencyKey(newIdempotencyKey());
    setWorkflowError("");
    setWaitDeadline(null);
    setReviewOpen(true);
  };

  const closeReview = () => {
    const cancellable = intent && ["reviewed", "armed_wait"].includes(intent.status);
    if (cancellable) {
      void cancelMaintenanceDemandDeleteIntent(
        intent.intent_id,
        intent.selection_digest,
      ).catch(() => undefined);
    }
    setReviewOpen(false);
    setIntent(null);
    setWaitDeadline(null);
    setWorking(false);
  };

  const prepareIntent = async () => {
    if (!reason.trim() || selectedRows.length === 0) return;
    setWorking(true);
    setWorkflowError("");
    try {
      const { data } = await createMaintenanceDemandDeleteIntent({
        source_order_ids: selectedRows.map((row) => row.source_order_id),
        reason: reason.trim(),
        idempotency_key: idempotencyKey,
      });
      setIntent(data);
    } catch (error) {
      setWorkflowError(errorText(error, "服务端复核清单生成失败，未执行删除。"));
    } finally {
      setWorking(false);
    }
  };

  const armIntent = async () => {
    if (!intent) return;
    setWorking(true);
    setWorkflowError("");
    try {
      const { data } = await armMaintenanceDemandDeleteIntent(
        intent.intent_id,
        intent.selection_digest,
      );
      setIntent(data);
      const notBefore = Date.parse(data.not_before || "");
      setWaitDeadline(Number.isFinite(notBefore) ? notBefore : Date.now() + 7_000);
    } catch (error) {
      setWorkflowError(errorText(error, "第一次确认失败，未开始安全等待。"));
    } finally {
      setWorking(false);
    }
  };

  const executeIntent = async () => {
    if (!intent) return;
    setWorking(true);
    setWorkflowError("");
    try {
      const { data } = await executeMaintenanceDemandDeleteIntent(
        intent.intent_id,
        intent.selection_digest,
      );
      setSuccess(`已逻辑删除 ${data.header_count} 张 WBDD、${data.line_count} 行；原始数据与审计仍保留。`);
      setSelected({});
      setReviewOpen(false);
      setIntent(null);
      setWaitDeadline(null);
      await load();
    } catch (error) {
      const retryDelay = retryDelayFrom425(error);
      if (retryDelay !== null) setWaitDeadline(Date.now() + retryDelay);
      setWorkflowError(errorText(error, "第二次确认失败，整批未删除。"));
    } finally {
      setWorking(false);
    }
  };

  const submitSearch = () => {
    setPage(1);
    setSubmittedQuery(query.trim());
  };

  const reviewLineCount = intent?.line_count ?? selectedLineCount;
  const footer = (
    <Space wrap>
      <Button onClick={closeReview} disabled={working}>取消</Button>
      {!intent && (
        <Button
          type="primary"
          disabled={!reason.trim() || selectedRows.length === 0}
          loading={working}
          onClick={() => void prepareIntent()}
        >
          生成服务端完整复核清单
        </Button>
      )}
      {intent?.status === "reviewed" && (
        <Button
          type="primary"
          danger
          loading={working}
          onClick={() => void armIntent()}
        >
          第一次确认并开始 7 秒等待
        </Button>
      )}
      {intent?.status === "armed_wait" && (
        <Button
          type="primary"
          danger
          disabled={countdown > 0}
          loading={working}
          onClick={() => void executeIntent()}
        >
          {countdown > 0 ? `第二次确认删除（${countdown} 秒）` : "第二次确认删除"}
        </Button>
      )}
    </Space>
  );

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title="维保需求单管理"
        subtitle="跨页检索 WBDD；删除仅生成可恢复墓碑，原始订单、明细和审计不会被物理删除。"
      />
      {loadError && <Alert type="error" showIcon message={loadError} />}
      {selectionError && <Alert type="warning" showIcon message={selectionError} closable />}
      {success && <Alert type="success" showIcon message={success} closable />}

      <Card>
        <Space.Compact style={{ width: "min(720px, 100%)" }}>
          <Input
            aria-label="搜索维保需求单"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onPressEnter={submitSearch}
            placeholder="搜索 WBDD 单号、项目、合同或 PN"
            allowClear
          />
          <Button type="primary" onClick={submitSearch}>搜索</Button>
        </Space.Compact>
      </Card>

      {canDeleteDemand && selectedRows.length > 0 && (
        <Card size="small">
          <Space wrap>
            <Typography.Text strong>
              已跨页选择 {selectedRows.length} 张 / {selectedLineCount} 行
            </Typography.Text>
            <Button danger onClick={beginReview}>
              {`复核并删除（${selectedRows.length}）`}
            </Button>
            <Button onClick={() => setSelected({})}>清空选择</Button>
          </Space>
        </Card>
      )}

      <Card>
        <Table<MaintenanceDemandSummary>
          rowKey="source_order_id"
          columns={columns}
          dataSource={rows}
          loading={loading}
          rowSelection={rowSelection}
          scroll={{ x: 980 }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: false,
            showTotal: (value) => `共 ${value} 张`,
            onChange: setPage,
          }}
        />
      </Card>

      <Modal
        open={reviewOpen}
        width={1_080}
        title={`完整复核清单（${canonicalReviewRows.length} 张 / ${reviewLineCount} 行）`}
        footer={footer}
        onCancel={closeReview}
        maskClosable={false}
        destroyOnHidden
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Alert
            type="warning"
            showIcon
            message="这是整单逻辑删除：每张 WBDD 的全部备件行会一起退出成本、库存、项目视图和导出。"
            description="源订单、源明细、项目归属和审计永久保留；需要恢复时必须走独立实名管理员入口。"
          />
          <Input
            aria-label="复核清单筛选"
            value={reviewFilter}
            onChange={(event) => setReviewFilter(event.target.value)}
            placeholder="仅筛选本复核清单，不改变实际选中项"
            allowClear
          />
          <Table<MaintenanceDemandSummary>
            rowKey="source_order_id"
            size="small"
            columns={[
              ...columns,
              ...(!intent ? [{
                title: "操作",
                width: 80,
                render: (_: unknown, row: MaintenanceDemandSummary) => (
                  <Button
                    type="link"
                    danger
                    onClick={() => applySelection([row], false)}
                  >
                    移出
                  </Button>
                ),
              }] : []),
            ]}
            dataSource={filteredReviewRows}
            pagination={false}
            scroll={{ x: 1_000, y: 320 }}
          />
          <Input.TextArea
            aria-label="删除理由"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            disabled={intent !== null}
            maxLength={1_000}
            showCount
            rows={3}
            placeholder="必填：说明为什么这些 WBDD 应从有效业务数据中删除"
          />
          {!intent && !reason.trim() && (
            <Typography.Text type="danger">删除理由不能为空。</Typography.Text>
          )}
          {intent?.status === "reviewed" && (
            <Alert
              type="info"
              showIcon
              message="服务端已冻结逐单版本和清单摘要；请再次核对上方全部条目后做第一次确认。"
            />
          )}
          {intent?.status === "armed_wait" && (
            <Alert
              type={countdown > 0 ? "warning" : "error"}
              showIcon
              message={countdown > 0
                ? `服务端安全等待中，还需 ${countdown} 秒。`
                : "七秒等待已结束；第二次确认后才会整批执行。"}
            />
          )}
          {workflowError && <Alert type="error" showIcon message={workflowError} />}
        </Space>
      </Modal>
    </Space>
  );
}
