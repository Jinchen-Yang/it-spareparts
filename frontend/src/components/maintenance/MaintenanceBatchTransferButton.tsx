import {
  CloudDownloadOutlined,
  CloudUploadOutlined,
  InboxOutlined,
  SwapOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Collapse,
  Descriptions,
  Divider,
  Modal,
  Row,
  Segmented,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Typography,
  Upload,
  message,
} from "antd";
import type { UploadFile, UploadProps } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Key } from "react";

import {
  applyMaintenanceBatchTransfer,
  downloadMaintenanceBatchTransfer,
  getMaintenanceBatchTransferOptions,
  previewMaintenanceBatchTransfer,
  type MaintenanceBatchApplyResponse,
  type MaintenanceBatchDownloadField,
  type MaintenanceBatchDownloadInput,
  type MaintenanceBatchMatchState,
  type MaintenanceBatchPreviewFile,
  type MaintenanceBatchPreviewResponse,
  type MaintenanceBatchPreviewRow,
  type MaintenanceBatchTransferOptions,
} from "../../api/maintenanceBatchTransfer";
import { saveBlob } from "../../api/maintenanceWorkbooks";

const { Text, Paragraph } = Typography;
const DEFAULT_MAX_FILES = 20;

const MATCH_LABELS: Record<MaintenanceBatchMatchState, string> = {
  matched: "已匹配",
  ambiguous: "有歧义",
  unmatched: "未匹配",
  invalid: "无效",
};

const MATCH_COLORS: Record<MaintenanceBatchMatchState, string> = {
  matched: "green",
  ambiguous: "orange",
  unmatched: "default",
  invalid: "red",
};

const ACTION_LABELS: Record<string, string> = {
  create_project: "新建项目",
  create_contract: "新建合同",
  update_contract: "更新合同",
  upsert_collection_snapshot: "更新回款快照",
  skip: "跳过",
  block: "阻断",
};

type MatchFilter = "all" | MaintenanceBatchMatchState;

export interface MaintenanceBatchTransferButtonProps {
  filters: Omit<MaintenanceBatchDownloadInput, "forms" | "fields">;
  onApplied: () => void | Promise<unknown>;
}

function readDetail(data: unknown): string | null {
  if (!data || typeof data !== "object" || !("detail" in data)) return null;
  const detail = (data as { detail: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    return String((detail as { message: unknown }).message);
  }
  return null;
}

async function errorMessage(error: unknown, fallback: string): Promise<string> {
  const data = (error as { response?: { data?: unknown } })?.response?.data;
  if (data instanceof Blob) {
    try {
      return readDetail(JSON.parse(await data.text())) ?? fallback;
    } catch {
      return fallback;
    }
  }
  return readDetail(data) ?? fallback;
}

function errorStatus(error: unknown): number | null {
  return (error as { response?: { status?: number } })?.response?.status ?? null;
}

function rawFile(file: UploadFile): File | null {
  return file.originFileObj ?? (file as unknown as File);
}

function rowCanApply(row: MaintenanceBatchPreviewRow): boolean {
  return row.match_state === "matched"
    && row.row_status === "ready"
    && row.action !== "skip"
    && row.action !== "block"
    && row.errors.length === 0;
}

function countsFromRows(rows: MaintenanceBatchPreviewRow[]) {
  return rows.reduce(
    (counts, row) => ({ ...counts, [row.match_state]: counts[row.match_state] + 1 }),
    { matched: 0, ambiguous: 0, unmatched: 0, invalid: 0 },
  );
}

function issueText(row: MaintenanceBatchPreviewRow): string {
  const issues = [...row.errors, ...row.warnings];
  if (issues.length) return issues.map((issue) => issue.message).join("；");
  if (row.match_state === "ambiguous" && row.candidates?.length) {
    return `候选：${row.candidates.map((item) => item.project_name).join("、")}`;
  }
  if (row.match_state === "unmatched") return "未找到唯一项目/合同";
  return "—";
}

function canonicalText(row: MaintenanceBatchPreviewRow): string {
  const pairs = Object.entries(row.canonical)
    .filter(([, value]) => value !== null && value !== "")
    .slice(0, 4)
    .map(([key, value]) => `${key}: ${String(value)}`);
  return pairs.length ? pairs.join("；") : "—";
}

function MappingPreview({ file }: { file: MaintenanceBatchPreviewFile }) {
  const columns: ColumnsType<MaintenanceBatchPreviewFile["detected_fields"][number]> = [
    { title: "源列", dataIndex: "source_column", width: 170 },
    {
      title: "识别为",
      key: "canonical",
      render: (_, field) => field.canonical_label || field.canonical_field || "未映射",
    },
    {
      title: "置信方式",
      dataIndex: "confidence",
      width: 100,
      render: (value: string) => <Tag>{value}</Tag>,
    },
    {
      title: "约束/口径",
      key: "basis",
      width: 220,
      render: (_, field) => (
        <Space size={4} wrap>
          {field.required ? <Tag color="red">必填</Tag> : null}
          {field.metric_basis ? <Text type="secondary">{field.metric_basis}</Text> : null}
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={10} style={{ width: "100%" }}>
      <Descriptions size="small" column={{ xs: 1, sm: 3 }}>
        <Descriptions.Item label="识别类型">{file.import_kind}</Descriptions.Item>
        <Descriptions.Item label="工作表">{file.detected_sheet || "—"}</Descriptions.Item>
        <Descriptions.Item label="表头行">
          {file.header_rows.length ? file.header_rows.join("、") : "—"}
        </Descriptions.Item>
      </Descriptions>
      {file.mapping_conflicts.map((conflict, index) => (
        <Alert
          key={`${conflict.canonical_field ?? "conflict"}-${index}`}
          type="warning"
          showIcon
          message={conflict.message}
          description={conflict.source_columns.join("、")}
        />
      ))}
      <Table
        size="small"
        pagination={false}
        rowKey={(field) => `${field.source_column}-${field.canonical_field ?? "unmapped"}`}
        dataSource={file.detected_fields}
        columns={columns}
        scroll={{ x: 620 }}
      />
    </Space>
  );
}

interface ImportPanelProps {
  options: MaintenanceBatchTransferOptions;
  onApplied: () => void | Promise<unknown>;
}

function ImportPanel({ options, onApplied }: ImportPanelProps) {
  const [files, setFiles] = useState<UploadFile[]>([]);
  const [preview, setPreview] = useState<MaintenanceBatchPreviewResponse | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<MatchFilter>("all");
  const [selectedRowKeys, setSelectedRowKeys] = useState<Key[]>([]);
  const [result, setResult] = useState<MaintenanceBatchApplyResponse | null>(null);
  const requestGeneration = useRef(0);

  const maxFiles = options.max_files || DEFAULT_MAX_FILES;
  const accepted = options.accepted_extensions.length
    ? options.accepted_extensions.join(",")
    : ".xlsx";

  useEffect(() => () => {
    requestGeneration.current += 1;
  }, []);

  const invalidatePreview = () => {
    requestGeneration.current += 1;
    setPreview(null);
    setResult(null);
    setSelectedRowKeys([]);
    setFilter("all");
    setError(null);
  };

  const uploadProps: UploadProps = {
    accept: accepted,
    multiple: true,
    fileList: files,
    disabled: previewing || applying,
    beforeUpload(file) {
      const isXlsx = /\.xlsx$/i.test(file.name);
      if (!isXlsx) {
        message.error(`${file.name} 不是 .xlsx 文件`);
        return Upload.LIST_IGNORE;
      }
      return false;
    },
    onChange(info) {
      invalidatePreview();
      setFiles(info.fileList.slice(0, maxFiles));
      if (info.fileList.length > maxFiles) {
        message.warning(`一次最多选择 ${maxFiles} 个文件`);
      }
    },
    onRemove() {
      invalidatePreview();
      return true;
    },
  };

  const runPreview = async () => {
    if (!files.length || previewing || applying) return;
    const sourceFiles = files.map(rawFile).filter((file): file is File => Boolean(file));
    if (sourceFiles.length !== files.length) {
      setError("部分文件无法读取，请移除后重新选择");
      return;
    }
    const generation = ++requestGeneration.current;
    setPreviewing(true);
    setError(null);
    setResult(null);
    try {
      const { data } = await previewMaintenanceBatchTransfer(sourceFiles);
      if (generation !== requestGeneration.current) return;
      setPreview(data);
      setSelectedRowKeys(data.rows.filter(rowCanApply).map((row) => row.row_key));
      setFilter("all");
    } catch (reason) {
      if (generation !== requestGeneration.current) return;
      setError(await errorMessage(reason, "批量预览失败，请检查文件后重试"));
    } finally {
      if (generation === requestGeneration.current) setPreviewing(false);
    }
  };

  const counts = useMemo(
    () => countsFromRows(preview?.rows ?? []),
    [preview],
  );
  const visibleRows = useMemo(
    () => preview?.rows.filter((row) => filter === "all" || row.match_state === filter) ?? [],
    [filter, preview],
  );
  const selectableKeys = useMemo(
    () => new Set(preview?.rows.filter(rowCanApply).map((row) => row.row_key) ?? []),
    [preview],
  );
  const safeSelectedKeys = selectedRowKeys.filter((key) => selectableKeys.has(String(key)));

  const apply = async () => {
    if (!preview || !safeSelectedKeys.length || applying) return;
    const generation = requestGeneration.current;
    setApplying(true);
    setError(null);
    try {
      const { data } = await applyMaintenanceBatchTransfer({
        preview_token: preview.preview_token,
        payload_hash: preview.payload_hash,
        data_version: preview.data_version,
        row_keys: safeSelectedKeys.map(String),
      });
      // 用户可能在网络请求期间关闭弹窗；后端一旦成功，主页仍必须刷新，不能因
      // ImportPanel 已卸载而留下旧卡片。弹窗内状态只在本次会话仍有效时更新。
      if (generation === requestGeneration.current) {
        setResult(data);
        setSelectedRowKeys([]);
      }
      await onApplied();
      message.success(`批量提交完成：成功 ${data.applied} 行`);
    } catch (reason) {
      if (generation !== requestGeneration.current) return;
      const status = errorStatus(reason);
      setError(
        status === 409
          ? "预览已过期或数据版本已变化，请重新预览后再提交"
          : status === 403
            ? "当前账号没有批量导入权限"
            : await errorMessage(reason, "批量提交失败，请稍后重试"),
      );
    } finally {
      if (generation === requestGeneration.current) setApplying(false);
    }
  };

  const rowColumns: ColumnsType<MaintenanceBatchPreviewRow> = [
    {
      title: "来源",
      key: "source",
      width: 190,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Text>{row.filename}</Text>
          <Text type="secondary">
            {row.detected_sheet ? `${row.detected_sheet} · ` : ""}第 {row.source_row} 行
          </Text>
        </Space>
      ),
    },
    {
      title: "匹配",
      key: "match",
      width: 190,
      render: (_, row) => (
        <Space direction="vertical" size={2}>
          <Tag color={MATCH_COLORS[row.match_state]}>{MATCH_LABELS[row.match_state]}</Tag>
          <Text>{row.matched_project_name || "—"}</Text>
        </Space>
      ),
    },
    {
      title: "动作",
      dataIndex: "action",
      width: 130,
      render: (value: string) => ACTION_LABELS[value] || value,
    },
    {
      title: "识别内容",
      key: "canonical",
      ellipsis: true,
      render: (_, row) => <Text title={canonicalText(row)}>{canonicalText(row)}</Text>,
    },
    {
      title: "问题/候选",
      key: "issues",
      width: 260,
      ellipsis: true,
      render: (_, row) => <Text title={issueText(row)}>{issueText(row)}</Text>,
    },
  ];

  const resultColumns: ColumnsType<MaintenanceBatchApplyResponse["rows"][number]> = [
    { title: "文件", dataIndex: "source_file", render: (value) => value || "—" },
    { title: "源行", dataIndex: "source_row", width: 80, render: (value) => value ?? "—" },
    {
      title: "结果",
      dataIndex: "status",
      width: 110,
      render: (value: string) => (
        <Tag color={value === "applied" ? "green" : value === "skipped" ? "default" : "red"}>
          {value}
        </Tag>
      ),
    },
    { title: "动作", dataIndex: "action", width: 130, render: (value) => ACTION_LABELS[value] || value || "—" },
    { title: "说明", dataIndex: "message", render: (value) => value || "—" },
  ];

  if (!options.can_import) {
    return <Alert type="info" showIcon message="当前账号没有批量导入权限" />;
  }

  return (
    <Space direction="vertical" size={14} style={{ width: "100%" }}>
      <Alert
        type="info"
        showIcon
        message="先预览，再提交"
        description="系统自动识别表单、字段和项目归属。字段映射只读展示；正式提交只消费冻结的预览凭证。歧义、未匹配或无效行不能勾选。"
      />

      <Upload.Dragger {...uploadProps}>
        <p className="ant-upload-drag-icon"><InboxOutlined /></p>
        <p className="ant-upload-text">拖入一个或多个 .xlsx，或点击选择文件</p>
        <p className="ant-upload-hint">最多 {maxFiles} 个；可混合销售合同与回款表，实际类型由服务端识别</p>
      </Upload.Dragger>

      <Space wrap>
        <Button
          aria-label="自动识别并预览"
          type="primary"
          icon={<CloudUploadOutlined />}
          loading={previewing}
          disabled={!files.length || applying}
          onClick={() => void runPreview()}
        >
          自动识别并预览
        </Button>
        {preview ? (
          <Text type="secondary">
            预览有效期至 {new Date(preview.expires_at).toLocaleString("zh-CN")}
          </Text>
        ) : null}
      </Space>

      {error ? <Alert type="error" showIcon message={error} /> : null}

      {preview ? (
        <>
          <Collapse
            size="small"
            items={preview.files.map((file) => ({
              key: file.file_id,
              label: `${file.filename} · 字段映射`,
              children: <MappingPreview file={file} />,
            }))}
          />

          <Card size="small" title="行匹配预览">
            <Space direction="vertical" size={12} style={{ width: "100%" }}>
              <Segmented<MatchFilter>
                value={filter}
                onChange={setFilter}
                options={[
                  { value: "all", label: `全部 ${preview.rows.length}` },
                  { value: "matched", label: `已匹配 ${counts.matched}` },
                  { value: "ambiguous", label: `有歧义 ${counts.ambiguous}` },
                  { value: "unmatched", label: `未匹配 ${counts.unmatched}` },
                  { value: "invalid", label: `无效 ${counts.invalid}` },
                ]}
              />
              <Table
                size="small"
                rowKey="row_key"
                dataSource={visibleRows}
                columns={rowColumns}
                pagination={{ pageSize: 10, showSizeChanger: false }}
                scroll={{ x: 980 }}
                rowSelection={{
                  selectedRowKeys: safeSelectedKeys,
                  preserveSelectedRowKeys: true,
                  onChange: setSelectedRowKeys,
                  getCheckboxProps: (row) => ({
                    disabled: !rowCanApply(row),
                    "aria-label": rowCanApply(row)
                      ? `选择 ${row.filename} 第 ${row.source_row} 行`
                      : `${MATCH_LABELS[row.match_state]}行不可提交`,
                  }),
                }}
              />
              <Row justify="space-between" align="middle" gutter={[12, 8]}>
                <Col>
                  <Text type="secondary">
                    可提交 {selectableKeys.size} 行，已选 {safeSelectedKeys.length} 行；其余行需修正源文件或后端归属后重新预览。
                  </Text>
                </Col>
                <Col>
                  <Button
                    type="primary"
                    loading={applying}
                    disabled={!preview.can_apply || !safeSelectedKeys.length || previewing}
                    onClick={() => void apply()}
                  >
                    提交 {safeSelectedKeys.length} 行
                  </Button>
                </Col>
              </Row>
            </Space>
          </Card>
        </>
      ) : null}

      {result ? (
        <Card size="small" title="逐行提交结果">
          <Space direction="vertical" size={10} style={{ width: "100%" }}>
            <Descriptions size="small" column={{ xs: 1, sm: 4 }}>
              <Descriptions.Item label="成功">{result.applied}</Descriptions.Item>
              <Descriptions.Item label="跳过">{result.skipped}</Descriptions.Item>
              <Descriptions.Item label="阻断">{result.blocked}</Descriptions.Item>
              <Descriptions.Item label="审计号">{result.audit_ref}</Descriptions.Item>
            </Descriptions>
            <Table
              size="small"
              rowKey="row_key"
              pagination={false}
              dataSource={result.rows}
              columns={resultColumns}
              scroll={{ x: 720 }}
            />
          </Space>
        </Card>
      ) : null}
    </Space>
  );
}

interface DownloadPanelProps {
  options: MaintenanceBatchTransferOptions;
  filters: Omit<MaintenanceBatchDownloadInput, "forms" | "fields">;
}

function fieldAvailable(field: MaintenanceBatchDownloadField, forms: string[]): boolean {
  return !field.form_keys.length || field.form_keys.some((key) => forms.includes(key));
}

function DownloadPanel({ options, filters }: DownloadPanelProps) {
  const availableFormKeys = options.download_forms.map((form) => form.key);
  const initialForms = options.default_forms.filter((key) => availableFormKeys.includes(key));
  const [forms, setForms] = useState<string[]>(
    initialForms.length
      ? initialForms
      : options.download_forms.filter((form) => form.default_selected).map((form) => form.key),
  );
  const initialAvailableFields = options.download_fields.filter((field) => fieldAvailable(field, forms));
  const initialDefaultFields = options.default_fields.filter((key) =>
    initialAvailableFields.some((field) => field.key === key),
  );
  const [fields, setFields] = useState<string[]>(
    initialDefaultFields.length
      ? initialDefaultFields
      : initialAvailableFields.filter((field) => field.default_selected).map((field) => field.key),
  );
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const availableFields = useMemo(
    () => options.download_fields.filter((field) => fieldAvailable(field, forms)),
    [forms, options.download_fields],
  );
  const groupedFields = useMemo(() => {
    const groups = new Map<string, MaintenanceBatchDownloadField[]>();
    availableFields.forEach((field) => {
      groups.set(field.group || "其他", [...(groups.get(field.group || "其他") ?? []), field]);
    });
    return [...groups.entries()];
  }, [availableFields]);

  const changeForms = (next: string[]) => {
    setForms(next);
    const eligible = new Set(
      options.download_fields.filter((field) => fieldAvailable(field, next)).map((field) => field.key),
    );
    setFields((current) => current.filter((key) => eligible.has(key)));
  };

  const restoreDefaults = () => {
    const nextForms = options.default_forms.filter((key) => availableFormKeys.includes(key));
    const normalizedForms = nextForms.length
      ? nextForms
      : options.download_forms.filter((form) => form.default_selected).map((form) => form.key);
    const eligible = new Set(
      options.download_fields
        .filter((field) => fieldAvailable(field, normalizedForms))
        .map((field) => field.key),
    );
    const defaults = options.default_fields.filter((key) => eligible.has(key));
    setForms(normalizedForms);
    setFields(
      defaults.length
        ? defaults
        : options.download_fields
          .filter((field) => eligible.has(field.key) && field.default_selected)
          .map((field) => field.key),
    );
  };

  const download = async () => {
    if (!forms.length || !fields.length || downloading) return;
    setDownloading(true);
    setError(null);
    try {
      const result = await downloadMaintenanceBatchTransfer({
        ...filters,
        forms,
        fields,
      });
      saveBlob(result.blob, result.filename);
      message.success("批量文件已开始下载");
    } catch (reason) {
      setError(await errorMessage(reason, "批量下载失败，请稍后重试"));
    } finally {
      setDownloading(false);
    }
  };

  if (!options.can_download) {
    return <Alert type="info" showIcon message="当前账号没有批量下载权限" />;
  }

  return (
    <Space direction="vertical" size={14} style={{ width: "100%" }}>
      <Alert
        type="info"
        showIcon
        message="导出范围服从维保主页当前筛选"
        description="会覆盖当前筛选命中的全部项目，不限于已经滚动加载的卡片。表单与字段均来自服务端权限白名单。"
      />

      <div>
        <Text strong>选择表单</Text>
        <Checkbox.Group value={forms} onChange={(values) => changeForms(values.map(String))} style={{ width: "100%" }}>
          <Row gutter={[12, 10]} style={{ marginTop: 8 }}>
            {options.download_forms.map((form) => (
              <Col xs={24} sm={12} key={form.key}>
                <Checkbox value={form.key}>
                  {form.label}
                  {form.description ? <Text type="secondary"> · {form.description}</Text> : null}
                </Checkbox>
              </Col>
            ))}
          </Row>
        </Checkbox.Group>
      </div>

      <Divider style={{ margin: 0 }} />

      <Space wrap>
        <Button size="small" onClick={() => setFields(availableFields.map((field) => field.key))} disabled={!availableFields.length}>
          字段全选
        </Button>
        <Button size="small" onClick={() => setFields([])} disabled={!fields.length}>取消字段</Button>
        <Button size="small" onClick={restoreDefaults}>恢复默认</Button>
        <Text type="secondary">已选 {forms.length} 个表单、{fields.length} 个字段</Text>
      </Space>

      {groupedFields.map(([group, items]) => (
        <div key={group}>
          <Text strong>{group}</Text>
          <Row gutter={[12, 10]} style={{ marginTop: 8 }}>
            {items.map((field) => (
              <Col xs={24} sm={12} key={field.key}>
                <Checkbox
                  checked={fields.includes(field.key)}
                  onChange={(event) => setFields((current) => event.target.checked
                    ? [...current, field.key]
                    : current.filter((key) => key !== field.key))}
                >
                  {field.label}
                </Checkbox>
              </Col>
            ))}
          </Row>
        </div>
      ))}

      {error ? <Alert type="error" showIcon message={error} /> : null}

      <Button
        aria-label="下载当前筛选全部项目"
        type="primary"
        icon={<CloudDownloadOutlined />}
        loading={downloading}
        disabled={!forms.length || !fields.length}
        onClick={() => void download()}
      >
        下载当前筛选全部项目
      </Button>
    </Space>
  );
}

export default function MaintenanceBatchTransferButton({
  filters,
  onApplied,
}: MaintenanceBatchTransferButtonProps) {
  const [open, setOpen] = useState(false);
  const [options, setOptions] = useState<MaintenanceBatchTransferOptions | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const openGeneration = useRef(0);

  const loadOptions = async () => {
    const generation = ++openGeneration.current;
    setLoading(true);
    setOptions(null);
    setError(null);
    try {
      const { data } = await getMaintenanceBatchTransferOptions();
      if (generation !== openGeneration.current) return;
      setOptions(data);
    } catch (reason) {
      if (generation !== openGeneration.current) return;
      setError(await errorMessage(reason, "无法读取批量导入/下载配置，请稍后重试"));
    } finally {
      if (generation === openGeneration.current) setLoading(false);
    }
  };

  useEffect(() => {
    if (open) void loadOptions();
    else openGeneration.current += 1;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <>
      <Button aria-label="批量导入 / 下载" icon={<SwapOutlined />} onClick={() => setOpen(true)}>
        批量导入 / 下载
      </Button>
      <Modal
        open={open}
        title="维保项目批量导入 / 下载"
        width={1180}
        footer={null}
        onCancel={() => setOpen(false)}
        destroyOnHidden
        styles={{ body: { maxHeight: "78vh", overflowY: "auto" } }}
      >
        {loading ? (
          <div style={{ padding: 48, textAlign: "center" }}><Spin /></div>
        ) : error ? (
          <Alert
            type="error"
            showIcon
            message={error}
            action={<Button size="small" onClick={() => void loadOptions()}>重试</Button>}
          />
        ) : options ? (
          <Tabs
            items={[
              {
                key: "import",
                label: "批量导入",
                children: <ImportPanel options={options} onApplied={onApplied} />,
              },
              {
                key: "download",
                label: "批量下载",
                children: <DownloadPanel options={options} filters={filters} />,
              },
            ]}
          />
        ) : (
          <Paragraph type="secondary">暂无可用配置</Paragraph>
        )}
      </Modal>
    </>
  );
}
