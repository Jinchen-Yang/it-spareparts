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
  SystemSettings,
  TaxDisplayBasis,
} from "../api/systemSettings";
import {
  announceTaxDisplayPolicyChanged,
  TAX_BASIS_OPTIONS,
  useTaxDisplayPolicy,
} from "../context/TaxBasis";

const { Text } = Typography;

interface SettingsDraft {
  purchase_display_basis: TaxDisplayBasis;
  sales_display_basis: TaxDisplayBasis;
  maintenance_display_basis: TaxDisplayBasis;
}

function toDraft(settings: SystemSettings): SettingsDraft {
  return {
    purchase_display_basis: settings.purchase_display_basis,
    sales_display_basis: settings.sales_display_basis,
    maintenance_display_basis: settings.maintenance_display_basis,
  };
}

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
  const [draft, setDraft] = useState<SettingsDraft | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const { refresh: refreshDisplayPolicy } = useTaxDisplayPolicy();

  const load = useCallback(async () => {
    setLoading(true);
    setSettings(null);
    setDraft(null);
    setError(null);
    setConflict(false);
    try {
      const { data } = await getSystemSettings();
      setSettings(data);
      setDraft(toDraft(data));
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
    if (!settings || !draft) return;
    setSaving(true);
    setError(null);
    setConflict(false);
    try {
      const { data } = await updateSystemSettings({
        ...draft,
        expected_version: settings.version,
      });
      setSettings(data);
      setDraft(toDraft(data));
      announceTaxDisplayPolicyChanged(data.version);
      await refreshDisplayPolicy(data.version);
      message.success("统一展示口径已保存并生效");
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

  const updateBasis = (
    key: keyof SettingsDraft,
    value: TaxDisplayBasis,
  ) => {
    setDraft((current) => current ? { ...current, [key]: value } : current);
    setError(null);
  };

  const dirty = Boolean(settings && draft && (
    draft.purchase_display_basis !== settings.purchase_display_basis
    || draft.sales_display_basis !== settings.sales_display_basis
    || draft.maintenance_display_basis !== settings.maintenance_display_basis
  ));

  const basisGroup = (
    title: string,
    description: string,
    key: keyof SettingsDraft,
  ) => (
    <div>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>{title}</div>
      <Text type="secondary">{description}</Text>
      <div style={{ marginTop: 10 }}>
        <div role="radiogroup" aria-label={`${title}展示口径`}>
          <Radio.Group
            name={`system-settings-${key}`}
            value={draft?.[key]}
            disabled={loading || saving || conflict}
            onChange={(event) => {
              updateBasis(key, event.target.value as TaxDisplayBasis);
            }}
            options={TAX_BASIS_OPTIONS.map((option) => ({
              ...option,
              label: option.value === "both" ? "同时显示" : option.label,
            }))}
          />
        </div>
      </div>
    </div>
  );

  return (
    <>
      <PageHeader
        title="系统设置"
        subtitle="由管理员统一控制采购、销售和项目维保的税口径展示。"
      />
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="管理员统一展示，不是个人偏好或计算开关"
        description="普通员工不能临时切换。修改这里只改变各业务模块展示哪一侧，不删除或覆盖原始业务数据；采购、销售仅有一侧原值时，另一侧统一按 13% 税率补齐。"
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
      <Card title="税口径统一展示策略">
        {loading && !settings ? (
          <Spin />
        ) : settings && draft ? (
          <Space direction="vertical" size={18} style={{ width: "100%" }}>
            {basisGroup(
              "采购",
              "采购分析、采购明细和采购成本相关页面使用此口径。",
              "purchase_display_basis",
            )}
            {basisGroup(
              "销售",
              "销售与利润页面使用此口径；系统初始默认展示未税。",
              "sales_display_basis",
            )}
            {basisGroup(
              "项目维保",
              "项目维保成本、合同级备件毛利和贡献毛利使用此口径。",
              "maintenance_display_basis",
            )}
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
                  || !dirty
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
