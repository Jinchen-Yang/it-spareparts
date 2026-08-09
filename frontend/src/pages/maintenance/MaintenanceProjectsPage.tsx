import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Empty, Input, Pagination, Segmented, Space } from "antd";
import { Navigate, useLocation, useSearchParams } from "react-router-dom";

import {
  listMaintenanceProjectOperations,
  type MaintenanceProjectOperationsSummary,
} from "../../api/maintenanceOperations";
import MaintenanceProjectCard from "../../components/maintenance/MaintenanceProjectCard";
import "../../components/maintenance/maintenanceOperations.css";
import {
  canUseMaintenanceReminderFilter,
  readMaintenanceCapabilities,
} from "../../components/maintenance/maintenancePermissions";
import PageHeader from "../../components/PageHeader";

const PAGE_SIZE = 24;

export default function MaintenanceProjectsPage() {
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const projectDeepLinkId = searchParams.get("project_id")?.trim() || "";
  const reminderFilter = searchParams.get("reminder") || undefined;
  const reminderFilterAllowed = canUseMaintenanceReminderFilter(reminderFilter);
  const [rows, setRows] = useState<MaintenanceProjectOperationsSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [q, setQ] = useState("");
  const [lifecycle, setLifecycle] = useState(reminderFilter ? "all" : "ongoing");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [permissionDenied, setPermissionDenied] = useState(false);
  const [capabilities] = useState(readMaintenanceCapabilities);
  const generation = useRef(0);

  const load = useCallback(async (
    nextPage: number,
    query: string,
    nextLifecycle: string,
  ) => {
    const request = ++generation.current;
    setLoading(true);
    setError(false);
    setPermissionDenied(false);
    try {
      const { data } = await listMaintenanceProjectOperations({
        page: nextPage,
        page_size: PAGE_SIZE,
        q: query || undefined,
        lifecycle: nextLifecycle,
        reminder: reminderFilter,
      });
      if (request !== generation.current) return;
      setRows(data.rows ?? []);
      setTotal(data.total ?? 0);
      setPage(data.page ?? nextPage);
    } catch (reason: unknown) {
      if (request !== generation.current) return;
      setRows([]);
      setTotal(0);
      const responseStatus = (
        reason as { response?: { status?: unknown } } | null
      )?.response?.status;
      if (responseStatus === 403) setPermissionDenied(true);
      else setError(true);
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, [reminderFilter]);

  useEffect(() => {
    if (projectDeepLinkId || !reminderFilterAllowed) return undefined;
    void load(1, q, lifecycle);
    return () => { generation.current += 1; };
  }, [lifecycle, load, projectDeepLinkId, q, reminderFilterAllowed]);

  if (projectDeepLinkId) {
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete("project_id");
    const nextSearch = nextParams.toString();
    return (
      <Navigate
        replace
        to={{
          pathname: `/maintenance/projects/${encodeURIComponent(projectDeepLinkId)}`,
          search: nextSearch ? `?${nextSearch}` : "",
          hash: location.hash,
        }}
      />
    );
  }

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title="维保项目"
        subtitle="按稳定项目查看全部合同、回款、现场领用、审批通过报销和系统提醒。缺失成本不会隐藏项目事实。"
      />
      <Space wrap style={{ width: "100%" }}>
        <Input.Search
          aria-label="搜索维保项目"
          placeholder="搜索项目编号或名称"
          allowClear
          style={{ width: "min(340px, 100%)" }}
          onSearch={(value) => {
            const next = value.trim();
            setQ(next);
          }}
        />
        <Segmented
          aria-label="项目期限筛选"
          value={lifecycle}
          options={[
            { label: "进行中", value: "ongoing" },
            { label: "已结束", value: "ended" },
            { label: "期限缺失", value: "missing" },
            { label: "全部", value: "all" },
          ]}
          onChange={(value) => {
            const next = String(value);
            setLifecycle(next);
          }}
        />
      </Space>
      {!reminderFilterAllowed || permissionDenied ? (
        <Alert
          type="warning"
          showIcon
          message="当前账号无权使用该提醒筛选"
          description="该筛选依赖当前账号不可见的经营字段；移除提醒参数后仍可查看获准的项目事实。"
        />
      ) : error ? (
        <Alert
          type="error"
          showIcon
          message="项目面板加载失败"
          action={(
            <Button size="small" danger onClick={() => void load(page, q, lifecycle)}>
              重试
            </Button>
          )}
        />
      ) : rows.length === 0 && !loading ? (
        <Empty description="当前筛选暂无项目" />
      ) : (
        <div
          data-testid="maintenance-project-grid"
          className="maintenance-project-grid"
          aria-busy={loading}
        >
          {rows.map((project) => (
            <MaintenanceProjectCard
              key={project.project_id}
              project={project}
              visibility={capabilities}
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
          onChange={(next) => void load(next, q, lifecycle)}
        />
      )}
    </Space>
  );
}
