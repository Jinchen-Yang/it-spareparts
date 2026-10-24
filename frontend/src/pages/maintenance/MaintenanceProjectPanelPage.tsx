import { useContextAdmission, useModuleAccess } from "../../authz/access";
import { useContextTransport } from "../../authz/domainTransport";
import { contextInputKey } from "../../authz/context";
import { withContextSurface, contextSurfaces } from "../../authz/ContextSurface";
import { actionRule, anyRule, routeRule } from "../../authz/rules";
import type { AuthorizationContext } from "../../authz/contextTypes";
import type { MaintenanceWorkspaceProject } from "../../api/maintenanceOperations";
import PageHeader from "../../components/PageHeader";
import { formatMoney } from "../../utils/format";
import { COLORS } from "../../theme";
import { forwardRef, lazy, Suspense, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { App, Alert, Button, Card, Col, DatePicker, Form, Input, Modal, Row, Select, Space, Spin, Statistic, Tabs, Tag, Tooltip, Typography } from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { EditOutlined } from "@ant-design/icons";
import type { BoardProjectRow } from "../../api/maintenanceBossBoard";
import { getBoardProject, searchBoardProjects } from "../../api/maintenanceBossBoard";
import {
  applyProjectMaster,
  downloadProjectMaster,
  validateProjectMaster,
} from "../../api/maintenanceWorkbooks";
import {
  getMaintenanceProject,
  updateMaintenanceProject,
} from "../../api/maintenanceProjects";
import type { MaintenanceProject } from "../../api/maintenanceProjects";
import { getMaintenanceProjectWorkspace } from "../../api/maintenanceOperations";
import type { MaintenanceCollectionSnapshotRow } from "../../api/maintenanceOperations";
import type { MaintenanceProjectOperationsSummary } from "../../api/maintenanceOperations";
import { searchMaintenanceManagerAccounts } from "../../api/maintenanceOperations";
import type { MaintenanceManagerAccount } from "../../api/maintenanceOperations";
import ProjectManagerAssignmentControl from "../../components/maintenance/ProjectManagerAssignmentControl";
import ProjectWorkbookRoundTrip from "../../components/maintenance/ProjectWorkbookRoundTrip";
import { readPermissionMap } from "../../nav";
import { maintenanceAnalyticsReturnPath, readProjectPanelTab } from "./analysisNavigation";
import { publishMaintenanceChange } from "../../utils/maintenanceRefresh";
import { readMaintenanceCapabilities } from "../../components/maintenance/maintenancePermissions";
import {
  LIFECYCLE_LABEL,
  type PanelRefresh,
  type RegisterPanelRefresh,
  STATUS_COLOR,
  readError,
  safeFilenamePart,
  statText,
} from "./panel/panelUtils";

const { Text } = Typography;
const OverviewTab = lazy(() => import("./panel/OverviewTab"));
const PartsOrdersTab = lazy(() => import("./panel/PartsOrdersTab"));
const ExpenseTab = lazy(() => import("./panel/ExpenseTab"));
const CollectionTab = lazy(() => import("./panel/CollectionTab"));
const SiteReturnTab = lazy(() => import("./panel/SiteReturnTab"));
const MaintenanceProjectAnalyticsContext = lazy(() => import("./panel/MaintenanceProjectAnalyticsContext"));
const AcceptanceTab = lazy(() => import("./panel/AcceptanceTab"));

interface HealthMetrics {
  received: number | null;
  progress: number | null;
  /** 四类成本：null=当前没有可靠口径，不能用 0 冒充。 */
  costs: {
    parts: number | null;
    expense: number | null;
    issued: number | null;
    returned: number | null;
  };
}

const HEALTH_VALUE_STYLE = { fontSize: 18, fontWeight: 600 } as const;

/**
 * 项目健康带（2026-08-19 重设计的 signature 区块）：一行四格——
 * 合同额、累计已回款、回款进度、成本率。任何一格算不出来就说人话，绝不落 0（铁律 5）；
 * 聚合行缺失（撞名/未入板）整格「聚合数据暂缺」，不影响下方明细。
 */
function HealthBand({ row, metrics }: { row: BoardProjectRow | null; metrics: HealthMetrics }) {
  const contractStat = row?.contract_amount_inc_tax;
  const contractText = !row
    ? "聚合数据暂缺"
    : contractStat?.state === "partial"
      ? contractStat.value === null || contractStat.value === ""
        ? "合同事实不完整（暂无已知小计）"
        : `${statText(contractStat)}（已知小计，合同事实不完整）`
      : statText(contractStat);
  const costValue = row?.known_apply_cost_inc_tax.value;
  const partsIsLowerBound = costValue?.quality === "incomplete"
    && costValue.known_amount != null
    && Number(costValue.coverage_pct ?? 0) > 0;

  let ratioText: string;
  let ratioColor: string | undefined;
  if (!row) {
    ratioText = "聚合数据暂缺";
  } else if (row.cost_ratio_pct?.state === "ready") {
    if (row.cost_ratio_pct.value === null || row.cost_ratio_pct.value === "") {
      ratioText = "数据不足";
    } else {
      ratioText = `${row.cost_ratio_pct.value}%${partsIsLowerBound ? "（已知下限）" : ""}`;
      ratioColor = row.card_status ? STATUS_COLOR[row.card_status] : undefined;
    }
  } else {
    ratioText = statText(row.cost_ratio_pct);
  }

  const costLines: {
    label: string;
    value: number | null;
    color: string;
    hint?: string;
    lowerBound?: boolean;
  }[] = [
    { label: "备件成本", value: metrics.costs.parts, color: COLORS.accent,
      lowerBound: partsIsLowerBound,
      hint: "本项目挂靠需求单明细的已知备件成本（含税）" },
    { label: "报销成本", value: metrics.costs.expense, color: COLORS.warning,
      hint: "已批准报销（含税）" },
    { label: "已领用成本", value: metrics.costs.issued, color: COLORS.text2,
      hint: "现场领用（已确认/已更正）的已知成本（含税）" },
    { label: "返还成本", value: metrics.costs.returned, color: COLORS.success,
      hint: "返还成本口径建设中，当前不提供猜测值" },
  ];

  return (
    <Card size="small" data-testid="panel-health-band">
      <Row gutter={16}>
        <Col xs={12} sm={6}>
          <Statistic title="合同总额（含税）" value={contractText} valueStyle={HEALTH_VALUE_STYLE} />
        </Col>
        <Col xs={12} sm={6}>
          <Statistic
            title="累计已回款"
            value={formatMoney(metrics.received, { mode: "fixed", prefix: "¥", empty: "数据不足" })}
            valueStyle={HEALTH_VALUE_STYLE}
          />
        </Col>
        <Col xs={12} sm={6}>
          <Statistic
            title="回款进度"
            value={metrics.progress == null ? "数据不足" : `${metrics.progress}%`}
            valueStyle={HEALTH_VALUE_STYLE}
          />
        </Col>
        <Col xs={12} sm={6}>
          <Statistic
            title="成本率"
            value={ratioText}
            valueStyle={{ ...HEALTH_VALUE_STYLE, color: ratioColor }}
          />
        </Col>
      </Row>
      <div style={{ marginTop: 10 }}>
        {costLines.map((line) => (
          <Row key={line.label} style={{ padding: "2px 0" }} gutter={8}>
            <Col span={6}>
              <Text strong>{line.label}</Text>
            </Col>
            <Col span={18}>
              <Tooltip title={line.hint}>
                <Text strong style={{ color: line.color, fontSize: 16 }}>
                  {line.value == null
                    ? "数据不足"
                    : formatMoney(line.value, { mode: "fixed", prefix: "¥", suffix: line.lowerBound ? "（已知下限）" : "" })}
                </Text>
              </Tooltip>
            </Col>
          </Row>
        ))}
      </div>
    </Card>
  );
}

/**
 * 项目面板（2026-08-19 重设计）：自上而下 = 用户视线顺序——
 * 我是谁（页头：项目名 + 状态 Tag + 总表两阶段回传）→ 我健康吗（健康带四格）
 * → 细节（概览 / 备件与需求单 / 报销 / 回款 / 领用与返还）。
 * 取数：基础信息以 stable 目录为准，活跃项目按稳定 ID 取展示板聚合卡；停用项目
 * 先查询展示板中是否仍有该稳定 ID，空集时不请求必然 404 的单卡。回款指标走
 * getMaintenanceProjectWorkspace。聚合行缺失时健康带「聚合数据暂缺」，不全页报错。
 */
function MaintenanceProjectPanelPageBody() {
  const { projectId = "" } = useParams();
  // React Router 会复用同一个 route element。以项目 ID 强制重建内部状态，确保
  // A 项目的在途请求、tab 筛选和旧金额都不能进入 B 项目的操作上下文。
  return <BoundProjectPanel projectId={projectId} />;
}

const PROJECT_TABS: Record<string, string[]> = {
  overview: ["maintenance_project.overview", "maintenance_source_assignment.sources", "maintenance_source_assignment_write.manage_sources"],
  analytics: ["maintenance_analysis.project_focus"],
  "parts-orders": ["maintenance_managed_workbook.parts_roundtrip", "maintenance_general_workbook.parts_roundtrip", "maintenance_general_workbook_write.parts_roundtrip", "maintenance_summary.project_orders", "maintenance_row_workbench.cost_rows", "maintenance_row_workbench.project_rows", "maintenance_managed_rows.project_rows", "maintenance_project_purchase.procurement"],
  expense: ["maintenance_managed_workbook.expense_roundtrip", "maintenance_general_workbook.expense_roundtrip", "maintenance_general_workbook_write.expense_roundtrip", "maintenance_expense.records"],
  collection: ["maintenance_managed_workbook.collection_roundtrip", "maintenance_general_workbook.collection_roundtrip", "maintenance_general_workbook_write.collection_roundtrip", "maintenance_collection.plans", "maintenance_collection.actuals", "maintenance_collection.manage_actuals"],
  site: ["maintenance_managed_workbook.site_roundtrip", "maintenance_general_workbook.site_roundtrip", "maintenance_general_workbook_write.site_roundtrip", "maintenance_issue.facts", "maintenance_issue.workflow", "maintenance_issue.manual", "maintenance_return_receipt.ledger", "maintenance_return_receipt.summary", "maintenance_return_receipt.batch_create", "maintenance_return_import.import"],
  acceptance: ["maintenance_acceptance.checklist", "maintenance_acceptance.checklist_import", "maintenance_acceptance.report", "maintenance_acceptance.attachments"],
};
function MaintenanceProjectPanelContent({ projectId }: { projectId: string }) {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const admission = useContextAdmission();
  const projectModule = useModuleAccess("maintenance_project");
  const readTransport = useContextTransport(projectId, true);
  const availableTabs = Object.keys(PROJECT_TABS).filter(key => PROJECT_TABS[key].some(id => admission.surface(id, true) !== "deny"));
  const requestedTab = readProjectPanelTab(searchParams.get("tab"));
  const activeTab = availableTabs.includes(requestedTab) ? requestedTab : availableTabs[0];
  const showOverview = admission.surface("maintenance_project.overview", true) !== "deny";
  const showSummary = admission.surface("maintenance_summary.project_health", true) !== "deny";
  const showOperationsHealth = admission.surface("maintenance_project_operations.project_health", true) !== "deny";
  const readWorkspace = showOperationsHealth || admission.surface("maintenance_project_operations.workspace", true) !== "deny" ||
    (activeTab === "collection" && admission.surface("maintenance_collection.actuals", true) !== "deny");
  const masterVisible = admission.state(contextSurfaces("maintenance_managed_workbook.project_master", "maintenance_general_workbook.project_master", "maintenance_general_workbook_write.project_master"), true) !== "deny";
  const workbookTransport = useContextTransport(`${projectId}:master-workbook`, true);
  const analyticsReturn = maintenanceAnalyticsReturnPath(searchParams.get("return"));
  const [row, setRow] = useState<BoardProjectRow | null>(null);
  const [project, setProject] = useState<MaintenanceProject | null>(null);
  const [operationsProject, setOperationsProject] =
    useState<MaintenanceWorkspaceProject | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [aggregateError, setAggregateError] = useState<string | null>(null);
  const [projectLoading, setProjectLoading] = useState(false);
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);
  const [collectionRows, setCollectionRows] = useState<MaintenanceCollectionSnapshotRow[]>([]);
  const [collectionLoading, setCollectionLoading] = useState(false);
  const projectRequestSeq = useRef(0);
  const workspaceRequestSeq = useRef(0);
  const tabRefreshers = useRef(new Map<string, PanelRefresh>());
  const [metrics, setMetrics] = useState<HealthMetrics>({
    received: null, progress: null,
    costs: { parts: null, expense: null, issued: null, returned: null },
  });

  const perms = readPermissionMap();
  const canManageManagerAssignment = admission.surface("maintenance_assignments.manager",
    localStorage.getItem("role") === "admin" && !!perms.action_maintenance_project_manage) !== "deny";
  const canReadManagerRelationship = admission.state(actionRule("maintenance_project", "read"), true) !== "deny";
  const readStableProject = showOverview || (canManageManagerAssignment && canReadManagerRelationship);
  // D-03（2026-09-02 拍板）：项目负责人/持利润键的销售对本人项目可传总表。后端上传门
  // 早已按此放行，入口显示跟服务端下发的 can_edit_master_workbook 走，不在前端复算
  // 挂靠关系。展示板单卡可能 404（无挂靠的停用项目 / 展示板关闭），稳定详情的
  // project 上带同一 flag 兜底。
  const legacyUpload = !!perms.action_maintenance_expense_collection_upload
    || !!row?.can_edit_master_workbook
    || !!project?.can_edit_master_workbook;
  const canUpload = admission.state(anyRule(actionRule("maintenance_managed_workbook", "validate"), actionRule("maintenance_general_workbook_write", "validate")), legacyUpload) !== "deny";
  const canDownload = admission.state(anyRule(actionRule("maintenance_managed_workbook", "download"), actionRule("maintenance_general_workbook", "download")), true) !== "deny";
  const canManageProject = admission.surface("maintenance_source_assignment_write.manage_sources", !!perms.action_maintenance_project_manage) !== "deny";
  const legacyEditBasics = !!perms.action_maintenance_project_manage && project?.is_active !== false && (
    localStorage.getItem("role") === "admin"
    || (!!perms.page_maintenance && !!perms.data_profit)
  );
  const canEditBasics = admission.surface("maintenance_project.edit_basics", legacyEditBasics) !== "deny";
  const editBasicsRef = useRef<{ open: () => void }>(null);
  // 验收清单导入 2026-08-22 起跟随维保页面权限（与 maintenancePermissions 同口径），
  // 不再看已停用的 action_maintenance_acceptance_checklist_import 旧键。
  const canImportChecklist = readMaintenanceCapabilities().canImportAcceptanceChecklist;

  // 下载文件名 = XSDD销售订单号（取第一个） + 维保项目名 + 表单类型（2026-08-17）
  const exportBase = (() => {
    const xsdd = row?.contract_nos?.[0] ?? project?.project_code ?? projectId;
    const name = row?.display_name ?? project?.display_name ?? projectId;
    return `${xsdd}-${safeFilenamePart(name)}`;
  })();

  const loadProject = useCallback(async () => {
    const seq = ++projectRequestSeq.current;
    setError(null);
    setAggregateError(null);
    setProjectLoading(true);
    try {
      let stable: MaintenanceProject | null = null;
      if (readStableProject) {
        try { stable = (await getMaintenanceProject(projectId, readTransport)).data.project; }
        catch (reason) {
          if (seq !== projectRequestSeq.current || !readTransport.isCurrent()) return false;
          setError(readError(reason, "项目基本信息加载失败"));
        }
      }
      if (seq !== projectRequestSeq.current || !readTransport.isCurrent()) return false;
      setProject(stable);
      let hit: BoardProjectRow | null = null;
      let aggregateReady = true;
      if (showSummary) {
        try {
          if (projectModule.legacy && stable?.is_active === false) {
            // 展示板只纳入仍带单的停用项目；搜索空集是正常状态。编号只缩小候选，
            // 必须遍历分页并按稳定 ID 取行，不能把同名项目或第一页当成身份。
            const pageSize = 200;
            for (let page = 1; ; page += 1) {
              const response = await searchBoardProjects({
                q: stable.project_code,
                page,
                page_size: pageSize,
                lifecycle: "all",
                business_type: "all",
              }, readTransport);
              if (seq !== projectRequestSeq.current || !readTransport.isCurrent()) return false;
              hit = response.data.rows.find((item) => item.project_id === projectId) ?? null;
              if (hit || !response.data.rows.length || page * pageSize >= response.data.total) break;
            }
          } else {
            hit = (await getBoardProject(projectId, readTransport)).data;
          }
        } catch (err) {
          // 稳定详情仍可使用；真正的读取失败独立显示重试入口。
          if (seq !== projectRequestSeq.current || !readTransport.isCurrent()) return false;
          hit = null;
          aggregateReady = false;
          setAggregateError(readError(err, "项目经营汇总加载失败"));
        }
      }
      if (seq !== projectRequestSeq.current || !readTransport.isCurrent()) return false;
      const costStat = hit?.known_apply_cost_inc_tax;
      const costValue = costStat?.value;
      const noLines = costValue?.known_amount == null;
      const allMissing = costValue?.quality === "incomplete"
        && Number(costValue.coverage_pct ?? 0) === 0
        && costValue.missing_lines > 0;
      const knownParts = costStat
        && ["ready", "partial", "stale"].includes(costStat.state)
        && costValue != null
        && !noLines
        && !allMissing
        ? Number(costValue.known_amount)
        : null;
      setMetrics((prev) => ({
        ...prev,
        costs: {
          ...prev.costs,
          parts: knownParts != null && Number.isFinite(knownParts) ? knownParts : null,
        },
      }));
      setRow(hit);
      if (!hit && !stable && readStableProject) setError("项目不存在或无权查看");
      return (!readStableProject || Boolean(stable)) && aggregateReady;
    } catch (err) {
      if (seq === projectRequestSeq.current && readTransport.isCurrent()) {
        // 读回失败必须失效旧项目快照；否则刚落库的新值旁边还会显示旧金额。
        setProject(null);
        setRow(null);
        setMetrics((prev) => ({
          ...prev,
          costs: { ...prev.costs, parts: null },
        }));
        setError(readError(err, "项目面板加载失败"));
      }
      return false;
    } finally {
      if (seq === projectRequestSeq.current && readTransport.isCurrent()) setProjectLoading(false);
    }
  }, [projectId, readTransport, readStableProject, showSummary, projectModule.legacy]);

  // 回款指标 + 快照行：健康带与「回款」tab 共用同一份 workspace 数据。
  const loadWorkspace = useCallback(async () => {
    const seq = ++workspaceRequestSeq.current;
    if (!readWorkspace) { setCollectionRows([]); setOperationsProject(null); return true; }
    setCollectionLoading(true);
    setWorkspaceError(null);
    try {
      const pageSize = 100;
      const response = await getMaintenanceProjectWorkspace(projectId, {
        collection_page: 1,
        collection_page_size: pageSize,
        requisition_page_size: 1,
        expense_page_size: 1,
      }, readTransport);
      if (seq !== workspaceRequestSeq.current || !readTransport.isCurrent()) return false;
      const collectionRows = [...response.data.collection_snapshots.rows];
      let collectionTotal = response.data.collection_snapshots.total;
      let page = 2;
      while (collectionTotal !== null && collectionRows.length < collectionTotal) {
        const next = await getMaintenanceProjectWorkspace(projectId, {
          collection_page: page,
          collection_page_size: pageSize,
          requisition_page_size: 1,
          expense_page_size: 1,
        }, readTransport);
        if (seq !== workspaceRequestSeq.current || !readTransport.isCurrent()) return false;
        const nextRows = next.data.collection_snapshots.rows;
        collectionTotal = next.data.collection_snapshots.total;
        if (!nextRows.length) break;
        collectionRows.push(...nextRows);
        page += 1;
      }
      setCollectionRows(collectionRows);
      setOperationsProject(response.data.project);
      const wsMetrics = showOperationsHealth ? response.data.project.metrics : null;
      // 函数式更新：workspace 与 boss-board 并发完成时，绝不能用闭包里的初始
      // parts=0/null 覆盖 loadProject 刚写入的真实成本。
      setMetrics((prev) => ({
        received: wsMetrics?.received_amount ?? null,
        progress: wsMetrics?.collection_progress_pct ?? null,
        costs: {
          ...prev.costs,
          expense: wsMetrics?.approved_expense_inc_tax == null
            ? null : Number(wsMetrics?.approved_expense_inc_tax),
          issued: wsMetrics?.site_requisition_known_cost_inc_tax == null
            ? null : Number(wsMetrics?.site_requisition_known_cost_inc_tax),
          returned: null,
        },
      }));
      return true;
    } catch (err) {
      if (seq === workspaceRequestSeq.current && readTransport.isCurrent()) {
        setCollectionRows([]);
        setOperationsProject(null);
        setMetrics((prev) => ({
          received: null,
          progress: null,
          costs: {
            ...prev.costs,
            expense: null,
            issued: null,
            returned: null,
          },
        }));
        setWorkspaceError(readError(err, "回款状态加载失败"));
      }
      return false;
    } finally {
      if (seq === workspaceRequestSeq.current && readTransport.isCurrent()) setCollectionLoading(false);
    }
  }, [projectId, readTransport, readWorkspace, showOperationsHealth]);

  const registerTabRefresh = useCallback<RegisterPanelRefresh>((key, refresh) => {
    if (refresh) tabRefreshers.current.set(key, refresh);
    else tabRefreshers.current.delete(key);
  }, []);

  const refreshProject = useCallback(async () => {
    // 初始取数走独立 loader；此入口由业务成功后的 onChanged/onAfterApply 调用。
    publishMaintenanceChange();
    // 快照在调用开始时固定：父级两个读回 + 当前已挂载 tab 的读回都结束，才允许
    // WorkbookRoundTrip 报“已覆盖并刷新”。某个 tab 失败时它负责清空自己的旧值。
    const refreshers: PanelRefresh[] = [
      loadProject,
      loadWorkspace,
      ...tabRefreshers.current.values(),
    ];
    const results = await Promise.allSettled(refreshers.map((refresh) => refresh()));
    return results.every((result) =>
      result.status === "fulfilled" && result.value !== false);
  }, [loadProject, loadWorkspace]);

  useEffect(() => {
    void loadProject();
    void loadWorkspace();
    return () => {
      projectRequestSeq.current += 1;
      workspaceRequestSeq.current += 1;
    };
  }, [loadProject, loadWorkspace]);

  const lifecycle = row?.lifecycle ?? project?.lifecycle_status;
  const managerProject = project?.manager_assignment !== undefined ? project : projectModule.legacy ? operationsProject : null;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <PageHeader title={row?.display_name ?? project?.display_name ?? projectId}
        subtitle={
        <Space align="center" wrap>
          {/* 返回保持（v1.35 #N3）：有来路（含筛选后的项目墙）就原路返回；直达面板
              无历史时退化为默认墙。href 兜底给中键/新标签一个可用的落点。 */}
          <a
            href={analyticsReturn ?? "/maintenance"}
            onClick={(event) => {
              // Keep the href usable for new tabs and modifier-clicks after a refresh.
              if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
              event.preventDefault();
              if (analyticsReturn) {
                navigate(analyticsReturn);
                return;
              }
              const historyIdx = (window.history.state as { idx?: number } | null)?.idx;
              if (historyIdx != null && historyIdx > 0) navigate(-1);
              else navigate("/maintenance");
            }}
          >{analyticsReturn ? "← 返回分析 · 保留筛选" : "← 返回项目墙"}</a>
          {row?.is_archived ? <Tag>已归档</Tag> : null}
          {lifecycle ? <Tag>{LIFECYCLE_LABEL[lifecycle] ?? lifecycle}</Tag> : null}
          {(projectModule.legacy || canEditBasics) && <EditBasicsButton
            key={projectId}
            ref={editBasicsRef}
            projectId={projectId}
            disabled={!canEditBasics}
            onSaved={refreshProject}
          />}
          {canManageManagerAssignment && managerProject ? (
            <ProjectManagerAssignmentControl
              project={{ project_id: projectId, project_manager_id: managerProject.project_manager_id ?? null, manager_assignment: managerProject.manager_assignment ?? null }}
              canManage={canManageManagerAssignment}
              onChanged={refreshProject}
            />
          ) : null}
        </Space>}
        extra={<ProjectWorkbookRoundTrip projectId={projectId} surface="project_master"
          title="本项目总表" filename={`${exportBase}-总表.xlsx`} canUpload={legacyUpload}
          hint="六 sheet 一次下载，回填后整份上传覆盖" onAfterApply={refreshProject} />} />

      {error ? (
        <Alert type="error" showIcon message={error}
          action={<Button size="small" loading={projectLoading} onClick={() => void loadProject()}>重试</Button>} />
      ) : null}
      {canManageManagerAssignment && !canReadManagerRelationship && <Alert type="warning" showIcon
        message="缺少项目关系核对权限，无法维护负责人"
        description="负责人维护需要项目基本信息的读取权限，以核对当前关系和版本；请联系权限管理员。" />}
      {canManageManagerAssignment && canReadManagerRelationship && project && !managerProject && <Alert type="error" showIcon
        message="项目关系元数据未返回，无法维护负责人，请刷新后重试" />}
      {aggregateError ? (
        <Alert type="warning" showIcon message={aggregateError}
          action={<Button size="small" loading={projectLoading} onClick={() => void loadProject()}>重试</Button>} />
      ) : null}
      {workspaceError ? (
        <Alert type="error" showIcon message={workspaceError}
          action={<Button size="small" loading={collectionLoading} onClick={() => void loadWorkspace()}>重试</Button>} />
      ) : null}

      {(showSummary || showOperationsHealth) && <HealthBand row={row} metrics={metrics} />}

      <Tabs
        activeKey={activeTab}
        onChange={(tab) => {
          setSearchParams((previous) => {
            const next = new URLSearchParams(previous);
            const selected = readProjectPanelTab(tab);
            if (selected === "overview") next.delete("tab");
            else next.set("tab", selected);
            return next;
          }, { replace: true });
        }}
        items={[
          {
            key: "overview",
            label: "概览",
            children: (
              <OverviewTab
                projectId={projectId}
                row={row}
                project={project}
                operationsProject={operationsProject}
                canAssign={canManageProject}
                onAssigned={refreshProject}
                registerRefresh={registerTabRefresh}
                onEditBasics={canEditBasics ? () => editBasicsRef.current?.open() : undefined}
              />
            ),
          },
          {
            key: "analytics",
            label: "分析定位",
            children: activeTab === "analytics" ? (
              <MaintenanceProjectAnalyticsContext
                projectId={projectId}
                analyticsReturn={analyticsReturn}
              />
            ) : null,
          },
          {
            key: "parts-orders",
            label: "备件与需求单",
            children: (
              <PartsOrdersTab
                projectId={projectId}
                exportBase={exportBase}
                canUpload={canUpload}
                contractNos={row?.contract_nos ?? []}
                onChanged={refreshProject}
                registerRefresh={registerTabRefresh}
              />
            ),
          },
          {
            key: "expense",
            label: "报销",
            children: (
              <ExpenseTab
                projectId={projectId}
                exportBase={exportBase}
                canUpload={canUpload}
                onChanged={refreshProject}
                registerRefresh={registerTabRefresh}
              />
            ),
          },
          {
            key: "collection",
            label: "回款",
            children: (
              <CollectionTab
                projectId={projectId}
                exportBase={exportBase}
                canUpload={canUpload}
                rows={collectionRows}
                loading={collectionLoading}
                onRefresh={refreshProject}
                registerRefresh={registerTabRefresh}
              />
            ),
          },
          {
            key: "site",
            label: "领用与返还",
            children: (
              <SiteReturnTab
                projectId={projectId}
                exportBase={exportBase}
                canUpload={canUpload}
                onChanged={refreshProject}
                registerRefresh={registerTabRefresh}
              />
            ),
          },
          {
            key: "acceptance",
            label: "验收",
            children: (
              <AcceptanceTab
                projectId={projectId}
                canImport={canImportChecklist}
                onChanged={refreshProject}
              />
            ),
          },
        ].filter(item => availableTabs.includes(item.key)).map((item) => ({
          ...item,
          children: item.key === activeTab || projectModule.legacy ? <Suspense fallback={<Spin aria-label="正在加载项目明细" />}>{item.children}</Suspense> : null,
        }))}
      />
    </Space>
  );
}

/** 项目名旁的编辑入口：改名称、销售、期限与可见账号；负责人账号走独立 OCC 接口。 */
const EditBasicsButton = forwardRef<{ open: () => void }, {
  projectId: string;
  disabled: boolean;
  onSaved: () => Promise<boolean>;
}>(function EditBasicsButton({
  projectId,
  disabled,
  onSaved,
}, ref) {
  const { message } = App.useApp();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const currentValues = Form.useWatch([], form);
  const readTransport = useContextTransport(`${projectId}:edit-basic-read`, true);
  const writeTransport = useContextTransport(JSON.stringify([projectId, open, currentValues]), true);
  const admission = useContextAdmission();
  const canReadAccounts = admission.surface("maintenance_manager_directory.candidates", true) !== "deny";
  const [saving, setSaving] = useState(false);
  const [accounts, setAccounts] = useState<MaintenanceManagerAccount[]>([]);
  const salespersonEditedRef = useRef(false);
  const businessTypeEditedRef = useRef(false);
  const noteEditedRef = useRef(false);
  const projectNoteEditedRef = useRef(false);
  const requestSeq = useRef(0);

  useEffect(() => () => { requestSeq.current += 1; }, []);

  const openModal = async () => {
    if (disabled) return;
    const seq = ++requestSeq.current;
    try {
      const resp = await getMaintenanceProject(projectId, readTransport);
      if (seq !== requestSeq.current) return;
      // 后端契约是 {project: {...}}（MaintenanceProjectOverview）
      const proj = resp.data.project;
      // Form 实例跨 Modal 开关持久存在；每次回显前清掉上一次编辑的值和
      // touched 元数据，避免旧会话的销售编辑污染本次 PATCH。
      form.resetFields();
      salespersonEditedRef.current = false;
      businessTypeEditedRef.current = false;
      noteEditedRef.current = false;
      projectNoteEditedRef.current = false;
      form.setFieldsValue({
        display_name: proj.display_name,
        salesperson: proj.salesperson,
        business_type: proj.business_type?.trim() || "",
        no_return_note: proj.no_return_note ?? "",
        project_note: proj.project_note ?? "",
        version: proj.version,
        period:
          proj.period_from || proj.period_to
            ? [
                proj.period_from ? dayjs(proj.period_from) : null,
                proj.period_to ? dayjs(proj.period_to) : null,
              ]
            : null,
        // 项目级可见账号（2026-08-25）：回显当前 viewer 名单
        visible_usernames: proj.visible_usernames ?? [],
      });
      // 加载系统内账号供项目可见范围选择。负责人账号改派由页头独立的
      // primary_manager OCC 控件完成，不能伪装成基础字段更新。
      try {
        if (!canReadAccounts) { setAccounts([]); setOpen(true); return; }
        // 评审阻塞点：账号列表只取前 100 无法选到后面的活跃账号——
        // 拉满 500（后端上限），超出极端规模再考虑远程搜索。
        const accountsResp = await searchMaintenanceManagerAccounts(
          { page: 1, page_size: 500 }, {}, readTransport);
        if (seq !== requestSeq.current) return;
        setAccounts(accountsResp.data.rows);
      } catch {
        if (seq !== requestSeq.current) return;
        setAccounts([]);
      }
      setOpen(true);
    } catch (err) {
      if (seq === requestSeq.current) message.error(readError(err, "读取项目主档失败"));
    }
  };
  useImperativeHandle(ref, () => ({ open: () => { void openModal(); } }));

  const submit = async () => {
    let values: Record<string, unknown>;
    try {
      values = await form.validateFields();
    } catch {
      // Form 已显示字段错误；不要把校验拒绝抛成未处理 Promise。
      return;
    }
    if (disabled || !writeTransport.isCurrent()) return;
    const seq = requestSeq.current;
    const { period, salesperson, business_type, no_return_note, project_note, ...rest } = values as {
      period?: [Dayjs | null, Dayjs | null] | null;
    } & Record<string, unknown>;
    const payload = {
      ...rest,
      ...(salespersonEditedRef.current
        ? { salesperson: salesperson === "" || salesperson == null ? null : salesperson }
        : {}),
      // 只提交用户明确修改的类型，保留未编辑的历史自由文本与旧接口缺字段。
      ...(businessTypeEditedRef.current ? { business_type: business_type ?? "" } : {}),
      ...(noteEditedRef.current ? {
        no_return_note: typeof no_return_note === "string" && no_return_note.trim()
          ? no_return_note : null,
      } : {}),
      ...(projectNoteEditedRef.current ? {
        project_note: typeof project_note === "string" && project_note.trim()
          ? project_note : null,
      } : {}),
      // 期限整组提交（#39/#51）；清空即回 missing
      period_from: period?.[0] ? period[0].format("YYYY-MM-DD") : null,
      period_to: period?.[1] ? period[1].format("YYYY-MM-DD") : null,
      reason: "面板编辑基本信息",
    };
    setSaving(true);
    try {
      await updateMaintenanceProject(
        projectId,
        payload as Parameters<typeof updateMaintenanceProject>[1], writeTransport,
      );
    } catch (err) {
      if (seq === requestSeq.current && writeTransport.isCurrent()) message.error(readError(err, "保存失败"));
      return;
    } finally {
      if (seq === requestSeq.current) setSaving(false);
    }
    if (seq !== requestSeq.current || !writeTransport.isCurrent()) return;
    setOpen(false);
    // PATCH 已提交，读回回调拒绝不能将成功的写入误报为“保存失败”。
    let refreshed = false;
    try {
      refreshed = await onSaved();
    } catch {
      // 下方独立报告刷新失败；不假定意外拒绝时旧快照已经失效。
    }
    if (seq !== requestSeq.current) return;
    if (refreshed) message.success("已保存并刷新");
    else message.warning("已保存，但页面刷新失败，请重试。");
  };

  return (
    <>
      <Button
        size="small"
        icon={<EditOutlined />}
        disabled={disabled}
        onClick={openModal}
      >
        编辑基本信息
      </Button>
      <Modal
        open={open}
        title="编辑项目基本信息"
        onCancel={() => { if (!saving) setOpen(false); }}
        onOk={submit}
        confirmLoading={saving}
        cancelButtonProps={{ disabled: saving }}
        closable={!saving}
        maskClosable={!saving}
        destroyOnHidden
      >
        <Form
          form={form}
          layout="vertical"
          onValuesChange={(changedValues) => {
            if (Object.prototype.hasOwnProperty.call(changedValues, "salesperson")) {
              salespersonEditedRef.current = true;
            }
            if (Object.prototype.hasOwnProperty.call(changedValues, "business_type")) {
              businessTypeEditedRef.current = true;
            }
            if (Object.prototype.hasOwnProperty.call(changedValues, "no_return_note")) {
              noteEditedRef.current = true;
            }
            if (Object.prototype.hasOwnProperty.call(changedValues, "project_note")) {
              projectNoteEditedRef.current = true;
            }
          }}
        >
          <Form.Item name="display_name" label="项目名称">
            <Input />
          </Form.Item>
          <Form.Item name="salesperson" label="销售人员">
            <Input allowClear maxLength={64} placeholder="可手工填写或清空" />
          </Form.Item>
          <Form.Item
            name="no_return_note"
            label="免返标注"
            extra="仅作提醒，返还要求以明细标记和规则为准。清空后显示未标注。"
            rules={[{ validator: async (_, value: string | undefined) => {
              if (Array.from(value ?? "").length > 2000) throw new Error("免返标注最多 2000 个字符");
            } }]}
          >
            <Input.TextArea autoSize={{ minRows: 3, maxRows: 8 }} allowClear
              showCount={{ formatter: ({ value }) => `${Array.from(value).length} / 2000` }} />
          </Form.Item>
          <Form.Item
            name="project_note"
            label="项目备注"
            extra="可自由记录项目经历和标记，最多 500 个字符；也可在项目总表的 01_项目概览中修改。"
            rules={[{ validator: async (_, value: string | undefined) => {
              if (Array.from(value ?? "").length > 500) throw new Error("项目备注最多 500 个字符");
            } }]}
          >
            <Input.TextArea autoSize={{ minRows: 3, maxRows: 8 }} allowClear
              showCount={{ formatter: ({ value }) => `${Array.from(value).length} / 500` }} />
          </Form.Item>
          <Form.Item name="business_type" label="业务类型">
            <Select
              options={[
                { label: "整体维保", value: "整体维保" },
                { label: "备件维保", value: "备件维保" },
                { label: "算力运维", value: "算力运维" },
                { label: "拆改配服务", value: "拆改配服务" },
                { label: "非维保", value: "非维保" },
                { label: "未标注", value: "" },
              ]}
            />
          </Form.Item>
          {/* #39/#51：起止时间可编辑；台账导入会以台账为权威覆盖 */}
          <Form.Item name="period" label="维保期限（起止）">
            <DatePicker.RangePicker style={{ width: "100%" }} allowEmpty={[true, true]} />
          </Form.Item>
          {/* 2026-08-25：项目级可见账号多选——被行级隔离的账号勾选后即可
              看到本项目（负责人∪销售∪可见账号）；管理员/老板不受影响。 */}
          <Form.Item
            name="visible_usernames"
            label="项目可见账号（多选）"
            tooltip="选中的账号无论角色都能看到本项目；不选则按默认可见性规则"
          >
            <Select
              mode="multiple"
              allowClear
              showSearch
              placeholder="选择需要看到本项目的账号"
              optionFilterProp="label"
              options={accounts.map((account) => ({
                value: account.username,
                label: account.display_name
                  ? `${account.username} · ${account.display_name}`
                  : account.username,
              }))}
            />
          </Form.Item>
          <Form.Item name="version" hidden>
            <Input />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
});

const BoundProjectPanel = withContextSurface(MaintenanceProjectPanelContent, routeRule("/maintenance/projects/:projectId"), props => props.projectId);
export const MaintenanceProjectPanelPage = MaintenanceProjectPanelPageBody;
export default MaintenanceProjectPanelPage;
