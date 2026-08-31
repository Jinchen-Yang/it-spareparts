import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Checkbox, Input, Modal, Select, Space, Tag, Typography, message } from "antd";

import {
  archiveMaintenanceProjectManager,
  assignMaintenanceProjectManager,
  searchMaintenanceManagerAccounts,
  type MaintenanceManagerAccount,
  type MaintenanceProjectOperationsSummary,
} from "../../api/maintenanceOperations";

type ManagerProjectContext = Pick<
  MaintenanceProjectOperationsSummary,
  "project_id" | "project_manager_id" | "manager_assignment"
>;

export default function ProjectManagerAssignmentControl({
  project,
  canManage,
  onChanged,
}: {
  project: ManagerProjectContext;
  canManage: boolean;
  onChanged: () => Promise<boolean> | boolean | void;
}) {
  const [open, setOpen] = useState(false);
  const [accounts, setAccounts] = useState<MaintenanceManagerAccount[]>([]);
  const [selectedUserId, setSelectedUserId] = useState<number>();
  const [reason, setReason] = useState("");
  const [syncSalesperson, setSyncSalesperson] = useState(true);
  const [searching, setSearching] = useState(false);
  const [saving, setSaving] = useState(false);
  const searchController = useRef<AbortController | null>(null);

  const searchAccounts = useCallback(async (q = "") => {
    searchController.current?.abort();
    const controller = new AbortController();
    searchController.current = controller;
    setSearching(true);
    try {
      const { data } = await searchMaintenanceManagerAccounts(
        { q, page: 1, page_size: 30 },
        { signal: controller.signal },
      );
      if (!controller.signal.aborted) setAccounts(data.rows ?? []);
    } catch {
      if (!controller.signal.aborted) message.error("负责人候选账号加载失败");
    } finally {
      if (!controller.signal.aborted) setSearching(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    void searchAccounts();
    return () => searchController.current?.abort();
  }, [open, searchAccounts]);

  if (!canManage) return null;

  const close = () => {
    searchController.current?.abort();
    setOpen(false);
    setSelectedUserId(undefined);
    setReason("");
    setSyncSalesperson(true);
  };
  const saveAssignment = async () => {
    const cleanedReason = reason.trim();
    if (!selectedUserId || !cleanedReason) return;
    setSaving(true);
    try {
      await assignMaintenanceProjectManager(project.project_id, {
        user_id: selectedUserId,
        expected_assignment_id: project.manager_assignment?.assignment_id ?? null,
        expected_assignment_version: project.manager_assignment?.version ?? null,
        sync_salesperson: syncSalesperson,
        reason: cleanedReason,
      });
      const action = project.manager_assignment ? "项目负责人已改派" : "项目负责人已映射";
      const refreshed = await onChanged();
      close();
      if (refreshed === false) {
        message.warning(`${action}，但页面刷新失败；旧数据已失效，请重试。`);
      } else {
        message.success(`${action}并刷新`);
      }
    } catch (error: unknown) {
      const status = (error as { response?: { status?: number } })?.response?.status;
      message.error(status === 409 ? "负责人关系已变化，请刷新后重试" : "负责人映射失败");
    } finally {
      setSaving(false);
    }
  };
  const archiveAssignment = async () => {
    const assignment = project.manager_assignment;
    const cleanedReason = reason.trim();
    if (!assignment || !cleanedReason) return;
    setSaving(true);
    try {
      await archiveMaintenanceProjectManager(assignment.assignment_id, {
        version: assignment.version,
        reason: cleanedReason,
      });
      const refreshed = await onChanged();
      close();
      if (refreshed === false) {
        message.warning("负责人关系已归档，但页面刷新失败；旧数据已失效，请重试。");
      } else {
        message.success("负责人关系已归档并刷新");
      }
    } catch (error: unknown) {
      const status = (error as { response?: { status?: number } })?.response?.status;
      message.error(status === 409 ? "负责人关系已变化，请刷新后重试" : "负责人关系归档失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <Button type="link" onClick={() => setOpen(true)}>
        {project.manager_assignment ? "管理负责人" : "映射负责人"}
      </Button>
      <Modal
        title="项目负责人账号映射"
        open={open}
        onCancel={close}
        footer={null}
        destroyOnHidden
      >
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <div>
            <Typography.Text type="secondary">源文保留：</Typography.Text>
            <Typography.Text>{project.project_manager_id || "未提供"}</Typography.Text>
          </div>
          {project.manager_assignment && (
            <div>
              <Typography.Text type="secondary">当前账号：</Typography.Text>
              <Typography.Text>
                {project.manager_assignment.display_name || project.manager_assignment.username}
                {` · ${project.manager_assignment.username}`}
              </Typography.Text>
              {project.manager_assignment.account_status === "inactive" && (
                <Tag color="red" style={{ marginInlineStart: 8 }}>负责人账号失效</Tag>
              )}
            </div>
          )}
          <Input.Search
            aria-label="搜索负责人账号"
            placeholder="按姓名或用户名搜索"
            allowClear
            onSearch={(value) => void searchAccounts(value)}
          />
          <Select
            aria-label="选择负责人账号"
            showSearch={false}
            loading={searching}
            value={selectedUserId}
            placeholder="请选择唯一系统账号"
            options={accounts.map((account) => ({
              value: account.user_id,
              label: `${account.display_name || "未设置姓名"} · ${account.username}`,
            }))}
            onChange={(value) => setSelectedUserId(value)}
          />
          <Input.TextArea
            aria-label="负责人映射或改派原因"
            value={reason}
            rows={3}
            maxLength={1000}
            showCount
            placeholder="必填：说明映射、改派或归档原因"
            onChange={(event) => setReason(event.target.value)}
          />
          <Checkbox
            checked={syncSalesperson}
            onChange={(event) => setSyncSalesperson(event.target.checked)}
          >
            同时同步销售人员
          </Checkbox>
          <Space style={{ justifyContent: "flex-end", width: "100%" }}>
            {project.manager_assignment && (
              <Button
                danger
                loading={saving}
                disabled={!reason.trim()}
                onClick={() => void archiveAssignment()}
              >
                归档当前关系
              </Button>
            )}
            <Button
              type="primary"
              loading={saving}
              disabled={!selectedUserId || !reason.trim()}
              onClick={() => void saveAssignment()}
            >
              {project.manager_assignment ? "确认改派" : "确认映射"}
            </Button>
          </Space>
        </Space>
      </Modal>
    </>
  );
}
