import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Drawer, Input, Modal, Space, Table, Tag, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import PageHeader from "../components/PageHeader";
import {
  listMaintenanceProjects,
  archiveMaintenanceProject,
  createMaintenanceProject,
  getMaintenanceProject,
  restoreMaintenanceProject,
  updateMaintenanceProject,
  type MaintenanceProject,
} from "../api/maintenanceProjects";

type DrawerMode = "create" | "edit" | null;
const PAGE_SIZE = 50;

function readLocalPermissions(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem("permissions") || "{}");
  } catch {
    return {};
  }
}

function errorDetail(error: unknown, fallback: string): string {
  if (typeof error === "object" && error !== null && "response" in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response;
    if (typeof response?.data?.detail === "string") return response.data.detail;
  }
  return fallback;
}

function isConflict(error: unknown): boolean {
  return Boolean(
    typeof error === "object"
    && error !== null
    && "response" in error
    && (error as { response?: { status?: number } }).response?.status === 409,
  );
}

function lifecycleTag() {
  return <Tag color="orange">业务期限待确认</Tag>;
}

export default function MaintenanceProjectMasterPage() {
  const [canManage] = useState(() => {
    return readLocalPermissions().action_maintenance_project_manage === true;
  });
  const [rows, setRows] = useState<MaintenanceProject[]>([]);
  const [total, setTotal] = useState(0);
  const [currentPage, setCurrentPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [drawerMode, setDrawerMode] = useState<DrawerMode>(null);
  const [selected, setSelected] = useState<MaintenanceProject | null>(null);
  const [projectCode, setProjectCode] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [projectManagerId, setProjectManagerId] = useState("");
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [lifecycleProject, setLifecycleProject] = useState<MaintenanceProject | null>(null);
  const [lifecycleTargetActive, setLifecycleTargetActive] = useState<boolean | null>(null);
  const [lifecycleReason, setLifecycleReason] = useState("");
  const [lifecycleSaving, setLifecycleSaving] = useState(false);
  const [lifecycleError, setLifecycleError] = useState<string | null>(null);
  const [lifecycleConflict, setLifecycleConflict] = useState(false);
  const loadGeneration = useRef(0);

  const load = useCallback(async (requestedPage: number) => {
    const generation = ++loadGeneration.current;
    setLoading(true);
    setLoadError(null);
    try {
      const { data } = await listMaintenanceProjects({
        page: requestedPage,
        page_size: PAGE_SIZE,
      });
      if (generation !== loadGeneration.current) return;
      setRows(data.rows ?? []);
      setTotal(data.total ?? 0);
      setCurrentPage(data.page ?? requestedPage);
    } catch (error) {
      if (generation !== loadGeneration.current) return;
      setRows([]);
      setTotal(0);
      setLoadError(errorDetail(error, "项目主档加载失败，请检查网络后重试。"));
    } finally {
      if (generation === loadGeneration.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(1);
    return () => { loadGeneration.current += 1; };
  }, [load]);

  const openCreate = () => {
    setSelected(null);
    setProjectCode("");
    setDisplayName("");
    setProjectManagerId("");
    setReason("");
    setFormError(null);
    setConflict(false);
    setDrawerMode("create");
  };

  const openEdit = (project: MaintenanceProject) => {
    setSelected(project);
    setProjectCode(project.project_code);
    setDisplayName(project.display_name);
    setProjectManagerId(project.project_manager_id ?? "");
    setReason("");
    setFormError(null);
    setConflict(false);
    setDrawerMode("edit");
  };

  const save = async () => {
    const cleanCode = projectCode.trim();
    const cleanName = displayName.trim();
    const cleanManager = projectManagerId.trim();
    const cleanReason = reason.trim();
    if (!cleanCode || !cleanName || !cleanReason) return;
    setSaving(true);
    setFormError(null);
    setConflict(false);
    try {
      if (drawerMode === "create") {
        await createMaintenanceProject({
          project_code: cleanCode,
          display_name: cleanName,
          project_manager_id: cleanManager || undefined,
          reason: cleanReason,
        });
        message.success("项目主档已创建");
      } else if (drawerMode === "edit" && selected) {
        const nameChanged = cleanName !== selected.display_name;
        const managerChanged = cleanManager !== (selected.project_manager_id ?? "");
        if (!nameChanged && !managerChanged) return;
        await updateMaintenanceProject(selected.project_id, {
          version: selected.version,
          ...(nameChanged ? { display_name: cleanName } : {}),
          ...(managerChanged
            ? { project_manager_id: cleanManager || null }
            : {}),
          reason: cleanReason,
        });
        message.success("项目主档已更新");
      }
      setDrawerMode(null);
      await load(currentPage);
    } catch (error) {
      setConflict(isConflict(error));
      setFormError(errorDetail(error, "保存失败，请检查后重试。"));
    } finally {
      setSaving(false);
    }
  };

  const refreshAfterConflict = async () => {
    const generation = ++loadGeneration.current;
    setLoading(true);
    setLoadError(null);
    try {
      const { data } = await listMaintenanceProjects({
        page: currentPage,
        page_size: PAGE_SIZE,
      });
      if (generation !== loadGeneration.current) return;
      const nextRows = data.rows ?? [];
      setRows(nextRows);
      setTotal(data.total ?? 0);
      setCurrentPage(data.page ?? currentPage);
      if (selected) {
        const nameWasEdited = displayName.trim() !== selected.display_name;
        const managerWasEdited = projectManagerId.trim()
          !== (selected.project_manager_id ?? "");
        const latest = nextRows.find((row) => row.project_id === selected.project_id);
        if (!latest) {
          setFormError("该项目已不在最新列表中，请关闭抽屉后重新确认。");
          return;
        }
        setSelected(latest);
        if (!nameWasEdited) setDisplayName(latest.display_name);
        if (!managerWasEdited) setProjectManagerId(latest.project_manager_id ?? "");
      }
      setConflict(false);
      setFormError(null);
      message.info("项目列表已刷新，抽屉草稿已保留");
    } catch (error) {
      if (generation !== loadGeneration.current) return;
      setFormError(errorDetail(error, "刷新失败，草稿仍已保留，请稍后重试。"));
    } finally {
      if (generation === loadGeneration.current) setLoading(false);
    }
  };

  const openLifecycleConfirm = (project: MaintenanceProject) => {
    setLifecycleProject(project);
    setLifecycleTargetActive(!project.is_active);
    setLifecycleReason("");
    setLifecycleError(null);
    setLifecycleConflict(false);
  };

  const submitLifecycleChange = async () => {
    const cleanReason = lifecycleReason.trim();
    if (!lifecycleProject || lifecycleTargetActive === null || !cleanReason) return;
    setLifecycleSaving(true);
    setLifecycleError(null);
    try {
      const input = { version: lifecycleProject.version, reason: cleanReason };
      if (lifecycleTargetActive) {
        await restoreMaintenanceProject(lifecycleProject.project_id, input);
        message.success("项目主档已恢复");
      } else {
        await archiveMaintenanceProject(lifecycleProject.project_id, input);
        message.success("项目主档已归档");
      }
      setLifecycleProject(null);
      setLifecycleTargetActive(null);
      setLifecycleConflict(false);
      await load(currentPage);
    } catch (error) {
      setLifecycleConflict(isConflict(error));
      setLifecycleError(errorDetail(
        error,
        isConflict(error)
          ? "项目主档已被他人修改，请刷新列表后再重试。"
          : "操作失败，请检查后重试。",
      ));
    } finally {
      setLifecycleSaving(false);
    }
  };

  const refreshLifecycleAfterConflict = async () => {
    if (!lifecycleProject || lifecycleTargetActive === null) return;
    setLifecycleSaving(true);
    try {
      const { data } = await getMaintenanceProject(lifecycleProject.project_id);
      const latest = data.project;
      setRows((current) => current.map((row) => (
        row.project_id === latest.project_id ? latest : row
      )));
      if (latest.is_active === lifecycleTargetActive) {
        const completedAction = lifecycleTargetActive ? "恢复" : "归档";
        setLifecycleProject(null);
        setLifecycleTargetActive(null);
        setLifecycleConflict(false);
        setLifecycleError(null);
        message.info(`项目已由他人完成${completedAction}，无需重复操作`);
        return;
      }
      setLifecycleProject(latest);
      setLifecycleConflict(false);
      setLifecycleError(null);
      message.info("项目最新版本已刷新，操作原因已保留");
    } catch (error) {
      setLifecycleError(errorDetail(error, "刷新失败，操作原因仍已保留，请稍后重试。"));
    } finally {
      setLifecycleSaving(false);
    }
  };

  const columns = useMemo<ColumnsType<MaintenanceProject>>(() => {
    const base: ColumnsType<MaintenanceProject> = [{
      title: "稳定项目编号",
      dataIndex: "project_code",
      key: "project_code",
    },
    {
      title: "项目名称",
      dataIndex: "display_name",
      key: "display_name",
    },
    {
      title: "项目经理标识",
      dataIndex: "project_manager_id",
      key: "project_manager_id",
      render: (value: string | null) => value || "—",
    },
    {
      title: "业务期限",
      dataIndex: "lifecycle_status",
      key: "lifecycle_status",
      render: () => lifecycleTag(),
    },
    {
      title: "主档状态",
      dataIndex: "is_active",
      key: "is_active",
      render: (active: boolean) => active
        ? <Tag color="green">有效</Tag>
        : <Tag>已归档</Tag>,
    },
    {
      title: "版本",
      dataIndex: "version",
      key: "version",
      width: 84,
    }];
    if (canManage) {
      base.push({
        title: "操作",
        key: "actions",
        render: (_value, project) => (
          <Space>
            {project.is_active && (
              <Button type="link" onClick={() => openEdit(project)}>编辑</Button>
            )}
            <Button type="link" onClick={() => openLifecycleConfirm(project)}>
              {project.is_active ? "归档" : "恢复"}
            </Button>
          </Space>
        ),
      });
    }
    return base;
  }, [canManage]);

  const editHasChanges = drawerMode !== "edit" || !selected || (
    displayName.trim() !== selected.display_name
    || projectManagerId.trim() !== (selected.project_manager_id ?? "")
  );
  const drawerCanSave = Boolean(
    projectCode.trim()
    && displayName.trim()
    && reason.trim()
    && editHasChanges
    && !conflict,
  );

  return (
    <>
      <PageHeader
        title="项目主档"
        subtitle="稳定项目编号是维保项目的长期身份；名称和负责人可审计变更。"
        extra={(
          <Space>
            <Button href="/maintenance/beta/project-master/source-orders">
              历史维保单归属
            </Button>
            {canManage && (
              <Button type="primary" onClick={openCreate}>新建项目</Button>
            )}
          </Space>
        )}
      />
      {loadError && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message={loadError}
          action={(
            <Button size="small" disabled={loading} onClick={() => void load(currentPage)}>
              重新加载
            </Button>
          )}
        />
      )}
      <Table<MaintenanceProject>
        rowKey="project_id"
        loading={loading}
        columns={columns}
        dataSource={rows}
        pagination={{
          current: currentPage,
          pageSize: PAGE_SIZE,
          total,
          showSizeChanger: false,
          showTotal: (count) => `共 ${count} 个项目`,
          onChange: (nextPage) => void load(nextPage),
        }}
        locale={{ emptyText: canManage ? "暂无项目主档，可新建项目。" : "暂无项目主档。" }}
      />
      {canManage && (
        <Drawer
          title={drawerMode === "create" ? "新建项目主档" : "编辑项目主档"}
          open={drawerMode !== null}
          width={480}
          destroyOnClose
          closable={!saving}
          maskClosable={!saving}
          onClose={() => setDrawerMode(null)}
          extra={(
            <Button
              type="primary"
              loading={saving}
              disabled={!drawerCanSave}
              onClick={() => void save()}
            >
              {drawerMode === "create" ? "保存建档" : "保存修改"}
            </Button>
          )}
        >
          {conflict && formError ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              message="存在并发修改，当前草稿尚未丢失"
              description={formError}
              action={(
                <Button size="small" onClick={() => void refreshAfterConflict()}>
                  刷新项目列表并保留草稿
                </Button>
              )}
            />
          ) : formError && (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 16 }}
              message={formError}
            />
          )}
          <Space direction="vertical" size={16} style={{ width: "100%" }}>
            <label htmlFor="maintenance-project-code">
              <div style={{ marginBottom: 6 }}>稳定项目编号</div>
              <Input
                id="maintenance-project-code"
                value={projectCode}
                disabled={drawerMode === "edit" || saving}
                maxLength={64}
                onChange={(event) => setProjectCode(event.target.value)}
              />
            </label>
            <label htmlFor="maintenance-project-name">
              <div style={{ marginBottom: 6 }}>项目名称</div>
              <Input
                id="maintenance-project-name"
                value={displayName}
                disabled={saving}
                maxLength={256}
                onChange={(event) => setDisplayName(event.target.value)}
              />
            </label>
            <label htmlFor="maintenance-project-manager">
              <div style={{ marginBottom: 6 }}>项目经理标识</div>
              <Input
                id="maintenance-project-manager"
                value={projectManagerId}
                disabled={saving}
                maxLength={64}
                placeholder="可留空；编辑时清空会显式解除负责人"
                onChange={(event) => setProjectManagerId(event.target.value)}
              />
            </label>
            <label htmlFor="maintenance-project-reason">
              <div style={{ marginBottom: 6 }}>操作原因</div>
              <Input.TextArea
                id="maintenance-project-reason"
                value={reason}
                disabled={saving}
                maxLength={1000}
                rows={4}
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
          </Space>
        </Drawer>
      )}
      {canManage && (
        <Modal
          title={`二次确认${lifecycleTargetActive ? "恢复" : "归档"}项目`}
          open={lifecycleProject !== null}
          okText={`确认${lifecycleTargetActive ? "恢复" : "归档"}`}
          cancelText="取消"
          confirmLoading={lifecycleSaving}
          okButtonProps={{ disabled: !lifecycleReason.trim() }}
          maskClosable={!lifecycleSaving}
          closable={!lifecycleSaving}
          onCancel={() => {
            setLifecycleProject(null);
            setLifecycleTargetActive(null);
          }}
          onOk={() => void submitLifecycleChange()}
        >
          <p>
            {lifecycleTargetActive
              ? "恢复后该主档会重新变为有效状态。"
              : "归档后该主档保留历史，但不能继续编辑；如需使用可再次恢复。"}
          </p>
          {lifecycleError && (
            <Alert
              type={lifecycleConflict ? "warning" : "error"}
              showIcon
              style={{ marginBottom: 16 }}
              message={lifecycleError}
              action={lifecycleConflict ? (
                <Button size="small" onClick={() => void refreshLifecycleAfterConflict()}>
                  刷新最新版本并保留原因
                </Button>
              ) : undefined}
            />
          )}
          <label htmlFor="maintenance-project-lifecycle-reason">
            <div style={{ marginBottom: 6 }}>
              {lifecycleTargetActive ? "恢复原因" : "归档原因"}
            </div>
            <Input.TextArea
              id="maintenance-project-lifecycle-reason"
              value={lifecycleReason}
              disabled={lifecycleSaving}
              maxLength={1000}
              rows={4}
              onChange={(event) => setLifecycleReason(event.target.value)}
            />
          </label>
        </Modal>
      )}
    </>
  );
}
