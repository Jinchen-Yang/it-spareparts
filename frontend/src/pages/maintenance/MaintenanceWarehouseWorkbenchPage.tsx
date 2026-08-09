import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import axios from "axios";

import {
  applyWarehouseImport,
  previewWarehouseImport,
  resolveWarehouseAmbiguity,
  searchWarehouseAmbiguities,
  searchWarehouseDocuments,
  type WarehouseAmbiguitySummary,
  type WarehouseDocumentSummary,
  type WarehouseImportPreview,
} from "../../api/maintenanceWarehouse";
import PageHeader from "../../components/PageHeader";
import { readMaintenanceCapabilities } from "../../components/maintenance/maintenancePermissions";


const ambiguityLabels: Record<string, string> = {
  unknown_version: "未知模板版本",
  missing_document_id: "缺少单据稳定 ID",
  missing_line_id: "缺少明细稳定 ID",
  missing_stable_link: "稳定键未命中",
  multiple_candidates: "稳定键多候选",
  field_conflict: "同一稳定 ID 内容冲突",
  unknown_enum: "未知枚举值",
  controlled_attachment: "附件需人工查看",
};

const documentLabels: Record<string, string> = {
  shipment: "发货/出库",
  return: "退货返库",
  receipt: "收货/入库",
};

function errorText(error: unknown, fallback: string): string {
  if (!axios.isAxiosError(error)) return fallback;
  const detail = error.response?.data?.detail;
  if (typeof detail === "string") return detail;
  return fallback;
}

function linkKind(targetType: string): string | undefined {
  return {
    maintenance_order: "maintenance_order",
    maintenance_project: "project",
    maintenance_site_issue: "site_issue",
    dim_part: "part",
    warehouse_document: "warehouse_document",
  }[targetType];
}

export default function MaintenanceWarehouseWorkbenchPage() {
  const [{ canManageWarehouse }] = useState(readMaintenanceCapabilities);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<WarehouseImportPreview | null>(null);
  const [importReason, setImportReason] = useState("");
  const [importBusy, setImportBusy] = useState(false);
  const [importMessage, setImportMessage] = useState("");
  const [importError, setImportError] = useState("");
  const [query, setQuery] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [documents, setDocuments] = useState<WarehouseDocumentSummary[]>([]);
  const [documentTotal, setDocumentTotal] = useState(0);
  const [documentPage, setDocumentPage] = useState(1);
  const [ambiguities, setAmbiguities] = useState<WarehouseAmbiguitySummary[]>([]);
  const [ambiguityTotal, setAmbiguityTotal] = useState(0);
  const [ambiguityPage, setAmbiguityPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [selected, setSelected] = useState<WarehouseAmbiguitySummary | null>(null);
  const [decision, setDecision] = useState<"acknowledge" | "link">("acknowledge");
  const [candidateIndex, setCandidateIndex] = useState<number | null>(null);
  const [resolutionReason, setResolutionReason] = useState("");
  const [resolutionBusy, setResolutionBusy] = useState(false);

  const loadDocuments = useCallback(async () => {
    const { data } = await searchWarehouseDocuments({
      q: submittedQuery || undefined,
      page: documentPage,
      page_size: 50,
    });
    setDocuments(data.items);
    setDocumentTotal(data.total);
  }, [documentPage, submittedQuery]);

  const loadAmbiguities = useCallback(async () => {
    const { data } = await searchWarehouseAmbiguities({
      q: submittedQuery || undefined,
      status: "open",
      page: ambiguityPage,
      page_size: 50,
    });
    setAmbiguities(data.items);
    setAmbiguityTotal(data.total);
  }, [ambiguityPage, submittedQuery]);

  const reload = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      await Promise.all([loadDocuments(), loadAmbiguities()]);
    } catch (error) {
      setLoadError(errorText(error, "仓库单据工作台加载失败，请重试。"));
    } finally {
      setLoading(false);
    }
  }, [loadAmbiguities, loadDocuments]);

  useEffect(() => { void reload(); }, [reload]);

  const startPreview = async () => {
    if (!file) return;
    setImportBusy(true);
    setImportError("");
    setImportMessage("");
    setPreview(null);
    try {
      const { data } = await previewWarehouseImport(file);
      setPreview(data);
    } catch (error) {
      setImportError(errorText(error, "预览失败，请检查模板后重试。"));
    } finally {
      setImportBusy(false);
    }
  };

  const applyImport = async () => {
    if (!file || !preview || !importReason.trim()) return;
    setImportBusy(true);
    setImportError("");
    try {
      const { data } = await applyWarehouseImport(preview, file, importReason.trim());
      setImportMessage(
        data.idempotent_replay
          ? "该文件已经应用过，本次重放零新增、零变更。"
          : `已固化 ${data.new_document_count} 张单据、${data.new_line_count} 行明细；${data.ambiguity_count} 条进入歧义队列。`,
      );
      setPreview(null);
      setFile(null);
      setImportReason("");
      await reload();
    } catch (error) {
      setImportError(errorText(error, "应用失败，系统已回滚本次写入。"));
    } finally {
      setImportBusy(false);
    }
  };

  const openResolution = (row: WarehouseAmbiguitySummary) => {
    setSelected(row);
    setDecision(row.candidates.length ? "link" : "acknowledge");
    setCandidateIndex(row.candidates.length === 1 ? 0 : null);
    setResolutionReason("");
  };

  const resolve = async () => {
    if (!selected || !resolutionReason.trim()) return;
    const candidate = candidateIndex == null ? undefined : selected.candidates[candidateIndex];
    if (decision === "link" && (!candidate || !linkKind(candidate.target_type))) return;
    setResolutionBusy(true);
    try {
      await resolveWarehouseAmbiguity(selected.ambiguity_id, {
        version: selected.version,
        reason: resolutionReason.trim(),
        decision,
        ...(decision === "link" && candidate ? {
          link_kind: linkKind(candidate.target_type),
          target_type: candidate.target_type,
          target_id: candidate.target_id,
        } : {}),
      });
      setSelected(null);
      await reload();
    } catch (error) {
      setLoadError(errorText(error, "裁决失败，请刷新后重试。"));
    } finally {
      setResolutionBusy(false);
    }
  };

  const documentColumns: ColumnsType<WarehouseDocumentSummary> = useMemo(() => [
    {
      title: "单据",
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{row.document_no || "未提供单号"}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {row.source_document_id}
          </Typography.Text>
        </Space>
      ),
    },
    { title: "类型", dataIndex: "document_type", width: 110, render: (value) => documentLabels[value] || value },
    { title: "日期", dataIndex: "document_date", width: 120, render: (value) => value || "—" },
    { title: "明细", dataIndex: "line_count", width: 90, render: (value) => `${value} 行` },
    {
      title: "关联状态",
      dataIndex: "open_ambiguity_count",
      width: 130,
      render: (value: number) => value > 0 ? <Tag color="gold">{value} 条待处理</Tag> : <Tag color="green">已明确</Tag>,
    },
  ], []);

  const ambiguityColumns: ColumnsType<WarehouseAmbiguitySummary> = useMemo(() => [
    {
      title: "单据",
      render: (_, row) => row.document ? (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{row.document.document_no || "未提供单号"}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {documentLabels[row.document.document_type] || row.document.document_type}
          </Typography.Text>
        </Space>
      ) : <Typography.Text type="secondary">仅文件级问题</Typography.Text>,
    },
    {
      title: "待确认原因",
      dataIndex: "ambiguity_type",
      render: (value, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text>{ambiguityLabels[value] || value}</Typography.Text>
          {row.field_code && <Typography.Text type="secondary" style={{ fontSize: 12 }}>{row.field_code}</Typography.Text>}
        </Space>
      ),
    },
    { title: "候选", dataIndex: "candidates", width: 90, render: (value) => `${value.length} 个` },
    {
      title: "操作",
      width: 100,
      render: (_, row) => canManageWarehouse
        ? <Button type="link" onClick={() => openResolution(row)}>人工裁决</Button>
        : <Typography.Text type="secondary">只读</Typography.Text>,
    },
  ], [canManageWarehouse]);

  return (
    <div>
      <PageHeader
        title="仓库单据与关联歧义"
        subtitle="发货、返库、入库统一固化为可追溯事实；仅按稳定 ID 关联，不按项目名或日期猜测。"
      />

      <Alert
        type="info"
        showIcon
        message="本工作台不会修改库存、成本或返还率"
        description="预览阶段零写入；应用后，无法明确关联的记录保留完整单据事实并进入人工队列。附件内容不进入事实库。"
        style={{ marginBottom: 16 }}
      />

      {canManageWarehouse && (
        <Card title="导入仓库导出文件" style={{ marginBottom: 16 }}>
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <input
              aria-label="选择仓库工作簿"
              type="file"
              accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              onChange={(event) => {
                setFile(event.target.files?.[0] || null);
                setPreview(null);
                setImportMessage("");
              }}
            />
            <Button type="primary" disabled={!file} loading={importBusy} onClick={() => void startPreview()}>
              零写入预览
            </Button>
            {preview && (
              <Card size="small" title="预览结果">
                <Space direction="vertical" size={8} style={{ width: "100%" }}>
                  <Typography.Text>
                    {documentLabels[preview.adapter_key] || preview.adapter_key} · {preview.document_count} 张 / {preview.line_count} 行
                  </Typography.Text>
                  {preview.version_state === "unknown_version" && (
                    <Tag color="gold">必填结构可识别，但完整版本未知；应用后需人工确认版本</Tag>
                  )}
                  <Typography.Text type="secondary">
                    当前预览发现 {Object.values(preview.adapter_ambiguity_counts).reduce((sum, value) => sum + value, 0)} 条结构歧义
                  </Typography.Text>
                  <Input.TextArea
                    aria-label="导入理由"
                    value={importReason}
                    onChange={(event) => setImportReason(event.target.value)}
                    maxLength={1000}
                    placeholder="填写本次数据版本、来源月份或业务原因"
                    autoSize={{ minRows: 2, maxRows: 4 }}
                  />
                  <Button
                    type="primary"
                    danger
                    disabled={!importReason.trim()}
                    loading={importBusy}
                    onClick={() => void applyImport()}
                  >
                    确认原子应用
                  </Button>
                </Space>
              </Card>
            )}
            {importError && <Alert type="error" showIcon message={importError} />}
            {importMessage && <Alert type="success" showIcon message={importMessage} />}
          </Space>
        </Card>
      )}

      <Card>
        <Space.Compact style={{ width: "min(520px, 100%)", marginBottom: 16 }}>
          <Input
            aria-label="搜索仓库单据"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onPressEnter={() => { setDocumentPage(1); setAmbiguityPage(1); setSubmittedQuery(query.trim()); }}
            placeholder="搜索单号或稳定 ID"
          />
          <Button onClick={() => { setDocumentPage(1); setAmbiguityPage(1); setSubmittedQuery(query.trim()); }}>搜索</Button>
        </Space.Compact>
        {loadError && <Alert type="error" showIcon message={loadError} style={{ marginBottom: 12 }} />}
        <Tabs items={[
          {
            key: "ambiguities",
            label: `待处理歧义 ${ambiguityTotal}`,
            children: (
              <Table
                rowKey="ambiguity_id"
                loading={loading}
                columns={ambiguityColumns}
                dataSource={ambiguities}
                pagination={{ current: ambiguityPage, pageSize: 50, total: ambiguityTotal, onChange: setAmbiguityPage }}
              />
            ),
          },
          {
            key: "documents",
            label: `单据事实 ${documentTotal}`,
            children: (
              <Table
                rowKey="document_id"
                loading={loading}
                columns={documentColumns}
                dataSource={documents}
                pagination={{ current: documentPage, pageSize: 50, total: documentTotal, onChange: setDocumentPage }}
              />
            ),
          },
        ]} />
      </Card>

      <Modal
        title="人工裁决关联歧义"
        open={selected !== null}
        onCancel={() => setSelected(null)}
        onOk={() => void resolve()}
        okText="实名确认裁决"
        confirmLoading={resolutionBusy}
        okButtonProps={{ disabled: !resolutionReason.trim() || (decision === "link" && candidateIndex == null) }}
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Alert
            type="warning"
            showIcon
            message={selected ? (ambiguityLabels[selected.ambiguity_type] || selected.ambiguity_type) : ""}
            description="系统会记录裁决前后关系、版本、实名操作人和理由。"
          />
          <Select
            aria-label="裁决方式"
            value={decision}
            style={{ width: "100%" }}
            onChange={(value) => setDecision(value)}
            options={[
              { value: "acknowledge", label: "仅确认问题已人工核实" },
              { value: "link", label: "选择稳定目标建立关联", disabled: !selected?.candidates.length },
            ]}
          />
          {decision === "link" && (
            <Select
              aria-label="稳定关联目标"
              value={candidateIndex}
              style={{ width: "100%" }}
              placeholder="选择系统查到的稳定目标"
              onChange={setCandidateIndex}
              options={(selected?.candidates || []).map((candidate, index) => ({
                value: index,
                label: `${candidate.label || candidate.target_id} · ${candidate.target_type}`,
              }))}
            />
          )}
          <Input.TextArea
            aria-label="裁决理由"
            value={resolutionReason}
            onChange={(event) => setResolutionReason(event.target.value)}
            maxLength={1000}
            placeholder="填写核实依据，不要只写“确认”"
            autoSize={{ minRows: 3, maxRows: 6 }}
          />
        </Space>
      </Modal>
    </div>
  );
}
