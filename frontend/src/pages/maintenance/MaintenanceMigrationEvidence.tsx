import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Empty, Select, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  getMaintenanceMigrationEvidence,
  type MigrationEvidencePage,
  type MigrationEvidenceRow,
  type MigrationEvidenceSection,
  type MigrationProjectPreview,
} from "../../api/maintenanceMigration";

const EVIDENCE_PAGE_SIZE = 20;

const SECTIONS: Array<{
  value: MigrationEvidenceSection;
  label: string;
}> = [
  { value: "warehouse_ambiguities", label: "仓库未决歧义" },
  { value: "truth_quantity_differences", label: "新旧数量差异" },
  { value: "legacy_cost_lines", label: "旧口径备件成本" },
  { value: "legacy_expenses", label: "旧口径已审批报销" },
  { value: "inventory_movements", label: "仓库出入库" },
  { value: "post_cutover_site_issues", label: "切换后现场领用" },
  { value: "historical_site_issues", label: "历史现场领用" },
  { value: "expenses", label: "已审批报销" },
  { value: "opening_balances", label: "库存期初候选" },
];

function errorDetail(error: unknown): string {
  if (typeof error === "object" && error !== null && "response" in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response;
    if (typeof response?.data?.detail === "string") return response.data.detail;
  }
  return "证据明细加载失败，请重试。";
}

function evidenceValue(value: unknown) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") {
    return <Tag color={value ? "green" : "orange"}>{value ? "是" : "否"}</Tag>;
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function column(
  title: string,
  dataIndex: string,
  width?: number,
): ColumnsType<MigrationEvidenceRow>[number] {
  return {
    title,
    dataIndex,
    width,
    render: evidenceValue,
  };
}

function evidenceColumns(section: MigrationEvidenceSection): ColumnsType<MigrationEvidenceRow> {
  if (section === "warehouse_ambiguities") {
    return [
      column("歧义类型", "ambiguity_type", 150),
      column("单据号", "document_no", 140),
      column("单据日期", "document_date", 115),
      column("明细稳定 ID", "line_id", 180),
      column("字段", "field_code", 150),
      column("来源行", "source_row", 90),
      column("候选", "candidates", 280),
      column("影响范围", "scope", 150),
      column("候选项目", "scope_project_ids", 220),
      column("歧义稳定 ID", "ambiguity_id", 220),
      column("证据指纹", "fingerprint", 300),
    ];
  }
  if (section === "legacy_cost_lines") {
    return [
      column("旧需求单稳定 ID", "source_order_id", 180),
      column("旧需求明细稳定 ID", "source_line_id", 190),
      column("需求单号", "order_no", 140),
      column("需求日期", "order_date", 115),
      column("PN", "pn", 130),
      column("SN", "sn", 130),
      column("需求数量", "demand_quantity", 100),
      column("退货数量", "return_quantity", 100),
      column("旧口径有效数量", "effective_quantity", 130),
      column("单位成本（未税）", "unit_cost_ex_tax", 130),
      column("单位成本（含税）", "unit_cost_inc_tax", 130),
      column("税价基准", "cost_tax_basis", 100),
      column("旧口径成本（未税）", "cost_amount_ex_tax", 140),
      column("旧口径成本（含税）", "cost_amount_inc_tax", 140),
    ];
  }
  if (section === "legacy_expenses") {
    return [
      column("报销单号", "expense_ref", 150),
      column("报销日期", "expense_date", 115),
      column("审批状态", "normalized_status", 110),
      column("旧流程状态", "raw_status", 110),
      column("归属合同号", "contract_no", 150),
      column("项目合同关系 ID", "project_contract_id", 190),
      column("合同关系版本", "contract_relation_version", 120),
      column("归属生效起", "contract_effective_from", 115),
      column("归属生效止", "contract_effective_to", 115),
      column("税价基准", "tax_basis", 100),
      column("未税金额", "amount_ex_tax", 120),
      column("含税金额", "amount_inc_tax", 120),
      column("报销稳定 ID", "expense_id", 180),
      column("导入批次", "import_batch_id", 110),
    ];
  }
  if (section === "truth_quantity_differences") {
    return [
      column("单据号", "document_no", 150),
      column("PN", "pn", 130),
      column("SN", "sn", 130),
      column("旧口径数量", "before_quantity", 120),
      column("现场领用数量", "after_quantity", 120),
      column("数量差额", "delta_quantity", 110),
      column("旧需求单稳定 ID", "source_order_id", 180),
      column("旧需求明细稳定 ID", "source_line_id", 190),
      column("对比稳定键", "comparison_key", 250),
    ];
  }
  if (section === "inventory_movements") {
    return [
      column("单据号", "document_no", 140),
      column("单据日期", "document_date", 115),
      column("变动类型", "movement_type", 110),
      column("PN", "pn", 130),
      column("SN", "sn", 130),
      column("数量", "quantity", 90),
      column("库存稳定键", "balance_key", 210),
      column("单据稳定 ID", "document_id", 180),
    ];
  }
  if (section === "historical_site_issues" || section === "post_cutover_site_issues") {
    return [
      column("领用单号", "issue_no", 140),
      column("领用日期", "issue_date", 115),
      column("PN", "pn", 130),
      column("SN", "sn", 130),
      column("数量", "quantity", 90),
      column("审批状态", "workflow_status", 110),
      column("稳定身份", "stable_identity", 100),
      column("归属校验", "link_state", 150),
      column("交付映射版本", "delivery_mapping_version", 150),
      column("成本来源", "cost_source_label", 210),
      column("销售估算", "cost_is_estimate", 100),
      column("关联采购明细", "linked_purchase_line_id", 140),
      column("人工取价证据", "manual_evidence", 220),
      column("未税成本", "cost_amount_ex_tax", 110),
      column("含税成本", "cost_amount_inc_tax", 110),
      column("样本数量", "reference_sample_count", 100),
      column("样本窗口起", "reference_window_from", 115),
      column("样本窗口止", "reference_window_to", 115),
      column("样本单据与数量", "reference_samples", 280),
      column("取价算法", "algorithm_version", 180),
      column("明细稳定 ID", "issue_line_id", 180),
    ];
  }
  if (section === "expenses") {
    return [
      column("报销单号", "expense_ref", 150),
      column("报销日期", "expense_date", 115),
      column("审批状态", "normalized_status", 110),
      column("未税金额", "amount_ex_tax", 110),
      column("含税金额", "amount_inc_tax", 110),
      column("报销稳定 ID", "expense_id", 180),
    ];
  }
  return [
    column("库存稳定键", "balance_key", 220),
    column("PN", "pn", 150),
    column("数量", "quantity", 100),
    column("已批准", "approved", 90),
    column("证据 SHA-256", "evidence_hash", 300),
  ];
}

function rowKey(row: MigrationEvidenceRow): string {
  const stableValue = row.movement_id
    ?? row.ambiguity_id
    ?? row.comparison_key
    ?? row.source_line_id
    ?? row.issue_line_id
    ?? row.expense_id
    ?? row.source_expense_id
    ?? row.expense_ref
    ?? row.balance_key
    ?? row.document_id;
  return String(stableValue ?? JSON.stringify(row));
}

type Props = {
  runId: string;
  projectId: string;
  projectLabel: string;
  preview: MigrationProjectPreview;
};

export default function MaintenanceMigrationEvidence({
  runId,
  projectId,
  projectLabel,
  preview,
}: Props) {
  const defaultSection = useMemo(
    () => SECTIONS.find((item) => (preview.evidence_summary[item.value] ?? 0) > 0)?.value
      ?? "inventory_movements",
    [preview.evidence_summary],
  );
  const [section, setSection] = useState<MigrationEvidenceSection>(defaultSection);
  const [page, setPage] = useState(1);
  const [data, setData] = useState<MigrationEvidencePage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestGeneration = useRef(0);

  useEffect(() => {
    setSection(defaultSection);
    setPage(1);
  }, [defaultSection, projectId, runId]);

  const load = useCallback(async () => {
    const generation = ++requestGeneration.current;
    const count = preview.evidence_summary[section] ?? 0;
    setError(null);
    if (count === 0) {
      setData({
        run_id: runId,
        project_id: projectId,
        section,
        source_snapshot_hash: preview.source_snapshot_hash,
        items: [],
        total: 0,
        page: 1,
        page_size: EVIDENCE_PAGE_SIZE,
      });
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const response = await getMaintenanceMigrationEvidence(runId, projectId, {
        section,
        page,
        page_size: EVIDENCE_PAGE_SIZE,
      });
      if (generation === requestGeneration.current) setData(response.data);
    } catch (loadError) {
      if (generation === requestGeneration.current) {
        setData(null);
        setError(errorDetail(loadError));
      }
    } finally {
      if (generation === requestGeneration.current) setLoading(false);
    }
  }, [page, preview.evidence_summary, preview.source_snapshot_hash, projectId, runId, section]);

  useEffect(() => {
    void load();
    return () => { requestGeneration.current += 1; };
  }, [load]);

  const sectionOptions = SECTIONS.map((item) => ({
    value: item.value,
    label: `${item.label}（${preview.evidence_summary[item.value] ?? 0}）`,
  }));

  return (
    <div className="maintenance-migration-evidence">
      <div className="maintenance-migration-evidence-toolbar">
        <div>
          <Typography.Text strong>逐页来源证据</Typography.Text>
          <Typography.Text type="secondary" className="maintenance-migration-evidence-hint">
            每一页均来自当前锁定快照，不依赖下载文件判断内容。
          </Typography.Text>
        </div>
        <Space wrap>
          <Tag color={preview.source_coverage.warehouse_source_ready ? "green" : "red"}>
            {preview.source_coverage.warehouse_source_ready ? "仓库来源已接入" : "仓库来源未接入"}
          </Tag>
          <Select
            aria-label={`${projectLabel} 证据分区`}
            value={section}
            options={sectionOptions}
            style={{ minWidth: 220 }}
            onChange={(value) => {
              setSection(value);
              setPage(1);
            }}
          />
        </Space>
      </div>
      {error && (
        <Alert
          type="error"
          showIcon
          message={error}
          action={<Button size="small" onClick={() => void load()}>重试</Button>}
          style={{ marginBottom: 12 }}
        />
      )}
      <Table<MigrationEvidenceRow>
        size="small"
        rowKey={rowKey}
        columns={evidenceColumns(section)}
        dataSource={data?.items ?? []}
        loading={loading}
        scroll={{ x: "max-content" }}
        pagination={{
          current: data?.page ?? page,
          pageSize: EVIDENCE_PAGE_SIZE,
          total: data?.total ?? (preview.evidence_summary[section] ?? 0),
          showSizeChanger: false,
          hideOnSinglePage: false,
          showTotal: (count) => `共 ${count} 条锁定证据`,
          onChange: setPage,
        }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该分区没有证据行" /> }}
      />
      <Typography.Text type="secondary" className="maintenance-migration-evidence-snapshot">
        项目快照：<Typography.Text copyable code>{preview.source_snapshot_hash}</Typography.Text>
      </Typography.Text>
    </div>
  );
}
