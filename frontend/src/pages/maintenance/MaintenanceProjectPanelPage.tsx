import { useCallback, useEffect, useState } from "react";
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
  Typography,
  message,
} from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { EditOutlined } from "@ant-design/icons";
import type { BoardProjectRow } from "../../api/maintenanceBossBoard";
import { searchBoardProjects } from "../../api/maintenanceBossBoard";
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
import { listAccounts } from "../../api/accounts";
import type { Account } from "../../api/accounts";
import { getMaintenanceProjectWorkspace } from "../../api/maintenanceOperations";
import type { MaintenanceCollectionSnapshotRow } from "../../api/maintenanceOperations";
import WorkbookRoundTrip from "../../components/maintenance/WorkbookRoundTrip";
import { readPermissionMap } from "../../nav";
import OverviewTab from "./panel/OverviewTab";
import PartsOrdersTab from "./panel/PartsOrdersTab";
import ExpenseTab from "./panel/ExpenseTab";
import CollectionTab from "./panel/CollectionTab";
import SiteReturnTab from "./panel/SiteReturnTab";
import {
  LIFECYCLE_LABEL,
  STATUS_COLOR,
  readError,
  safeFilenamePart,
  statText,
} from "./panel/panelUtils";

const { Title } = Typography;

interface HealthMetrics {
  received: number | null;
  progress: number | null;
}

const HEALTH_VALUE_STYLE = { fontSize: 18, fontWeight: 600 } as const;

/**
 * 项目健康带（2026-08-19 重设计的 signature 区块）：一行四格——
 * 合同额、累计已回款、回款进度、成本率。任何一格算不出来就说人话，绝不落 0（铁律 5）；
 * 聚合行缺失（撞名/未入板）整格「聚合数据暂缺」，不影响下方明细。
 */
function HealthBand({ row, metrics }: { row: BoardProjectRow | null; metrics: HealthMetrics }) {
  const contractText = row ? statText(row.contract_amount_inc_tax) : "聚合数据暂缺";

  let ratioText: string;
  let ratioColor: string | undefined;
  if (!row) {
    ratioText = "聚合数据暂缺";
  } else if (row.cost_ratio_pct?.state === "ready") {
    if (row.cost_ratio_pct.value === null || row.cost_ratio_pct.value === "") {
      ratioText = "数据不足";
    } else {
      ratioText = `${row.cost_ratio_pct.value}%`;
      ratioColor = row.card_status ? STATUS_COLOR[row.card_status] : undefined;
    }
  } else {
    ratioText = statText(row.cost_ratio_pct);
  }

  return (
    <Card size="small" data-testid="panel-health-band">
      <Row gutter={16}>
        <Col xs={12} sm={6}>
          <Statistic title="合同额" value={contractText} valueStyle={HEALTH_VALUE_STYLE} />
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
  const [row, setRow] = useState<BoardProjectRow | null>(null);
  const [project, setProject] = useState<MaintenanceProject | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [collectionRows, setCollectionRows] = useState<MaintenanceCollectionSnapshotRow[]>([]);
  const [collectionLoading, setCollectionLoading] = useState(false);
  const [metrics, setMetrics] = useState<HealthMetrics>({ received: null, progress: null });

  const perms = readPermissionMap();
  const canUpload = !!perms.action_maintenance_expense_collection_upload;
  const canManageProject = !!perms.action_maintenance_project_manage;

  // 下载文件名 = XSDD销售订单号（取第一个） + 维保项目名 + 表单类型（2026-08-17）
  const exportBase = (() => {
    const xsdd = row?.contract_nos?.[0] ?? project?.project_code ?? projectId;
    const name = row?.display_name ?? project?.display_name ?? projectId;
    return `${xsdd}-${safeFilenamePart(name)}`;
  })();

  const loadProject = useCallback(async () => {
    setError(null);
    try {
      const detail = await getMaintenanceProject(projectId);
      const stable = detail.data?.project ?? null;
      setProject(stable);
      let hit: BoardProjectRow | null = null;
      if (stable?.display_name) {
        const resp = await searchBoardProjects({
          q: stable.display_name.slice(0, 128),
          page_size: 50,
        });
        hit = resp.data.rows.find((item) => item.project_id === projectId) ?? null;
      }
      setRow(hit);
      if (!hit && !stable) setError("项目不存在或无权查看");
    } catch (err) {
      setError(readError(err, "项目面板加载失败"));
    }
  }, [projectId]);

  // 回款指标 + 快照行：健康带与「回款」tab 共用同一份 workspace 数据。
  const loadWorkspace = useCallback(async () => {
    setCollectionLoading(true);
    try {
      const response = await getMaintenanceProjectWorkspace(projectId, {
        collection_page: 1,
        collection_page_size: 100,
        requisition_page_size: 1,
        expense_page_size: 1,
      });
      setCollectionRows(response.data.collection_snapshots.rows);
      setMetrics({
        received: response.data.project.metrics.received_amount,
        progress: response.data.project.metrics.collection_progress_pct,
      });
    } catch (err) {
      message.error(readError(err, "回款状态加载失败"));
    } finally {
      setCollectionLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadProject();
    void loadWorkspace();
  }, [loadProject, loadWorkspace]);

  const lifecycle = row?.lifecycle ?? project?.lifecycle_status;

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
            onSaved={loadProject}
          />
        </Space>
        <WorkbookRoundTrip
          size="small"
          title="本项目总表"
          filename={`${exportBase}-总表.xlsx`}
          canUpload={canUpload}
          hint="六 sheet 一次下载，回填后整份上传覆盖"
          onDownload={() => downloadProjectMaster(projectId)}
          onValidate={(file) => validateProjectMaster(projectId, file)}
          onApply={async (file) => {
            const result = await applyProjectMaster(projectId, file);
            await Promise.all([loadProject(), loadWorkspace()]);
            return result;
          }}
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
                canAssign={canManageProject}
                onAssigned={loadProject}
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
              />
            ),
          },
          {
            key: "expense",
            label: "报销",
            children: (
              <ExpenseTab projectId={projectId} exportBase={exportBase} canUpload={canUpload} />
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
                onRefresh={loadWorkspace}
              />
            ),
          },
          {
            key: "site",
            label: "领用与返还",
            children: (
              <SiteReturnTab projectId={projectId} exportBase={exportBase} canUpload={canUpload} />
            ),
          },
        ]}
      />
    </Space>
  );
}

/** 项目名旁的编辑入口：改起止时间/负责人等，与表 6 sheet 01 联通（#39）。 */
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
  const [accounts, setAccounts] = useState<Account[]>([]);

  const openModal = async () => {
    try {
      const resp = await getMaintenanceProject(projectId);
      // 后端契约是 {project: {...}}（MaintenanceProjectOverview）
      const proj = resp.data.project;
      form.setFieldsValue({
        display_name: proj.display_name,
        project_manager_id: proj.project_manager_id,
        version: proj.version,
        period:
          proj.period_from || proj.period_to
            ? [
                proj.period_from ? dayjs(proj.period_from) : null,
                proj.period_to ? dayjs(proj.period_to) : null,
              ]
            : null,
      });
      // 加载系统内账号供「维保负责人」下拉选择
      try {
        const accountsResp = await listAccounts();
        setAccounts(accountsResp.data);
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
          <Form.Item name="project_manager_id" label="维保负责人">
            <Select
              allowClear
              showSearch
              placeholder="选择系统内账号/人名"
              optionFilterProp="label"
              options={accounts.map((account) => ({
                value: account.username,
                label: account.display_name
                  ? `${account.username} · ${account.display_name}`
                  : account.username,
              }))}
            />
          </Form.Item>
          {/* #39/#51：起止时间可编辑；台账导入会以台账为权威覆盖 */}
          <Form.Item name="period" label="维保期限（起止）">
            <DatePicker.RangePicker style={{ width: "100%" }} allowEmpty={[true, true]} />
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
