import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Table,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { Link, useSearchParams } from "react-router-dom";

import {
  listMaintenanceCostGaps,
  listMaintenanceProjectOperations,
  recomputeMaintenanceCostGaps,
  updateMaintenanceCostGap,
  type MaintenanceCostGap,
  type MaintenanceCostGapRecomputeResult,
  type MaintenanceCostReference,
  type MaintenanceProjectOperationsSummary,
} from "../../api/maintenanceOperations";
import PageHeader from "../../components/PageHeader";
import { readMaintenanceCapabilities } from "../../components/maintenance/maintenancePermissions";
import { money, moneyExact, splitFixed } from "../../utils/format";

const SOURCE_LABELS: Record<string, string> = {
  direct_purchase: "关联采购",
  linked_purchase: "关联采购",
  purchase_window: "采购 ±7 天加权",
  sales_window: "销售 ±7 天加权",
  manual: "人工回填",
};

function referenceLabel(reference: MaintenanceCostReference): string {
  return SOURCE_LABELS[reference.source] || reference.source;
}

function referenceEvidence(reference: MaintenanceCostReference): string {
  return [
    referenceLabel(reference),
    reference.document_no ? `单据 ${reference.document_no}` : null,
    reference.document_date ? `日期 ${reference.document_date}` : null,
    reference.distance_days === null ? null : `距领用日 ${reference.distance_days} 天`,
    reference.sample_lines ? `${reference.sample_lines} 个样本` : null,
    reference.weighted_unit_price === null
      ? null : `加权未税单价 ${reference.weighted_unit_price}`,
  ].filter(Boolean).join("；");
}

function ReferenceList({ references, selectable, onSelect }: {
  references: MaintenanceCostReference[];
  selectable?: boolean;
  onSelect?: (reference: MaintenanceCostReference) => void;
}) {
  if (references.length === 0) return <Tag color="orange">无可用参考，需人工核实后留痕</Tag>;
  return (
    <Space direction="vertical" size={5} style={{ width: "100%" }}>
      {references.map((reference, index) => (
        <div key={`${reference.source}-${reference.document_no || index}`}>
          <Space size={6} wrap>
            <Tag color={reference.source === "direct_purchase" || reference.source === "linked_purchase"
              ? "green" : "blue"}>
              {referenceLabel(reference)}
            </Tag>
            <span>{reference.document_no || "无单据号"}</span>
            <span>{money(reference.weighted_unit_price)}</span>
            {reference.distance_days !== null && <span>{`${reference.distance_days} 天`}</span>}
            <span style={{ color: "var(--mb-text-3)", fontSize: 12 }}>
              {`${reference.sample_lines} 个样本`}
            </span>
            {selectable && reference.weighted_unit_price !== null && (
              <Button
                size="small"
                aria-label={`采用${referenceLabel(reference)}参考`}
                onClick={() => onSelect?.(reference)}
              >
                采用该参考
              </Button>
            )}
          </Space>
        </div>
      ))}
    </Space>
  );
}

export default function MaintenanceCostRefillPage({ projectId }: { projectId?: string }) {
  const [{ canManageProject }] = useState(readMaintenanceCapabilities);
  const [searchParams] = useSearchParams();
  const initialProjectId = projectId || searchParams.get("project_id") || "";
  const [selectedProjectId, setSelectedProjectId] = useState(initialProjectId);
  const [projects, setProjects] = useState<MaintenanceProjectOperationsSummary[]>([]);
  const [rows, setRows] = useState<MaintenanceCostGap[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [loadingProjects, setLoadingProjects] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [editing, setEditing] = useState<MaintenanceCostGap | null>(null);
  const [unitCost, setUnitCost] = useState<number | null>(null);
  const [evidence, setEvidence] = useState("");
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [refreshingConflict, setRefreshingConflict] = useState(false);
  const [conflictBlocked, setConflictBlocked] = useState(false);
  const [formError, setFormError] = useState("");
  const [success, setSuccess] = useState("");
  const [recomputing, setRecomputing] = useState(false);
  const [recomputeError, setRecomputeError] = useState("");
  const loadGeneration = useRef(0);
  const projectListGeneration = useRef(0);
  const conflictGeneration = useRef(0);
  const selectedProjectIdRef = useRef(selectedProjectId);
  const editingRef = useRef<MaintenanceCostGap | null>(editing);
  const recomputeGeneration = useRef(0);
  const skipNextPageLoad = useRef(false);
  selectedProjectIdRef.current = selectedProjectId;
  editingRef.current = editing;

  const loadProjectOptions = useCallback(async (q = "") => {
    if (!canManageProject || projectId) return;
    const generation = ++projectListGeneration.current;
    setLoadingProjects(true);
    try {
      const { data } = await listMaintenanceProjectOperations({
        page: 1,
        page_size: 200,
        q: q || undefined,
      });
      if (generation === projectListGeneration.current) setProjects(data.rows);
    } catch {
      if (generation === projectListGeneration.current) setProjects([]);
    } finally {
      if (generation === projectListGeneration.current) setLoadingProjects(false);
    }
  }, [canManageProject, projectId]);

  useEffect(() => {
    void loadProjectOptions();
    return () => { projectListGeneration.current += 1; };
  }, [loadProjectOptions]);

  const load = useCallback(async (targetPage = page): Promise<boolean> => {
    if (!canManageProject || !selectedProjectId) {
      loadGeneration.current += 1;
      setRows([]);
      setTotal(0);
      return true;
    }
    const generation = ++loadGeneration.current;
    setLoading(true);
    setLoadError(false);
    try {
      const { data } = await listMaintenanceCostGaps(selectedProjectId, {
        page: targetPage,
        page_size: 20,
      });
      if (generation !== loadGeneration.current) return false;
      setRows(data.rows);
      setTotal(data.total);
      return true;
    } catch {
      if (generation !== loadGeneration.current) return false;
      setRows([]);
      setTotal(0);
      setLoadError(true);
      return false;
    } finally {
      if (generation === loadGeneration.current) setLoading(false);
    }
  }, [canManageProject, page, selectedProjectId]);

  useEffect(() => {
    if (skipNextPageLoad.current) {
      skipNextPageLoad.current = false;
      return () => { loadGeneration.current += 1; };
    }
    void load();
    return () => { loadGeneration.current += 1; };
  }, [load]);

  useEffect(() => () => { recomputeGeneration.current += 1; }, []);

  if (!canManageProject) {
    return (
      <Space direction="vertical" size="large" style={{ width: "100%" }}>
        <PageHeader title="缺失成本人工回填" />
        <Alert
          showIcon
          type="warning"
          message="无人工成本回填权限"
          description="请联系管理员授予维保项目管理权限。"
        />
      </Space>
    );
  }

  const openRefill = (gap: MaintenanceCostGap) => {
    conflictGeneration.current += 1;
    editingRef.current = gap;
    setEditing(gap);
    setUnitCost(gap.current_unit_cost);
    setEvidence("");
    setReason("");
    setFormError("");
    setConflictBlocked(false);
  };

  const closeRefill = () => {
    conflictGeneration.current += 1;
    editingRef.current = null;
    setEditing(null);
    setRefreshingConflict(false);
    setConflictBlocked(false);
  };

  const chooseReference = (reference: MaintenanceCostReference) => {
    setUnitCost(reference.weighted_unit_price);
    setEvidence(referenceEvidence(reference));
    setFormError("");
  };

  const refreshLatestGap = async (staleGap: MaintenanceCostGap) => {
    const request = ++conflictGeneration.current;
    setRefreshingConflict(true);
    setConflictBlocked(true);
    try {
      const { data } = await listMaintenanceCostGaps(staleGap.project_id, {
        page,
        page_size: 20,
      });
      const currentEditing = editingRef.current;
      if (
        request !== conflictGeneration.current
        || selectedProjectIdRef.current !== staleGap.project_id
        || !currentEditing
        || currentEditing.project_id !== staleGap.project_id
        || currentEditing.line_id !== staleGap.line_id
      ) return false;
      setRows(data.rows);
      setTotal(data.total);
      const latestGap = data.rows.find((row) => row.line_id === staleGap.line_id);
      if (!latestGap) {
        setFormError("该行已不在待回填清单中；草稿已保留，请返回项目核对最新状态。");
        return false;
      }
      editingRef.current = latestGap;
      setEditing(latestGap);
      setConflictBlocked(false);
      setFormError("已刷新到最新版本；当前草稿已保留，请核对后重新保存。");
      return true;
    } catch {
      if (request === conflictGeneration.current) {
        setFormError("最新版本加载失败；草稿已保留，请重新获取后再保存。");
      }
      return false;
    } finally {
      if (request === conflictGeneration.current) setRefreshingConflict(false);
    }
  };

  const save = async () => {
    if (!editing || unitCost === null || unitCost < 0 || !evidence.trim() || !reason.trim()) {
      setFormError("请填写有效未税单位成本、证据和回填原因。成本不会由系统猜测。");
      return;
    }
    setSaving(true);
    setFormError("");
    try {
      const { data } = await updateMaintenanceCostGap(editing.project_id, {
        line_id: editing.line_id,
        version: editing.version,
        unit_cost_ex_tax: unitCost,
        evidence: evidence.trim(),
        reason: reason.trim(),
      });
      conflictGeneration.current += 1;
      editingRef.current = null;
      setEditing(null);
      setConflictBlocked(false);
      setSuccess(data.resolution === "automatic_evidence"
        ? "保存时发现新的系统价格，已采用系统证据并刷新清单。"
        : "成本已回填");
      await load();
    } catch (error) {
      if ((error as { response?: { status?: number } }).response?.status === 409) {
        await refreshLatestGap(editing);
      } else {
        setFormError("保存失败，当前草稿已保留，请稍后重试。");
      }
    } finally {
      setSaving(false);
    }
  };

  const recompute = async () => {
    const targetProjectId = selectedProjectId;
    if (!targetProjectId) return;
    const request = ++recomputeGeneration.current;
    setRecomputing(true);
    setRecomputeError("");
    setSuccess("");
    let result: MaintenanceCostGapRecomputeResult;
    try {
      const response = await recomputeMaintenanceCostGaps(targetProjectId, {
        reason: "重新匹配后到采购或销售价格证据",
      });
      result = response.data;
    } catch {
      if (
        request === recomputeGeneration.current
        && selectedProjectIdRef.current === targetProjectId
      ) {
        setRecomputeError("系统价格重新匹配失败，缺价数据未改动，请稍后重试。");
        setRecomputing(false);
      }
      return;
    }
    if (
      request !== recomputeGeneration.current
      || selectedProjectIdRef.current !== targetProjectId
    ) return;
    setSuccess(result.resolved > 0
      ? `已更新 ${result.resolved} 行系统价格，仍有 ${result.remaining} 行缺价。`
      : `没有发现新的系统价格，仍有 ${result.remaining} 行缺价。`);
    const refreshed = await load(1);
    if (
      page !== 1
      && request === recomputeGeneration.current
      && selectedProjectIdRef.current === targetProjectId
    ) {
      skipNextPageLoad.current = true;
      setPage(1);
    }
    if (
      !refreshed
      && request === recomputeGeneration.current
      && selectedProjectIdRef.current === targetProjectId
    ) {
      setRecomputeError("系统价格已更新，但缺价清单刷新失败，请手动重试。");
    }
    if (request === recomputeGeneration.current) setRecomputing(false);
  };

  const columns: ColumnsType<MaintenanceCostGap> = [
    { title: "领用日期", dataIndex: "order_date", width: 110, render: (value) => value || "—" },
    { title: "现场领用单", dataIndex: "order_no", width: 140 },
    { title: "合同", dataIndex: "contract_no", width: 130, render: (value) => value || "—" },
    { title: "PN", dataIndex: "pn", width: 150, render: (value) => value || "—" },
    { title: "描述", dataIndex: "description", width: 170, render: (value) => value || "—" },
    { title: "数量", dataIndex: "quantity", width: 80, align: "right" },
    {
      title: "可核对价格证据",
      dataIndex: "references",
      width: 360,
      render: (references: MaintenanceCostReference[]) => <ReferenceList references={references} />,
    },
    {
      title: "操作",
      width: 90,
      fixed: "right",
      render: (_, gap) => (
        <Button
          type="link"
          aria-label={`回填 ${gap.pn || gap.order_no}`}
          onClick={() => openRefill(gap)}
        >
          回填
        </Button>
      ),
    },
  ];
  const fallbackProject = rows[0] && rows[0].project_id === selectedProjectId
    ? {
      project_id: rows[0].project_id,
      project_code: rows[0].project_code,
      display_name: "当前项目",
    }
    : null;
  const projectOptions = fallbackProject
    && !projects.some((project) => project.project_id === fallbackProject.project_id)
    ? [fallbackProject, ...projects]
    : projects;
  const incTaxUnitCostPreview = splitFixed(unitCost, "ex").inc;
  const incTaxUnitCostPreviewText = incTaxUnitCostPreview === null
    ? moneyExact(null)
    : `¥${incTaxUnitCostPreview.toLocaleString("zh-CN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title="缺失成本人工回填"
        subtitle="只列出系统无法自动取价的现场领用行；先核对关联采购，再看前后 7 天采购和销售加权参考。"
        extra={selectedProjectId ? (
          <Link to={`/maintenance/beta/projects/${encodeURIComponent(selectedProjectId)}`}>
            返回项目
          </Link>
        ) : undefined}
      />
      <Card>
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          {!projectId && (
            <Select
              showSearch
              loading={loadingProjects}
              style={{ width: "min(100%, 420px)" }}
              placeholder="选择维保项目"
              value={selectedProjectId || undefined}
              filterOption={false}
              options={projectOptions.map((project) => ({
                value: project.project_id,
                label: `${project.project_code} · ${project.display_name}`,
              }))}
              onSearch={(value) => void loadProjectOptions(value.trim())}
              onOpenChange={(open) => {
                if (open && projects.length === 0) void loadProjectOptions();
              }}
              onChange={(value) => {
                conflictGeneration.current += 1;
                selectedProjectIdRef.current = value;
                editingRef.current = null;
                setEditing(null);
                setRefreshingConflict(false);
                setConflictBlocked(false);
                setFormError("");
                recomputeGeneration.current += 1;
                setRecomputing(false);
                setRecomputeError("");
                setSuccess("");
                setSelectedProjectId(value);
                setPage(1);
              }}
            />
          )}
          <Alert
            showIcon
            type="info"
            message="采购或销售数据可能晚于领用到达；可先重新匹配系统价格，仍无可靠证据的行再人工回填。"
          />
          <Button
            style={{ alignSelf: "flex-start" }}
            loading={recomputing}
            disabled={!selectedProjectId || loading}
            onClick={() => void recompute()}
          >
            重新匹配系统价格
          </Button>
          {success && <Alert showIcon closable type="success" message={success} />}
          {recomputeError && <Alert showIcon type="error" message={recomputeError} />}
          {loadError && (
            <Alert
              showIcon
              type="error"
              message="缺失成本清单加载失败"
              action={<Button size="small" danger onClick={() => void load()}>重试</Button>}
            />
          )}
          {!selectedProjectId ? (
            <Empty description="请先选择项目" />
          ) : (
            <Table
              rowKey="line_id"
              loading={loading}
              columns={columns}
              dataSource={rows}
              scroll={{ x: 1240 }}
              locale={{ emptyText: "当前项目没有待回填成本" }}
              pagination={{
                current: page,
                pageSize: 20,
                total,
                showSizeChanger: false,
                onChange: setPage,
              }}
            />
          )}
        </Space>
      </Card>

      <Modal
        title="回填成本"
        open={Boolean(editing)}
        onCancel={() => !saving && closeRefill()}
        footer={null}
        destroyOnHidden
      >
        {editing && (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Alert
              type="warning"
              showIcon
              message={`${editing.order_no} · ${editing.pn || "无 PN"} · 数量 ${editing.quantity ?? "—"}`}
            />
            <ReferenceList references={editing.references} selectable onSelect={chooseReference} />
            <label>
              未税单位成本
              <InputNumber
                aria-label="未税单位成本"
                min={0}
                precision={6}
                style={{ width: "100%", marginTop: 5 }}
                value={unitCost}
                onChange={(value) => setUnitCost(value)}
              />
            </label>
            <div
              data-testid="inc-tax-unit-cost-preview"
              style={{
                borderRadius: 8,
                background: "var(--mb-inset)",
                color: "var(--mb-text-2)",
                padding: "9px 11px",
                fontSize: 12.5,
              }}
            >
              {`按 13% 增值税和 HALF_UP 换算，含税单位成本预览：${incTaxUnitCostPreviewText}`}
            </div>
            <label>
              价格证据
              <Input.TextArea
                aria-label="价格证据"
                rows={2}
                style={{ marginTop: 5 }}
                value={evidence}
                onChange={(event) => setEvidence(event.target.value)}
              />
            </label>
            <label>
              回填原因
              <Input.TextArea
                aria-label="回填原因"
                rows={2}
                style={{ marginTop: 5 }}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
            {formError && <Alert showIcon type="error" message={formError} />}
            <Space>
              <Button
                type="primary"
                loading={saving}
                disabled={conflictBlocked}
                onClick={() => void save()}
              >
                保存成本
              </Button>
              {conflictBlocked && (
                <Button
                  loading={refreshingConflict}
                  onClick={() => editing && void refreshLatestGap(editing)}
                >
                  重新获取最新版本
                </Button>
              )}
              <Button disabled={saving} onClick={closeRefill}>取消</Button>
            </Space>
          </Space>
        )}
      </Modal>
    </Space>
  );
}
