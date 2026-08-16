import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Pagination,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from "antd";
import { DownloadOutlined } from "@ant-design/icons";
import { Link } from "react-router-dom";

import {
  listMaintenanceProjects,
  searchMaintenanceProjects,
  type MaintenanceProject,
} from "../../api/maintenanceProjects";
import {
  downloadMaintenanceProjectWorkbookV3,
  getMaintenanceProjectFrontStock,
  getMaintenanceProjectReturnRate,
  type MaintenanceFrontStockSummary,
  type MaintenanceReturnRate,
} from "../../api/maintenanceOperations";
import "../../components/maintenance/maintenanceOperations.css";
import PageHeader from "../../components/PageHeader";
import { moneyIncTax } from "../../utils/format";

const { Text } = Typography;

const PAGE_SIZE = 24;

const NOT_READY = "尚未接入";

const LIFECYCLE_META: Record<string, { label: string; color?: string }> = {
  ongoing: { label: "服务中", color: "blue" },
  ended: { label: "已结项" },
  missing: { label: "期限待确认", color: "orange" },
};

interface WorkbenchProjectDetail {
  returnRate: MaintenanceReturnRate | null;
  frontStock: MaintenanceFrontStockSummary | null;
}

function errorText(error: unknown): string {
  const payload = error as { response?: { data?: { detail?: string | { message?: string } } } };
  const detail = payload.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  return "操作失败，请刷新后重试";
}

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function safeFilenamePart(value: string): string {
  return value.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").trim() || "维保项目";
}

function WorkbenchProjectCard({
  project,
  detail,
  onDownloadWorkbook,
}: {
  project: MaintenanceProject;
  detail: WorkbenchProjectDetail | undefined;
  onDownloadWorkbook: (project: MaintenanceProject) => void;
}) {
  const lifecycle = LIFECYCLE_META[project.lifecycle_status]
    ?? { label: "期限待确认", color: "orange" };
  const detailLink = `/maintenance/beta/projects/${encodeURIComponent(project.project_id)}`;
  const returnRate = detail?.returnRate ?? null;
  const frontStock = detail?.frontStock ?? null;
  const mismatch = returnRate?.pn_mismatch_warning ?? [];
  return (
    <Card
      data-testid={`maintenance-workbench-card-${project.project_id}`}
      className="maintenance-project-card"
      title={(
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 12, color: "var(--mb-text-3)", marginBottom: 2 }}>
            {project.project_code}
          </div>
          <div title={project.display_name} style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
            {project.display_name}
          </div>
        </div>
      )}
      extra={<Tag color={lifecycle.color}>{lifecycle.label}</Tag>}
      actions={[
        <Link key="detail" to={detailLink}>项目详情</Link>,
        <Link key="front-stock" to={`${detailLink}#front-stock`}>前置库</Link>,
        <Link key="recovery" to={`${detailLink}#recovery-summary`}>收回清单</Link>,
        <Button key="workbook" type="link" onClick={() => onDownloadWorkbook(project)}>
          工作簿下载
        </Button>,
      ]}
      styles={{ body: { display: "flex", flexDirection: "column", gap: 14 } }}
    >
      <div className="maintenance-workbench-block">
        <div className="maintenance-workbench-block-title">官方返还率</div>
        {returnRate ? (
          <Space wrap size={[6, 6]}>
            {returnRate.status === "not_ready" && <Tag color="orange">返还率未导入</Tag>}
            {returnRate.status === "basis_incomplete" && <Tag color="orange">基础待完善</Tag>}
            {returnRate.status === "no_return_required" && <Tag color="green">无应返义务</Tag>}
            {returnRate.status === "available" && (
              returnRate.official_rate_pct != null
                ? <Tag color="green">官方返还率 {returnRate.official_rate_pct}%</Tag>
                : <Tag color="orange">返还率暂未发布</Tag>
            )}
            <Tag>应返 {returnRate.required_quantity}</Tag>
          </Space>
        ) : (
          <Text type="secondary">{NOT_READY}</Text>
        )}
        {mismatch.length > 0 && (
          <Alert
            showIcon
            type="warning"
            message={`返还 PN 与领用清单不一致 ${mismatch.length} 项`}
            description={mismatch.slice(0, 3).join("、") + (mismatch.length > 3 ? "…" : "")}
          />
        )}
      </div>

      <div className="maintenance-workbench-block">
        <div className="maintenance-workbench-block-title">前置库结存</div>
        {frontStock ? (
          <Space wrap size={[6, 6]}>
            <Tag>超90天未领用 {frontStock.stale_90d_count}</Tag>
            <Tag>{frontStock.rows.length} 行</Tag>
            {frontStock.value_completeness === "incomplete" && (
              <Tag color="orange">金额估值不完整</Tag>
            )}
            {frontStock.cost_visible
              ? (
                <Tag color="blue">
                  金额（含税）{frontStock.total_value_inc_tax != null
                    ? moneyIncTax(frontStock.total_value_inc_tax)
                    : NOT_READY}
                </Tag>
              )
              : <Tag>金额不可见</Tag>}
          </Space>
        ) : (
          <Text type="secondary">{NOT_READY}</Text>
        )}
      </div>
    </Card>
  );
}

export default function MaintenanceWorkbenchPage() {
  const [projects, setProjects] = useState<MaintenanceProject[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [details, setDetails] = useState<Record<string, WorkbenchProjectDetail>>({});
  const generation = useRef(0);

  const fetchProjectDetail = useCallback(
    (project: MaintenanceProject, active: () => boolean) => {
      void Promise.allSettled([
        getMaintenanceProjectReturnRate(project.project_id),
        getMaintenanceProjectFrontStock(project.project_id),
      ]).then(([rateResult, stockResult]) => {
        if (!active()) return;
        setDetails((prev) => ({
          ...prev,
          [project.project_id]: {
            returnRate: rateResult.status === "fulfilled" ? rateResult.value.data : null,
            frontStock: stockResult.status === "fulfilled" ? stockResult.value.data : null,
          },
        }));
        if (rateResult.status === "rejected") {
          message.error(`「${project.display_name}」返还率加载失败：${errorText(rateResult.reason)}`);
        }
        if (stockResult.status === "rejected") {
          message.error(`「${project.display_name}」前置库加载失败：${errorText(stockResult.reason)}`);
        }
      });
    },
    [],
  );

  const load = useCallback(async (nextPage: number, q: string) => {
    const request = ++generation.current;
    setLoading(true);
    setLoadError(false);
    try {
      const { data } = q.trim()
        ? await searchMaintenanceProjects({
          q: q.trim(),
          page: nextPage,
          page_size: PAGE_SIZE,
          include_inactive: false,
        })
        : await listMaintenanceProjects({
          page: nextPage,
          page_size: PAGE_SIZE,
          include_inactive: false,
        });
      if (request !== generation.current) return;
      setProjects(data.rows ?? []);
      setTotal(data.total ?? 0);
      setPage(data.page ?? nextPage);
      const isActive = () => request === generation.current;
      for (const project of data.rows ?? []) {
        fetchProjectDetail(project, isActive);
      }
    } catch (error) {
      if (request !== generation.current) return;
      setProjects([]);
      setTotal(0);
      setLoadError(true);
      message.error(`项目目录加载失败：${errorText(error)}`);
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, [fetchProjectDetail]);

  useEffect(() => {
    void load(page, query);
    return () => { generation.current += 1; };
    // load() 自身以 generation 防竞态；page/query 变化即重新拉取。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, query]);

  const downloadWorkbook = async (project: MaintenanceProject) => {
    try {
      const { data } = await downloadMaintenanceProjectWorkbookV3(project.project_id);
      saveBlob(data, `${safeFilenamePart(project.project_code)}_项目工作簿v3.xlsx`);
      message.success("项目工作簿已生成");
    } catch (error) {
      message.error(`工作簿下载失败：${errorText(error)}`);
    }
  };

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title="我的维保"
        subtitle="聚合我的维保项目：官方返还率、前置库结存与超90天未领用提示；未接入的数据一律显示「尚未接入」占位，不隐藏。"
        extra={<Spin spinning={loading}><span /></Spin>}
      />
      <Space wrap style={{ width: "100%" }}>
        <Input.Search
          aria-label="搜索我的维保项目"
          placeholder="搜索项目编号或名称"
          allowClear
          style={{ width: "min(340px, 100%)" }}
          onSearch={(value) => {
            setPage(1);
            setQuery(value.trim());
          }}
        />
        <Text type="secondary">共 {total} 个项目</Text>
      </Space>
      {loadError ? (
        <Alert
          type="error"
          showIcon
          message="我的维保加载失败"
          action={(
            <Button size="small" danger onClick={() => void load(page, query)}>重试</Button>
          )}
        />
      ) : projects.length === 0 && !loading ? (
        <Empty description="当前筛选暂无项目" />
      ) : (
        <div data-testid="maintenance-workbench-grid" className="maintenance-project-grid">
          {projects.map((project) => (
            <WorkbenchProjectCard
              key={project.project_id}
              project={project}
              detail={details[project.project_id]}
              onDownloadWorkbook={(target) => void downloadWorkbook(target)}
            />
          ))}
        </div>
      )}
      {total > PAGE_SIZE && (
        <Pagination
          current={page}
          pageSize={PAGE_SIZE}
          total={total}
          showSizeChanger={false}
          onChange={(next) => setPage(next)}
        />
      )}
    </Space>
  );
}
