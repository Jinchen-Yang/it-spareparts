import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Empty, Select, Space, Spin, Tag } from "antd";
import { useSearchParams } from "react-router-dom";

import {
  getMaintenanceProjectWorkspace,
  listMaintenanceProjectOperations,
  type MaintenanceProjectOperationsSummary,
  type MaintenanceProjectWorkspace,
} from "../../api/maintenanceOperations";
import ProjectWorkbookActions from "../../components/maintenance/ProjectWorkbookActions";
import WorkbookFourSheetPreview from "../../components/maintenance/WorkbookFourSheetPreview";
import PageHeader from "../../components/PageHeader";
import "../../components/maintenance/maintenanceOperations.css";

export default function MaintenanceProjectUpdatesPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [projects, setProjects] = useState<MaintenanceProjectOperationsSummary[]>([]);
  const [projectId, setProjectId] = useState(searchParams.get("project_id") || "");
  const [workspace, setWorkspace] = useState<MaintenanceProjectWorkspace | null>(null);
  const [loadingProjects, setLoadingProjects] = useState(true);
  const [loadingWorkspace, setLoadingWorkspace] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const generation = useRef(0);
  const projectListGeneration = useRef(0);

  const loadProjectOptions = useCallback(async (q = "") => {
    const request = ++projectListGeneration.current;
    setLoadingProjects(true);
    try {
      const { data } = await listMaintenanceProjectOperations({
        page: 1,
        page_size: 200,
        q: q || undefined,
      });
      if (request === projectListGeneration.current) setProjects(data.rows);
    } catch {
      if (request === projectListGeneration.current) setProjects([]);
    } finally {
      if (request === projectListGeneration.current) setLoadingProjects(false);
    }
  }, []);

  useEffect(() => {
    void loadProjectOptions();
    return () => { projectListGeneration.current += 1; };
  }, [loadProjectOptions]);

  const loadWorkspace = useCallback(async () => {
    if (!projectId) {
      setWorkspace(null);
      setLoadError(false);
      return;
    }
    const request = ++generation.current;
    setLoadingWorkspace(true);
    setLoadError(false);
    try {
      const { data } = await getMaintenanceProjectWorkspace(projectId);
      if (request === generation.current) setWorkspace(data);
    } catch {
      if (request === generation.current) {
        setWorkspace(null);
        setLoadError(true);
      }
    } finally {
      if (request === generation.current) setLoadingWorkspace(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadWorkspace();
    return () => { generation.current += 1; };
  }, [loadWorkspace]);

  const selectProject = (value: string) => {
    setProjectId(value);
    setSearchParams({ project_id: value }, { replace: true });
  };
  const optionProjects = workspace
    && !projects.some((project) => project.project_id === workspace.project.project_id)
    ? [workspace.project, ...projects]
    : projects;

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title="维保项目月度更新"
        subtitle="先在页面看清四张表的内容和写入边界，再下载全量表、追加回款记录并回传校验。"
      />
      <Card title="选择项目">
        <Select
          showSearch
          loading={loadingProjects}
          style={{ width: "min(100%, 520px)" }}
          placeholder="按项目编号或名称选择"
          value={projectId || undefined}
          optionFilterProp="label"
          filterOption={false}
          options={optionProjects.map((project) => ({
            value: project.project_id,
            label: `${project.project_code} · ${project.display_name}`,
          }))}
          onSearch={(value) => void loadProjectOptions(value.trim())}
          onOpenChange={(open) => {
            if (open && projects.length === 0) void loadProjectOptions();
          }}
          onChange={selectProject}
        />
      </Card>

      {loadError && (
        <Alert
          type="error"
          showIcon
          message="项目工作簿信息加载失败"
          action={<Button size="small" danger onClick={() => void loadWorkspace()}>重试</Button>}
        />
      )}
      {loadingWorkspace && <Spin tip="正在读取项目工作簿"><span /></Spin>}
      {!projectId && !loadingWorkspace && <Card><Empty description="请选择要更新的项目" /></Card>}

      {workspace && (
        <Card
          title={workspace.project.display_name}
          extra={<Tag>{workspace.project.project_code}</Tag>}
        >
          <Space direction="vertical" size={16} style={{ width: "100%" }}>
            <Alert
              type="info"
              showIcon
              message="完整四表内容"
              description="01_总览仅允许在回款表尾追加新记录；备件消耗、报销单、项目经理追踪与提醒均由系统生成，上传时会校验是否被改动。"
            />
            <WorkbookFourSheetPreview preview={workspace.workbook_preview} />
            <ProjectWorkbookActions
              projectId={workspace.project.project_id}
              projectCode={workspace.project.project_code}
              onApplied={loadWorkspace}
            />
          </Space>
        </Card>
      )}
    </Space>
  );
}
