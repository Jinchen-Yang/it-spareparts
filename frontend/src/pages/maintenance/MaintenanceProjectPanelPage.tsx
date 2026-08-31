import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  Col,
  DatePicker,
  Flex,
  Form,
  Input,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { EditOutlined } from "@ant-design/icons";
import type { BoardProjectRow } from "../../api/maintenanceBossBoard";
import { getBoardProject } from "../../api/maintenanceBossBoard";
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
import WorkbookRoundTrip from "../../components/maintenance/WorkbookRoundTrip";
import { readPermissionMap } from "../../nav";
import OverviewTab from "./panel/OverviewTab";
import PartsOrdersTab from "./panel/PartsOrdersTab";
import ExpenseTab from "./panel/ExpenseTab";
import CollectionTab from "./panel/CollectionTab";
import SiteReturnTab from "./panel/SiteReturnTab";
import AcceptanceTab from "./panel/AcceptanceTab";
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

const { Text, Title } = Typography;

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
    { label: "备件成本", value: metrics.costs.parts, color: "#1677ff",
      lowerBound: partsIsLowerBound,
      hint: "本项目挂靠需求单明细的已知备件成本（含税）" },
    { label: "报销成本", value: metrics.costs.expense, color: "#fa8c16",
      hint: "已批准报销（含税）" },
    { label: "已领用成本", value: metrics.costs.issued, color: "#722ed1",
      hint: "现场领用（已确认/已更正）的已知成本（含税）" },
    { label: "返还成本", value: metrics.costs.returned, color: "#13c2c2",
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
            value={metrics.received == null ? "数据不足" : `¥${metrics.received.toFixed(2)}`}
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
                    : `¥${line.value.toFixed(2)}${line.lowerBound ? "（已知下限）" : ""}`}
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
 * 取数：基础信息以 stable 目录为准；成本率/合同额用**项目名称**回查 boss-board
 * 聚合行（按 UUID 搜永远搜不到，2026-08-17 取数缺陷的修复口径）；回款指标走
 * getMaintenanceProjectWorkspace。聚合行缺失时健康带「聚合数据暂缺」，不全页报错。
 */
export function MaintenanceProjectPanelPage() {
  const { projectId = "" } = useParams();
  // React Router 会复用同一个 route element。以项目 ID 强制重建内部状态，确保
  // A 项目的在途请求、tab 筛选和旧金额都不能进入 B 项目的操作上下文。
  return <MaintenanceProjectPanelContent key={projectId} projectId={projectId} />;
}

function MaintenanceProjectPanelContent({ projectId }: { projectId: string }) {
  const [row, setRow] = useState<BoardProjectRow | null>(null);
  const [project, setProject] = useState<MaintenanceProject | null>(null);
  const [operationsProject, setOperationsProject] =
    useState<MaintenanceProjectOperationsSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
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
  const canUpload = !!perms.action_maintenance_expense_collection_upload;
  const canManageProject = !!perms.action_maintenance_project_manage;
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
    try {
      const detail = await getMaintenanceProject(projectId);
      const stable = detail.data?.project ?? null;
      if (seq !== projectRequestSeq.current) return false;
      setProject(stable);
      let hit: BoardProjectRow | null = null;
      let aggregateReady = true;
      if (stable) {
        try {
          hit = (await getBoardProject(projectId)).data;
        } catch {
          // stable 项目仍可展示基础信息；聚合卡可能因归档无活动或来源暂不可用
          // 而不存在，不能让整个详情页随之白屏。
          hit = null;
          aggregateReady = false;
        }
      }
      if (seq !== projectRequestSeq.current) return false;
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
      if (!hit && !stable) setError("项目不存在或无权查看");
      return Boolean(stable) && aggregateReady;
    } catch (err) {
      if (seq === projectRequestSeq.current) {
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
    }
  }, [projectId]);

  // 回款指标 + 快照行：健康带与「回款」tab 共用同一份 workspace 数据。
  const loadWorkspace = useCallback(async () => {
    const seq = ++workspaceRequestSeq.current;
    setCollectionLoading(true);
    try {
      const pageSize = 100;
      const response = await getMaintenanceProjectWorkspace(projectId, {
        collection_page: 1,
        collection_page_size: pageSize,
        requisition_page_size: 1,
        expense_page_size: 1,
      });
      if (seq !== workspaceRequestSeq.current) return false;
      const collectionRows = [...response.data.collection_snapshots.rows];
      let collectionTotal = response.data.collection_snapshots.total;
      let page = 2;
      while (collectionRows.length < collectionTotal) {
        const next = await getMaintenanceProjectWorkspace(projectId, {
          collection_page: page,
          collection_page_size: pageSize,
          requisition_page_size: 1,
          expense_page_size: 1,
        });
        if (seq !== workspaceRequestSeq.current) return false;
        const nextRows = next.data.collection_snapshots.rows;
        collectionTotal = next.data.collection_snapshots.total;
        if (!nextRows.length) break;
        collectionRows.push(...nextRows);
        page += 1;
      }
      setCollectionRows(collectionRows);
      setOperationsProject(response.data.project);
      const wsMetrics = response.data.project.metrics;
      // 函数式更新：workspace 与 boss-board 并发完成时，绝不能用闭包里的初始
      // parts=0/null 覆盖 loadProject 刚写入的真实成本。
      setMetrics((prev) => ({
        received: wsMetrics.received_amount,
        progress: wsMetrics.collection_progress_pct,
        costs: {
          ...prev.costs,
          expense: wsMetrics.approved_expense_inc_tax == null
            ? null : Number(wsMetrics.approved_expense_inc_tax),
          issued: wsMetrics.site_requisition_known_cost_inc_tax == null
            ? null : Number(wsMetrics.site_requisition_known_cost_inc_tax),
          returned: null,
        },
      }));
      return true;
    } catch (err) {
      if (seq === workspaceRequestSeq.current) {
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
        message.error(readError(err, "回款状态加载失败"));
      }
      return false;
    } finally {
      if (seq === workspaceRequestSeq.current) setCollectionLoading(false);
    }
  }, [projectId]);

  const registerTabRefresh = useCallback<RegisterPanelRefresh>((key, refresh) => {
    if (refresh) tabRefreshers.current.set(key, refresh);
    else tabRefreshers.current.delete(key);
  }, []);

  const refreshProject = useCallback(async () => {
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

  const refreshAfterChange = useCallback(async () => {
    if (!(await refreshProject())) {
      message.warning("操作已写入，但页面刷新失败；旧数据已失效，请点击重试。");
    }
  }, [refreshProject]);

  useEffect(() => {
    void loadProject();
    void loadWorkspace();
    return () => {
      projectRequestSeq.current += 1;
      workspaceRequestSeq.current += 1;
    };
  }, [loadProject, loadWorkspace]);

  const lifecycle = row?.lifecycle ?? project?.lifecycle_status;
  const canManageManagerAssignment =
    localStorage.getItem("role") === "admin" && canManageProject;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Flex justify="space-between" align="flex-start" wrap gap={12}>
        <Space align="center" wrap>
          <Link to="/maintenance">← 返回项目墙</Link>
          <Title level={4} style={{ margin: 0 }}>
            {row?.display_name ?? project?.display_name ?? projectId}
          </Title>
          {row?.is_archived ? <Tag>已归档</Tag> : null}
          {lifecycle ? <Tag>{LIFECYCLE_LABEL[lifecycle] ?? lifecycle}</Tag> : null}
          <EditBasicsButton
            projectId={projectId}
            disabled={!canManageProject}
            onSaved={() => { void refreshAfterChange(); }}
          />
          {operationsProject ? (
            <ProjectManagerAssignmentControl
              project={operationsProject}
              canManage={canManageManagerAssignment}
              onChanged={refreshProject}
            />
          ) : null}
        </Space>
        <WorkbookRoundTrip
          size="small"
          title="本项目总表"
          filename={`${exportBase}-总表.xlsx`}
          canUpload={canUpload}
          hint="六 sheet 一次下载，回填后整份上传覆盖"
          onDownload={() => downloadProjectMaster(projectId)}
          onValidate={(file) => validateProjectMaster(projectId, file)}
          onApply={(file) => applyProjectMaster(projectId, file)}
          onAfterApply={refreshProject}
        />
      </Flex>

      {error ? <Alert type="error" showIcon message={error} /> : null}

      <HealthBand row={row} metrics={metrics} />

      <Tabs
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
              />
            ),
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
        ]}
      />
    </Space>
  );
}

/** 项目名旁的编辑入口：改名称、销售、期限与可见账号；负责人账号走独立 OCC 接口。 */
function EditBasicsButton({
  projectId,
  disabled,
  onSaved,
}: {
  projectId: string;
  disabled: boolean;
  onSaved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [accounts, setAccounts] = useState<MaintenanceManagerAccount[]>([]);

  const openModal = async () => {
    try {
      const resp = await getMaintenanceProject(projectId);
      // 后端契约是 {project: {...}}（MaintenanceProjectOverview）
      const proj = resp.data.project;
      form.setFieldsValue({
        display_name: proj.display_name,
        salesperson: proj.salesperson,
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
        // 评审阻塞点：账号列表只取前 100 无法选到后面的活跃账号——
        // 拉满 500（后端上限），超出极端规模再考虑远程搜索。
        const accountsResp = await searchMaintenanceManagerAccounts(
          { page: 1, page_size: 500 });
        setAccounts(accountsResp.data.rows);
      } catch {
        setAccounts([]);
      }
      setOpen(true);
    } catch (err) {
      message.error(readError(err, "读取项目主档失败"));
    }
  };

  const submit = async () => {
    const values = await form.validateFields();
    const { period, ...rest } = values as {
      period?: [Dayjs | null, Dayjs | null] | null;
    } & Record<string, unknown>;
    const payload = {
      ...rest,
      // 期限整组提交（#39/#51）；清空即回 missing
      period_from: period?.[0] ? period[0].format("YYYY-MM-DD") : null,
      period_to: period?.[1] ? period[1].format("YYYY-MM-DD") : null,
      reason: "面板编辑基本信息",
    };
    setSaving(true);
    try {
      await updateMaintenanceProject(
        projectId,
        payload as Parameters<typeof updateMaintenanceProject>[1],
      );
      message.success("已保存");
      setOpen(false);
      onSaved();
    } catch (err) {
      message.error(readError(err, "保存失败"));
    } finally {
      setSaving(false);
    }
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
        onCancel={() => setOpen(false)}
        onOk={submit}
        confirmLoading={saving}
        destroyOnHidden
      >
        <Form form={form} layout="vertical">
          <Form.Item name="display_name" label="项目名称">
            <Input />
          </Form.Item>
          <Form.Item name="salesperson" label="销售人员">
            <Input allowClear maxLength={64} placeholder="可手工填写或清空" />
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
}

export default MaintenanceProjectPanelPage;
