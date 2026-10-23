import { Alert, Checkbox, Collapse, Radio, Space, Tag, Typography } from "antd";
import {
  configurationErrors,
  denialConfiguration,
  type AuthzCatalog,
  type ResourceConfiguration,
} from "../../api/authz";
export type ModuleBundle = Record<string, ResourceConfiguration>;
export function bundleErrors(
  catalog: AuthzCatalog,
  bundle: ModuleBundle,
): string[] {
  return Object.entries(bundle).flatMap(([id, c]) => {
    const m = catalog.modules.find((x) => x.id === id);
    return !m
      ? ["未知资源"]
      : configurationErrors(m, c).map((e) => `${m.label}：${e}`);
  });
}
export default function ModuleGrantEditor({
  catalog,
  value,
  onChange,
  disabled = false,
}: {
  catalog: AuthzCatalog;
  value: ModuleBundle;
  onChange: (id: string, c: ResourceConfiguration | null) => void;
  disabled?: boolean;
}) {
  return (
    <Collapse
      items={catalog.modules.map((m) => {
        const c = value[m.id],
          editable =
            m.configurable &&
            m.migration_state === "implemented" &&
            ["enabled", "suspended"].includes(m.runtime_state);
        const set = (patch: Partial<ResourceConfiguration>) => {
          if (c) onChange(m.id, { ...c, ...patch });
        };
        return {
          key: m.id,
          label: (
            <Space>
              {m.label}
              <Tag>
                {m.migration_state === "retired"
                  ? "已退役"
                  : m.migration_state === "planned"
                    ? "待实施"
                    : m.runtime_state === "legacy"
                      ? "尚未切换"
                      : m.runtime_state === "suspended"
                        ? "暂停运行"
                        : "已启用"}
              </Tag>
              {c && <Tag>{c.actions.length ? "已配置" : "明确拒绝"}</Tag>}
            </Space>
          ),
          children: (
            <Space direction="vertical" style={{ width: "100%" }}>
              {m.notice && (
                <Typography.Paragraph>{m.notice}</Typography.Paragraph>
              )}
              {!editable ? (
                <Alert
                  type="info"
                  message="此资源当前只读，不能保存新授权。"
                  description={m.legacy_sources.join("、")}
                />
              ) : (
                <>
                  {m.runtime_state === "suspended" && (
                    <Alert
                      type="warning"
                      message="可以修正配置；业务当前仍拒绝访问。"
                    />
                  )}
                  <Checkbox
                    disabled={disabled}
                    checked={!!c}
                    onChange={(e) =>
                      onChange(
                        m.id,
                        e.target.checked ? denialConfiguration(m) : null,
                      )
                    }
                  >
                    配置此资源（空动作表示拒绝）
                  </Checkbox>
                  {c && (
                    <>
                      <div>操作</div>
                      <Checkbox.Group
                        disabled={disabled}
                        value={c.actions}
                        options={m.actions.map((a) => ({
                          value: a.id,
                          label: a.label,
                        }))}
                        onChange={(v) => set({ actions: v as string[] })}
                      />
                      <div>数据范围</div>
                      <Radio.Group
                        disabled={disabled}
                        value={c.scope}
                        onChange={(e) => set({ scope: e.target.value })}
                        options={m.scopes.map((s) => ({
                          value: s.id,
                          label: s.label,
                        }))}
                      />
                      <div>可见字段</div>
                      <Checkbox.Group
                        disabled={disabled}
                        value={c.field_permissions}
                        options={m.field_permissions.map((f) => ({
                          value: f.id,
                          label: f.label,
                        }))}
                        onChange={(v) =>
                          set({ field_permissions: v as string[] })
                        }
                      />
                      {configurationErrors(m, c).map((e, i) => (
                        <Alert key={i} type="error" message={e} />
                      ))}
                    </>
                  )}
                </>
              )}
            </Space>
          ),
        };
      })}
    />
  );
}
