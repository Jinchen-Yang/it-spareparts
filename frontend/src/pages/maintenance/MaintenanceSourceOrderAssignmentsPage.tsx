import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Input, Modal, Select, Space, Table, Tag, message } from "antd";
import type { ColumnsType } from "antd/es/table";

import {
  assignMaintenanceSourceOrders,
  listMaintenanceSourceOrders,
  type MaintenanceSourceOrderRow,
  unassignMaintenanceSourceOrders,
} from "../../api/maintenanceSourceAssignments";
import {
  listMaintenanceProjects,
  type MaintenanceProject,
} from "../../api/maintenanceProjects";
import PageHeader from "../../components/PageHeader";

const PAGE_SIZE = 50;
type AssignmentStatus = "unassigned" | "assigned" | "all";

export function reconcileSourceOrderSelection(
  previous: MaintenanceSourceOrderRow[],
  visible: MaintenanceSourceOrderRow[],
  selectedKeys: ReadonlyArray<unknown>,
): MaintenanceSourceOrderRow[] {
  if (selectedKeys.length > 100) return previous;
  const known = new Map(
    [...previous, ...visible].map((row) => [row.raw_order_id, row]),
  );
  return selectedKeys
    .map((key) => known.get(String(key)))
    .filter((row): row is MaintenanceSourceOrderRow => row !== undefined);
}

function canWriteAssignments(): boolean {
  try {
    const permissions = JSON.parse(localStorage.getItem("permissions") || "{}");
    return permissions?.page_maintenance === true
      && permissions?.data_profit === true
      && permissions?.action_maintenance_project_manage === true;
  } catch {
    return false;
  }
}

function errorDetail(error: unknown): string {
  if (typeof error === "object" && error !== null && "response" in error) {
    const detail = (error as { response?: { data?: { detail?: unknown } } })
      .response?.data?.detail;
    if (typeof detail === "string") return detail;
  }
  return "操作失败，请检查网络后重试。";
}

function isConflictError(error: unknown): boolean {
  return typeof error === "object" && error !== null && "response" in error
    && (error as { response?: { status?: number } }).response?.status === 409;
}

export default function MaintenanceSourceOrderAssignmentsPage() {
  const initialProjectId = useMemo(
    () => new URLSearchParams(window.location.search).get("project_id") || undefined,
    [],
  );
  const [canManage] = useState(canWriteAssignments);
  const [rows, setRows] = useState<MaintenanceSourceOrderRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [assignmentStatus, setAssignmentStatus] = useState<AssignmentStatus>(
    initialProjectId ? "assigned" : "unassigned",
  );
  const [projectFilterId, setProjectFilterId] = useState<string | undefined>(initialProjectId);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedRows, setSelectedRows] = useState<MaintenanceSourceOrderRow[]>([]);
  const [projects, setProjects] = useState<MaintenanceProject[]>([]);
  const [projectLoading, setProjectLoading] = useState(false);
  const [assignOpen, setAssignOpen] = useState(false);
  const [actionMode, setActionMode] = useState<"assign" | "reassign">("assign");
  const [actionRow, setActionRow] = useState<MaintenanceSourceOrderRow | null>(null);
  const [unassignOpen, setUnassignOpen] = useState(false);
  const [targetProjectId, setTargetProjectId] = useState<string>();
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [writeError, setWriteError] = useState<string | null>(null);
  const [writeConflict, setWriteConflict] = useState(false);
  const generation = useRef(0);
  const projectGeneration = useRef(0);

  const load = useCallback(async (
    requestedPage: number,
    status: AssignmentStatus,
    search?: string,
    requestedProjectId?: string,
  ) => {
    const request = ++generation.current;
    setLoading(true);
    setLoadError(null);
    const params: Parameters<typeof listMaintenanceSourceOrders>[0] = {
      assignment_status: status,
      page: requestedPage,
      page_size: PAGE_SIZE,
    };
    if (search?.trim()) params.q = search.trim();
    if (requestedProjectId) params.project_id = requestedProjectId;
    try {
      const { data } = await listMaintenanceSourceOrders(params);
      if (request !== generation.current) return;
      setRows(data.rows ?? []);
      setTotal(data.total ?? 0);
      setPage(data.page ?? requestedPage);
    } catch (error) {
      if (request !== generation.current) return;
      setRows([]);
      setTotal(0);
      setLoadError(errorDetail(error));
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(1, initialProjectId ? "assigned" : "unassigned", undefined, initialProjectId);
    return () => { generation.current += 1; };
  }, [initialProjectId, load]);

  const loadProjects = useCallback(async (search?: string) => {
    const request = ++projectGeneration.current;
    setProjectLoading(true);
    try {
      const params: Parameters<typeof listMaintenanceProjects>[0] = {
        page: 1,
        page_size: 100,
      };
      if (search?.trim()) params.q = search.trim();
      const { data } = await listMaintenanceProjects(params);
      if (request !== projectGeneration.current) return;
      setProjects(data.rows ?? []);
    } catch {
      if (request !== projectGeneration.current) return;
      setProjects([]);
    } finally {
      if (request === projectGeneration.current) setProjectLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadProjects();
    return () => { projectGeneration.current += 1; };
  }, [loadProjects]);

  const clearWriteDialog = () => {
    setAssignOpen(false);
    setUnassignOpen(false);
    setActionRow(null);
    setTargetProjectId(undefined);
    setReason("");
    setWriteError(null);
    setWriteConflict(false);
  };

  const submitAssignment = async () => {
    const cleanReason = reason.trim();
    const items = actionMode === "reassign" && actionRow ? [actionRow] : selectedRows;
    if (!targetProjectId || !cleanReason || items.length === 0) return;
    setSaving(true);
    setWriteError(null);
    setWriteConflict(false);
    try {
      await assignMaintenanceSourceOrders({
        project_id: targetProjectId,
        items: items.map((row) => ({
          source_order_id: row.raw_order_id,
          expected_assignment_id: row.assignment_id,
          expected_version: row.assignment_version,
        })),
        reason: cleanReason,
      });
      message.success(actionMode === "reassign"
        ? "来源维保单已改派"
        : `已确认 ${items.length} 张来源维保单的项目归属`);
      setSelectedRows([]);
      clearWriteDialog();
      await load(page, assignmentStatus, query, projectFilterId);
    } catch (error) {
      setWriteError(errorDetail(error));
      setWriteConflict(isConflictError(error));
    } finally {
      setSaving(false);
    }
  };

  const submitUnassignment = async () => {
    if (!actionRow?.assignment_id || actionRow.assignment_version == null || !reason.trim()) return;
    setSaving(true);
    setWriteError(null);
    setWriteConflict(false);
    try {
      await unassignMaintenanceSourceOrders({
        items: [{
          assignment_id: actionRow.assignment_id,
          expected_version: actionRow.assignment_version,
        }],
        reason: reason.trim(),
      });
      message.success("来源维保单归属已撤销，历史记录保留");
      clearWriteDialog();
      await load(page, assignmentStatus, query, projectFilterId);
    } catch (error) {
      setWriteError(errorDetail(error));
      setWriteConflict(isConflictError(error));
    } finally {
      setSaving(false);
    }
  };

  const refreshConflictDraft = async () => {
    const draftIds = new Set([
      ...selectedRows.map((row) => row.raw_order_id),
      ...(actionRow ? [actionRow.raw_order_id] : []),
    ]);
    setWriteError(null);
    setWriteConflict(false);
    try {
      const { data } = await listMaintenanceSourceOrders({
        source_order_id: [...draftIds],
        assignment_status: "all",
        page: 1,
        page_size: 100,
      });
      const fresh = (data.rows ?? []).filter((row) => draftIds.has(row.raw_order_id));
      if (actionRow) {
        const next = fresh.find((row) => row.raw_order_id === actionRow.raw_order_id);
        if (next) setActionRow(next);
      } else {
        const freshById = new Map(fresh.map((row) => [row.raw_order_id, row]));
        setSelectedRows((previous) => previous.map(
          (row) => freshById.get(row.raw_order_id) ?? row,
        ));
      }
      message.info("目录版本已刷新，请重新核对后再次确认");
    } catch (error) {
      setWriteError(errorDetail(error));
    }
  };

  const columns = useMemo<ColumnsType<MaintenanceSourceOrderRow>>(() => {
    const base: ColumnsType<MaintenanceSourceOrderRow> = [
      { title: "业务单号", dataIndex: "order_no", key: "order_no", width: 170 },
      {
        title: "日期",
        dataIndex: "order_date",
        key: "order_date",
        width: 120,
        render: (value: string | null) => value || "—",
      },
      {
        title: "原项目名",
        dataIndex: "project_raw",
        key: "project_raw",
        render: (value: string | null) => value || "未提供",
      },
      {
        title: "历史标准化文字",
        dataIndex: "project_std",
        key: "project_std",
        render: (value: string | null) => value || "未提供",
      },
      {
        title: "当前归属",
        key: "assigned_project",
        render: (_value, row) => row.assigned_project ? (
          <Space size={4}>
            <Tag color="blue">{row.assigned_project.project_code}</Tag>
            <span>{row.assigned_project.display_name}</span>
          </Space>
        ) : <Tag>未归属</Tag>,
      },
    ];
    if (canManage) {
      base.push({
        title: "操作",
        key: "actions",
        width: 150,
        render: (_value, row) => row.assignment_id ? (
          <Space size={4}>
            <Button
              size="small"
              onClick={() => {
                setActionMode("reassign");
                setActionRow(row);
                setTargetProjectId(undefined);
                setReason("");
                setWriteError(null);
                setWriteConflict(false);
                setAssignOpen(true);
              }}
            >改派</Button>
            <Button
              size="small"
              danger
              onClick={() => {
                setActionRow(row);
                setReason("");
                setWriteError(null);
                setWriteConflict(false);
                setUnassignOpen(true);
              }}
            >撤销归属</Button>
          </Space>
        ) : "—",
      });
    }
    return base;
  }, [canManage]);

  return (
    <>
      <PageHeader
        title="历史维保单归属"
        subtitle="逐张确认来源维保单属于哪个稳定项目；系统不会按名称自动合并。"
      />
      <Space wrap style={{ marginBottom: 16 }}>
        <Input.Search
          aria-label="搜索来源维保单"
          placeholder="搜索业务单号或原项目名"
          value={query}
          allowClear
          style={{ width: 320 }}
          onChange={(event) => setQuery(event.target.value)}
          onSearch={() => void load(1, assignmentStatus, query, projectFilterId)}
        />
        <Select<AssignmentStatus>
          aria-label="归属状态"
          value={assignmentStatus}
          style={{ width: 150 }}
          options={[
            { value: "unassigned", label: "未归属" },
            { value: "assigned", label: "已归属" },
            { value: "all", label: "全部" },
          ]}
          onChange={(value) => {
            setAssignmentStatus(value);
            void load(1, value, query, projectFilterId);
          }}
        />
        <Select
          aria-label="稳定项目筛选"
          value={projectFilterId}
          allowClear
          showSearch
          filterOption={false}
          loading={projectLoading}
          placeholder="全部稳定项目"
          style={{ width: 240 }}
          options={projects.map((project) => ({
            value: project.project_id,
            label: `${project.project_code} · ${project.display_name}`,
          }))}
          onSearch={(value) => void loadProjects(value)}
          onChange={(value) => {
            setProjectFilterId(value);
            void load(1, assignmentStatus, query, value);
          }}
        />
        {canManage && (
          <Button
            type="primary"
            disabled={selectedRows.length === 0}
            onClick={() => {
              setActionMode("assign");
              setActionRow(null);
              setTargetProjectId(undefined);
              setReason("");
              setWriteError(null);
              setWriteConflict(false);
              setAssignOpen(true);
            }}
          >
            批量归属
          </Button>
        )}
      </Space>
      {loadError && <Alert type="error" showIcon message={loadError} />}
      <Table<MaintenanceSourceOrderRow>
        rowKey="raw_order_id"
        loading={loading}
        columns={columns}
        dataSource={rows}
        rowSelection={canManage ? {
          selectedRowKeys: selectedRows.map((row) => row.raw_order_id),
          preserveSelectedRowKeys: true,
          getCheckboxProps: (row) => ({ disabled: row.assignment_id !== null }),
          onChange: (keys) => {
            if (keys.length > 100) {
              message.warning("每批最多选择 100 张来源维保单");
              return;
            }
            setSelectedRows((previous) => reconcileSourceOrderSelection(
              previous,
              rows,
              keys,
            ));
          },
        } : undefined}
        pagination={{
          current: page,
          pageSize: PAGE_SIZE,
          total,
          showSizeChanger: false,
          showTotal: (count) => `共 ${count} 张来源维保单`,
          onChange: (nextPage) => void load(
            nextPage,
            assignmentStatus,
            query,
            projectFilterId,
          ),
        }}
        locale={{ emptyText: "暂无符合条件的来源维保单" }}
      />
      {canManage && (
        <Modal
          title={actionMode === "reassign" ? "改派来源维保单" : "批量归属来源维保单"}
          open={assignOpen}
          okText={actionMode === "reassign" ? "确认改派" : "确认归属"}
          cancelText="取消"
          confirmLoading={saving}
          okButtonProps={{
            disabled: !targetProjectId || !reason.trim()
              || (actionMode === "assign" && selectedRows.length === 0),
          }}
          maskClosable={!saving}
          closable={!saving}
          onCancel={clearWriteDialog}
          onOk={() => void submitAssignment()}
        >
          <p>{actionMode === "reassign"
            ? `将替换当前归属：${actionRow?.assigned_project?.project_code || "未知"}；旧记录仍会保留。`
            : `已明确选择 ${selectedRows.length} 张来源维保单；系统不会按名称补选。`}</p>
          {writeError && (
            <Alert
              type={writeConflict ? "warning" : "error"}
              showIcon
              style={{ marginBottom: 16 }}
              message={writeError}
              action={writeConflict ? (
                <Button size="small" onClick={() => void refreshConflictDraft()}>
                  刷新目录并保留草稿
                </Button>
              ) : undefined}
            />
          )}
          <Space direction="vertical" size={16} style={{ width: "100%" }}>
            <label htmlFor="source-assignment-project">
              <div style={{ marginBottom: 6 }}>目标稳定项目</div>
              <Select
                id="source-assignment-project"
                aria-label="目标稳定项目"
                value={targetProjectId}
                showSearch
                filterOption={false}
                loading={projectLoading}
                style={{ width: "100%" }}
                options={projects.filter((project) => project.is_active).map((project) => ({
                  value: project.project_id,
                  label: `${project.project_code} · ${project.display_name}`,
                }))}
                onChange={setTargetProjectId}
                onSearch={(value) => void loadProjects(value)}
              />
            </label>
            <label htmlFor="source-assignment-reason">
              <div style={{ marginBottom: 6 }}>
                {actionMode === "reassign" ? "改派原因" : "归属原因"}
              </div>
              <Input.TextArea
                id="source-assignment-reason"
                aria-label={actionMode === "reassign" ? "改派原因" : "归属原因"}
                value={reason}
                rows={4}
                maxLength={1000}
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
          </Space>
        </Modal>
      )}
      {canManage && (
        <Modal
          title="撤销来源维保单归属"
          open={unassignOpen}
          okText="确认撤销"
          okButtonProps={{ danger: true, disabled: !reason.trim() }}
          confirmLoading={saving}
          maskClosable={!saving}
          closable={!saving}
          onCancel={clearWriteDialog}
          onOk={() => void submitUnassignment()}
        >
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message="撤销后不会删除来源维保单，也不会删除历史归属记录。"
          />
          {writeError && (
            <Alert
              type={writeConflict ? "warning" : "error"}
              showIcon
              style={{ marginBottom: 16 }}
              message={writeError}
              action={writeConflict ? (
                <Button size="small" onClick={() => void refreshConflictDraft()}>
                  刷新目录并保留草稿
                </Button>
              ) : undefined}
            />
          )}
          <label htmlFor="source-unassignment-reason">
            <div style={{ marginBottom: 6 }}>撤销原因</div>
            <Input.TextArea
              id="source-unassignment-reason"
              aria-label="撤销原因"
              value={reason}
              rows={4}
              maxLength={1000}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
        </Modal>
      )}
    </>
  );
}
