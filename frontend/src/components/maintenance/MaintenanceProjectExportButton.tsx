import { DownloadOutlined } from "@ant-design/icons";
import { Alert, Button, Checkbox, Col, Modal, Row, Space, Spin, Typography, message } from "antd";
import { useEffect, useMemo, useState } from "react";

import {
  downloadBoardProjectsExport,
  getBoardProjectExportOptions,
  type BoardProjectExportField,
  type BoardProjectExportInput,
} from "../../api/maintenanceBossBoard";
import { saveBlob } from "../../api/maintenanceWorkbooks";

const { Text } = Typography;
const STORAGE_KEY = "maintenance_project_export_fields_v1";

export interface MaintenanceProjectExportButtonProps {
  filters: Omit<BoardProjectExportInput, "fields">;
}

function storedFields(): string[] | null {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    return Array.isArray(value) && value.every((item) => typeof item === "string")
      ? value
      : null;
  } catch {
    return null;
  }
}

function rememberFields(fields: string[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(fields));
  } catch {
    // 浏览器禁用 localStorage 时不影响本次导出。
  }
}

function detailMessage(data: unknown): string | null {
  if (data && typeof data === "object" && "detail" in data) {
    const detail = (data as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && "message" in detail) {
      return String((detail as { message: unknown }).message);
    }
  }
  return null;
}

async function readError(error: unknown): Promise<string> {
  const data = (error as { response?: { data?: unknown } })?.response?.data;
  if (data instanceof Blob) {
    try {
      const parsed: unknown = JSON.parse(await data.text());
      return detailMessage(parsed) ?? "项目清单导出失败，请稍后重试";
    } catch {
      // 非 JSON 错误体使用下面的固定文案。
    }
  }
  const message = detailMessage(data);
  if (message) return message;
  return "项目清单导出失败，请稍后重试";
}

/**
 * 维保项目清单导出入口。
 *
 * 字段目录始终来自服务端权限白名单；本地只记住这些公开 key 的选择状态，
 * 每次打开都会重新取目录并做交集，权限收窄后不会残留不可见字段。
 */
export default function MaintenanceProjectExportButton({
  filters,
}: MaintenanceProjectExportButtonProps) {
  const [open, setOpen] = useState(false);
  const [fields, setFields] = useState<BoardProjectExportField[]>([]);
  const [defaultFields, setDefaultFields] = useState<string[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [loadingOptions, setLoadingOptions] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const grouped = useMemo(() => {
    const groups = new Map<string, BoardProjectExportField[]>();
    fields.forEach((field) => {
      const label = field.group || "其他";
      groups.set(label, [...(groups.get(label) ?? []), field]);
    });
    return [...groups.entries()];
  }, [fields]);

  const applySelection = (keys: string[], available = fields) => {
    const wanted = new Set(keys);
    const normalized = available.filter((field) => wanted.has(field.key)).map((field) => field.key);
    setSelected(normalized);
    rememberFields(normalized);
  };

  const loadOptions = async () => {
    setLoadingOptions(true);
    setError(null);
    // 先清掉上次目录，避免切换账号/权限后的旧字段在请求期间短暂可见。
    setFields([]);
    setSelected([]);
    try {
      const { data } = await getBoardProjectExportOptions();
      const availableKeys = new Set(data.fields.map((field) => field.key));
      const defaults = data.default_fields.filter((key) => availableKeys.has(key));
      const fallbackDefaults = defaults.length
        ? defaults
        : data.fields.filter((field) => field.default_selected).map((field) => field.key);
      const remembered = storedFields()?.filter((key) => availableKeys.has(key)) ?? [];
      const initial = remembered.length ? remembered : fallbackDefaults;
      setFields(data.fields);
      setDefaultFields(fallbackDefaults);
      applySelection(initial, data.fields);
    } catch (loadError) {
      setDefaultFields([]);
      setSelected([]);
      setError(await readError(loadError));
    } finally {
      setLoadingOptions(false);
    }
  };

  useEffect(() => {
    if (open) void loadOptions();
    // 只以弹窗开关触发：filters 改变不会重拉字段目录，下载时仍读取最新 props。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const toggleField = (key: string, checked: boolean) => {
    const next = checked
      ? [...selected, key]
      : selected.filter((selectedKey) => selectedKey !== key);
    applySelection(next);
  };

  const download = async () => {
    if (!selected.length || downloading) return;
    setDownloading(true);
    setError(null);
    try {
      const result = await downloadBoardProjectsExport({
        ...filters,
        fields: selected,
      });
      saveBlob(result.blob, result.filename);
      message.success("维保项目清单已开始下载");
      setOpen(false);
    } catch (downloadError) {
      setError(await readError(downloadError));
    } finally {
      setDownloading(false);
    }
  };

  return (
    <>
      <Button icon={<DownloadOutlined />} onClick={() => setOpen(true)}>
        导出项目清单
      </Button>
      <Modal
        open={open}
        title="导出维保项目清单"
        width={680}
        okText={`下载 Excel${selected.length ? `（${selected.length} 项）` : ""}`}
        cancelText="取消"
        confirmLoading={downloading}
        okButtonProps={{ disabled: loadingOptions || !fields.length || !selected.length }}
        onOk={() => void download()}
        onCancel={() => {
          if (!downloading) setOpen(false);
        }}
        styles={{ body: { maxHeight: "65vh", overflowY: "auto" } }}
        destroyOnHidden
      >
        <Space direction="vertical" size={14} style={{ width: "100%" }}>
          <Text type="secondary">
            下载当前筛选命中的全部项目，不限于已经加载的卡片。可选字段由系统按当前账号权限提供；选择会保存在本浏览器。
          </Text>
          <Space wrap>
            <Button size="small" disabled={!fields.length} onClick={() => applySelection(fields.map((field) => field.key))}>
              全选
            </Button>
            <Button size="small" disabled={!selected.length} onClick={() => applySelection([])}>
              取消全选
            </Button>
            <Button size="small" disabled={!fields.length} onClick={() => applySelection(defaultFields)}>
              恢复默认
            </Button>
            <Text type="secondary">已选 {selected.length} / {fields.length} 项</Text>
          </Space>

          {error ? (
            <Alert
              type="error"
              showIcon
              message={error}
              action={fields.length ? undefined : (
                <Button size="small" onClick={() => void loadOptions()}>重试</Button>
              )}
            />
          ) : null}

          {loadingOptions ? (
            <div style={{ textAlign: "center", padding: 32 }}><Spin /></div>
          ) : (
            grouped.map(([group, items]) => (
              <div key={group}>
                <Text strong>{group}</Text>
                <Row gutter={[12, 10]} style={{ marginTop: 8 }}>
                  {items.map((field) => (
                    <Col xs={24} sm={12} key={field.key}>
                      <Checkbox
                        checked={selected.includes(field.key)}
                        onChange={(event) => toggleField(field.key, event.target.checked)}
                      >
                        {field.label}
                      </Checkbox>
                    </Col>
                  ))}
                </Row>
              </div>
            ))
          )}
        </Space>
      </Modal>
    </>
  );
}
