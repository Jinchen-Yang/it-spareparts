import { useCallback, useEffect, useRef, useState } from "react";
import {
  CheckCircleOutlined,
  DownloadOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  DatePicker,
  Descriptions,
  Space,
  Spin,
  Steps,
  Tag,
  Typography,
} from "antd";
import dayjs, { type Dayjs } from "dayjs";

import {
  applyMaintenanceManagerWorkbook,
  downloadMaintenanceManagerWorkbook,
  getMaintenanceManagerWorkbookStatus,
  validateMaintenanceManagerWorkbook,
  type MaintenanceManagerWorkbookStatus,
  type MaintenanceManagerWorkbookValidation,
} from "../../api/maintenanceOperations";
import PageHeader from "../../components/PageHeader";


function safeFilename(value: string): string {
  return value.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_");
}


function statusTag(status: MaintenanceManagerWorkbookStatus["latest_batch"]): JSX.Element {
  if (!status) return <Tag color="gold">本月待上传</Tag>;
  if (status.status === "applied") return <Tag color="green">本月已应用</Tag>;
  if (status.status === "valid") return <Tag color="blue">已校验待确认</Tag>;
  if (status.status === "expired") return <Tag>校验已过期</Tag>;
  return <Tag color="red">最近校验未通过</Tag>;
}


export default function MaintenanceManagerWorkbookPage() {
  const [month, setMonth] = useState<Dayjs>(() => dayjs().startOf("month"));
  const [summary, setSummary] = useState<MaintenanceManagerWorkbookStatus | null>(null);
  const [validation, setValidation] = useState<MaintenanceManagerWorkbookValidation | null>(null);
  const [loading, setLoading] = useState(true);
  const [downloading, setDownloading] = useState(false);
  const [validating, setValidating] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const generation = useRef(0);
  const downloadLock = useRef(false);
  const applyLock = useRef(false);
  const reportMonth = month.format("YYYY-MM");

  const loadStatus = useCallback(async () => {
    const request = ++generation.current;
    setLoading(true);
    setError(null);
    try {
      const { data } = await getMaintenanceManagerWorkbookStatus(reportMonth);
      if (request === generation.current) setSummary(data);
    } catch (reason: unknown) {
      if (request !== generation.current) return;
      setSummary(null);
      const response = (reason as {
        response?: { status?: number; data?: { detail?: unknown } };
      } | null)?.response;
      const detail = response?.data?.detail;
      setError(response?.status === 403 && typeof detail === "string" && detail.includes("未分配")
        ? "当前账号尚未分配任何有效维保项目，请先完成项目负责人配置。"
        : response?.status === 403
          ? "当前账号没有查看全部合同额所需的数据权限，请联系管理员配置后再使用。"
        : "月度工作簿状态加载失败，请重试。");
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, [reportMonth]);

  useEffect(() => {
    void loadStatus();
    return () => { generation.current += 1; };
  }, [loadStatus]);

  const changeMonth = (next: Dayjs | null) => {
    if (!next) return;
    generation.current += 1;
    setMonth(next.startOf("month"));
    setSummary(null);
    setValidation(null);
    setSuccess(null);
    setError(null);
  };

  const download = async () => {
    if (downloadLock.current) return;
    downloadLock.current = true;
    setDownloading(true);
    setError(null);
    setSuccess(null);
    try {
      const { data } = await downloadMaintenanceManagerWorkbook(reportMonth);
      const url = URL.createObjectURL(data);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = safeFilename(`维保项目经理月度全量工作簿_${reportMonth}.xlsx`);
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      setSuccess("已下载本人负责项目的全量工作簿，可在原文件中追加或更新后回传。");
    } catch {
      setError("全量工作簿下载失败，请确认账号权限后重试。");
    } finally {
      downloadLock.current = false;
      setDownloading(false);
    }
  };

  const chooseFile = async (file: File | undefined) => {
    if (!file) return;
    if (!file.name.toLocaleLowerCase().endsWith(".xlsx")) {
      setError("只接受系统生成并回填的 .xlsx 工作簿。");
      if (inputRef.current) inputRef.current.value = "";
      return;
    }
    const request = ++generation.current;
    setValidating(true);
    setValidation(null);
    setError(null);
    setSuccess(null);
    try {
      const { data } = await validateMaintenanceManagerWorkbook(reportMonth, file);
      if (request === generation.current) setValidation(data);
    } catch {
      if (request === generation.current) {
        setError("文件校验失败，系统没有写入任何业务数据。请重新下载本月全量表后回填。");
      }
    } finally {
      if (request === generation.current) {
        setValidating(false);
        if (inputRef.current) inputRef.current.value = "";
      }
    }
  };

  const apply = async () => {
    if (!validation?.can_apply || applyLock.current) return;
    applyLock.current = true;
    setApplying(true);
    setError(null);
    setSuccess(null);
    try {
      const { data } = await applyMaintenanceManagerWorkbook({
        validation_token: validation.validation_token,
        data_version: validation.data_version,
      });
      setValidation(null);
      setSuccess(data.changed_rows === 0
        ? "已确认本月全量表无数据变化，本月任务已关闭"
        : `已应用 ${data.changed_rows.toLocaleString("zh-CN")} 项更新，本月任务已关闭`);
      await loadStatus();
    } catch {
      setError("确认应用失败。项目范围或数据可能已变化；本次未写入，请重新下载后回填。");
    } finally {
      applyLock.current = false;
      setApplying(false);
    }
  };

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <PageHeader
        title="项目经理月度全量工作簿"
        subtitle="下载本人负责项目的全量表，追加或更新计划后回传；系统先给出校验预览，确认后才一次性应用。"
      />

      <Alert
        type="warning"
        showIcon
        message="计划回款不等于财务确认实收"
        description="24 期计划节点只用于项目跟踪和提醒。财务确认实收始终只读，上传工作簿不会覆盖、补写或删除实收记录。"
      />

      <Card>
        <Steps
          responsive
          items={[
            { title: "下载全量表", description: "系统生成本人项目与 24 期空位" },
            { title: "追加或更新", description: "原文件内补期限和计划节点" },
            { title: "校验预览", description: "先看变更、警告与冲突" },
            { title: "确认应用", description: "全部成功或全部不写" },
          ]}
        />
      </Card>

      <Card
        title="本月处理范围"
        extra={summary ? statusTag(summary.latest_batch) : null}
      >
        <Space direction="vertical" size={16} style={{ width: "100%" }}>
          <Space wrap>
            <DatePicker
              picker="month"
              allowClear={false}
              value={month}
              disabled={validating || applying}
              disabledDate={(current) => current.startOf("month").isAfter(dayjs().startOf("month"))}
              aria-label="选择月度工作簿月份"
              onChange={changeMonth}
            />
            <Button
              aria-label="下载本月全量表"
              icon={<DownloadOutlined />}
              loading={downloading}
              disabled={loading || !summary}
              onClick={() => void download()}
            >
              下载本月全量表
            </Button>
            <Button
              aria-label="上传回填后的全量表"
              icon={<UploadOutlined />}
              disabled={loading || validating || applying || !summary}
              onClick={() => inputRef.current?.click()}
            >
              上传回填后的全量表
            </Button>
            <input
              ref={inputRef}
              hidden
              type="file"
              accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              aria-label="选择项目经理月度工作簿"
              onChange={(event) => void chooseFile(event.target.files?.[0])}
            />
          </Space>

          {loading ? (
            <Spin size="small" tip="正在确认本人项目范围"><span /></Spin>
          ) : summary ? (
            <Descriptions bordered size="small" column={{ xs: 1, sm: 2, lg: 3 }}>
              <Descriptions.Item label="范围">
                本人负责项目 {summary.project_count.toLocaleString("zh-CN")} 个
              </Descriptions.Item>
              <Descriptions.Item label="审批角色">
                {summary.approval_role === "pending_business_configuration"
                  ? <Tag color="gold">待业务配置</Tag>
                  : <Tag color="green">已配置</Tag>}
              </Descriptions.Item>
              <Descriptions.Item label="附件载体">
                {summary.attachment_carrier === "pending_business_configuration"
                  ? <Tag color="gold">待业务配置</Tag>
                  : <Tag color="green">已配置</Tag>}
              </Descriptions.Item>
            </Descriptions>
          ) : null}
        </Space>
      </Card>

      {validating && <Spin tip="正在做整表安全校验与冲突预览"><span /></Spin>}
      {error && (
        <Alert
          type="error"
          showIcon
          message={error}
          action={!summary ? (
            <Button size="small" onClick={() => void loadStatus()}>
              重新检查状态
            </Button>
          ) : undefined}
        />
      )}
      {success && <Alert type="success" showIcon message={success} />}

      {validation && (
        <Card
          data-testid="manager-workbook-validation-preview"
          title="校验预览（尚未写入）"
          extra={<Tag color={validation.can_apply ? "green" : "red"}>
            {validation.can_apply ? "可确认" : "不可应用"}
          </Tag>}
        >
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            {validation.already_applied ? (
              <Alert type="success" showIcon message="这份工作簿已应用，无需重复操作" />
            ) : (
              <Descriptions bordered size="small" column={{ xs: 1, sm: 3 }}>
                <Descriptions.Item label="变更">
                  维保期限 {validation.changes.service_periods.toLocaleString("zh-CN")} 项
                </Descriptions.Item>
                <Descriptions.Item label="变更">
                  计划回款节点 {validation.changes.planned_collection_milestones.toLocaleString("zh-CN")} 项
                </Descriptions.Item>
                <Descriptions.Item label="写入方式">
                  原子应用（全部成功或全部不写）
                </Descriptions.Item>
              </Descriptions>
            )}
            {validation.unchanged && !validation.already_applied && (
              <Alert
                type="info"
                showIcon
                message="未检测到数据变化；仍可确认完成本月全量更新任务"
              />
            )}
            {validation.warnings.map((issue, index) => (
              <Alert
                key={`${issue.code}-${issue.row ?? index}`}
                type="warning"
                showIcon
                message={issue.message}
                description={issue.sheet && issue.row
                  ? `${issue.sheet} · 第 ${issue.row} 行`
                  : undefined}
              />
            ))}
            {validation.errors.map((issue, index) => (
              <Alert
                key={`${issue.code}-${issue.row ?? index}`}
                type="error"
                showIcon
                message={issue.message}
                description={issue.sheet && issue.row
                  ? `${issue.sheet} · 第 ${issue.row} 行`
                  : undefined}
              />
            ))}
            {validation.can_apply && (
              <Space direction="vertical" size={6}>
                <Typography.Text type="secondary">
                  确认后系统会再次核对负责人范围、文件版本和每条记录版本；任一冲突都会整批取消。
                </Typography.Text>
                <Button
                  type="primary"
                  icon={<CheckCircleOutlined />}
                  loading={applying}
                  aria-label="确认原子应用"
                  onClick={() => void apply()}
                >
                  确认原子应用
                </Button>
              </Space>
            )}
          </Space>
        </Card>
      )}
    </Space>
  );
}
