import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Divider,
  Drawer,
  Empty,
  Input,
  Modal,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Timeline,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import PageHeader from "../../components/PageHeader";
import {
  approveMaintenanceMigrationRun,
  getMaintenanceMigrationRun,
  previewMaintenanceMigration,
  reconcileMaintenanceMigrationRun,
  searchMaintenanceMigrationRuns,
  type MigrationProjectInput,
  type MigrationProjectSignoff,
  type MigrationRunDetail,
  type MigrationRunStatus,
  type MigrationRunSummary,
} from "../../api/maintenanceMigration";
import {
  listMaintenanceProjects,
  type MaintenanceProject,
} from "../../api/maintenanceProjects";
import MaintenanceMigrationEvidence from "./MaintenanceMigrationEvidence";
import "./maintenanceMigration.css";


type DraftOpening = {
  balanceKey: string;
  pn: string;
  quantity: string;
  evidenceHash: string;
};

type DraftProject = {
  localId: string;
  projectId: string;
  cutoverDate: string;
  historicalMode: "approved_cost_baseline" | "stable_site_issues";
  baselineExTax: string;
  baselineIncTax: string;
  baselineEvidenceHash: string;
  openings: DraftOpening[];
};

type CommandMode = "reconcile" | "approve" | null;

type ProjectReviewDraft = {
  acknowledged: boolean;
  reason: string;
  baselineSelected: boolean;
  openingBalanceIds: string[];
};

const PAGE_SIZE = 20;
const SHA256 = /^[a-f0-9]{64}$/;

function operationKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `migration-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function newOpening(): DraftOpening {
  return { balanceKey: "", pn: "", quantity: "", evidenceHash: "" };
}

function newDraftProject(): DraftProject {
  return {
    localId: operationKey(),
    projectId: "",
    cutoverDate: today(),
    historicalMode: "approved_cost_baseline",
    baselineExTax: "",
    baselineIncTax: "",
    baselineEvidenceHash: "",
    openings: [newOpening()],
  };
}

function errorDetail(error: unknown, fallback: string): string {
  if (typeof error === "object" && error !== null && "response" in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response;
    if (typeof response?.data?.detail === "string") return response.data.detail;
    if (Array.isArray(response?.data?.detail)) {
      return response.data.detail
        .map((item) => (typeof item?.msg === "string" ? item.msg : "输入无效"))
        .join("；");
    }
  }
  return fallback;
}

function money(value: string): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return value || "—";
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: "CNY",
    minimumFractionDigits: 2,
  }).format(number);
}

function statusTag(status: MigrationRunStatus) {
  if (status === "approved") return <Tag color="green">已独立审批</Tag>;
  if (status === "reconciled") return <Tag color="blue">已实名对账</Tag>;
  return <Tag color="orange">待对账</Tag>;
}

function projectInput(draft: DraftProject): MigrationProjectInput {
  const openings = draft.openings
    .filter((row) => row.balanceKey.trim() || row.quantity.trim() || row.evidenceHash.trim())
    .map((row) => ({
      balance_key: row.balanceKey.trim(),
      pn: row.pn.trim() || null,
      quantity: row.quantity.trim(),
      evidence_hash: row.evidenceHash.trim().toLowerCase(),
    }));
  return {
    project_id: draft.projectId,
    cutover_date: draft.cutoverDate,
    historical_mode: draft.historicalMode,
    historical_baseline: draft.historicalMode === "approved_cost_baseline"
      ? {
          amount_ex_tax: draft.baselineExTax.trim(),
          amount_inc_tax: draft.baselineIncTax.trim(),
          evidence_hash: draft.baselineEvidenceHash.trim().toLowerCase(),
        }
      : null,
    opening_balances: openings,
  };
}

function validateDrafts(drafts: DraftProject[], reason: string): string | null {
  if (!reason.trim()) return "请填写生成本次 dry-run 的业务理由。";
  const projectIds = drafts.map((draft) => draft.projectId).filter(Boolean);
  if (projectIds.length !== drafts.length) return "每张卡片都必须选择稳定项目。";
  if (new Set(projectIds).size !== projectIds.length) return "同一项目不能重复加入一次 dry-run。";
  for (const draft of drafts) {
    if (!draft.cutoverDate) return "每个项目都必须填写切换日期。";
    if (draft.historicalMode === "approved_cost_baseline") {
      if (!draft.baselineExTax.trim() || !draft.baselineIncTax.trim()) {
        return "成本基线模式必须填写未税和含税金额；零金额请明确填 0。";
      }
      if (!SHA256.test(draft.baselineEvidenceHash.trim().toLowerCase())) {
        return "成本基线证据必须填写 64 位 SHA-256。";
      }
    }
    const nonEmptyOpenings = draft.openings.filter((row) => (
      row.balanceKey.trim() || row.quantity.trim() || row.evidenceHash.trim()
    ));
    for (const row of nonEmptyOpenings) {
      if (!row.balanceKey.trim() || !row.quantity.trim()) {
        return "库存期初行一旦填写，就必须同时填写稳定键和数量。";
      }
      if (!SHA256.test(row.evidenceHash.trim().toLowerCase())) {
        return "每条库存期初证据必须填写 64 位 SHA-256。";
      }
    }
  }
  return null;
}

export default function MaintenanceMigrationPage() {
  const [runs, setRuns] = useState<MigrationRunSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [statuses, setStatuses] = useState<MigrationRunStatus[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [projects, setProjects] = useState<MaintenanceProject[]>([]);
  const [createOpen, setCreateOpen] = useState(false);
  const [drafts, setDrafts] = useState<DraftProject[]>([newDraftProject()]);
  const [previewReason, setPreviewReason] = useState("");
  const [previewKey, setPreviewKey] = useState<string | null>(null);
  const [previewSaving, setPreviewSaving] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [selected, setSelected] = useState<MigrationRunDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [commandMode, setCommandMode] = useState<CommandMode>(null);
  const [commandReason, setCommandReason] = useState("");
  const [commandKey, setCommandKey] = useState<string | null>(null);
  const [commandSaving, setCommandSaving] = useState(false);
  const [commandError, setCommandError] = useState<string | null>(null);
  const [reviewDrafts, setReviewDrafts] = useState<Record<string, ProjectReviewDraft>>({});
  const [activeEvidenceProjectId, setActiveEvidenceProjectId] = useState<string | null>(null);
  const loadGeneration = useRef(0);

  const reviewIdentity = useMemo(() => {
    if (!selected || selected.status !== "previewed") return "";
    return [
      selected.run_id,
      selected.version,
      ...selected.plans.flatMap((plan) => [
        plan.project_id,
        plan.version,
        plan.historical_baseline?.baseline_id ?? "no-baseline",
        plan.historical_baseline?.version ?? 0,
        ...plan.opening_balances.flatMap((row) => [row.opening_balance_id, row.version]),
      ]),
    ].join(":");
  }, [selected]);

  useEffect(() => {
    if (!selected || !reviewIdentity) {
      setReviewDrafts({});
      return;
    }
    setReviewDrafts(Object.fromEntries(selected.plans.map((plan) => [
      plan.project_id,
      {
        acknowledged: false,
        reason: "",
        baselineSelected: false,
        openingBalanceIds: [],
      },
    ])));
  }, [reviewIdentity]);

  useEffect(() => {
    setActiveEvidenceProjectId(selected?.plans[0]?.project_id ?? null);
  }, [selected?.run_id]);

  const reviewReady = useMemo(() => (
    Boolean(selected)
    && selected?.status === "previewed"
    && selected.plans.length > 0
    && selected.plans.every((plan) => {
      const draft = reviewDrafts[plan.project_id];
      if (!draft?.acknowledged || !draft.reason.trim()) return false;
      if (plan.historical_baseline && !draft.baselineSelected) return false;
      return plan.opening_balances.every((row) => (
        draft.openingBalanceIds.includes(row.opening_balance_id)
      ));
    })
  ), [reviewDrafts, selected]);

  const loadRuns = useCallback(async (requestedPage: number, requestedStatuses: MigrationRunStatus[]) => {
    const generation = ++loadGeneration.current;
    setLoading(true);
    setLoadError(null);
    try {
      const { data } = await searchMaintenanceMigrationRuns({
        statuses: requestedStatuses,
        page: requestedPage,
        page_size: PAGE_SIZE,
      });
      if (generation !== loadGeneration.current) return;
      setRuns(data.items ?? []);
      setTotal(data.total ?? 0);
      setPage(data.page ?? requestedPage);
    } catch (error) {
      if (generation !== loadGeneration.current) return;
      setRuns([]);
      setTotal(0);
      setLoadError(errorDetail(error, "迁移核对清单加载失败，请稍后重试。"));
    } finally {
      if (generation === loadGeneration.current) setLoading(false);
    }
  }, []);

  const loadProjectOptions = useCallback(async () => {
    const rows: MaintenanceProject[] = [];
    let requestedPage = 1;
    let expectedTotal = Number.POSITIVE_INFINITY;
    while (rows.length < expectedTotal && requestedPage <= 50) {
      const { data } = await listMaintenanceProjects({
        page: requestedPage,
        page_size: 200,
      });
      rows.push(...(data.rows ?? []));
      expectedTotal = data.total ?? rows.length;
      if (!(data.rows ?? []).length) break;
      requestedPage += 1;
    }
    setProjects(rows.filter((project) => project.is_active));
  }, []);

  useEffect(() => {
    void loadRuns(1, statuses);
    return () => { loadGeneration.current += 1; };
  }, [loadRuns, statuses]);

  useEffect(() => {
    void loadProjectOptions().catch(() => setProjects([]));
  }, [loadProjectOptions]);

  const touchDrafts = (updater: (current: DraftProject[]) => DraftProject[]) => {
    setDrafts(updater);
    setPreviewKey(null);
    setPreviewError(null);
  };

  const updateDraft = (localId: string, patch: Partial<DraftProject>) => {
    touchDrafts((current) => current.map((draft) => (
      draft.localId === localId ? { ...draft, ...patch } : draft
    )));
  };

  const updateOpening = (
    localId: string,
    index: number,
    patch: Partial<DraftOpening>,
  ) => {
    touchDrafts((current) => current.map((draft) => {
      if (draft.localId !== localId) return draft;
      return {
        ...draft,
        openings: draft.openings.map((row, rowIndex) => (
          rowIndex === index ? { ...row, ...patch } : row
        )),
      };
    }));
  };

  const submitPreview = async () => {
    const validation = validateDrafts(drafts, previewReason);
    if (validation) {
      setPreviewError(validation);
      return;
    }
    const key = previewKey ?? operationKey();
    setPreviewKey(key);
    setPreviewSaving(true);
    setPreviewError(null);
    try {
      const { data } = await previewMaintenanceMigration({
        idempotency_key: key,
        reason: previewReason.trim(),
        projects: drafts.map(projectInput),
      });
      setSelected(data);
      setCreateOpen(false);
      setDrafts([newDraftProject()]);
      setPreviewReason("");
      setPreviewKey(null);
      message.success("dry-run 已生成；当前没有执行任何生产切换");
      await loadRuns(1, statuses);
    } catch (error) {
      setPreviewError(errorDetail(error, "dry-run 生成失败；当前草稿和幂等键已保留，可安全重试。"));
    } finally {
      setPreviewSaving(false);
    }
  };

  const openDetail = async (runId: string) => {
    setDetailLoading(true);
    setDetailError(null);
    try {
      const { data } = await getMaintenanceMigrationRun(runId);
      setSelected(data);
    } catch (error) {
      setDetailError(errorDetail(error, "迁移详情加载失败，请刷新后重试。"));
    } finally {
      setDetailLoading(false);
    }
  };

  const openCommand = (mode: Exclude<CommandMode, null>) => {
    if (mode === "reconcile" && !reviewReady) return;
    setCommandMode(mode);
    setCommandReason("");
    setCommandKey(operationKey());
    setCommandError(null);
  };

  const updateReviewDraft = (
    projectId: string,
    patch: Partial<ProjectReviewDraft>,
  ) => {
    setReviewDrafts((current) => {
      const previous = current[projectId] ?? {
        acknowledged: false,
        reason: "",
        baselineSelected: false,
        openingBalanceIds: [],
      };
      return {
        ...current,
        [projectId]: {
          ...previous,
          ...patch,
        },
      };
    });
    setCommandError(null);
  };

  const projectSignoffs = (): MigrationProjectSignoff[] => {
    if (!selected) return [];
    return selected.plans.map((plan) => {
      const draft = reviewDrafts[plan.project_id];
      return {
        project_id: plan.project_id,
        expected_plan_version: plan.version,
        reason: draft.reason.trim(),
        historical_baseline: plan.historical_baseline && draft.baselineSelected
          ? {
              baseline_id: plan.historical_baseline.baseline_id,
              expected_version: plan.historical_baseline.version,
            }
          : null,
        opening_balances: plan.opening_balances
          .filter((row) => draft.openingBalanceIds.includes(row.opening_balance_id))
          .map((row) => ({
            opening_balance_id: row.opening_balance_id,
            expected_version: row.version,
          })),
      };
    });
  };

  const submitCommand = async () => {
    if (!selected || !commandMode || !commandKey || !commandReason.trim()) return;
    if (commandMode === "reconcile" && !reviewReady) return;
    setCommandSaving(true);
    setCommandError(null);
    try {
      const response = commandMode === "reconcile"
        ? await reconcileMaintenanceMigrationRun(selected.run_id, {
            expected_version: selected.version,
            operation_key: commandKey,
            reason: commandReason.trim(),
            project_signoffs: projectSignoffs(),
          })
        : await approveMaintenanceMigrationRun(selected.run_id, {
            expected_version: selected.version,
            operation_key: commandKey,
            reason: commandReason.trim(),
            supplied_fingerprint: selected.preview.input_fingerprint,
          });
      setSelected(response.data);
      setCommandMode(null);
      setCommandReason("");
      setCommandKey(null);
      message.success(commandMode === "reconcile" ? "实名对账已记录" : "独立审批 manifest 已生成");
      await loadRuns(page, statuses);
    } catch (error) {
      setCommandError(errorDetail(
        error,
        "操作失败；理由和幂等键已保留。来源变化时请关闭后重新生成 dry-run。",
      ));
    } finally {
      setCommandSaving(false);
    }
  };

  const projectName = useMemo(() => new Map(
    projects.map((project) => [project.project_id, `${project.project_code} · ${project.display_name}`]),
  ), [projects]);
  const projectCode = useMemo(() => new Map(
    projects.map((project) => [project.project_id, project.project_code]),
  ), [projects]);

  const columns = useMemo<ColumnsType<MigrationRunSummary>>(() => [
    {
      title: "状态",
      dataIndex: "status",
      width: 130,
      render: (value: MigrationRunStatus) => statusTag(value),
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      width: 180,
      render: (value: string) => new Date(value).toLocaleString("zh-CN"),
    },
    {
      title: "创建 / 对账 / 审批",
      key: "operators",
      render: (_value, row) => (
        <span>{row.created_by} / {row.reconciled_by || "—"} / {row.approved_by || "—"}</span>
      ),
    },
    {
      title: "未解决项",
      dataIndex: "blocker_count",
      width: 100,
      render: (value: number) => value ? <Tag color="red">{value}</Tag> : <Tag color="green">0</Tag>,
    },
    {
      title: "规则版本",
      dataIndex: "rule_version",
      width: 190,
    },
    {
      title: "操作",
      key: "action",
      width: 90,
      render: (_value, row) => (
        <Button type="link" onClick={() => void openDetail(row.run_id)}>查看</Button>
      ),
    },
  ], []);

  return (
    <>
      <PageHeader
        title="成本与库存迁移核对"
        subtitle="先生成可复算 dry-run，再实名对账、独立审批；审批只生成 manifest，不会切换生产口径。"
        extra={<Button type="primary" onClick={() => setCreateOpen(true)}>新建 dry-run</Button>}
      />
      <Alert
        type="warning"
        showIcon
        className="maintenance-migration-gate"
        message="生产切换保持关闭"
        description="本页面没有“启用生产”按钮。代码合并、真实数据迁移、生产启用和验收是四个独立闸门。"
      />
      <Card
        title="核对记录"
        extra={(
          <Select<MigrationRunStatus[]>
            mode="multiple"
            allowClear
            value={statuses}
            placeholder="全部状态"
            style={{ minWidth: 240 }}
            options={[
              { value: "previewed", label: "待对账" },
              { value: "reconciled", label: "已实名对账" },
              { value: "approved", label: "已独立审批" },
            ]}
            onChange={(value) => { setPage(1); setStatuses(value); }}
          />
        )}
      >
        {loadError && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            message={loadError}
            action={<Button size="small" onClick={() => void loadRuns(page, statuses)}>重试</Button>}
          />
        )}
        <Table
          rowKey="run_id"
          columns={columns}
          dataSource={runs}
          loading={loading}
          pagination={{
            current: page,
            pageSize: PAGE_SIZE,
            total,
            showSizeChanger: false,
            onChange: (nextPage) => { setPage(nextPage); void loadRuns(nextPage, statuses); },
          }}
          locale={{ emptyText: <Empty description="还没有迁移 dry-run" /> }}
        />
      </Card>

      <Drawer
        title="新建迁移 dry-run"
        width={860}
        open={createOpen}
        destroyOnClose={false}
        onClose={() => { if (!previewSaving) setCreateOpen(false); }}
        extra={(
          <Space>
            <Button disabled={previewSaving} onClick={() => setCreateOpen(false)}>取消</Button>
            <Button type="primary" loading={previewSaving} onClick={() => void submitPreview()}>
              生成核对清单
            </Button>
          </Space>
        )}
      >
        <Alert
          type="info"
          showIcon
          message="这里填写的是待核对候选，不是直接写入生产的结果"
          description="成本基线和库存期初在首次 dry-run 中均标记为待审批；后续实名对账才会计入结果。"
          style={{ marginBottom: 16 }}
        />
        {previewError && <Alert type="error" showIcon message={previewError} style={{ marginBottom: 16 }} />}
        {drafts.map((draft, draftIndex) => (
          <Card
            key={draft.localId}
            size="small"
            className="maintenance-migration-project-draft"
            title={`项目 ${draftIndex + 1}`}
            extra={drafts.length > 1 ? (
              <Button
                danger
                type="link"
                onClick={() => touchDrafts((current) => current.filter((item) => item.localId !== draft.localId))}
              >
                移除
              </Button>
            ) : undefined}
          >
            <div className="maintenance-migration-form-grid">
              <label>
                <span>稳定项目</span>
                <Select
                  showSearch
                  value={draft.projectId || undefined}
                  placeholder="按项目编号或名称搜索"
                  optionFilterProp="label"
                  options={projects.map((project) => ({
                    value: project.project_id,
                    label: `${project.project_code} · ${project.display_name}`,
                  }))}
                  onChange={(value) => updateDraft(draft.localId, { projectId: value })}
                />
              </label>
              <label>
                <span>切换日期</span>
                <Input
                  type="date"
                  value={draft.cutoverDate}
                  onChange={(event) => updateDraft(draft.localId, { cutoverDate: event.target.value })}
                />
              </label>
              <label className="maintenance-migration-span-two">
                <span>历史成本方式</span>
                <Select
                  value={draft.historicalMode}
                  options={[
                    { value: "approved_cost_baseline", label: "实名审批的历史成本基线" },
                    { value: "stable_site_issues", label: "有稳定身份的历史现场领用" },
                  ]}
                  onChange={(value) => updateDraft(draft.localId, { historicalMode: value })}
                />
              </label>
              {draft.historicalMode === "approved_cost_baseline" && (
                <>
                  <label>
                    <span>历史基线（未税）</span>
                    <Input
                      inputMode="decimal"
                      value={draft.baselineExTax}
                      placeholder="0.00"
                      onChange={(event) => updateDraft(draft.localId, { baselineExTax: event.target.value })}
                    />
                  </label>
                  <label>
                    <span>历史基线（含税）</span>
                    <Input
                      inputMode="decimal"
                      value={draft.baselineIncTax}
                      placeholder="0.00"
                      onChange={(event) => updateDraft(draft.localId, { baselineIncTax: event.target.value })}
                    />
                  </label>
                  <label className="maintenance-migration-span-two">
                    <span>基线证据 SHA-256</span>
                    <Input
                      value={draft.baselineEvidenceHash}
                      maxLength={64}
                      placeholder="归档证据文件的 64 位 SHA-256"
                      onChange={(event) => updateDraft(draft.localId, { baselineEvidenceHash: event.target.value })}
                    />
                  </label>
                </>
              )}
            </div>
            <Divider orientation="left">切换日库存期初</Divider>
            <Typography.Paragraph type="secondary">
              可以先留空生成“缺少期初”的阻塞项；一旦填写某行，稳定键、数量和证据哈希必须完整。
            </Typography.Paragraph>
            {draft.openings.map((opening, openingIndex) => (
              <div className="maintenance-migration-opening-row" key={`${draft.localId}-${openingIndex}`}>
                <Input
                  aria-label={`项目 ${draftIndex + 1} 库存稳定键 ${openingIndex + 1}`}
                  value={opening.balanceKey}
                  placeholder="稳定键（仓库/项目 + 备件稳定身份）"
                  onChange={(event) => updateOpening(draft.localId, openingIndex, { balanceKey: event.target.value })}
                />
                <Input
                  aria-label={`项目 ${draftIndex + 1} PN ${openingIndex + 1}`}
                  value={opening.pn}
                  placeholder="PN（展示用）"
                  onChange={(event) => updateOpening(draft.localId, openingIndex, { pn: event.target.value })}
                />
                <Input
                  aria-label={`项目 ${draftIndex + 1} 数量 ${openingIndex + 1}`}
                  inputMode="decimal"
                  value={opening.quantity}
                  placeholder="数量"
                  onChange={(event) => updateOpening(draft.localId, openingIndex, { quantity: event.target.value })}
                />
                <Input
                  aria-label={`项目 ${draftIndex + 1} 证据哈希 ${openingIndex + 1}`}
                  value={opening.evidenceHash}
                  maxLength={64}
                  placeholder="证据 SHA-256"
                  onChange={(event) => updateOpening(draft.localId, openingIndex, { evidenceHash: event.target.value })}
                />
                <Button
                  danger
                  disabled={draft.openings.length === 1}
                  onClick={() => touchDrafts((current) => current.map((item) => (
                    item.localId === draft.localId
                      ? { ...item, openings: item.openings.filter((_row, index) => index !== openingIndex) }
                      : item
                  )))}
                >
                  删除行
                </Button>
              </div>
            ))}
            <Button
              onClick={() => touchDrafts((current) => current.map((item) => (
                item.localId === draft.localId
                  ? { ...item, openings: [...item.openings, newOpening()] }
                  : item
              )))}
            >
              新增库存期初行
            </Button>
          </Card>
        ))}
        <Button
          block
          style={{ marginBottom: 16 }}
          onClick={() => touchDrafts((current) => [...current, newDraftProject()])}
        >
          新增项目
        </Button>
        <label className="maintenance-migration-reason">
          <span>生成理由（实名留痕）</span>
          <Input.TextArea
            rows={3}
            value={previewReason}
            maxLength={1000}
            showCount
            placeholder="说明本次核对的范围、证据日期和责任人"
            onChange={(event) => {
              setPreviewReason(event.target.value);
              setPreviewKey(null);
              setPreviewError(null);
            }}
          />
        </label>
      </Drawer>

      <Drawer
        title="迁移核对详情"
        width={980}
        open={Boolean(selected) || detailLoading || Boolean(detailError)}
        onClose={() => { setSelected(null); setDetailError(null); }}
        extra={selected ? (
          <Space>
            {selected.status === "previewed" && (
              <Button
                type="primary"
                disabled={!reviewReady}
                onClick={() => openCommand("reconcile")}
              >
                实名对账
              </Button>
            )}
            {selected.status === "reconciled" && (
              <Button type="primary" disabled={!selected.preview.can_approve} onClick={() => openCommand("approve")}>独立审批</Button>
            )}
          </Space>
        ) : undefined}
      >
        {detailLoading && <Spin />}
        {detailError && <Alert type="error" showIcon message={detailError} />}
        {selected && (
          <>
            <Alert
              type={selected.preview.approval_blocker_count ? "warning" : "success"}
              showIcon
              message={selected.preview.approval_blocker_count
                ? `仍有 ${selected.preview.approval_blocker_count} 个阻塞项`
                : "当前快照无阻塞项"}
              description="返件不冲成本；现场领用和返还登记不改变库存。本结果仍未启用生产。"
              style={{ marginBottom: 16 }}
            />
            <Descriptions bordered size="small" column={2}>
              <Descriptions.Item label="状态">{statusTag(selected.status)}</Descriptions.Item>
              <Descriptions.Item label="版本">{selected.version}</Descriptions.Item>
              <Descriptions.Item label="创建人">{selected.created_by}</Descriptions.Item>
              <Descriptions.Item label="对账人">{selected.reconciled_by || "—"}</Descriptions.Item>
              <Descriptions.Item label="审批人">{selected.approved_by || "—"}</Descriptions.Item>
              <Descriptions.Item label="规则版本">{selected.rule_version}</Descriptions.Item>
              <Descriptions.Item label="来源快照" span={2}>
                <Typography.Text copyable code>{selected.source_snapshot_hash}</Typography.Text>
              </Descriptions.Item>
              {selected.manifest_hash && (
                <Descriptions.Item label="manifest 哈希" span={2}>
                  <Typography.Text copyable code>{selected.manifest_hash}</Typography.Text>
                </Descriptions.Item>
              )}
              {selected.manifest_key_id && (
                <Descriptions.Item label="manifest 签名密钥 ID" span={2}>
                  <Typography.Text copyable code>{selected.manifest_key_id}</Typography.Text>
                </Descriptions.Item>
              )}
            </Descriptions>
            {selected.plans.map((plan) => {
              const preview = selected.preview.projects.find((item) => item.project_id === plan.project_id);
              const label = projectName.get(plan.project_id) || plan.project_id;
              const code = projectCode.get(plan.project_id) || plan.project_id;
              const review = reviewDrafts[plan.project_id] ?? {
                acknowledged: false,
                reason: "",
                baselineSelected: false,
                openingBalanceIds: [],
              };
              return (
                <Card
                  key={plan.plan_id}
                  className="maintenance-migration-plan"
                  title={label}
                  extra={plan.blocker_count ? <Tag color="red">{plan.blocker_count} 个阻塞项</Tag> : <Tag color="green">可复算</Tag>}
                >
                  <div className="maintenance-migration-cost-grid">
                    <div><span>历史成本</span><strong>{money(plan.cost.historical_ex_tax)}</strong></div>
                    <div><span>切换后现场领用</span><strong>{money(plan.cost.post_cutover_ex_tax)}</strong></div>
                    <div><span>已审批报销</span><strong>{money(plan.cost.approved_expense_ex_tax)}</strong></div>
                    <div><span>项目成本合计</span><strong>{money(plan.cost.total_ex_tax)}</strong></div>
                  </div>
                  <Divider orientation="left">候选成本基线与库存期初</Divider>
                  {selected.status === "previewed" && (
                    <Alert
                      type="info"
                      showIcon
                      message="候选默认不批准，必须逐项查看并明确勾选"
                      description="后端会校验本项目、计划版本和候选版本的精确集合；漏选、多选或来源变化都会整批拒绝。"
                      style={{ marginBottom: 12 }}
                    />
                  )}
                  {plan.historical_baseline ? (
                    <div className="maintenance-migration-review-candidate">
                      <div>
                        <Typography.Text strong>历史成本基线</Typography.Text>
                        <div>
                          未税 {money(plan.historical_baseline.amount_ex_tax)} · 含税 {money(plan.historical_baseline.amount_inc_tax)}
                        </div>
                        <Typography.Text type="secondary" copyable code>
                          {plan.historical_baseline.evidence_hash}
                        </Typography.Text>
                      </div>
                      {selected.status === "previewed" ? (
                        <Checkbox
                          aria-label={`确认 ${code} 历史成本基线`}
                          checked={review.baselineSelected}
                          onChange={(event) => updateReviewDraft(plan.project_id, {
                            baselineSelected: event.target.checked,
                            acknowledged: false,
                          })}
                        >
                          明确批准此基线
                        </Checkbox>
                      ) : (
                        <Tag color={plan.historical_baseline.approval_state === "approved" ? "green" : "orange"}>
                          {plan.historical_baseline.approval_state === "approved" ? "已批准" : "待批准"}
                        </Tag>
                      )}
                    </div>
                  ) : (
                    <Typography.Paragraph type="secondary">本项目不使用历史成本基线候选。</Typography.Paragraph>
                  )}
                  <Table
                    size="small"
                    rowKey="opening_balance_id"
                    pagination={false}
                    dataSource={plan.opening_balances}
                    locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有库存期初候选" /> }}
                    columns={[
                      ...(selected.status === "previewed" ? [{
                        title: "明确选择",
                        key: "selection",
                        width: 120,
                        render: (_value: unknown, row: MigrationRunDetail["plans"][number]["opening_balances"][number]) => (
                          <Checkbox
                            aria-label={`确认 ${code} 库存期初 ${row.pn || row.balance_key}`}
                            checked={review.openingBalanceIds.includes(row.opening_balance_id)}
                            onChange={(event) => updateReviewDraft(plan.project_id, {
                              openingBalanceIds: event.target.checked
                                ? [...review.openingBalanceIds, row.opening_balance_id]
                                : review.openingBalanceIds.filter((id) => id !== row.opening_balance_id),
                              acknowledged: false,
                            })}
                          >
                            批准
                          </Checkbox>
                        ),
                      }] : [{
                        title: "状态",
                        dataIndex: "approval_state",
                        width: 100,
                        render: (value: string) => (
                          <Tag color={value === "approved" ? "green" : "orange"}>
                            {value === "approved" ? "已批准" : "待批准"}
                          </Tag>
                        ),
                      }]),
                      { title: "库存稳定键", dataIndex: "balance_key" },
                      { title: "PN", dataIndex: "pn", render: (value: string | null) => value || "—" },
                      { title: "数量", dataIndex: "quantity", width: 90 },
                      {
                        title: "证据 SHA-256",
                        dataIndex: "evidence_hash",
                        render: (value: string) => <Typography.Text copyable code>{value}</Typography.Text>,
                      },
                    ]}
                  />
                  {selected.status === "previewed" ? (
                    <div className="maintenance-migration-project-signoff">
                      <label>
                        <span>本项目对账理由</span>
                        <Input.TextArea
                          aria-label={`${code} 项目对账理由`}
                          rows={2}
                          value={review.reason}
                          maxLength={1000}
                          showCount
                          placeholder="说明本项目来源、金额、数量和异常项的核对结论"
                          onChange={(event) => updateReviewDraft(plan.project_id, {
                            reason: event.target.value,
                            acknowledged: false,
                          })}
                        />
                      </label>
                      <Checkbox
                        aria-label={`已查看 ${code} 全部分页证据并确认候选完整`}
                        checked={review.acknowledged}
                        onChange={(event) => updateReviewDraft(plan.project_id, {
                          acknowledged: event.target.checked,
                        })}
                      >
                        我已查看本项目的分页来源证据，并确认以上候选完整、版本正确
                      </Checkbox>
                    </div>
                  ) : plan.reconciled_by ? (
                    <Alert
                      type="success"
                      showIcon
                      message={`本项目已由 ${plan.reconciled_by} 实名对账`}
                      description={plan.reconciliation_reason || undefined}
                      style={{ marginTop: 12 }}
                    />
                  ) : null}
                  <Divider orientation="left">差异与阻塞</Divider>
                  {plan.discrepancies.length ? (
                    <Space direction="vertical" style={{ width: "100%" }}>
                      {plan.discrepancies.map((row) => (
                        <Alert
                          key={row.discrepancy_id}
                          type={row.status === "resolved" ? "success" : "warning"}
                          showIcon
                          message={row.detail.detail || row.code}
                          description={row.entity_id ? `关联条目：${row.entity_id}` : undefined}
                        />
                      ))}
                    </Space>
                  ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有差异" />}
                  <Divider orientation="left">库存重算（不含现场领用/返还登记）</Divider>
                  <Table
                    size="small"
                    rowKey="balance_key"
                    pagination={false}
                    dataSource={preview?.inventory ?? []}
                    columns={[
                      { title: "稳定键", dataIndex: "balance_key" },
                      { title: "期初", dataIndex: "opening_quantity" },
                      { title: "发货扣减", dataIndex: "delivery_quantity" },
                      { title: "正式可用入库", dataIndex: "available_receipt_quantity" },
                      { title: "期末", dataIndex: "closing_quantity" },
                    ]}
                  />
                  <Divider orientation="left">来源证据</Divider>
                  <Button
                    type="link"
                    className="maintenance-migration-evidence-toggle"
                    onClick={() => setActiveEvidenceProjectId((current) => (
                      current === plan.project_id ? null : plan.project_id
                    ))}
                  >
                    {activeEvidenceProjectId === plan.project_id ? "收起分页证据" : "查看分页证据"}
                  </Button>
                  {activeEvidenceProjectId === plan.project_id && preview && (
                    <MaintenanceMigrationEvidence
                      runId={selected.run_id}
                      projectId={plan.project_id}
                      projectLabel={label}
                      preview={preview}
                    />
                  )}
                </Card>
              );
            })}
            <Divider orientation="left">实名状态记录</Divider>
            <Timeline
              items={selected.events.map((event) => ({
                color: event.action === "approve" ? "green" : event.action === "reconcile" ? "blue" : "gray",
                children: (
                  <div>
                    <strong>{event.action === "preview" ? "生成 dry-run" : event.action === "reconcile" ? "实名对账" : "独立审批"}</strong>
                    <div>{event.operated_by} · {new Date(event.operated_at).toLocaleString("zh-CN")}</div>
                    <Typography.Text type="secondary">{event.reason}</Typography.Text>
                  </div>
                ),
              }))}
            />
          </>
        )}
      </Drawer>

      <Modal
        title={commandMode === "reconcile" ? "确认实名对账" : "确认独立审批"}
        open={Boolean(commandMode)}
        confirmLoading={commandSaving}
        okText={commandMode === "reconcile" ? "记录对账" : "生成签名 manifest"}
        okButtonProps={{ disabled: !commandReason.trim() }}
        onOk={() => void submitCommand()}
        onCancel={() => {
          if (commandSaving) return;
          setCommandMode(null);
          setCommandReason("");
          setCommandKey(null);
          setCommandError(null);
        }}
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={commandMode === "reconcile"
            ? "只会批准刚才逐项目、逐候选明确勾选的精确集合"
            : "最终审批人必须不同于创建人与对账人"}
          description="任何项目、候选版本或来源快照变化都会使整批操作失败；本操作仍不会启用生产。"
        />
        {commandError && <Alert type="error" showIcon message={commandError} style={{ marginBottom: 16 }} />}
        <Input.TextArea
          rows={4}
          value={commandReason}
          maxLength={1000}
          showCount
          placeholder="填写核对依据和结论；必须是可审计的业务理由"
          onChange={(event) => setCommandReason(event.target.value)}
        />
      </Modal>
    </>
  );
}
