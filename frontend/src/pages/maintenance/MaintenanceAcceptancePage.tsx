import { useEffect, useRef, useState } from "react";
import { Alert, Button, Input, Select, Space, Table, Tag } from "antd";
import { Link } from "react-router-dom";

import {
  searchMaintenanceAcceptance,
  type MaintenanceAcceptanceDirectory,
} from "../../api/maintenanceOperations";
import PageHeader from "../../components/PageHeader";


export default function MaintenanceAcceptancePage() {
  const [directory, setDirectory] = useState<MaintenanceAcceptanceDirectory | null>(null);
  const [q, setQ] = useState("");
  const [submission, setSubmission] = useState<"not_submitted" | "submitted" | "not_configured" | undefined>();
  const [approval, setApproval] = useState<"not_reviewed" | "approved" | "rejected" | undefined>();
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const generation = useRef(0);

  useEffect(() => {
    const request = ++generation.current;
    setLoading(true);
    setError(false);
    void searchMaintenanceAcceptance({
      q,
      submission_status: submission,
      approval_status: approval,
      page,
      page_size: 24,
    }).then(({ data }) => {
      if (request === generation.current) setDirectory(data);
    }).catch(() => {
      if (request === generation.current) setError(true);
    }).finally(() => {
      if (request === generation.current) setLoading(false);
    });
    return () => { generation.current += 1; };
  }, [q, submission, approval, page]);

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title="验收报告"
        subtitle="按本人获准的项目范围搜索验收截止、附件、提交和审批状态；具体操作在项目工作台完成。"
      />
      <Alert
        type="warning"
        showIcon
        message="审批角色尚待业务确认"
        description="当前仅持有独立高风险审批权限的管理员可以审批，项目经理不能审批自己提交的报告。"
      />
      <Space wrap>
        <Input.Search
          aria-label="搜索验收项目"
          placeholder="搜索项目编号或名称"
          allowClear
          style={{ width: 320 }}
          onSearch={(value) => { setPage(1); setQ(value.trim()); }}
        />
        <Select
          aria-label="验收提交状态"
          allowClear
          placeholder="提交状态"
          style={{ width: 160 }}
          options={[
            { label: "未配置截止日", value: "not_configured" },
            { label: "未提交", value: "not_submitted" },
            { label: "已提交", value: "submitted" },
          ]}
          onChange={(value) => { setPage(1); setSubmission(value); }}
        />
        <Select
          aria-label="验收审批状态"
          allowClear
          placeholder="审批状态"
          style={{ width: 150 }}
          options={[
            { label: "待审批", value: "not_reviewed" },
            { label: "审批通过", value: "approved" },
            { label: "已驳回", value: "rejected" },
          ]}
          onChange={(value) => { setPage(1); setApproval(value); }}
        />
      </Space>
      {error && <Alert type="error" showIcon message="验收报告列表加载失败，请重试。" />}
      <Table
        rowKey="project_id"
        loading={loading}
        dataSource={directory?.rows ?? []}
        pagination={{
          current: directory?.page ?? page,
          pageSize: directory?.page_size ?? 24,
          total: directory?.total ?? 0,
          showSizeChanger: false,
          onChange: setPage,
        }}
        columns={[
          {
            title: "项目",
            render: (_, row) => (
              <div>
                <div>{row.project_code}</div>
                <div>{row.display_name}</div>
              </div>
            ),
          },
          {
            title: "验收截止日",
            render: (_, row) => row.acceptance.due_date || <Tag color="orange">待补</Tag>,
          },
          {
            title: "附件",
            render: (_, row) => row.acceptance.attachments.length
              ? <Tag color="green">{row.acceptance.attachments.length} 个有效附件</Tag>
              : <Tag color="orange">待上传</Tag>,
          },
          {
            title: "提交",
            render: (_, row) => row.acceptance.submission_status === "submitted"
              ? <Tag color="blue">已提交</Tag> : <Tag>未提交</Tag>,
          },
          {
            title: "审批",
            render: (_, row) => row.acceptance.approval_status === "approved"
              ? <Tag color="green">审批通过</Tag>
              : row.acceptance.approval_status === "rejected"
                ? <Tag color="red">已驳回</Tag>
                : <Tag color="gold">待审批</Tag>,
          },
          {
            title: "操作",
            render: (_, row) => (
              <Link to={`/maintenance/projects/${encodeURIComponent(row.project_id)}`}>
                <Button type="link">打开项目</Button>
              </Link>
            ),
          },
        ]}
      />
    </Space>
  );
}
