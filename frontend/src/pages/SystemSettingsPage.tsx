import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Radio,
  Space,
  Spin,
  Typography,
  message,
} from "antd";
import PageHeader from "../components/PageHeader";
import {
  getSystemSettings,
  updateSystemSettings,
} from "../api/systemSettings";
import type {
  MaintenanceProfitDefaultBasis,
  SystemSettings,
} from "../api/systemSettings";

const { Text } = Typography;

function errorDetail(error: unknown): string | undefined {
  if (
    typeof error === "object"
    && error !== null
    && "response" in error
  ) {
    const response = (error as {
      response?: { data?: { detail?: unknown } };
    }).response;
    return typeof response?.data?.detail === "string"
      ? response.data.detail
      : undefined;
  }
  return undefined;
}

function isConflict(error: unknown): boolean {
  return Boolean(
    typeof error === "object"
    && error !== null
    && "response" in error
    && (error as { response?: { status?: number } }).response?.status === 409,
  );
}

export default function SystemSettingsPage() {
  const [settings, setSettings] = useState<SystemSettings | null>(null);
  const [basis, setBasis] = useState<MaintenanceProfitDefaultBasis>("both");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setSettings(null);
    setError(null);
    setConflict(false);
    try {
      const { data } = await getSystemSettings();
      setSettings(data);
      setBasis(data.maintenance_project_profit_default_basis);
    } catch {
      setError("加载系统设置失败，请检查网络后重新加载。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    if (!settings) return;
    setSaving(true);
    setError(null);
    setConflict(false);
    try {
      const { data } = await updateSystemSettings({
        maintenance_project_profit_default_basis: basis,
        expected_version: settings.version,
      });
      setSettings(data);
      setBasis(data.maintenance_project_profit_default_basis);
      message.success("系统设置已保存");
    } catch (requestError) {
      if (isConflict(requestError)) {
        setConflict(true);
        setError(
          errorDetail(requestError)
          ?? "设置已被其他管理员修改，请重新加载后再保存。",
        );
      } else {
        setError(
          errorDetail(requestError)
          ?? "保存失败，请检查网络后重试；当前页面没有假定保存成功。",
        );
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <PageHeader
        title="系统设置"
        subtitle="集中管理跨账号生效的业务默认值。"
      />
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="展示默认值，不是计算开关"
        description="此设置只影响项目成本页的默认展示；含税与未税毛利会同时保留，切换默认值不会删除、覆盖或重算业务数据。"
      />
      {error && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message={error}
          action={(
            <Button
              size="small"
              disabled={loading}
              onClick={() => void load()}
            >
              重新加载
            </Button>
          )}
        />
      )}
      <Card title="维保合同级毛利默认展示口径">
        {loading && !settings ? (
          <Spin />
        ) : settings ? (
          <Space direction="vertical" size={18} style={{ width: "100%" }}>
            <Radio.Group
              value={basis}
              disabled={loading || saving || conflict}
              onChange={(event) => {
                setBasis(event.target.value as MaintenanceProfitDefaultBasis);
                setError(null);
              }}
            >
              <Space direction="vertical" size={12}>
                <Radio value="inc">含税毛利</Radio>
                <Radio value="ex">未税毛利</Radio>
                <Radio value="both">同时显示</Radio>
              </Space>
            </Radio.Group>
            <Text type="secondary">
              当前版本：v{settings.version}
              {settings.updated_by ? ` · 最近由 ${settings.updated_by} 更新` : ""}
            </Text>
            <div>
              <Button
                type="primary"
                loading={saving}
                disabled={
                  saving
                  || loading
                  || conflict
                  || basis === settings.maintenance_project_profit_default_basis
                }
                onClick={() => void save()}
              >
                保存设置
              </Button>
            </div>
          </Space>
        ) : null}
      </Card>
    </>
  );
}
