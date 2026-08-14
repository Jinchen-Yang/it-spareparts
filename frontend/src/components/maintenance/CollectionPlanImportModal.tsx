import { useRef, useState } from "react";
import { Alert, Button, Card, ConfigProvider, Input, Modal, Select, Space, Steps } from "antd";

import {
  applyCollectionPlan,
  previewCollectionPlan,
  searchCollectionPlanBindingOptions,
  type CollectionApplyRequest,
  type CollectionApplyResponse,
  type CollectionBindingOptionProject,
  type CollectionPreviewResponse,
} from "../../api/maintenanceCollectionReminders";
import { COLLECTION_IMPORT, COLLECTION_PAGE } from "./maintenanceLanguage";

/** 每次“解析预览”生成新的幂等键；同一次请求的网络重试复用同一键。 */
function newPreviewKey() {
  return `preview-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

const BINDING_OPTIONS_PAGE_SIZE = 50;

const MILESTONE_CHANGE_LABELS: Record<string, string> = {
  create: COLLECTION_IMPORT.diffCreate,
  update: COLLECTION_IMPORT.diffUpdate,
  unchanged: COLLECTION_IMPORT.diffUnchanged,
  source_missing: COLLECTION_IMPORT.diffSourceMissing,
};

interface BindingChoice {
  project: CollectionBindingOptionProject;
  contract: CollectionBindingOptionProject["contracts"][number] | null;
}

interface BindingOptionsState {
  options: CollectionBindingOptionProject[];
  loading: boolean;
}

interface CollectionPlanImportModalProps {
  open: boolean;
  onClose: () => void;
  onApplied: () => void;
}

function errorDetailMessage(reason: unknown): string | null {
  return (reason as { response?: { data?: { detail?: { message?: string } } } })
    ?.response?.data?.detail?.message ?? null;
}

function errorStatus(reason: unknown): number | null {
  return (reason as { response?: { status?: number } })?.response?.status ?? null;
}

export default function CollectionPlanImportModal({
  open,
  onClose,
  onApplied,
}: CollectionPlanImportModalProps) {
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<CollectionPreviewResponse | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [currentStep, setCurrentStep] = useState(0);
  const [bindings, setBindings] = useState<Record<string, BindingChoice>>({});
  const [bindingOptions, setBindingOptions] = useState<Record<string, BindingOptionsState>>({});
  const [reassignmentReason, setReassignmentReason] = useState("");
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [applyResult, setApplyResult] = useState<CollectionApplyResponse | null>(null);
  const previewKeyRef = useRef<string | null>(null);
  const bindingSearchControllerRef = useRef<AbortController | null>(null);

  const pendingRows = preview?.rows.filter((row) => row.binding.status === "pending_review") ?? [];
  const hasReassignment = pendingRows.some((row) => row.binding.existing_binding_version != null);

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    setFile(event.target.files?.[0] ?? null);
  };

  const runPreview = async () => {
    if (!file || previewing) return;
    if (!previewKeyRef.current) previewKeyRef.current = newPreviewKey();
    setPreviewing(true);
    setPreviewError(null);
    try {
      const { data } = await previewCollectionPlan(file, previewKeyRef.current);
      setPreview(data);
      setBindings({});
      setBindingOptions({});
      setReassignmentReason("");
      setApplyError(null);
      setApplyResult(null);
      setCurrentStep(1);
    } catch (reason_) {
      const status = errorStatus(reason_);
      setPreviewError(
        status === 409
          ? COLLECTION_IMPORT.versionConflict
          : status === 403
            ? COLLECTION_IMPORT.permissionDenied
            : errorDetailMessage(reason_) || COLLECTION_IMPORT.previewFailed,
      );
    } finally {
      setPreviewing(false);
    }
  };

  const handleRepreview = () => {
    previewKeyRef.current = newPreviewKey();
    void runPreview();
  };

  const handleBindingSearch = (rowKey: string, value: string) => {
    const query = value.trim();
    if (query.length < 2) return;
    bindingSearchControllerRef.current?.abort();
    const controller = new AbortController();
    bindingSearchControllerRef.current = controller;
    setBindingOptions((current) => ({ ...current, [rowKey]: { options: [], loading: true } }));
    void searchCollectionPlanBindingOptions(
      preview?.batch_id ?? "",
      { q: query, page: 1, page_size: BINDING_OPTIONS_PAGE_SIZE },
      { signal: controller.signal },
    )
      .then(({ data }) => {
        if (controller.signal.aborted) return;
        setBindingOptions((current) => ({ ...current, [rowKey]: { options: data.rows, loading: false } }));
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setBindingOptions((current) => ({ ...current, [rowKey]: { options: [], loading: false } }));
      });
  };

  const selectBindingProject = (rowKey: string, projectId: string) => {
    const project = bindingOptions[rowKey]?.options.find((p) => p.project_id === projectId);
    if (!project) return;
    setBindings((current) => ({ ...current, [rowKey]: { project, contract: null } }));
  };

  const selectBindingContract = (rowKey: string, contractId: string) => {
    const project = bindings[rowKey]?.project;
    const contract = project?.contracts.find((c) => c.project_contract_id === contractId);
    if (!project || !contract) return;
    setBindings((current) => ({ ...current, [rowKey]: { project, contract } }));
  };

  const allPendingBound = pendingRows.every((row) => {
    const choice = bindings[row.row_key];
    return Boolean(choice?.project && choice?.contract);
  });
  const reassignmentReasonOk = !hasReassignment || reassignmentReason.trim().length > 0;
  const canGoToBindingsReview = Boolean(preview?.can_apply);
  const canGoToApply = allPendingBound && reassignmentReasonOk;

  const handleApply = async () => {
    if (!preview || !canGoToApply || applying) return;
    const body: CollectionApplyRequest = {
      expected_batch_version: preview.batch_version,
      expected_data_version: preview.data_version,
      bindings: pendingRows.map((row) => {
        const choice = bindings[row.row_key];
        if (!choice?.contract) {
          throw new Error("missing reviewed collection binding");
        }
        return {
          row_key: row.row_key,
          external_order_no: row.external_order_no,
          project_id: choice.project.project_id,
          project_version: choice.project.version,
          project_contract_id: choice.contract.project_contract_id,
          project_contract_version: choice.contract.version,
          existing_binding_version: row.binding.existing_binding_version,
          reason: row.binding.existing_binding_version != null
            ? (reassignmentReason.trim() || null)
            : null,
        };
      }),
    };
    setApplying(true);
    setApplyError(null);
    try {
      const { data } = await applyCollectionPlan(preview.batch_id, body);
      setApplyResult(data);
    } catch (reason_) {
      const status = errorStatus(reason_);
      setApplyError(
        status === 409
          ? COLLECTION_IMPORT.versionConflict
          : status === 403
            ? COLLECTION_IMPORT.permissionDenied
            : errorDetailMessage(reason_) || COLLECTION_IMPORT.applyFailed,
      );
    } finally {
      setApplying(false);
    }
  };

  const steps = [
    { title: COLLECTION_IMPORT.stepSelectFile },
    { title: COLLECTION_IMPORT.stepPreview },
    { title: COLLECTION_IMPORT.stepReviewBindings },
    { title: COLLECTION_IMPORT.stepApply },
  ];

  return (
    <ConfigProvider button={{ autoInsertSpace: false }}>
      <Modal
        open={open}
        title={COLLECTION_IMPORT.title}
        width={760}
        destroyOnHidden
        onCancel={onClose}
        footer={null}
      >
        <Steps current={currentStep} size="small" items={steps} style={{ marginBottom: 16 }} />

      {currentStep === 0 && (
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Input
            type="file"
            accept=".xls"
            aria-label={COLLECTION_IMPORT.filePickLabel}
            onChange={handleFileChange}
          />
          <div style={{ color: "var(--mb-text-2)", fontSize: 13 }}>{COLLECTION_IMPORT.fileHint}</div>
          {file && (
            <div style={{ color: "var(--mb-text-1)", fontSize: 13 }}>
              {COLLECTION_IMPORT.filePicked(file.name, file.size)}
            </div>
          )}
          {previewError && <Alert type="error" showIcon message={previewError} />}
          <Button
            type="primary"
            disabled={!file || previewing}
            loading={previewing}
            onClick={() => void runPreview()}
          >
            {COLLECTION_IMPORT.previewAction}
          </Button>
        </Space>
      )}

      {currentStep === 1 && preview && (
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Alert type="info" showIcon message={COLLECTION_IMPORT.previewZeroWriteHint} />
          {previewError && <Alert type="error" showIcon message={previewError} />}
          <Space wrap size={12}>
            <span>{COLLECTION_IMPORT.countProjects} {preview.counts.projects}</span>
            <span>{COLLECTION_IMPORT.countMilestones} {preview.counts.milestones}</span>
            <span>{COLLECTION_IMPORT.countBound} {preview.counts.bound}</span>
            <span>{COLLECTION_IMPORT.countPendingBinding} {preview.counts.pending_binding}</span>
            <span>{COLLECTION_IMPORT.countBlockers} {preview.counts.blockers}</span>
            <span>{COLLECTION_IMPORT.countWarnings} {preview.counts.warnings}</span>
          </Space>
          {preview.counts.blockers > 0 && (
            <Alert type="error" showIcon message={COLLECTION_IMPORT.blockerHint} />
          )}
          {preview.rows.map((row) => (
            <Card
              key={row.row_key}
              size="small"
              title={(
                <span>
                  <span className="mcr-order-no">{row.external_order_no}</span>
                  <span> · {row.source_project_name ?? "—"}</span>
                </span>
              )}
            >
              <Space direction="vertical" size={4} style={{ width: "100%" }}>
                {row.milestone_diffs.map((diff) => (
                  <div key={diff.sequence} style={{ fontSize: 13 }}>
                    {COLLECTION_PAGE.sequenceOf(diff.sequence)}
                    {" · "}
                    {diff.planned_month ?? "—"}
                    {" · "}
                    {diff.planned_amount ?? "—"}
                    {" · "}
                    {MILESTONE_CHANGE_LABELS[diff.change] ?? diff.change}
                  </div>
                ))}
                {row.warning_codes.map((code) => (
                  <div key={code} style={{ fontSize: 13, color: "#d48806" }}>{code}</div>
                ))}
                {row.blocker_codes.map((code) => (
                  <div key={code} style={{ fontSize: 13, color: "#cf1322" }}>{code}</div>
                ))}
              </Space>
            </Card>
          ))}
          {preview.issues.map((issue) => (
            <div key={`${issue.code}-${issue.row_key ?? ""}-${issue.sequence ?? ""}`} style={{ fontSize: 13 }}>
              {issue.message}
            </div>
          ))}
          <Space>
            <Button onClick={() => setCurrentStep(0)}>{COLLECTION_IMPORT.prevStep}</Button>
            <Button onClick={handleRepreview}>{COLLECTION_IMPORT.repreview}</Button>
            <Button
              type="primary"
              disabled={!canGoToBindingsReview}
              onClick={() => setCurrentStep(2)}
            >
              {COLLECTION_IMPORT.nextStep}
            </Button>
          </Space>
        </Space>
      )}

      {currentStep === 2 && preview && (
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          {previewError && <Alert type="error" showIcon message={previewError} />}
          {pendingRows.map((row) => (
            <div key={row.row_key}>
              <div style={{ fontWeight: 600, marginBottom: 6 }}>
                <span className="mcr-order-no">{row.external_order_no}</span>
                <span> · {row.source_project_name ?? "—"}</span>
              </div>
              <Space wrap>
                <span>
                  <label htmlFor={`mcr-binding-project-${row.row_key}`} style={{ marginRight: 8 }}>
                    {COLLECTION_IMPORT.bindingProjectLabel} {row.external_order_no}
                  </label>
                  <Select
                    id={`mcr-binding-project-${row.row_key}`}
                    placeholder={COLLECTION_IMPORT.bindingSearchPlaceholder}
                    style={{ width: 280 }}
                    showSearch
                    filterOption={false}
                    virtual={false}
                    loading={bindingOptions[row.row_key]?.loading}
                    options={(bindingOptions[row.row_key]?.options ?? []).map((p) => ({
                      label: `${p.project_code} · ${p.display_name}`,
                      value: p.project_id,
                    }))}
                    value={bindings[row.row_key]?.project.project_id}
                    onSearch={(value) => handleBindingSearch(row.row_key, value)}
                    onSelect={(projectId) => selectBindingProject(row.row_key, projectId)}
                  />
                </span>
                <span>
                  <label htmlFor={`mcr-binding-contract-${row.row_key}`} style={{ marginRight: 8 }}>
                    {COLLECTION_IMPORT.bindingContractLabel} {row.external_order_no}
                  </label>
                  <Select
                    id={`mcr-binding-contract-${row.row_key}`}
                    placeholder={COLLECTION_IMPORT.bindingContractPlaceholder}
                    style={{ width: 220 }}
                    virtual={false}
                    options={(bindings[row.row_key]?.project.contracts ?? []).map((c) => ({
                      label: c.contract_no ?? c.project_contract_id,
                      value: c.project_contract_id,
                    }))}
                    value={bindings[row.row_key]?.contract?.project_contract_id}
                    onSelect={(contractId) => selectBindingContract(row.row_key, contractId)}
                  />
                </span>
              </Space>
            </div>
          ))}
          {hasReassignment && (
            <div>
              <Input
                aria-label={COLLECTION_IMPORT.bindingReasonLabel}
                value={reassignmentReason}
                maxLength={1000}
                placeholder={COLLECTION_IMPORT.bindingReasonLabel}
                onChange={(event) => setReassignmentReason(event.target.value)}
              />
              {!reassignmentReasonOk && (
                <div style={{ color: "#cf1322", fontSize: 13, marginTop: 4 }}>
                  {COLLECTION_IMPORT.bindingReasonRequired}
                </div>
              )}
            </div>
          )}
          <Space>
            <Button onClick={() => setCurrentStep(1)}>{COLLECTION_IMPORT.prevStep}</Button>
            <Button type="primary" disabled={!canGoToApply} onClick={() => setCurrentStep(3)}>
              {COLLECTION_IMPORT.nextStep}
            </Button>
          </Space>
        </Space>
      )}

      {currentStep === 3 && preview && applyResult && (
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <h3 style={{ margin: 0 }}>{COLLECTION_IMPORT.applyResultTitle}</h3>
          <div>{COLLECTION_IMPORT.countCreated} {applyResult.counts.created}</div>
          <div>{COLLECTION_IMPORT.countUpdated} {applyResult.counts.updated}</div>
          <div>{COLLECTION_IMPORT.countUnchanged} {applyResult.counts.unchanged}</div>
          <div>{COLLECTION_IMPORT.countSourceMissing} {applyResult.counts.source_missing}</div>
          <div>{COLLECTION_IMPORT.countNeedsReview} {applyResult.counts.needs_review}</div>
          <Button
            type="primary"
            onClick={() => {
              onApplied();
              onClose();
            }}
          >
            {COLLECTION_IMPORT.complete}
          </Button>
        </Space>
      )}

      {currentStep === 3 && preview && !applyResult && (
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          {pendingRows.map((row) => {
            const choice = bindings[row.row_key];
            return (
              <div key={row.row_key} style={{ fontSize: 13 }}>
                <span className="mcr-order-no">{row.external_order_no}</span>
                {" → "}
                {choice?.contract ? `${choice.project.display_name} / ${choice.contract.contract_no ?? choice.contract.project_contract_id}` : "—"}
              </div>
            );
          })}
          {applyError && <Alert type="error" showIcon message={applyError} />}
          <Space>
            <Button onClick={() => setCurrentStep(2)}>{COLLECTION_IMPORT.prevStep}</Button>
            <Button type="primary" loading={applying} onClick={() => void handleApply()}>
              {COLLECTION_IMPORT.apply}
            </Button>
          </Space>
        </Space>
      )}
      </Modal>
    </ConfigProvider>
  );
}
