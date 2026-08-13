import { useState } from "react";
import {
  Alert, Button, Card, Descriptions, Empty, Result, Space, Steps, Table,
  Tag, Upload,
} from "antd";
import { CloudUploadOutlined } from "@ant-design/icons";
import type { UploadFile } from "antd";

import {
  applyProjectImport,
  previewProjectImport,
  type ProjectImportPreview,
} from "../../api/maintenanceProjectImports";
import PageHeader from "../../components/PageHeader";
import { PAGE_TITLES } from "../../components/maintenance/maintenanceLanguage";

export default function MaintenanceProjectImportPage() {
  const [step, setStep] = useState<"upload" | "preview" | "applied" | "error">("upload");
  const [preview, setPreview] = useState<ProjectImportPreview | null>(null);
  const [importId, setImportId] = useState<number | null>(null);
  const [uploading, setUploading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleUpload = async (file: UploadFile) => {
    setUploading(true);
    setError(null);
    try {
      const { data } = await previewProjectImport(file as unknown as File);
      setPreview(data);
      setImportId(data.import_id);
      if (data.status === "error") {
        setStep("error");
        setError(data.errors?.join("；") ?? "文件解析失败");
      } else {
        setStep("preview");
      }
    } catch {
      setStep("error");
      setError("上传或解析失败，请检查文件格式是否为氚云导出的 .xls 文件");
    } finally {
      setUploading(false);
    }
    return false; // Prevent default upload behavior
  };

  const handleApply = async () => {
    if (importId == null) return;
    setApplying(true);
    try {
      const { data } = await applyProjectImport(importId);
      setStep("applied");
      setPreview((prev) => prev ? { ...prev, status: "applied" } : null);
    } catch (e: any) {
      setError(e?.response?.data?.detail ?? "应用失败，请重试");
    } finally {
      setApplying(false);
    }
  };

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title={PAGE_TITLES.adminProjectMaster}
        subtitle="上传氚云导出的维保项目表（.xls 格式）。系统会解析项目清单并预览新增和变更，确认后才会写入项目主档。"
      />
      <Card>
        <Steps current={step === "upload" ? 0 : step === "preview" ? 1 : step === "applied" ? 2 : 0} style={{ marginBottom: 24 }}>
          <Steps.Step title="上传项目表" description="选择氚云导出的 .xls 文件" />
          <Steps.Step title="查看变化" description="确认新增和更新的项目" />
          <Steps.Step title="确认同步" description="写入项目主档" />
        </Steps>

        {step === "upload" && (
          <Upload.Dragger
            accept=".xls,.xlsx"
            maxCount={1}
            beforeUpload={handleUpload}
            showUploadList={false}
            disabled={uploading}
          >
            <p className="ant-upload-drag-icon"><CloudUploadOutlined /></p>
            <p className="ant-upload-text">点击或拖拽氚云项目表到此处上传</p>
            <p className="ant-upload-hint">支持 .xls 格式（WPS Office 导出）</p>
          </Upload.Dragger>
        )}

        {uploading && <Alert type="info" message="正在解析文件……" showIcon />}

        {step === "error" && (
          <Result
            status="error"
            title="文件解析失败"
            subTitle={error}
            extra={<Button onClick={() => { setStep("upload"); setError(null); }}>重新上传</Button>}
          />
        )}

        {step === "preview" && preview && (
          <>
            <Descriptions bordered size="small" column={2} style={{ marginBottom: 16 }}>
              <Descriptions.Item label="总行数">{preview.row_count}</Descriptions.Item>
              <Descriptions.Item label="新增项目">
                <Tag color="green">{preview.new_count} 个</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="更新项目">
                <Tag color="blue">{preview.updated_count} 个</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="状态">{preview.status}</Descriptions.Item>
            </Descriptions>

            {preview.errors && preview.errors.length > 0 && (
              <Alert
                type="error"
                showIcon
                message="文件存在问题"
                description={preview.errors.join("；")}
                style={{ marginBottom: 16 }}
              />
            )}

            {preview.new_count > 0 && (
              <Card title="新增项目" size="small" style={{ marginBottom: 12 }}>
                <Table
                  rowKey="source_id"
                  size="small"
                  dataSource={preview.new_projects ?? []}
                  columns={[
                    { title: "订单编号", dataIndex: "source_id", width: 180 },
                    { title: "项目名称", dataIndex: "project_name" },
                    { title: "业务类型", dataIndex: "business_type", width: 100 },
                  ]}
                  pagination={false}
                />
              </Card>
            )}

            {preview.updated_count > 0 && (
              <Card title="更新项目" size="small" style={{ marginBottom: 16 }}>
                <Table
                  rowKey="source_id"
                  size="small"
                  dataSource={preview.updated_projects ?? []}
                  columns={[
                    { title: "订单编号", dataIndex: "source_id", width: 180 },
                    { title: "新名称", dataIndex: "project_name" },
                  ]}
                  pagination={false}
                />
              </Card>
            )}

            <Space>
              <Button type="primary" loading={applying} onClick={handleApply}>
                确认同步
              </Button>
              <Button onClick={() => { setStep("upload"); setPreview(null); }}>
                重新选择文件
              </Button>
            </Space>
          </>
        )}

        {step === "applied" && (
          <Result
            status="success"
            title="项目同步完成"
            subTitle={`新增 ${preview?.new_count ?? 0} 个项目，更新 ${preview?.updated_count ?? 0} 个项目`}
            extra={<Button onClick={() => { setStep("upload"); setPreview(null); }}>再导入一个文件</Button>}
          />
        )}
      </Card>
    </Space>
  );
}
