import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  Alert,
  Button,
  Card,
  DatePicker,
  Descriptions,
  Form,
  Input,
  Modal,
  Progress,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { EditOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import type {
  BoardLineRow,
  BoardOrderRow,
  BoardProjectRow,
} from "../../api/maintenanceBossBoard";
import {
  getBoardOrderLines,
  getBoardProjectOrders,
  searchBoardProjects,
} from "../../api/maintenanceBossBoard";
import type { ProjectExpenseRow, ProjectPartsRow } from "../../api/maintenanceWorkbooks";
import {
  SHEETS,
  applyProjectMaster,
  downloadProjectMaster,
  listProjectExpenseRows,
  listProjectPartsRows,
} from "../../api/maintenanceWorkbooks";
import {
  getMaintenanceProject,
  updateMaintenanceProject,
} from "../../api/maintenanceProjects";
import type { MaintenanceProject } from "../../api/maintenanceProjects";
import { listAccounts } from "../../api/accounts";
import type { Account } from "../../api/accounts";
import {
  assignMaintenanceSourceOrders,
  autoAssignMaintenanceSourceOrders,
  listMaintenanceSourceOrders,
} from "../../api/maintenanceSourceAssignments";
import type { MaintenanceSourceOrderRow } from "../../api/maintenanceSourceAssignments";
import type {
  MaintenanceBadReturn,
  MaintenanceCollectionSnapshotRow,
  MaintenanceReturnObligation,
  SiteIssueDocument,
  SiteIssueLine,
} from "../../api/maintenanceOperations";
import {
  getMaintenanceProjectWorkspace,
  searchMaintenanceBadReturns,
  searchMaintenanceReturnObligations,
  searchSiteIssues,
} from "../../api/maintenanceOperations";
import WorkbookRoundTrip from "../../components/maintenance/WorkbookRoundTrip";
import { readPermissionMap } from "../../nav";

const { Text, Title } = Typography;

/** 导出文件名片段清洗：去掉路径/非法字符，避免项目名破坏文件名（2026-08-17）。 */
function safeFilenamePart(value: string): string {
  return value.replace(/[\\/:*?"<>|\r\n\t]+/g, "_").trim().replace(/\.+$/, "") || "项目";
}

/** 数值渲染：非 ready 状态一律说人话，绝不落回 0（铁律 5）。 */
function statText(stat: { state: string; value: unknown } | undefined): string {
  if (!stat) return "—";
  if (stat.state === "not_imported") return "尚未导入";
  if (stat.state === "restricted") return "无权限";
  if (stat.state === "error") return "暂不可用";
  return stat.value === null || stat.value === "" ? "—" : String(stat.value);
}

function raw(value: unknown) {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

const COST_SOURCE_LABEL: Record<string, string> = {
  direct: "直接采购价",
  window: "7天采购窗口",
  purchase_history: "采购历史",
  pool_purchase: "备件池采购",
  sales_history: "销售历史",
  pool_sales: "备件池销售",
  month_avg: "月均价",
  none: "暂无成本",
};

function CostSourceTag({ row }: { row: ProjectPartsRow }) {
  const confidence = row.confidence ?? (row.cost_source === "none" ? "none" : null);
  const color = confidence === "high" ? "green"
    : confidence === "medium" ? "orange"
      : confidence === "low" ? "red" : "default";
  const label = row.cost_source_label || COST_SOURCE_LABEL[row.cost_source || ""] || raw(row.cost_source);
  const suffix = row.missing_kind === "out_of_scope" ? "（起算日前）"
    : row.missing_kind === "none" ? "（未找到）" : "";
  return <Tag color={color}>{label}{suffix}</Tag>;
}

const LIFECYCLE_LABEL: Record<string, string> = {
  ongoing: "进行中",
  ended: "已结束",
  missing: "期限缺失",
};

/** 三态色与卡墙一致（#35/#43）：<80% 绿、80–100% 黄、>100% 红。 */
const STATUS_COLOR: Record<string, string> = {
  normal: "#52c41a",
  warning: "#faad14",
  alert: "#ff4d4f",
};

const COLLECTION_STATUS: Record<string, { label: string; color: string }> = {
  confirmed: { label: "已确认", color: "green" },
  unconfirmed: { label: "待确认", color: "gold" },
  void: { label: "已作废", color: "default" },
};

const ISSUE_STATUS: Record<string, { label: string; color: string }> = {
  draft: { label: "领用草稿", color: "gold" },
  confirmed: { label: "领用已确认", color: "green" },
  corrected: { label: "领用已更正", color: "blue" },
  void: { label: "领用已作废", color: "default" },
};

const RETURN_DOCUMENT_STATUS: Record<string, string> = {
  draft: "返还草稿",
  submitted: "已提交返还",
  in_transit: "返还在途",
  warehouse_confirmed: "仓库已确认",
  void: "返还已作废",
};

/** 成本÷合同额进度条（#35）：算不出来就说算不出来，不画 0% 的绿条（铁律 5）。 */
function CostRatioBar({ row }: { row: BoardProjectRow | null }) {
  const stat = row?.cost_ratio_pct;
  const ratio =
    stat?.state === "ready" && stat.value !== null ? Number(stat.value) : null;
  if (ratio === null) {
    return (
      <Text type="secondary" style={{ fontSize: 12 }} data-testid="panel-ratio-unknown">
        数据不足（缺合同额或成本）
      </Text>
    );
  }
  return (
    <Progress
      percent={Math.min(ratio, 100)}
      strokeColor={row?.card_status ? STATUS_COLOR[row.card_status] : undefined}
      size="small"
      style={{ maxWidth: 420 }}
      format={() => `${ratio}%`}
    />
  );
}

/**
 * 项目面板——页面定稿两页之二（REQUIREMENTS #33/#38/#39/#45）。
 *
 * 顶部＝出库明细（按合同筛选）；子 tab＝表 6 的 web 呈现
 * （基础信息/备件成本/报销/回款/维保领用与返还）；
 * tab 栏右侧下载**本项目总表**，每个 tab 内单独下载对应 sheet——**在哪下载就在哪上传**。
 * 归属挂靠在「基础信息」tab（#45：判定依据＝XSDD 销售订单）。
 */
export function MaintenanceProjectPanelPage() {
  const { projectId = "" } = useParams();
  const [row, setRow] = useState<BoardProjectRow | null>(null);
  const [project, setProject] = useState<MaintenanceProject | null>(null);
  const [orders, setOrders] = useState<BoardOrderRow[]>([]);
  const [contractFilter, setContractFilter] = useState<string | undefined>();
  const [selectedOrder, setSelectedOrder] = useState<string | null>(null);
  const [lines, setLines] = useState<BoardLineRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

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
    setLoading(true);
    setError(null);
    try {
      // 基础信息以 stable 目录为准；卡墙聚合行（成本率/合同额，口径与项目墙一致）
      // 用**项目名称**回查 boss-board 搜索——按 UUID 搜名称/编号/合同号永远搜不到，
      // 这正是 2026-08-17 面板全「—」的取数缺陷。
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
      const ordersResp = await getBoardProjectOrders(projectId, { page_size: 200 });
      setOrders(ordersResp.data.rows);
    } catch (err) {
      setError(readError(err, "项目面板加载失败"));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadProject();
  }, [loadProject]);

  useEffect(() => {
    if (!selectedOrder) {
      setLines([]);
      return;
    }
    void getBoardOrderLines(selectedOrder, { page_size: 200 })
      .then((resp) => setLines(resp.data.rows))
      .catch((err) => message.error(readError(err, "明细加载失败")));
  }, [selectedOrder]);

  const contractNos = row?.contract_nos ?? [];
  const shownOrders = contractFilter
    ? orders.filter((order) => order.order_no.includes(contractFilter)
        || (order.project_raw ?? "").includes(contractFilter))
    : orders;

  const orderColumns: ColumnsType<BoardOrderRow> = [
    {
      title: "需求单号",
      dataIndex: "order_no",
      render: (value: string, order) => (
        <a onClick={() => setSelectedOrder(order.source_order_id)}>{value}</a>
      ),
    },
    { title: "制单日期", dataIndex: "order_date", render: raw },
    { title: "数据状态", dataIndex: "data_status", render: raw },
    { title: "明细行", dataIndex: "line_count" },
    {
      title: "已知申请估算成本(含税)",
      render: (_: unknown, order) =>
        order.known_apply_cost_inc_tax.state === "ready"
          ? String(order.known_apply_cost_inc_tax.value?.known_amount ?? "—")
          : statText(order.known_apply_cost_inc_tax),
    },
    {
      title: "实发（项目口径）",
      render: (_: unknown, order) => statText(order.facts.shipped_qty),
    },
  ];

  const lineColumns: ColumnsType<BoardLineRow> = [
    { title: "PN", dataIndex: "pn_std", render: (v, r) => raw(v || r.pn_raw) },
    { title: "描述", dataIndex: "description", render: raw },
    { title: "需求", dataIndex: "qty", render: raw },
    // 以下为流转状态列：原样展示，不参与任何计算（铁律 3）
    { title: "已采", dataIndex: "purchased_qty", render: raw },
    { title: "待供", dataIndex: "pending_supply_qty", render: raw },
    { title: "待返", dataIndex: "pending_return_qty", render: raw },
    { title: "领用", dataIndex: "consumed_qty", render: raw },
    {
      title: "已知申请估算成本(含税)",
      render: (_: unknown, line) => statText(line.known_apply_cost_inc_tax),
    },
    { title: "取价来源", render: (_: unknown, line) => statText(line.cost_source) },
  ];

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Space align="center" wrap>
        <Link to="/maintenance">← 返回项目墙</Link>
        <Title level={4} style={{ margin: 0 }}>
          {row?.display_name ?? project?.display_name ?? projectId}
        </Title>
        {row?.is_archived ? <Tag>已归档</Tag> : null}
        <EditBasicsButton
          projectId={projectId}
          disabled={!canManageProject}
          onSaved={loadProject}
        />
      </Space>

      {error ? <Alert type="error" showIcon message={error} /> : null}

      <Card
        size="small"
        title="出库明细"
        extra={
          <Space size={8}>
            {contractNos.length > 1 ? (
              <Select
                allowClear
                size="small"
                style={{ width: 220 }}
                placeholder="全部合同"
                value={contractFilter}
                onChange={setContractFilter}
                options={contractNos.map((no) => ({ label: no, value: no }))}
              />
            ) : null}
            <WorkbookRoundTrip
              size="small"
              title="本项目总表"
              filename={`${exportBase}-总表.xlsx`}
              canUpload={canUpload}
              hint="六 sheet 一次下载，回填后整份上传覆盖"
              onDownload={() => downloadProjectMaster(projectId)}
              onApply={(file) => applyProjectMaster(projectId, file)}
            />
          </Space>
        }
      >
        <Table<BoardOrderRow>
          rowKey="source_order_id"
          size="small"
          loading={loading}
          dataSource={shownOrders}
          columns={orderColumns}
          pagination={{ pageSize: 10, showSizeChanger: false }}
        />
        {selectedOrder ? (
          <>
            <Text type="secondary" style={{ fontSize: 11.5 }}>
              流转状态列（已采/待供/待返/领用）为氚云原样数据，系统只展示、不参与任何计算。
            </Text>
            <Table<BoardLineRow>
              rowKey="raw_line_id"
              size="small"
              dataSource={lines}
              columns={lineColumns}
              scroll={{ x: 1100 }}
              pagination={{ pageSize: 10, showSizeChanger: false }}
            />
          </>
        ) : null}
      </Card>

      <Tabs
        items={[
          {
            key: SHEETS.basics,
            label: "项目基础信息",
            children: (
              <BasicsTab
                projectId={projectId}
                exportBase={exportBase}
                row={row}
                project={project}
                canUpload={canUpload}
                canAssign={canManageProject}
                onAssigned={loadProject}
              />
            ),
          },
          {
            key: SHEETS.parts,
            label: "备件成本",
            children: (
              <PartsTab
                projectId={projectId}
                exportBase={exportBase}
                canUpload={canUpload}
              />
            ),
          },
          {
            key: SHEETS.expense,
            label: "报销",
            children: (
              <ExpenseTab projectId={projectId} exportBase={exportBase} canUpload={canUpload} />
            ),
          },
          {
            key: SHEETS.collection,
            label: "回款",
            children: (
              <CollectionTab projectId={projectId} exportBase={exportBase} canUpload={canUpload} />
            ),
          },
          {
            key: SHEETS.site,
            label: "维保领用与返还",
            children: (
              <SiteReturnTab projectId={projectId} exportBase={exportBase} canUpload={canUpload} />
            ),
          },
        ]}
      />
    </Space>
  );
}

/** 回款 tab：展示累计回款、项目回款进度和每条快照的确认状态。 */
function CollectionTab({
  projectId,
  exportBase,
  canUpload,
}: {
  projectId: string;
  exportBase: string;
  canUpload: boolean;
}) {
  const [rows, setRows] = useState<MaintenanceCollectionSnapshotRow[]>([]);
  const [metrics, setMetrics] = useState<{
    received: number | null;
    contract: number | null;
    progress: number | null;
  }>({ received: null, contract: null, progress: null });
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await getMaintenanceProjectWorkspace(projectId, {
        collection_page: 1,
        collection_page_size: 100,
        requisition_page_size: 1,
        expense_page_size: 1,
      });
      setRows(response.data.collection_snapshots.rows);
      setMetrics({
        received: response.data.project.metrics.received_amount,
        contract: response.data.project.metrics.total_contract_amount,
        progress: response.data.project.metrics.collection_progress_pct,
      });
    } catch (err) {
      message.error(readError(err, "回款状态加载失败"));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <WorkbookRoundTrip
        size="small"
        title="回款"
        filename={`${exportBase}-${SHEETS.collection}.xlsx`}
        canUpload={canUpload}
        hint="下载后可回填累计实收、状态、凭证号和备注"
        onDownload={() => downloadProjectMaster(projectId, [SHEETS.collection])}
        onApply={async (file) => {
          const result = await applyProjectMaster(projectId, file);
          await load();
          return result;
        }}
      />
      <Descriptions bordered size="small" column={3}>
        <Descriptions.Item label="累计已回款">
          {metrics.received == null ? "—" : `¥${metrics.received.toFixed(2)}`}
        </Descriptions.Item>
        <Descriptions.Item label="合同总额（含税）">
          {metrics.contract == null ? "—" : `¥${metrics.contract.toFixed(2)}`}
        </Descriptions.Item>
        <Descriptions.Item label="回款进度">
          {metrics.progress == null ? "数据不足" : `${metrics.progress}%`}
        </Descriptions.Item>
      </Descriptions>
      <Table<MaintenanceCollectionSnapshotRow>
        rowKey="collection_id"
        size="small"
        loading={loading}
        dataSource={rows}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        locale={{ emptyText: "本项目暂无回款记录" }}
        columns={[
          { title: "合同编号", dataIndex: "contract_no", render: raw },
          { title: "报告月份", dataIndex: "report_month", render: raw },
          {
            title: "累计实收金额（含税）",
            dataIndex: "cumulative_amount",
            render: (value) => value == null ? "—" : `¥${Number(value).toFixed(2)}`,
          },
          {
            title: "回款状态",
            dataIndex: "status",
            render: (value: string) => {
              const status = COLLECTION_STATUS[value];
              return <Tag color={status?.color}>{status?.label ?? raw(value)}</Tag>;
            },
          },
          { title: "回款凭证号", dataIndex: "receipt_reference", render: raw },
          { title: "备注", dataIndex: "remark", render: raw },
        ]}
      />
    </Space>
  );
}

interface SiteReturnRow {
  issueLineId: string;
  issue: SiteIssueDocument;
  line: SiteIssueLine;
  obligation: MaintenanceReturnObligation | null;
  returns: MaintenanceBadReturn[];
}

function returnStatus(row: SiteReturnRow): { label: string; color: string } {
  if (row.issue.workflow_status === "void") return { label: "领用已作废", color: "default" };
  if (row.line.no_return === true || row.obligation?.classification === "exempt") {
    return { label: "免返", color: "blue" };
  }
  if (row.obligation?.classification === "pending_category") {
    return { label: "待确认品类", color: "gold" };
  }
  if (!row.obligation) return { label: "待生成返还义务", color: "gold" };
  const remaining = Number(row.obligation.remaining_quantity);
  const confirmed = Number(row.obligation.warehouse_confirmed_quantity);
  const registered = Number(row.obligation.registered_quantity);
  if (remaining <= 0 && confirmed > 0) return { label: "仓库已确认返还", color: "green" };
  if (registered > 0 && remaining > 0) return { label: "部分返还", color: "orange" };
  const activeReturn = row.returns.find((item) => item.status !== "void");
  if (activeReturn) {
    return {
      label: RETURN_DOCUMENT_STATUS[activeReturn.status] ?? activeReturn.status,
      color: activeReturn.status === "in_transit" ? "cyan" : "blue",
    };
  }
  return { label: "待返还", color: "red" };
}

/** 维保领用与返还 tab：以领用行 part_id 为主轴，合并返还义务和返还单状态。 */
function SiteReturnTab({
  projectId,
  exportBase,
  canUpload,
}: {
  projectId: string;
  exportBase: string;
  canUpload: boolean;
}) {
  const [rows, setRows] = useState<SiteReturnRow[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [issuesResponse, obligationsResponse, returnsResponse] = await Promise.all([
        searchSiteIssues({ project_id: projectId, page: 1, page_size: 100 }),
        searchMaintenanceReturnObligations({
          project_id: projectId,
          active_only: false,
          page: 1,
          page_size: 200,
        }),
        searchMaintenanceBadReturns({ project_id: projectId, page: 1, page_size: 100 }),
      ]);
      const obligations = new Map(
        obligationsResponse.data.rows.map((item) => [item.issue_line_id, item]),
      );
      const returns = new Map<string, MaintenanceBadReturn[]>();
      for (const document of returnsResponse.data.rows) {
        for (const line of document.lines) {
          const current = returns.get(line.obligation_id) ?? [];
          current.push(document);
          returns.set(line.obligation_id, current);
        }
      }
      setRows(issuesResponse.data.rows.flatMap((issue) =>
        issue.lines.map((line) => {
          const obligation = obligations.get(line.issue_line_id) ?? null;
          return {
            issueLineId: line.issue_line_id,
            issue,
            line,
            obligation,
            returns: obligation ? returns.get(obligation.obligation_id) ?? [] : [],
          };
        }),
      ));
    } catch (err) {
      message.error(readError(err, "维保领用与返还状态加载失败"));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <WorkbookRoundTrip
        size="small"
        title="维保领用与返还"
        filename={`${exportBase}-${SHEETS.site}.xlsx`}
        canUpload={canUpload}
        hint="可回填领用事实和是否应返还；上传后页面立即刷新"
        onDownload={() => downloadProjectMaster(projectId, [SHEETS.site])}
        onApply={async (file) => {
          const result = await applyProjectMaster(projectId, file);
          await load();
          return result;
        }}
      />
      <Table<SiteReturnRow>
        rowKey="issueLineId"
        size="small"
        loading={loading}
        dataSource={rows}
        scroll={{ x: 1300 }}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        locale={{ emptyText: "本项目暂无维保领用记录" }}
        columns={[
          { title: "领用单号", render: (_value, item) => raw(item.issue.issue_no) },
          { title: "领用日期", render: (_value, item) => raw(item.issue.issue_date) },
          {
            title: "领用状态",
            render: (_value, item) => {
              const status = ISSUE_STATUS[item.issue.workflow_status];
              return <Tag color={status?.color}>{status?.label ?? item.issue.workflow_status}</Tag>;
            },
          },
          { title: "PN", render: (_value, item) => raw(item.line.pn) },
          { title: "SN", render: (_value, item) => raw(item.line.serial_number) },
          { title: "领用数量", render: (_value, item) => raw(item.line.quantity) },
          {
            title: "应返数量",
            render: (_value, item) => raw(item.obligation?.required_quantity),
          },
          {
            title: "已登记返还",
            render: (_value, item) => raw(item.obligation?.registered_quantity),
          },
          {
            title: "仓库确认返还",
            render: (_value, item) => raw(item.obligation?.warehouse_confirmed_quantity),
          },
          {
            title: "返还状态",
            render: (_value, item) => {
              const status = returnStatus(item);
              return <Tag color={status.color}>{status.label}</Tag>;
            },
          },
          {
            title: "返还单号",
            render: (_value, item) => {
              const numbers = item.returns
                .filter((document) => document.status !== "void")
                .map((document) => document.return_no);
              return numbers.length ? numbers.join("、") : "—";
            },
          },
        ]}
      />
    </Space>
  );
}

/** 报销 tab：04 表的 web 呈现（含备注，#47）+ 下载上传。只展示，不散改。 */
function ExpenseTab({
  projectId,
  exportBase,
  canUpload,
}: {
  projectId: string;
  exportBase: string;
  canUpload: boolean;
}) {
  const [rows, setRows] = useState<ProjectExpenseRow[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRows((await listProjectExpenseRows(projectId)).rows);
    } catch (err) {
      message.error(readError(err, "报销明细加载失败"));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <WorkbookRoundTrip
        size="small"
        title="报销"
        filename={`${exportBase}-${SHEETS.expense}.xlsx`}
        canUpload={canUpload}
        hint="在哪下载就在哪上传：黄底的「未税金额」「备注」两列可改"
        onDownload={() => downloadProjectMaster(projectId, [SHEETS.expense])}
        onApply={async (file) => {
          const result = await applyProjectMaster(projectId, file);
          await load();          // 上传覆盖后立刻回读，页面不留旧值
          return result;
        }}
      />
      <Table<ProjectExpenseRow>
        rowKey="raw_line_id"
        size="small"
        loading={loading}
        dataSource={rows}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        locale={{ emptyText: "本项目暂无报销行" }}
        columns={[
          { title: "报销单号", dataIndex: "bxd_no", render: raw },
          { title: "报销日期", dataIndex: "expense_date", render: raw },
          { title: "报销人员", dataIndex: "person", render: raw },
          { title: "费用分类", dataIndex: "fee_category", render: raw },
          { title: "合同编号", dataIndex: "contract_no", render: raw },
          { title: "未税金额", dataIndex: "amount_ex_tax", render: raw },
          { title: "含税金额(系统计算)", dataIndex: "amount_inc_tax", render: raw },
          { title: "流程状态", dataIndex: "data_status", render: raw },
          { title: "备注", dataIndex: "remark", render: raw },
        ]}
      />
    </Space>
  );
}


/** 备件成本 tab：V2 03_备件明细 的九列事实展示 + 下载上传。 */
function PartsTab({
  projectId,
  exportBase,
  canUpload,
}: {
  projectId: string;
  exportBase: string;
  canUpload: boolean;
}) {
  const [rows, setRows] = useState<ProjectPartsRow[]>([]);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRows((await listProjectPartsRows(projectId)).rows);
    } catch (err) {
      message.error(readError(err, "备件订单明细加载失败"));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <WorkbookRoundTrip
        size="small"
        title="备件成本"
        filename={`${exportBase}-${SHEETS.parts}.xlsx`}
        canUpload={canUpload}
        hint="成本只读展示；缺成本请使用下载→修改黄色覆盖列→上传"
        onDownload={() => downloadProjectMaster(projectId, [SHEETS.parts])}
        onApply={async (file) => {
          const result = await applyProjectMaster(projectId, file);
          await load();          // 上传覆盖后立刻回读，页面不留旧值
          return result;
        }}
      />
      <Table<ProjectPartsRow>
        rowKey="line_id"
        size="small"
        loading={loading}
        dataSource={rows}
        scroll={{ x: 1200 }}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        locale={{ emptyText: "本项目暂无备件订单明细" }}
        columns={[
          { title: "维保单号", dataIndex: "order_no", render: raw },
          { title: "制单日期", dataIndex: "order_date", render: raw },
          { title: "PN", dataIndex: "pn_std", render: raw },
          { title: "产品描述", dataIndex: "description", render: raw, ellipsis: true },
          { title: "数量", dataIndex: "qty", render: raw },
          { title: "出库仓库", dataIndex: "warehouse", render: raw },
          { title: "成本来源", render: (_: unknown, line) => <CostSourceTag row={line} /> },
          {
            title: "未税单价",
            render: (_: unknown, line) =>
              line.unit_cost_ex_tax == null ? "—" : `¥${Number(line.unit_cost_ex_tax).toFixed(2)}`,
          },
          {
            title: "含税单价",
            render: (_: unknown, line) =>
              line.unit_cost_inc_tax == null ? "—" : `¥${Number(line.unit_cost_inc_tax).toFixed(2)}`,
          },
        ]}
      />
    </Space>
  );
}


/** 基础信息 tab：表 6 sheet 01 的 web 呈现 + 归属挂靠（#39/#45）。 */
function BasicsTab({
  projectId,
  exportBase,
  row,
  project,
  canUpload,
  canAssign,
  onAssigned,
}: {
  projectId: string;
  exportBase: string;
  row: BoardProjectRow | null;
  /** stable 目录的基础信息——boss-board 聚合行缺位时的回退源（数据源不同，字段较少）。 */
  project: MaintenanceProject | null;
  canUpload: boolean;
  canAssign: boolean;
  onAssigned: () => void;
}) {
  const [candidates, setCandidates] = useState<MaintenanceSourceOrderRow[]>([]);
  const [busy, setBusy] = useState(false);

  const loadCandidates = useCallback(async () => {
    try {
      // #48：让后端按本项目 XSDD 集合排序——前端只拿一页，若在前端筛会漏掉
      // 命中但排在 20 条之外的单。多合同项目的全部 XSDD 都由后端从台账取。
      const resp = await listMaintenanceSourceOrders({
        page: 1, page_size: 20, assignment_status: "unassigned",
        xsdd_project_id: projectId,
      });
      setCandidates(resp.data.rows ?? []);
    } catch {
      setCandidates([]);
    }
  }, []);

  useEffect(() => {
    if (canAssign) void loadCandidates();
  }, [canAssign, loadCandidates]);

  const confirm = async (sourceOrderId: string) => {
    setBusy(true);
    try {
      await assignMaintenanceSourceOrders({
        project_id: projectId,
        items: [{ source_order_id: sourceOrderId }],
        reason: "项目面板确认挂靠",
      });
      message.success("已确认挂靠");
      await loadCandidates();
      onAssigned();
    } catch (err) {
      message.error(readError(err, "挂靠失败"));
    } finally {
      setBusy(false);
    }
  };

  const autoAssign = async () => {
    setBusy(true);
    try {
      const { data } = await autoAssignMaintenanceSourceOrders();
      const r = data.result;
      message.success(
        `自动挂靠完成：${r.assigned_orders} 张单挂到 ${r.matched_projects} 个项目` +
          (r.skipped_groups ? `；${r.skipped_groups} 个对不上项目名已跳过` : ""),
      );
      await loadCandidates();
      onAssigned();
    } catch (err) {
      message.error(readError(err, "自动挂靠失败"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <WorkbookRoundTrip
        size="small"
        title="基础信息表"
        filename={`${exportBase}-${SHEETS.basics}.xlsx`}
        canUpload={canUpload}
        hint="01 表为只读呈现；可编辑内容在 03/04/05 各自的 tab 里回填"
        onDownload={() => downloadProjectMaster(projectId, [SHEETS.basics])}
        onApply={(file) => applyProjectMaster(projectId, file)}
      />
      <Descriptions bordered size="small" column={2}>
        <Descriptions.Item label="项目名称">
          {row?.display_name ?? project?.display_name ?? "—"}
        </Descriptions.Item>
        <Descriptions.Item label="项目编号">
          {row?.project_code ?? project?.project_code ?? "—"}
        </Descriptions.Item>
        <Descriptions.Item label="合同号(XSDD)">
          {row?.contract_nos.length ? row.contract_nos.join("、") : "—"}
        </Descriptions.Item>
        <Descriptions.Item label="项目经理">
          {row?.project_manager ?? project?.project_manager_id ?? "—"}
        </Descriptions.Item>
        <Descriptions.Item label="合同总额">
          <Space size={6}>
            <span>{statText(row?.contract_amount_inc_tax)}</span>
            {row?.contract_shared ? (
              <Tooltip title="有销售订单同时挂在多个项目上，合同额会在项目间重复计入，仅作参考">
                <Tag color="orange">共用单</Tag>
              </Tooltip>
            ) : null}
            {row?.contract_incomplete ? (
              <Tooltip title="部分关联销售订单未在销售表中找到金额，合同额被低估">
                <Tag color="orange">不完整</Tag>
              </Tooltip>
            ) : null}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="维保期限">
          {(() => {
            const pf = row?.period_from ?? project?.period_from;
            const pt = row?.period_to ?? project?.period_to;
            if (!pf && !pt) return "—";
            return `${pf ?? "—"} ~ ${pt ?? "—"}`;
          })()}
        </Descriptions.Item>
        <Descriptions.Item label="生命周期">
          {(() => {
            const lc = row?.lifecycle ?? project?.lifecycle_status;
            return lc ? LIFECYCLE_LABEL[lc] ?? lc : "—";
          })()}
        </Descriptions.Item>
        <Descriptions.Item label="成本率（成本÷合同额）" span={2}>
          <CostRatioBar row={row} />
        </Descriptions.Item>
      </Descriptions>

      {canAssign ? (
        <Card
          size="small"
          title="归属挂靠（判定依据＝XSDD 销售订单）"
          extra={(
            <Button size="small" type="primary" ghost loading={busy} onClick={() => void autoAssign()}>
              自动匹配挂靠
            </Button>
          )}
        >
          <Text type="secondary" style={{ fontSize: 11.5 }}>
            同一销售订单＝同一项目。点「自动匹配挂靠」会按单据自带的项目名自动挂到已有项目，
            对不上的才留在下面人工确认；标「同 XSDD」的是命中本项目销售单的候选，已排最前。
          </Text>
          <Table<MaintenanceSourceOrderRow>
            rowKey="raw_order_id"
            size="small"
            dataSource={candidates}
            pagination={false}
            locale={{ emptyText: "没有待确认的未归属单据" }}
            columns={[
              {
                title: "需求单号",
                dataIndex: "order_no",
                render: (value: string, order) => (
                  <Space size={4}>
                    <span>{value}</span>
                    {order.matches_project_xsdd ? (
                      <Tag color="blue">同 XSDD</Tag>
                    ) : null}
                  </Space>
                ),
              },
              { title: "制单日期", dataIndex: "order_date", render: raw },
              { title: "项目原文", dataIndex: "project_raw", render: raw },
              {
                title: "操作",
                render: (_: unknown, order) => (
                  <Button
                    size="small"
                    loading={busy}
                    onClick={() => confirm(order.raw_order_id)}
                  >
                    确认挂靠到本项目
                  </Button>
                ),
              },
            ]}
          />
        </Card>
      ) : null}
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

function readError(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response
    ?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    return String((detail as { message: unknown }).message);
  }
  return fallback;
}

export default MaintenanceProjectPanelPage;
